import copy

import pytest

from backend.services.creator.responsibility_graph import (
    GraphDraft,
    GraphTransaction,
    compile_responsibility_graph,
    compute_legal_source_domain,
    export_script_generation_contracts,
    port_id,
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
    ])
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
    assert set(prompts[0]["candidates"][0]) == {"candidate_id", "source_node", "source_output"}

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
    good = transaction.candidate([item("scripts/a.py", ["user_request"], ["text"])])
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
    ])
    await compile_responsibility_graph(draft)
    contracts = export_script_generation_contracts(draft)
    assert contracts[1]["runtime_inputs"] == [{
        "name": "topic", "source_node": "scripts/a.py", "source_output": "topic"
    }]
    assert contracts[1]["runtime_outputs"] == [{
        "name": "text", "consumers": ["platform_output_node.text"]
    }]
