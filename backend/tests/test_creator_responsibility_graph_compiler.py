import copy
from unittest.mock import AsyncMock

import pytest

from backend.services.creator.responsibility_graph import (
    GraphDraft,
    GraphTransaction,
    InputBinding,
    FinalOutputBinding,
    compile_responsibility_graph,
    compute_legal_source_domain,
    export_script_generation_contracts,
    port_id,
    patch_graph_node_contract,
    public_edges,
)


def item(target, inputs, outputs, *, defaults=None):
    return {
        "target_file": target,
        "role": "processor",
        "purpose": f"Process {target}",
        "inputs": inputs,
        "outputs": outputs,
        "required_capabilities": [],
        "constraints": [],
        "default_values": defaults or {},
    }


@pytest.mark.asyncio
async def test_unique_sources_and_platform_boundaries_are_backend_compiled_without_model():
    draft = GraphDraft.freeze([
        item("scripts/extract.py", ["user_request", "tone"], ["topic"], defaults={"tone": "plain"}),
        item("scripts/summarize.py", ["topic"], ["text"]),
    ], workflow_topology={"scripts/extract.py": [], "scripts/summarize.py": ["scripts/extract.py"]},
       input_bindings=[InputBinding("scripts/extract.py", "user_request", "platform_parameter",
                                   source_root="user_request", source_key="user_request")])
    calls = []

    async def selector(payload):
        calls.append(payload)
        raise AssertionError("unique sources must not call the selector")

    await compile_responsibility_graph(draft, source_selector=selector)

    assert draft.status == "committed"
    assert calls == []
    assert draft.metrics["model_source_selection_count"] == 0
    edges = public_edges(draft)
    assert ("platform_input_node", "user_request", "scripts/extract.py", "user_request") in {
        (edge["from_node"], edge["from_output"], edge["to_node"], edge["to_input"])
        for edge in edges
    }
    assert not any(edge["to_input"] == "tone" for edge in edges)
    assert any(edge["from_node"] == "scripts/summarize.py" and edge["to_node"] == "platform_output_node" for edge in edges)


@pytest.mark.asyncio
async def test_ambiguous_source_selector_can_only_choose_candidate_id():
    draft = GraphDraft.freeze([
        item("scripts/a.py", [], ["text"]),
        item("scripts/b.py", ["text"], ["markdown"]),
    ])
    prompts = []

    async def selector(payload):
        prompts.append(payload)
        upstream = next(candidate for candidate in payload["candidates"] if candidate["source_node"] == "scripts/a.py")
        return {"decision": "selected", "selected_candidate_id": upstream["candidate_id"]}

    await compile_responsibility_graph(draft, source_selector=selector)
    assert draft.status == "committed"
    assert len(prompts) == 1
    assert {"candidate_id", "source_node", "source_output", "source_type", "target_type"} <= set(prompts[0]["candidates"][0])

    invalid = GraphDraft.freeze(copy.deepcopy(draft.function_items))
    await compile_responsibility_graph(
        invalid, source_selector=lambda _payload: {"decision": "selected", "selected_candidate_id": "outside"}
    )
    assert invalid.status == "validation_failed"
    assert any(issue["issue_type"] == "wrong_source_selection" for issue in invalid.issues)
    with pytest.raises(ValueError, match="committed"):
        public_edges(invalid)


@pytest.mark.asyncio
async def test_no_source_is_contract_gap_and_candidate_is_not_exportable():
    draft = GraphDraft.freeze([item("scripts/image.py", ["scene_description"], ["image_path"])])
    await compile_responsibility_graph(draft)
    assert draft.status == "validation_failed"
    assert draft.issues[0] == {
        "issue_type": "node_contract_gap",
        "target_node": "scripts/image.py",
        "target_input": "scene_description",
        "legal_sources": [],
    }
    assert draft.metrics["model_source_selection_count"] == 0


@pytest.mark.asyncio
async def test_failed_candidate_does_not_pollute_committed_graph():
    transaction = GraphTransaction()
    good = transaction.candidate([item("scripts/a.py", ["user_request"], ["text"])],
        input_bindings=[InputBinding("scripts/a.py", "user_request", "platform_parameter",
                                    source_root="user_request", source_key="user_request")])
    await compile_responsibility_graph(good)
    committed = transaction.commit(good)
    bad = transaction.candidate([item("scripts/b.py", ["missing"], ["text"])])
    await compile_responsibility_graph(bad)
    with pytest.raises(ValueError, match="cannot replace"):
        transaction.commit(bad)
    assert transaction.committed_graph == committed


def test_source_domain_freezes_topology_and_excludes_non_executable_nodes():
    items = [item("scripts/a.py", [], ["value"]), item("scripts/b.py", ["value"], ["text"])]
    assert compute_legal_source_domain("scripts/b.py", "value", items, None, {"scripts/b.py": []}) == []
    candidates = compute_legal_source_domain(
        "scripts/b.py", "value", items, None, {"scripts/b.py": ["references/info.md", "assets/a.png", "scripts/a.py"]}
    )
    assert [(candidate.source_node, candidate.source_output) for candidate in candidates] == [("scripts/a.py", "value")]
    assert candidates[0].source_port_id == port_id("scripts/a.py", "output", "value")


@pytest.mark.asyncio
async def test_committed_graph_exports_stable_script_contracts():
    draft = GraphDraft.freeze([
        item("scripts/a.py", ["user_request"], ["topic"]),
        item("scripts/b.py", ["topic"], ["text"]),
    ], workflow_topology={"scripts/a.py": [], "scripts/b.py": ["scripts/a.py"]},
       input_bindings=[InputBinding("scripts/a.py", "user_request", "platform_parameter",
                                   source_root="user_request", source_key="user_request")])
    await compile_responsibility_graph(draft)
    contracts = export_script_generation_contracts(draft)
    assert contracts[1]["runtime_inputs"] == [{
        "name": "topic", "resolution_kind": "upstream_runtime", "source_node": "scripts/a.py", "source_output": "topic"
    }]
    assert contracts[1]["runtime_outputs"] == [{
        "name": "text", "consumers": ["platform_output_node.text"]
    }]


def typed(name, value_type, *, semantic_id=""):
    return {"name": name, "value_type": value_type, "semantic_id": semantic_id}


@pytest.mark.parametrize("order", [
    ["scripts/a.py", "scripts/b.py", "scripts/c.py"],
    ["scripts/c.py", "scripts/a.py", "scripts/b.py"],
])
@pytest.mark.asyncio
async def test_explicit_topology_is_independent_of_function_item_order(order):
    values = {
        "scripts/a.py": item("scripts/a.py", [], [typed("left", "string")]),
        "scripts/b.py": item("scripts/b.py", [], [typed("right", "string")]),
        "scripts/c.py": item("scripts/c.py", [typed("combined", "object")], ["text"]),
    }
    bindings = [InputBinding("scripts/c.py", "combined", "script_output", "scripts/a.py", "left")]
    topology = {"scripts/a.py": [], "scripts/b.py": [], "scripts/c.py": ["scripts/a.py", "scripts/b.py"]}
    draft = GraphDraft.freeze([values[path] for path in order], workflow_topology=topology,
                              input_bindings=bindings)
    # Explicit binding is admitted even with a renamed port, but incompatible
    # types are still rejected.
    await compile_responsibility_graph(draft)
    assert any(issue["issue_type"] == "type_mismatch" for issue in draft.issues)


def test_open_topology_contains_every_other_script_and_rejects_bad_explicit_nodes():
    items = [item("scripts/a.py", [], ["x"]), item("scripts/b.py", ["x"], ["text"])]
    open_draft = GraphDraft.freeze(items)
    assert open_draft.allowed_predecessors == {"scripts/a.py": ["scripts/b.py"], "scripts/b.py": ["scripts/a.py"]}
    invalid = GraphDraft.freeze(items, workflow_topology={
        "scripts/a.py": ["scripts/a.py", "references/a.md"], "scripts/missing.py": ["scripts/a.py"]
    })
    assert len([issue for issue in invalid.issues if issue["issue_type"] == "topology_contract_issue"]) == 3


@pytest.mark.parametrize(("source_output", "target_input"), [
    ("document_text", "source_text"), ("entities", "key_entities"),
])
@pytest.mark.asyncio
async def test_explicit_renamed_script_binding(source_output, target_input):
    draft = GraphDraft.freeze([
        item("scripts/source.py", [], [typed(source_output, "string")]),
        item("scripts/target.py", [typed(target_input, "string")], ["text"]),
    ], input_bindings=[InputBinding("scripts/target.py", target_input, "script_output",
                                   "scripts/source.py", source_output)])
    await compile_responsibility_graph(draft)
    assert any(edge["from_output"] == source_output and edge["to_input"] == target_input for edge in public_edges(draft))


@pytest.mark.asyncio
async def test_renamed_compatible_ports_are_candidates_and_incompatible_ports_are_excluded():
    compatible = GraphDraft.freeze([
        item("scripts/a.py", [], [typed("document_text", "string")]),
        item("scripts/b.py", [typed("source_text", "string")], ["text"]),
    ], workflow_topology={"scripts/a.py": [], "scripts/b.py": ["scripts/a.py"]})
    await compile_responsibility_graph(compatible)
    assert compatible.status == "committed"
    incompatible = GraphDraft.freeze([
        item("scripts/a.py", [], [typed("same", "object")]),
        item("scripts/b.py", [typed("same", "array")], ["text"]),
    ], workflow_topology={"scripts/a.py": [], "scripts/b.py": ["scripts/a.py"]})
    await compile_responsibility_graph(incompatible)
    mismatch = next(issue for issue in incompatible.issues if issue["issue_type"] == "type_mismatch")
    assert mismatch["source_type"] == "object" and mismatch["target_type"] == "array"


@pytest.mark.parametrize(("root", "source_key", "target"), [
    ("fields", "topic", "theme"), ("options", "style", "visual_style"),
    ("payload", "document", "source_document"), ("input_files", "paths", "document_paths"),
    ("resources", "manifest", "resource_manifest"),
])
@pytest.mark.asyncio
async def test_structured_platform_parameter_binding_projects_compatible_constraint(root, source_key, target):
    draft = GraphDraft.freeze([item("scripts/a.py", [target], ["text"])], input_bindings=[
        InputBinding("scripts/a.py", target, "platform_parameter", source_root=root,
                     source_key=source_key, required=True)
    ])
    await compile_responsibility_graph(draft)
    edge = next(edge for edge in public_edges(draft) if edge["to_node"] == "scripts/a.py")
    assert edge["from_output"] == root and edge["to_input"] == target
    assert edge["constraints"] == [{"type": "platform_parameter_binding", "source_key": source_key, "required": True}]


@pytest.mark.asyncio
async def test_optional_platform_parameter_requires_default():
    invalid = GraphDraft.freeze([item("scripts/a.py", ["style"], ["text"])], input_bindings=[
        InputBinding("scripts/a.py", "style", "platform_parameter", source_root="options",
                     source_key="style", required=False)
    ])
    await compile_responsibility_graph(invalid)
    assert invalid.status == "validation_failed"
    valid = GraphDraft.freeze([item("scripts/a.py", ["style"], ["text"])], input_bindings=[
        InputBinding("scripts/a.py", "style", "platform_parameter", source_root="options",
                     source_key="style", required=False, default="plain")
    ])
    await compile_responsibility_graph(valid)
    assert public_edges(valid)[0]["constraints"][0]["default"] == "plain"


@pytest.mark.parametrize(("source", "slot"), [
    ("result", "text"), ("report", "markdown"), ("output_file", "pdf_path"), ("paths", "file_outputs"),
])
@pytest.mark.asyncio
async def test_explicit_renamed_final_output_binding(source, slot):
    draft = GraphDraft.freeze([item("scripts/a.py", [], [source])],
        final_output_bindings=[FinalOutputBinding(slot, "scripts/a.py", source)])
    await compile_responsibility_graph(draft)
    assert public_edges(draft) == [{"from_node": "scripts/a.py", "from_output": source,
        "to_node": "platform_output_node", "to_input": slot, "purpose": f"Deliver final {slot}", "constraints": []}]


@pytest.mark.asyncio
async def test_multiple_final_outputs_and_ambiguous_producer_selection():
    multi = GraphDraft.freeze([item("scripts/a.py", [], ["result", "report"])], final_output_bindings=[
        FinalOutputBinding("text", "scripts/a.py", "result"), FinalOutputBinding("markdown", "scripts/a.py", "report")])
    await compile_responsibility_graph(multi)
    assert multi.metrics["platform_output_edge_count"] == 2
    ambiguous = GraphDraft.freeze([item("scripts/a.py", [], ["text"]), item("scripts/b.py", [], ["text"])])
    calls = []
    async def choose(payload):
        calls.append(payload); return {"decision": "selected", "selected_candidate_id": payload["candidates"][0]["candidate_id"]}
    await compile_responsibility_graph(ambiguous, source_selector=choose)
    assert ambiguous.status == "committed" and len(calls) == 1
    gap = GraphDraft.freeze([item("scripts/a.py", [], ["result"])])
    await compile_responsibility_graph(gap)
    assert gap.issues == [{"issue_type": "final_output_contract_gap", "platform_slot": None}]


@pytest.mark.asyncio
async def test_fan_out_fan_in_and_parallel_dag_compile():
    items = [item("scripts/root.py", [], ["seed"]), item("scripts/left.py", ["seed"], ["left"]),
             item("scripts/right.py", ["seed"], ["right"]),
             item("scripts/join.py", ["left", "right"], ["text"])]
    topology = {"scripts/root.py": [], "scripts/left.py": ["scripts/root.py"],
                "scripts/right.py": ["scripts/root.py"], "scripts/join.py": ["scripts/left.py", "scripts/right.py"]}
    draft = GraphDraft.freeze(items, workflow_topology=topology, input_bindings=[
        InputBinding("scripts/join.py", "left", "script_output", "scripts/left.py", "left"),
        InputBinding("scripts/join.py", "right", "script_output", "scripts/right.py", "right"),
    ])
    await compile_responsibility_graph(draft)
    assert draft.status == "committed"
    assert sum(edge["from_node"] == "scripts/root.py" for edge in public_edges(draft)) == 2
    assert sum(edge["to_node"] == "scripts/join.py" for edge in public_edges(draft)) == 2


@pytest.mark.asyncio
async def test_cycle_and_duplicate_provenance_are_rejected():
    cycle = GraphDraft.freeze([item("scripts/a.py", ["b"], ["a", "text"]), item("scripts/b.py", ["a"], ["b"])],
        input_bindings=[InputBinding("scripts/a.py", "b", "script_output", "scripts/b.py", "b"),
                        InputBinding("scripts/b.py", "a", "script_output", "scripts/a.py", "a")])
    await compile_responsibility_graph(cycle)
    assert any(issue["issue_type"] == "cycle_detected" for issue in cycle.issues)
    duplicate = GraphDraft.freeze([item("scripts/a.py", [], ["x"]), item("scripts/b.py", ["x"], ["text"])])
    duplicate.compiled_edges = [{"from_node": "scripts/a.py", "from_output": "x", "to_node": "scripts/b.py", "to_input": "x"}]
    # Compilation starts from a clean candidate, so stale/partial edges cannot create duplicate provenance.
    await compile_responsibility_graph(duplicate)
    assert duplicate.status == "committed"


@pytest.mark.parametrize("resolver", [
    {"kind": "static_reference", "path": "references/template.md"},
    {"kind": "uploaded_asset", "path": "assets/logo.png"},
    {"kind": "runtime_constant", "value": 3},
    {"kind": "environment_value", "name": "OUTPUT_DIR"},
])
@pytest.mark.asyncio
async def test_static_resolvers_do_not_create_executable_edges_and_are_exported(resolver):
    kwargs = {"authorized_references": {"references/template.md"}, "authorized_assets": {"assets/logo.png"}}
    draft = GraphDraft.freeze([item("scripts/a.py", ["config"], ["text"])], input_bindings=[
        InputBinding("scripts/a.py", "config", "static_value", resolver=resolver)], **kwargs)
    await compile_responsibility_graph(draft)
    assert draft.metrics["static_resolution_count"] == 1
    assert not any(edge["to_input"] == "config" for edge in public_edges(draft))
    assert export_script_generation_contracts(draft)[0]["runtime_inputs"][0]["resolver_kind"] == resolver["kind"]


@pytest.mark.asyncio
async def test_unauthorized_asset_and_failed_local_patch_rollback():
    draft = GraphDraft.freeze([item("scripts/a.py", ["logo"], ["text"])], input_bindings=[
        InputBinding("scripts/a.py", "logo", "static_value", resolver={"kind": "uploaded_asset", "path": "assets/no.png"})])
    await compile_responsibility_graph(draft)
    assert any(issue["issue_type"] == "unauthorized_static_resource" for issue in draft.issues)
    before = copy.deepcopy(draft.function_items)
    with pytest.raises(ValueError, match="outside allowed"):
        patch_graph_node_contract(draft, [{"op": "replace", "path": "/function_items/0/target_file", "value": "scripts/new.py"}],
                                  allowed_patch_paths={"/function_items/0/inputs/0"})
    assert draft.function_items == before


@pytest.mark.asyncio
async def test_default_export_selector_retry_and_failed_exports():
    defaulted = GraphDraft.freeze([item("scripts/a.py", ["style"], ["text"], defaults={"style": "ink"})])
    await compile_responsibility_graph(defaulted)
    exported = export_script_generation_contracts(defaulted)[0]["runtime_inputs"][0]
    assert exported == {"name": "style", "resolution_kind": "local_default", "resolver_kind": "local_default", "default": "ink"}
    failed = GraphDraft.freeze([item("scripts/a.py", [], ["text"]), item("scripts/b.py", ["text"], ["markdown"])])
    await compile_responsibility_graph(failed, source_selector=lambda _payload: {"edge": "forbidden"})
    assert failed.metrics["model_source_selection_count"] == 2
    with pytest.raises(ValueError): public_edges(failed)
    with pytest.raises(ValueError): export_script_generation_contracts(failed)


@pytest.mark.asyncio
async def test_renamed_graphs_and_purpose_languages_are_structurally_isomorphic():
    async def compile_named(prefix, purpose):
        first, second = f"scripts/{prefix}1.py", f"scripts/{prefix}2.py"
        values = [item(first, [], [typed(f"{prefix}_out", "string")]), item(second, [typed(f"{prefix}_in", "string")], ["text"])]
        values[0]["purpose"] = purpose
        draft = GraphDraft.freeze(list(reversed(values)), input_bindings=[
            InputBinding(second, f"{prefix}_in", "script_output", first, f"{prefix}_out")],
            final_output_bindings=[FinalOutputBinding("text", second, "text")])
        await compile_responsibility_graph(draft); return draft
    english = await compile_named("alpha", "transform input")
    chinese = await compile_named("beta", "转换输入")
    assert english.status == chinese.status == "committed"
    assert len(public_edges(english)) == len(public_edges(chinese)) == 2


@pytest.mark.asyncio
async def test_production_binding_uses_transaction_and_frozen_parameter_edges(monkeypatch):
    from backend.services.creator import api

    items = [item("scripts/a.py", ["theme"], ["result"])]
    edges = [
        {"from_node": "platform_input_node", "from_output": "fields", "to_node": "scripts/a.py",
         "to_input": "theme", "purpose": "bind", "constraints": [
             {"type": "platform_parameter_binding", "source_key": "topic", "required": True}]},
        {"from_node": "scripts/a.py", "from_output": "result", "to_node": "platform_output_node",
         "to_input": "text", "purpose": "return", "constraints": []},
    ]
    commits = []
    real_transaction = api.GraphTransaction

    class ObservedTransaction(real_transaction):
        def commit(self, candidate):
            commits.append(candidate.status)
            return super().commit(candidate)

    monkeypatch.setattr(api, "GraphTransaction", ObservedTransaction)
    monkeypatch.setattr(api, "_frozen_function_items_from_blueprint", lambda **_kwargs: items)
    monkeypatch.setattr(api.settings, "creator_graph_binding_mode", "compiled_v2")
    async def forbidden_model(*_args, **_kwargs):
        raise AssertionError("frozen unique bindings must not invoke a model")
    monkeypatch.setattr(api, "complete_creator_role_once", forbidden_model)
    result = await api._bind_executable_responsibility_plan(
        request=api.PreparePlanRequest(user_request="demo"),
        current_planner_result={"internal_blueprint_text": "frozen", "responsibility_edges": edges},
        planner_model="test", allowed_function_item_targets=["scripts/a.py"])
    assert commits == ["committed"]
    assert [(edge["from_node"], edge["from_output"], edge["to_node"], edge["to_input"])
            for edge in result["responsibility_edges"]] == [
        ("platform_input_node", "fields", "scripts/a.py", "theme"),
        ("scripts/a.py", "result", "platform_output_node", "text")]
    assert result["responsibility_edges"][0]["constraints"] == edges[0]["constraints"]


@pytest.mark.asyncio
async def test_production_contract_gap_uses_one_local_patch_not_graph_regeneration(monkeypatch):
    from backend.services.creator import api

    items = [item("scripts/a.py", ["missing_runtime_input"], ["text"])]
    monkeypatch.setattr(api, "_frozen_function_items_from_blueprint", lambda **_kwargs: items)
    monkeypatch.setattr(api.settings, "creator_graph_binding_mode", "compiled_v2")
    calls = []
    async def model(messages, *_args, **_kwargs):
        calls.append(messages[0]["content"])
        return '{"operations":[{"op":"replace","path":"/function_items/0/inputs/0","value":"user_request"}]}'
    monkeypatch.setattr(api, "complete_creator_role_once", model)
    monkeypatch.setattr(api, "_regenerate_responsibility_graph", AsyncMock(side_effect=AssertionError("forbidden")))
    monkeypatch.setattr(api, "_replan_blueprint_for_graph_closure", AsyncMock(side_effect=AssertionError("forbidden")))
    result = await api._bind_executable_responsibility_plan(
        request=api.PreparePlanRequest(user_request="demo"),
        current_planner_result={"internal_blueprint_text": "frozen"}, planner_model="test",
        allowed_function_item_targets=["scripts/a.py"])
    assert result["function_items"][0]["inputs"] == ["user_request"]
    assert len(calls) == 1 and "bounded JSON Patch" in calls[0]
    assert api._regenerate_responsibility_graph.await_count == 0
    assert api._replan_blueprint_for_graph_closure.await_count == 0
