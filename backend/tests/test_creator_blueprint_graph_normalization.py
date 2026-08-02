import json

import pytest

from backend.services.creator.blueprint_graph import (
    apply_ambiguity_selections,
    normalize_blueprint_graph_facts,
    parse_ambiguity_selections,
)
from backend.services.creator.requirement_coverage import (
    audit_committed_requirement_coverage,
    freeze_requirements,
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
    selected = apply_ambiguity_selections(
        facts, {ambiguity["ambiguity_id"]: ambiguity["candidate_sources"][1]["candidate_id"]}
    )
    assert selected["ambiguities"] == []
    assert selected["input_bindings"][-1]["source_node"] == "scripts/b.py"
    assert selected["metrics"]["model_selection_call_count"] == 1


def test_unique_port_match_without_dependency_is_inferred_acyclically():
    facts = normalize_blueprint_graph_facts(function_items=[
        item("scripts/a.py", [], [{"name": "payload", "value_type": "object"}]),
        item("scripts/b.py", [{"name": "payload", "value_type": "object"}], ["text"]),
    ], platform_contract={"platform_skill_boundary": {"final_output_fields": ["text"]}})
    assert facts["ambiguities"] == []
    assert facts["workflow_topology"]["scripts/b.py"] == ["scripts/a.py"]
    assert facts["input_bindings"][0]["source_node"] == "scripts/a.py"


def test_selection_protocol_accepts_identical_duplicate_and_rejects_unknown():
    ambiguities = [{"ambiguity_id": "A1", "candidate_sources": [{"candidate_id": "A1-C1"}]}]
    payload = json.dumps({"selections": [{"ambiguity_id": "A1", "selected_candidate_id": "A1-C1"}]})
    assert parse_ambiguity_selections(payload + payload, ambiguities) == {"A1": "A1-C1"}
    with pytest.raises(ValueError, match="invalid_candidate"):
        parse_ambiguity_selections('{"selections":[{"ambiguity_id":"A1","selected_candidate_id":"A1-C9"}]}', ambiguities)


def test_requirement_freeze_and_post_commit_audit_are_read_only():
    requirements = freeze_requirements("Create the requested output")
    committed_items = [item("scripts/a.py", [], ["text"])]
    committed_edges = [{"from_node": "scripts/a.py", "from_output": "text",
                        "to_node": "platform_output_node", "to_input": "text"}]
    original = json.dumps([committed_items, committed_edges], sort_keys=True)
    audit = audit_committed_requirement_coverage(
        requirements, normalized_blueprint={}, function_items=committed_items,
        responsibility_edges=committed_edges,
    )
    assert audit[0]["status"] == "unverifiable"
    assert audit[0]["owner_stage"] == "runtime"
    assert json.dumps([committed_items, committed_edges], sort_keys=True) == original
