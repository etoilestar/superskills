import copy
import json
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
    normalize_type_descriptor,
    patch_graph_node_contract,
    type_compatibility,
    public_edges,
)


@pytest.mark.asyncio
async def test_compiled_api_batches_and_applies_graph_ambiguities_once(monkeypatch):
    from backend.services.creator import api

    items = [
        item("scripts/a.py", [], ["value"]),
        item("scripts/b.py", [], ["value"]),
        item("scripts/c.py", ["value"], ["text"]),
    ]
    calls = []

    async def choose(messages, role, fallback_model):
        calls.append(messages)
        return '{"selections":[{"ambiguity_id":"A1","selected_candidate_id":"A1-C2"}]}'

    monkeypatch.setattr(api, "_frozen_function_items_from_blueprint", lambda **_kwargs: items)
    monkeypatch.setattr(api, "_graph_resource_authority", lambda **_kwargs: (set(), set()))
    monkeypatch.setattr(api, "complete_creator_role_once", choose)
    monkeypatch.setattr(api.settings, "creator_graph_binding_mode", "compiled_v2")
    result = await api._bind_executable_responsibility_plan(
        request=api.PreparePlanRequest(user_request="demo"),
        current_planner_result={"internal_blueprint_text": "frozen",
                                "final_output_bindings": [{"platform_slot": "text", "source_node": "scripts/c.py", "source_output": "text"}]},
        planner_model="test", allowed_function_item_targets=[value["target_file"] for value in items],
    )
    assert len(calls) == 1
    payload = json.loads(calls[0][1]["content"])
    assert payload["ambiguities"][0]["target"]["purpose"] == "Process scripts/c.py"
    assert payload["ambiguities"][0]["candidates"][0]["source_purpose"]
    assert payload["ambiguities"][0]["candidates"][0]["match_confidence"] == "weak_name"
    edge = next(value for value in result["responsibility_edges"] if value["to_node"] == "scripts/c.py")
    assert edge["from_node"] == "scripts/b.py"


@pytest.mark.asyncio
async def test_compiled_api_resolves_multiple_ambiguities_in_one_model_call(monkeypatch):
    from backend.services.creator import api

    items = [item("scripts/a.py", [], ["x", "y"]), item("scripts/b.py", [], ["x", "y"]),
             item("scripts/c.py", ["x"], ["text"]),
             item("scripts/d.py", ["y"], ["markdown"])]
    calls = []
    async def choose(messages, role, fallback_model):
        calls.append(messages)
        return json.dumps({"selections": [
            {"ambiguity_id": "A1", "selected_candidate_id": "A1-C2"},
            {"ambiguity_id": "A2", "selected_candidate_id": "A2-C2"},
        ]})
    monkeypatch.setattr(api, "_frozen_function_items_from_blueprint", lambda **_kwargs: items)
    monkeypatch.setattr(api, "_graph_resource_authority", lambda **_kwargs: (set(), set()))
    monkeypatch.setattr(api, "complete_creator_role_once", choose)
    monkeypatch.setattr(api.settings, "creator_graph_binding_mode", "compiled_v2")
    result = await api._bind_executable_responsibility_plan(
        request=api.PreparePlanRequest(user_request="demo"),
        current_planner_result={"internal_blueprint_text": "frozen",
            "input_bindings": [],
            "final_output_bindings": [
                {"platform_slot": "text", "source_node": "scripts/c.py", "source_output": "text"},
                {"platform_slot": "markdown", "source_node": "scripts/d.py", "source_output": "markdown"}],
            "workflow_topology": {},},
        planner_model="test", allowed_function_item_targets=[value["target_file"] for value in items],
    )
    assert len(calls) == 1
    assert sum(value["to_node"] in {"scripts/c.py", "scripts/d.py"} for value in result["responsibility_edges"]) == 2


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
    ], workflow_topology={"scripts/a.py": [], "scripts/b.py": ["scripts/a.py"]})
    prompts = []

    async def selector(payload):
        prompts.append(payload)
        upstream = next(candidate for candidate in payload["candidates"] if candidate["source_node"] == "scripts/a.py")
        return {"decision": "selected", "selected_candidate_id": upstream["candidate_id"]}

    await compile_responsibility_graph(draft, source_selector=selector)
    assert draft.status == "committed"
    assert len(prompts) == 1
    assert {"candidate_id", "source_node", "source_output", "source_type", "target_type"} <= set(prompts[0]["candidates"][0])

    invalid = GraphDraft.freeze(copy.deepcopy(draft.function_items),
        workflow_topology={"scripts/a.py": [], "scripts/b.py": ["scripts/a.py"]})
    await compile_responsibility_graph(
        invalid, source_selector=lambda _payload: {"decision": "selected", "selected_candidate_id": "outside"}
    )
    assert invalid.status == "validation_failed"
    assert any(issue["issue_type"] == "wrong_source_selection" for issue in invalid.issues)
    with pytest.raises(ValueError, match="committed"):
        public_edges(invalid)

    mutated = GraphDraft.freeze(copy.deepcopy(draft.function_items),
        workflow_topology={"scripts/a.py": [], "scripts/b.py": ["scripts/a.py"]})
    def mutate_target(payload):
        payload["target_input"] = "changed"
        return {"decision": "selected", "selected_candidate_id": payload["candidates"][0]["candidate_id"]}
    await compile_responsibility_graph(mutated, source_selector=mutate_target)
    assert any(issue["issue_type"] == "protocol_shape_error" for issue in mutated.issues)


@pytest.mark.asyncio
async def test_no_source_is_contract_gap_and_candidate_is_not_exportable():
    draft = GraphDraft.freeze([item("scripts/image.py", ["scene_description"], ["image_path"])])
    await compile_responsibility_graph(draft)
    assert draft.status == "validation_failed"
    assert draft.issues[0] == {
        "issue_type": "node_contract_gap", "target_node": "scripts/image.py",
        "target_input": "scene_description", "legal_sources": [], "node_set_candidate": True,
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


def blueprint_with_resources(*paths):
    blocks = [
        "- path: `SKILL.md`\n  role: skill_overview\n  purpose: Guide\n  inputs: []\n  outputs: []\n"
        "  default_values: {}\n  dependencies: []\n  required_capabilities: []\n"
        "  forbidden_capabilities: []\n  references: []\n  constraints: []",
        "- path: `scripts/a.py`\n  role: generic_script\n  purpose: Run\n  inputs: []\n  outputs: [text]\n"
        "  default_values: {}\n  dependencies: []\n  required_capabilities: []\n"
        "  forbidden_capabilities: []\n  references: []\n  constraints: []",
    ]
    for path in paths:
        role = "reference" if path.startswith("references/") else "asset"
        source = "\n  source: bundled" if path.startswith("assets/") else ""
        blocks.append(
            f"- path: `{path}`\n  role: {role}\n  purpose: Declared resource\n  inputs: []\n  outputs: []\n"
            "  default_values: {}\n  dependencies: []\n  required_capabilities: []\n"
            f"  forbidden_capabilities: []\n  references: []\n  constraints: []{source}"
        )
    return """## 📋 Skill 架构蓝图
### 基本信息
- **Skill Name**: graph-test
### I/O Contract
- **Input**: value
- **Output**: result
### 目录结构
- SKILL.md
### Workflow Logic
1. Run.
### SkillPlan / 文件职责计划
""" + "\n".join(blocks) + """
### 宿主执行方式
- 需要脚本/命令: execute scripts
### Resource List
- declared only
"""


def test_complete_blueprint_graph_fact_parser_preserves_resources_and_topology():
    from backend.services.creator import api

    facts = api.parse_blueprint_graph_facts(
        blueprint_with_resources("references/rules.md"),
        allowed_function_item_targets=["scripts/a.py"],
    )
    assert facts["workflow_topology"] == {"scripts/a.py": []}
    assert facts["resources"] == ["references/rules.md"]
    assert facts["function_items"][0]["target_file"] == "scripts/a.py"


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


def test_missing_topology_does_not_connect_scripts_and_rejects_bad_explicit_nodes():
    items = [item("scripts/a.py", [], ["x"]), item("scripts/b.py", ["x"], ["text"])]
    open_draft = GraphDraft.freeze(items)
    assert open_draft.allowed_predecessors == {"scripts/a.py": [], "scripts/b.py": []}
    invalid = GraphDraft.freeze(items, workflow_topology={
        "scripts/a.py": ["scripts/a.py", "references/a.md"], "scripts/missing.py": ["scripts/a.py"]
    })
    assert len([issue for issue in invalid.issues if issue["issue_type"] == "topology_contract_issue"]) == 3


@pytest.mark.asyncio
async def test_unique_semantic_identity_can_authorize_source_without_topology():
    draft = GraphDraft.freeze([
        item("scripts/q10.py", [], [typed("v10", "string", semantic_id="S1")]),
        item("scripts/q11.py", [typed("v11", "string", semantic_id="S1")], ["text"]),
    ])
    await compile_responsibility_graph(draft)
    assert draft.status == "committed"
    assert any(edge["from_node"] == "scripts/q10.py" and edge["to_node"] == "scripts/q11.py"
               for edge in public_edges(draft))


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
    duplicate = GraphDraft.freeze([item("scripts/a.py", [], ["x"]), item("scripts/b.py", ["x"], ["text"])],
        workflow_topology={"scripts/a.py": [], "scripts/b.py": ["scripts/a.py"]})
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


def test_local_patch_supports_multiple_inputs_and_rejects_platform_pseudo_closure():
    draft = GraphDraft.freeze([
        item("scripts/source.py", [], ["left", "right"]),
        item("scripts/target.py", ["bad_left", "bad_right"], ["text"]),
    ], workflow_topology={"scripts/source.py": [], "scripts/target.py": ["scripts/source.py"]})
    paths = {"/function_items/1/inputs/0", "/function_items/1/inputs/1"}
    allowed = {paths.pop(): {"left", "right"}}
    other_path = next(iter(paths))
    allowed[other_path] = {"left", "right"}
    all_paths = set(allowed)
    patched = patch_graph_node_contract(draft, [
        {"op": "replace", "path": "/function_items/1/inputs/0", "value": "left"},
        {"op": "replace", "path": "/function_items/1/inputs/1", "value": "right"},
    ], allowed_patch_paths=all_paths, allowed_replacement_values_by_path=allowed)
    assert patched.function_items[1]["inputs"] == ["left", "right"]
    with pytest.raises(ValueError, match="no legal structural producer"):
        patch_graph_node_contract(draft, [
            {"op": "replace", "path": "/function_items/1/inputs/0", "value": "user_request"},
        ], allowed_patch_paths=all_paths, allowed_replacement_values_by_path=allowed)


@pytest.mark.asyncio
async def test_default_export_selector_retry_and_failed_exports():
    defaulted = GraphDraft.freeze([item("scripts/a.py", ["style"], ["text"], defaults={"style": "ink"})])
    await compile_responsibility_graph(defaulted)
    exported = export_script_generation_contracts(defaulted)[0]["runtime_inputs"][0]
    assert exported == {"name": "style", "resolution_kind": "local_default", "resolver_kind": "local_default", "default": "ink"}
    failed = GraphDraft.freeze([item("scripts/a.py", [], ["text"]), item("scripts/b.py", ["text"], ["markdown"])],
        workflow_topology={"scripts/a.py": [], "scripts/b.py": ["scripts/a.py"]})
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
    monkeypatch.setattr(api, "_graph_resource_authority", lambda **_kwargs: (set(), set()))
    monkeypatch.setattr(api.settings, "creator_graph_binding_mode", "compiled_v2")
    async def forbidden_model(*_args, **_kwargs):
        raise AssertionError("frozen unique bindings must not invoke a model")
    monkeypatch.setattr(api, "complete_creator_role_once", forbidden_model)
    result = await api._bind_executable_responsibility_plan(
        request=api.PreparePlanRequest(user_request="demo"),
        current_planner_result={"internal_blueprint_text": "frozen", "responsibility_edges": edges,
            "input_bindings": [{"target_node": "scripts/a.py", "target_input": "theme",
                "binding_kind": "platform_parameter", "source_root": "fields", "source_key": "topic"}],
            "final_output_bindings": [{"platform_slot": "text", "source_node": "scripts/a.py", "source_output": "result"}]},
        planner_model="test", allowed_function_item_targets=["scripts/a.py"])
    assert commits == ["committed"]
    assert [(edge["from_node"], edge["from_output"], edge["to_node"], edge["to_input"])
            for edge in result["responsibility_edges"]] == [
        ("platform_input_node", "fields", "scripts/a.py", "theme"),
        ("scripts/a.py", "result", "platform_output_node", "text")]
    assert result["responsibility_edges"][0]["constraints"] == edges[0]["constraints"]


@pytest.mark.asyncio
async def test_compiled_v2_ignores_planner_authored_complete_edges(monkeypatch):
    from backend.services.creator import api

    items = [item("scripts/a.py", [], ["text"])]
    monkeypatch.setattr(api, "_frozen_function_items_from_blueprint", lambda **_kwargs: items)
    monkeypatch.setattr(api, "_graph_resource_authority", lambda **_kwargs: (set(), set()))
    monkeypatch.setattr(api.settings, "creator_graph_binding_mode", "compiled_v2")
    result = await api._bind_executable_responsibility_plan(
        request=api.PreparePlanRequest(user_request="demo"),
        current_planner_result={"internal_blueprint_text": "frozen", "responsibility_edges": [{
            "from_node": "scripts/attacker.py", "from_output": "invented", "to_node": "platform_output_node",
            "to_input": "text", "purpose": "planner-authored complete graph", "constraints": []}],
            "final_output_bindings": [{"platform_slot": "text", "source_node": "scripts/a.py", "source_output": "text"}]},
        planner_model="test", allowed_function_item_targets=["scripts/a.py"])
    assert result["responsibility_edges"][0]["from_node"] == "scripts/a.py"
    assert all(edge["from_node"] != "scripts/attacker.py" for edge in result["responsibility_edges"])


@pytest.mark.asyncio
async def test_shadow_compiler_failure_cannot_replace_stable_graph(monkeypatch):
    from backend.services.creator import api

    items = [item("scripts/a.py", [], ["text"])]
    stable = [{"from_node": "scripts/a.py", "from_output": "text", "to_node": "platform_output_node",
               "to_input": "text", "purpose": "stable", "constraints": []}]
    monkeypatch.setattr(api, "_frozen_function_items_from_blueprint", lambda **_kwargs: items)
    monkeypatch.setattr(api, "_graph_resource_authority", lambda **_kwargs: (set(), set()))
    monkeypatch.setattr(api.settings, "creator_graph_binding_mode", "shadow")
    monkeypatch.setattr(api, "_plan_executable_requirement_allocations", AsyncMock())
    monkeypatch.setattr(api, "complete_creator_role_once", AsyncMock(return_value=json.dumps({
        "responsibility_edges": stable,
    })))
    async def explode(*_args, **_kwargs):
        raise RuntimeError("shadow compiler unavailable")
    monkeypatch.setattr(api, "compile_responsibility_graph", explode)
    result = await api._bind_executable_responsibility_plan(
        request=api.PreparePlanRequest(user_request="demo"),
        current_planner_result={"internal_blueprint_text": "frozen", "responsibility_edges": stable},
        planner_model="test", allowed_function_item_targets=["scripts/a.py"], legacy_adapter=True)
    assert result["responsibility_edges"] == stable


@pytest.mark.asyncio
async def test_compiler_does_not_replan_or_mutate_node_set(monkeypatch):
    from backend.services.creator import api

    items = [item("scripts/a.py", ["missing_runtime_input"], ["text"])]
    original = copy.deepcopy(items)
    facts = api.normalize_blueprint_graph_facts(
        function_items=items,
        platform_contract={"platform_skill_boundary": {"final_output_fields": ["text"]}},
    )
    assert any(value == {
        "issue_type": "unbound_required_input", "target_node": "scripts/a.py",
        "target_input": "missing_runtime_input",
    } for value in facts["structural_issues"])
    assert items == original


@pytest.mark.asyncio
async def test_legacy_mode_never_constructs_compiler_state(monkeypatch):
    from backend.services.creator import api

    items = [item("scripts/a.py", ["value"], ["text"])]
    edges = [
        {"from_node": "platform_input_node", "from_output": "fields", "to_node": "scripts/a.py",
         "to_input": "value", "purpose": "stable", "constraints": [
             {"type": "platform_parameter_binding", "source_key": "value", "required": True}]},
        {"from_node": "scripts/a.py", "from_output": "text", "to_node": "platform_output_node",
         "to_input": "text", "purpose": "stable", "constraints": []},
    ]
    monkeypatch.setattr(api.settings, "creator_graph_binding_mode", "legacy")
    monkeypatch.setattr(api, "_plan_executable_requirement_allocations", AsyncMock())
    monkeypatch.setattr(api, "_frozen_function_items_from_blueprint", lambda **_kwargs: items)
    monkeypatch.setattr(api, "complete_creator_role_once", AsyncMock(return_value=json.dumps({
        "responsibility_edges": edges,
    })))
    monkeypatch.setattr(api, "GraphTransaction", lambda: (_ for _ in ()).throw(AssertionError("compiler constructed")))
    monkeypatch.setattr(api, "compile_responsibility_graph", AsyncMock(side_effect=AssertionError("compiler called")))
    result = await api._bind_executable_responsibility_plan(
        request=api.PreparePlanRequest(user_request="demo"),
        current_planner_result={"internal_blueprint_text": "frozen", "responsibility_edges": edges},
        planner_model="test", allowed_function_item_targets=["scripts/a.py"], legacy_adapter=True)
    assert result["function_items"] == items
    assert result["responsibility_edges"] == edges


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [
    "resource_authority", "transaction", "candidate", "freeze", "port", "topology",
    "input_binding", "final_binding", "compile", "public_edges", "diff", "logging",
])
async def test_shadow_fail_open_boundary_returns_immutable_legacy_result(monkeypatch, stage):
    from backend.services.creator import api
    from backend.services.creator import responsibility_graph as graph_module

    items = [item("scripts/a.py", ["value"], ["text"])]
    edges = [
        {"from_node": "platform_input_node", "from_output": "fields", "to_node": "scripts/a.py",
         "to_input": "value", "purpose": "stable", "constraints": [
             {"type": "platform_parameter_binding", "source_key": "value", "required": True}]},
        {"from_node": "scripts/a.py", "from_output": "text", "to_node": "platform_output_node",
         "to_input": "text", "purpose": "stable", "constraints": []},
    ]
    legacy = {"function_items": items, "responsibility_edges": edges,
              "internal_blueprint_text": blueprint_with_resources(),
              "allowed_function_item_targets": ["scripts/a.py"]}
    if stage == "resource_authority":
        monkeypatch.setattr(api, "_graph_resource_authority", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError(stage)))
    else:
        monkeypatch.setattr(api, "_graph_resource_authority", lambda **_kwargs: (set(), set()))
    if stage == "transaction":
        monkeypatch.setattr(api, "GraphTransaction", lambda: (_ for _ in ()).throw(RuntimeError(stage)))
    elif stage == "candidate":
        class BrokenTransaction:
            def candidate(self, *_args, **_kwargs): raise RuntimeError(stage)
        monkeypatch.setattr(api, "GraphTransaction", BrokenTransaction)
    elif stage == "freeze":
        monkeypatch.setattr(graph_module.GraphDraft, "freeze", classmethod(
            lambda _cls, *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(stage))))
    elif stage == "port":
        monkeypatch.setattr(graph_module, "_port", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(stage)))
    elif stage == "topology":
        monkeypatch.setattr(graph_module, "_compile_topology", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(stage)))
    elif stage == "input_binding":
        monkeypatch.setattr(graph_module, "_binding_from_edge", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(stage)))
    elif stage == "final_binding":
        monkeypatch.setattr(graph_module, "FinalOutputBinding", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(stage)))
    elif stage == "compile":
        monkeypatch.setattr(api, "compile_responsibility_graph", AsyncMock(side_effect=RuntimeError(stage)))
    elif stage == "public_edges":
        monkeypatch.setattr(api, "public_edges", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(stage)))
    elif stage == "diff":
        monkeypatch.setattr(api, "public_edges", lambda *_args, **_kwargs: [{
            "from_node": [], "from_output": "x", "to_node": "y", "to_input": "z",
        }])
    elif stage == "logging":
        monkeypatch.setattr(api.logger, "info", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(stage)))
        monkeypatch.setattr(api.logger, "exception", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(stage)))
    result = await api._run_shadow_after_legacy(
        request=api.PreparePlanRequest(user_request="demo"),
        legacy_result=legacy, planner_model="test")
    assert result == legacy
    assert result is not legacy
    assert result["function_items"] is not legacy["function_items"]
    assert result["responsibility_edges"] is not legacy["responsibility_edges"]


@pytest.mark.asyncio
async def test_shadow_model_selection_defaults_off_and_never_repairs(monkeypatch):
    from backend.services.creator import api

    items = [item("scripts/a.py", ["missing"], ["text"])]
    legacy = {"function_items": items, "responsibility_edges": [],
              "internal_blueprint_text": blueprint_with_resources(),
              "allowed_function_item_targets": ["scripts/a.py"]}
    monkeypatch.setattr(api.settings, "creator_graph_shadow_model_selection", False)
    monkeypatch.setattr(api, "_graph_resource_authority", lambda **_kwargs: (set(), set()))
    monkeypatch.setattr(api, "complete_creator_role_once", AsyncMock(side_effect=AssertionError("selector called")))
    monkeypatch.setattr(api, "_replan_blueprint_for_graph_closure", AsyncMock(side_effect=AssertionError("replan called")))
    monkeypatch.setattr(api, "_regenerate_responsibility_graph", AsyncMock(side_effect=AssertionError("regenerate called")))
    result = await api._run_shadow_after_legacy(
        request=api.PreparePlanRequest(user_request="demo"), legacy_result=legacy,
        planner_model="test")
    assert result == legacy
    assert api.complete_creator_role_once.await_count == 0


@pytest.mark.asyncio
async def test_shadow_model_selection_opt_in_is_bounded_to_one_retry(monkeypatch):
    from backend.services.creator import api

    items = [item("scripts/q12.py", [], ["v12"]), item("scripts/q13.py", [], ["v13"]),
             item("scripts/q14.py", ["v14"], ["text"])]
    legacy = {"function_items": items, "responsibility_edges": [],
              "internal_blueprint_text": blueprint_with_resources(),
              "workflow_topology": {"scripts/q12.py": [], "scripts/q13.py": [],
                                    "scripts/q14.py": ["scripts/q12.py", "scripts/q13.py"]}}
    monkeypatch.setattr(api.settings, "creator_graph_shadow_model_selection", True)
    model = AsyncMock(return_value=json.dumps({
        "decision": "selected", "selected_candidate_id": "outside",
    }))
    monkeypatch.setattr(api, "complete_creator_role_once", model)
    await api._run_shadow_after_legacy(
        request=api.PreparePlanRequest(user_request="demo"), legacy_result=legacy,
        planner_model="test")
    assert model.await_count == 2


def test_refreeze_cannot_add_node_outside_frozen_domain():
    original = [item("scripts/a.py", ["theme"], ["result"])]
    draft = GraphDraft.freeze(original, allowed_node_targets={"scripts/a.py"})
    with pytest.raises(ValueError, match="outside the frozen target domain"):
        draft.refreeze([*original, item("scripts/new.py", [], ["theme"])])


def test_api_resource_authority_uses_only_declared_and_uploaded_paths():
    from backend.services.creator import api

    references, assets = api._graph_resource_authority(
        blueprint_text=blueprint_with_resources("references/template.md", "assets/declared.bin"),
        uploaded_files=[{"target_path": "assets/uploaded.bin"}, {"path": "outside.bin"}],
    )
    assert references == {"references/template.md"}
    assert assets == {"assets/declared.bin", "assets/uploaded.bin"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("resolver", "declared_path"), [
    ({"kind": "static_reference", "path": "references/template.md"}, "references/template.md"),
    ({"kind": "uploaded_asset", "path": "assets/declared.bin"}, "assets/declared.bin"),
])
async def test_compiled_api_accepts_only_authorized_static_resources(monkeypatch, resolver, declared_path):
    from backend.services.creator import api

    items = [item("scripts/a.py", ["template"], ["text"])]
    monkeypatch.setattr(api.settings, "creator_graph_binding_mode", "compiled_v2")
    monkeypatch.setattr(api, "_frozen_function_items_from_blueprint", lambda **_kwargs: items)
    result = await api._bind_executable_responsibility_plan(
        request=api.PreparePlanRequest(user_request="demo"),
            current_planner_result={
                "internal_blueprint_text": blueprint_with_resources(declared_path),
            "input_bindings": [{"target_node": "scripts/a.py", "target_input": "template",
                                    "binding_kind": "static_value", "resolver": resolver}],
                "final_output_bindings": [{"platform_slot": "text", "source_node": "scripts/a.py", "source_output": "text"}],
        }, planner_model="test", allowed_function_item_targets=["scripts/a.py"])
    assert result["function_items"] == items
    assert not any(edge["to_input"] == "template" for edge in result["responsibility_edges"])


@pytest.mark.asyncio
async def test_compiled_api_rejects_undeclared_static_resource(monkeypatch):
    from backend.services.creator import api

    items = [item("scripts/a.py", ["template"], ["text"])]
    monkeypatch.setattr(api.settings, "creator_graph_binding_mode", "compiled_v2")
    monkeypatch.setattr(api, "_frozen_function_items_from_blueprint", lambda **_kwargs: items)
    with pytest.raises(api.PreparePlanProtocolError, match="unauthorized_static_resource"):
        await api._bind_executable_responsibility_plan(
            request=api.PreparePlanRequest(user_request="demo"),
                current_planner_result={
                "internal_blueprint_text": blueprint_with_resources(),
                "input_bindings": [{"target_node": "scripts/a.py", "target_input": "template",
                                    "binding_kind": "static_value",
                                        "resolver": {"kind": "static_reference", "path": "references/missing.md"}}],
                    "final_output_bindings": [{"platform_slot": "text", "source_node": "scripts/a.py", "source_output": "text"}],
            }, planner_model="test", allowed_function_item_targets=["scripts/a.py"])


@pytest.mark.asyncio
async def test_shadow_resource_rejection_does_not_change_legacy_result(monkeypatch):
    from backend.services.creator import api

    items = [item("scripts/a.py", ["template"], ["text"])]
    edges = [{"from_node": "platform_input_node", "from_output": "fields", "to_node": "scripts/a.py",
              "to_input": "template", "purpose": "stable", "constraints": [
                  {"type": "platform_parameter_binding", "source_key": "template", "required": True}]},
             {"from_node": "scripts/a.py", "from_output": "text", "to_node": "platform_output_node",
              "to_input": "text", "purpose": "stable", "constraints": []}]
    legacy = {"function_items": items, "responsibility_edges": edges,
              "internal_blueprint_text": blueprint_with_resources(),
              "input_bindings": [{"target_node": "scripts/a.py", "target_input": "template",
                                  "binding_kind": "static_value",
                                  "resolver": {"kind": "uploaded_asset", "path": "assets/missing.bin"}}]}
    monkeypatch.setattr(api.settings, "creator_graph_shadow_model_selection", False)
    result = await api._run_shadow_after_legacy(
        request=api.PreparePlanRequest(user_request="demo"), legacy_result=legacy,
        planner_model="test")
    assert result == legacy


@pytest.mark.asyncio
async def test_historical_graph_failure_matrix_is_stable_across_three_runs():
    async def run_once():
        snapshots = []
        missing = GraphDraft.freeze([item("scripts/q1.py", ["v1"], ["text"])])
        await compile_responsibility_graph(missing)
        snapshots.append((missing.status, tuple(sorted(issue["issue_type"] for issue in missing.issues))))

        renamed = GraphDraft.freeze([item("scripts/q2.py", ["v2"], ["o2"])],
            input_bindings=[InputBinding("scripts/q2.py", "v2", "platform_parameter",
                                        source_root="fields", source_key="k2")],
            final_output_bindings=[FinalOutputBinding("text", "scripts/q2.py", "o2")])
        await compile_responsibility_graph(renamed)
        snapshots.append((renamed.status, tuple((edge["from_node"], edge["from_output"], edge["to_node"], edge["to_input"])
                                                for edge in public_edges(renamed))))

        fan = GraphDraft.freeze([
            item("scripts/q3.py", [], ["v3"]), item("scripts/q4.py", ["v3"], ["v4"]),
            item("scripts/q5.py", ["v3"], ["v5"]), item("scripts/q6.py", ["v4", "v5"], ["text"]),
        ], workflow_topology={"scripts/q3.py": [], "scripts/q4.py": ["scripts/q3.py"],
            "scripts/q5.py": ["scripts/q3.py"], "scripts/q6.py": ["scripts/q4.py", "scripts/q5.py"]},
            input_bindings=[InputBinding("scripts/q6.py", "v4", "script_output", "scripts/q4.py", "v4"),
                            InputBinding("scripts/q6.py", "v5", "script_output", "scripts/q5.py", "v5")])
        await compile_responsibility_graph(fan)
        snapshots.append((fan.status, len(public_edges(fan))))

        invalid_selector = GraphDraft.freeze([
            item("scripts/q7.py", [], ["v7"]), item("scripts/q8.py", [], ["v8"]),
            item("scripts/q9.py", [typed("v9", "string")], ["markdown"]),
        ], workflow_topology={"scripts/q7.py": [], "scripts/q8.py": [],
                              "scripts/q9.py": ["scripts/q7.py", "scripts/q8.py"]})
        await compile_responsibility_graph(invalid_selector,
            source_selector=lambda _payload: {"decision": "selected", "selected_candidate_id": "invalid"})
        snapshots.append((invalid_selector.status,
                          invalid_selector.metrics["model_source_selection_count"],
                          tuple(sorted(issue["issue_type"] for issue in invalid_selector.issues))))
        return snapshots

    first = await run_once()
    assert await run_once() == first
    assert await run_once() == first


def test_node_authority_is_frozen_set_not_file_suffix():
    targets = {"scripts/process.py", "scripts/process.sh", "scripts/render.js", "scripts/query.sql"}
    draft = GraphDraft.freeze([item(target, [], ["text"]) for target in sorted(targets)], allowed_node_targets=targets)
    assert {entry["target_file"] for entry in draft.function_items} == targets
    for unauthorized in ("scripts/unplanned.py", "references/info.md", "assets/logo.png", "SKILL.md"):
        with pytest.raises(ValueError, match="outside the frozen target domain"):
            GraphDraft.freeze([item(unauthorized, [], ["text"])], allowed_node_targets=targets)


def test_open_type_descriptors_and_simple_compatibility():
    custom = ["image", "audio", "video", "bytes", "stream", "dataframe", "tensor", "document", "custom_record"]
    GraphDraft.freeze([item("scripts/types.any", [{"name": value, "value_type": value} for value in custom], ["text"])])
    assert normalize_type_descriptor(" INT ") == "integer"
    assert type_compatibility("custom_record", "custom_record") == "compatible"
    assert type_compatibility("image", "image") == "compatible"
    assert type_compatibility("integer", "number") == "compatible"
    assert type_compatibility("unknown", "image") == "unknown"
    assert type_compatibility("image", "dataframe") == "unknown"


def test_graph_binding_mode_default_and_environment_overrides(monkeypatch):
    from backend.config import Settings
    monkeypatch.delenv("CREATOR_GRAPH_BINDING_MODE", raising=False)
    monkeypatch.delenv("creator_graph_binding_mode", raising=False)
    assert Settings(_env_file=None).creator_graph_binding_mode == "compiled_v2"
    monkeypatch.setenv("CREATOR_GRAPH_BINDING_MODE", "legacy")
    assert Settings(_env_file=None).creator_graph_binding_mode == "legacy"
    monkeypatch.setenv("CREATOR_GRAPH_BINDING_MODE", "shadow")
    assert Settings(_env_file=None).creator_graph_binding_mode == "shadow"
