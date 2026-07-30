import pytest

from backend.services.creator.responsibility_graph import (
    GraphDraft, GraphTransaction, compile_responsibility_graph,
    export_script_generation_contracts, public_edges,
    validate_source_selection,
)


BOUNDARY = {
    "input_envelope_fields": ["text", "options"],
    "final_output_fields": ["text", "file_outputs"],
}


def item(path, inputs, outputs, defaults=None):
    return {
        "target_file": path, "role": "worker", "purpose": "work",
        "inputs": inputs, "outputs": outputs, "required_capabilities": [],
        "constraints": [], "default_values": defaults or {},
    }


@pytest.mark.asyncio
async def test_unique_sources_and_platform_boundaries_are_backend_compiled_without_model():
    draft = GraphDraft.freeze([
        item("scripts/a.py", ["text", "options"], ["topic"], {"options": {}}),
        item("scripts/b.py", ["topic"], ["text"]),
    ], BOUNDARY)

    async def forbidden(_payload):
        raise AssertionError("unique and default inputs must not invoke a model")

    result = await compile_responsibility_graph(draft, forbidden)
    assert result.status == "committed"
    edges = public_edges(result)
    assert [(e["from_node"], e["to_node"], e["to_input"]) for e in edges] == [
        ("platform_input_node", "scripts/a.py", "text"),
        ("scripts/a.py", "scripts/b.py", "topic"),
        ("scripts/b.py", "platform_output_node", "text"),
    ]
    assert not any(e["to_input"] == "options" for e in edges)
    contracts = export_script_generation_contracts(result)
    assert contracts[1]["runtime_inputs"][0]["source_node"] == "scripts/a.py"


@pytest.mark.asyncio
async def test_ambiguous_source_selector_gets_ids_only_and_retries_once():
    draft = GraphDraft.freeze([
        item("scripts/a.py", ["options"], ["text"]),
        item("scripts/b.py", ["text"], ["file_outputs"]),
    ], BOUNDARY)
    calls = []

    async def selector(payload):
        calls.append(payload)
        if len(calls) == 1:
            return {"decision": "selected", "selected_candidate_id": "outside"}
        return {"decision": "selected", "selected_candidate_id": "C2"}

    result = await compile_responsibility_graph(draft, selector)
    assert result.status == "committed"
    assert len(calls) == 2
    assert set(calls[0]["candidates"][0]) == {"candidate_id", "source_node", "source_output"}
    assert any(e["from_node"] == "scripts/a.py" and e["to_node"] == "scripts/b.py" for e in public_edges(result))


@pytest.mark.asyncio
async def test_unresolved_candidate_does_not_replace_committed_graph():
    transaction = GraphTransaction()
    good = GraphDraft.freeze([item("scripts/a.py", ["text"], ["text"])], BOUNDARY)
    await transaction.compile_candidate(good)
    before = public_edges(transaction.require_committed())

    bad = GraphDraft.freeze([item("scripts/a.py", ["unknown"], ["text"])], BOUNDARY)
    result = await transaction.compile_candidate(bad)
    assert result.status == "validation_failed"
    assert result.issues[0]["issue_type"] == "node_contract_gap"
    assert public_edges(transaction.require_committed()) == before


def test_selector_protocol_rejects_extra_fields_and_out_of_domain_ids():
    with pytest.raises(ValueError):
        validate_source_selection({"decision": "selected", "selected_candidate_id": "C1", "from": "x"}, {"C1"})
    with pytest.raises(ValueError):
        validate_source_selection({"decision": "selected", "selected_candidate_id": "C9"}, {"C1"})
