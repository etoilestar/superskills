import json

import pytest

from backend.services.creator.blueprint_graph import (
    apply_ambiguity_selections,
    blueprint_graph_fingerprint,
    match_port_evidence,
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


def test_weak_name_is_not_silently_connected():
    facts = normalize_blueprint_graph_facts(function_items=[
        item("scripts/a.py", [], ["content"]), item("scripts/b.py", ["content"], []),
    ], platform_contract={})
    assert facts["input_bindings"] == []
    assert any(value["issue_type"] == "unbound_required_input" for value in facts["structural_issues"])
    assert match_port_evidence({"name": "content", "semantic_id": "", "value_type": "unknown"},
                               {"name": "content", "semantic_id": "", "value_type": "unknown"})["confidence"] == "weak_name"


def test_typed_name_is_deterministic_and_platform_peer_competes_when_equally_weak():
    typed = normalize_blueprint_graph_facts(function_items=[
        item("scripts/a.py", [], [{"name": "data", "value_type": "object"}]),
        item("scripts/b.py", [{"name": "data", "value_type": "object"}], []),
    ], platform_contract={})
    assert typed["input_bindings"][0]["source_node"] == "scripts/a.py"
    competing = normalize_blueprint_graph_facts(function_items=[
        item("scripts/a.py", [], ["payload"]), item("scripts/b.py", ["payload"], []),
    ], platform_contract={"platform_skill_boundary": {"input_envelope_fields": ["payload"]}})
    assert competing["input_bindings"] == []
    assert len(competing["ambiguities"]) == 1
    assert {value["source_node"] for value in competing["ambiguities"][0]["candidate_sources"]} == {
        "scripts/a.py", "platform_input_node"
    }


def test_dependency_limits_nodes_but_does_not_bind_unrelated_ports_or_hide_platform():
    facts = normalize_blueprint_graph_facts(function_items=[
        item("scripts/a.py", [], [
            {"name": "document", "semantic_id": "doc", "value_type": "object"},
            {"name": "unused", "value_type": "string"},
        ]),
        item("scripts/b.py", [
            {"name": "source", "semantic_id": "doc", "value_type": "object"},
            {"name": "tone", "value_type": "string"},
        ], [], ["scripts/a.py"]),
    ], platform_contract={"platform_skill_boundary": {"input_envelope_fields": [
        {"name": "tone", "value_type": "string"}
    ]}})
    sources = {(value["target_input"], value["source_node"], value["source_output"])
               for value in facts["input_bindings"]}
    assert sources == {("source", "scripts/a.py", "document"),
                       ("tone", "platform_input_node", "tone")}
    assert all(value.get("source_output") != "unused" for value in facts["input_candidate_registry"][("scripts/b.py", "source")])

    unrelated = normalize_blueprint_graph_facts(function_items=[
        item("scripts/a.py", [], [{"name": "left", "value_type": "string"}]),
        item("scripts/b.py", [{"name": "right", "value_type": "string"}], [], ["scripts/a.py"]),
    ], platform_contract={})
    assert unrelated["input_bindings"] == []


def test_invalid_explicit_binding_and_weak_final_output_are_structural_issues():
    facts = normalize_blueprint_graph_facts(function_items=[
        item("scripts/a.py", [], [{"name": "value", "value_type": "object"}]),
        item("scripts/b.py", [{"name": "value", "value_type": "string"}], ["text"]),
    ], input_bindings=[{"binding_kind": "script_output", "source_node": "scripts/a.py",
                       "source_output": "value", "target_node": "scripts/b.py", "target_input": "value"}],
       platform_contract={"platform_skill_boundary": {"final_output_fields": ["text"]}})
    assert "blueprint_binding_type_mismatch" in {value["issue_type"] for value in facts["structural_issues"]}
    assert facts["final_output_bindings"] == []
    assert any(value["issue_type"] == "missing_required_output" for value in facts["structural_issues"])


def test_unknown_dependency_is_preserved_and_reported():
    facts = normalize_blueprint_graph_facts(function_items=[
        item("scripts/a.py", [], ["text"], ["scripts/missing.py"]),
    ], platform_contract={})
    issue = next(value for value in facts["structural_issues"]
                 if value["issue_type"] == "blueprint_unknown_dependency")
    assert issue["dependencies"] == ["scripts/missing.py"]


def test_ambiguity_selection_that_combines_into_cycle_is_rejected():
    facts = normalize_blueprint_graph_facts(function_items=[
        item("scripts/a.py", ["y"], ["x"]), item("scripts/b.py", ["x"], ["y"]),
    ], platform_contract={"platform_skill_boundary": {"input_envelope_fields": ["x", "y"]}})
    assert len(facts["ambiguities"]) == 2
    selections = {}
    for ambiguity in facts["ambiguities"]:
        script = next(value for value in ambiguity["candidate_sources"]
                      if value["source_node"] != "platform_input_node")
        selections[ambiguity["ambiguity_id"]] = script["candidate_id"]
    with pytest.raises(ValueError, match="ambiguity_selection_structural_conflict"):
        apply_ambiguity_selections(facts, selections)


def test_normalization_is_order_independent_and_batch_cycle_safe():
    values = [
        item("scripts/a.py", [], [{"name": "document", "semantic_id": "doc"}]),
        item("scripts/b.py", [{"name": "source", "semantic_id": "doc"}], ["text"]),
        item("scripts/c.py", [], ["other"]),
    ]
    variants = [values, [values[2], values[0], values[1]], [values[1], values[2], values[0]]]
    normalized = [normalize_blueprint_graph_facts(function_items=value, platform_contract={}) for value in variants]
    assert normalized[0] == normalized[1] == normalized[2]
    cyclic = normalize_blueprint_graph_facts(function_items=[
        item("scripts/a.py", [{"name": "from_b", "semantic_id": "ba"}],
             [{"name": "to_b", "semantic_id": "ab"}]),
        item("scripts/b.py", [{"name": "from_a", "semantic_id": "ab"}],
             [{"name": "to_a", "semantic_id": "ba"}]),
    ], platform_contract={})
    assert cyclic["input_bindings"] == []
    assert "cycle_detected" in {value["issue_type"] for value in cyclic["structural_issues"]}


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


def test_graph_fingerprint_covers_topology_boundaries_constraints_and_resources():
    base = {"function_items": [], "workflow_topology": {}, "input_bindings": [],
            "final_output_bindings": [], "constraints": [], "resources": []}
    fingerprints = {blueprint_graph_fingerprint({**base, key: value}) for key, value in (
        ("workflow_topology", {"scripts/a.py": []}),
        ("input_bindings", [{"target_node": "scripts/a.py"}]),
        ("final_output_bindings", [{"platform_slot": "text"}]),
        ("constraints", [{"kind": "offline"}]),
        ("resources", ["references/a.md"]),
    )}
    assert len(fingerprints) == 5
