import json

import pytest

from backend.services.creator.blueprint_graph import (
    normalize_blueprint_graph_facts,
    parse_ambiguity_selections,
)


def item(path, inputs, outputs, dependencies=()):
    return {"target_file": path, "role": "processor", "purpose": path,
            "inputs": inputs, "outputs": outputs, "dependencies": list(dependencies),
            "required_capabilities": [], "forbidden_capabilities": ["network"],
            "constraints": [{"kind": "offline"}], "default_values": {},
            "references": ["references/r.md"], "extension_fact": {"kept": True}}


def test_unique_chain_is_inferred_without_ambiguity_and_preserves_metadata():
    facts = normalize_blueprint_graph_facts(
        function_items=[
            item("scripts/a.py", [], [{"name": "document", "semantic_id": "doc"}]),
            item("scripts/b.py", [{"name": "source", "semantic_id": "doc"}], ["text"], ["scripts/a.py"]),
        ], platform_contract={"platform_skill_boundary": {"final_output_fields": ["text"]}},
    )
    assert facts["ambiguities"] == []
    assert facts["metrics"]["model_selection_call_count"] == 0
    assert facts["input_bindings"][0]["source_node"] == "scripts/a.py"
    assert facts["function_items"][0]["extension_fact"] == {"kept": True}
    assert facts["function_items"][0]["forbidden_capabilities"] == ["network"]


def test_multiple_producers_create_one_finite_ambiguity():
    facts = normalize_blueprint_graph_facts(function_items=[
        item("scripts/a.py", [], ["value"]), item("scripts/b.py", [], ["value"]),
        item("scripts/c.py", ["value"], ["text"], ["scripts/a.py", "scripts/b.py"]),
    ], platform_contract={"platform_skill_boundary": {"final_output_fields": ["text"]}})
    ambiguity = facts["ambiguities"][0]
    assert ambiguity["target_node"] == "scripts/c.py"
    assert len(ambiguity["candidate_sources"]) == 2


def test_selection_protocol_accepts_identical_duplicate_and_rejects_unknown():
    ambiguities = [{"ambiguity_id": "A1", "candidate_sources": [{"candidate_id": "A1-C1"}]}]
    payload = json.dumps({"selections": [{"ambiguity_id": "A1", "selected_candidate_id": "A1-C1"}]})
    assert parse_ambiguity_selections(payload + payload, ambiguities) == {"A1": "A1-C1"}
    with pytest.raises(ValueError, match="invalid_candidate"):
        parse_ambiguity_selections('{"selections":[{"ambiguity_id":"A1","selected_candidate_id":"A1-C9"}]}', ambiguities)
