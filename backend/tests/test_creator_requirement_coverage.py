from backend.services.creator.requirement_coverage import (
    CoverageProjectionConflict,
    build_requirement_coverage_candidates,
    candidate_fingerprint,
    coverage_claims_from_selections,
    extract_frozen_requirements,
    materialize_legacy_requirement_view,
    project_coverage_claims_to_graph_hints,
    requirement_fingerprint,
    should_replan_blueprint,
    validate_compiled_coverage_claims,
    validate_requirement_coverage_preflight,
)

CONTRACT = {"platform_skill_boundary": {"input_envelope_fields": ["payload"], "final_output_fields": ["result", "artifact"]}}


def requirements():
    return extract_frozen_requirements([
        {"requirement": "first", "source_evidence": [{"source": "user_request", "quote": "one"}]},
        {"requirement": "second", "source_evidence": [{"source": "user_request", "quote": "two"}]},
    ])


def items(names=("a", "b")):
    return [{"target_file": f"scripts/{name}.py", "purpose": name,
             "inputs": [{"name": "shared", "type": "string"}],
             "outputs": [{"name": "shared", "type": "string"}],
             "constraints": (["bounded"] if name == "a" else [])} for name in names]


def registry(names=("a", "b"), **blueprint):
    functions = items(names)
    return build_requirement_coverage_candidates(
        frozen_requirements=requirements(), frozen_blueprint=blueprint,
        function_items=functions,
        allowed_function_item_targets=[x["target_file"] for x in functions],
        platform_contract=CONTRACT, authorized_references=["references/rules"],
        authorized_assets=["assets/style"], uploaded_files=[{"path": "uploads/data"}],
    )


def test_requirement_freeze_is_backend_owned_and_stable():
    frozen = requirements()
    assert [x["requirement_id"] for x in frozen] == ["R1", "R2"]
    assert requirement_fingerprint(frozen) == requirement_fingerprint(requirements())
    assert extract_frozen_requirements(frozen) == frozen


def test_candidate_registry_is_stable_and_does_not_complete_dag():
    kwargs = {"workflow_topology": {"scripts/b.py": ["scripts/a.py"]}}
    first = registry(**kwargs)
    assert first == registry(**kwargs)
    assert candidate_fingerprint(first) == candidate_fingerprint(registry(**kwargs))
    flows = [x for x in first.values() if x["kind"] == "dataflow"]
    assert [(x["source_target"], x["target_target"]) for x in flows] == [("scripts/a.py", "scripts/b.py")]


def test_serial_branch_and_merge_candidates_only_follow_topology():
    functions = items(("a", "b", "c", "d"))
    graph = {"scripts/b.py": ["scripts/a.py"], "scripts/c.py": ["scripts/a.py"], "scripts/d.py": ["scripts/b.py", "scripts/c.py"]}
    result = build_requirement_coverage_candidates(
        frozen_requirements=requirements(), frozen_blueprint={"workflow_topology": graph},
        function_items=functions, allowed_function_item_targets=[x["target_file"] for x in functions],
        platform_contract=CONTRACT,
    )
    flows = {(x["source_target"], x["target_target"]) for x in result.values() if x["kind"] == "dataflow"}
    assert flows == {("scripts/a.py", "scripts/b.py"), ("scripts/a.py", "scripts/c.py"),
                     ("scripts/b.py", "scripts/d.py"), ("scripts/c.py", "scripts/d.py")}


def test_terminal_boundary_domain_excludes_intermediate_nodes():
    result = registry(workflow_topology={"scripts/b.py": ["scripts/a.py"]})
    outputs = [x for x in result.values() if x["kind"] == "boundary" and x["direction"] == "output"]
    assert outputs and {x["node_target"] for x in outputs} == {"scripts/b.py"}
    assert {x["platform_slot"] for x in outputs} == {"result", "artifact"}


def test_resources_and_constraints_are_structural_not_owners():
    result = registry(resources=[{"path": "bundle", "kind": "bundled_resource"}], constraints=["global"])
    resource_ids = [key for key, value in result.items() if value["kind"] == "resource"]
    constraint_ids = [key for key, value in result.items() if value["kind"] == "constraint"]
    claims = coverage_claims_from_selections(requirements(), [
        {"requirement_id": "R1", "decision": "selected", "selected_candidate_ids": resource_ids},
        {"requirement_id": "R2", "decision": "selected", "selected_candidate_ids": constraint_ids},
    ], result)
    legacy = materialize_legacy_requirement_view(requirements(), claims, result)
    assert legacy["requirement_allocations"][0]["owners"] == []
    assert legacy["requirement_allocations"][1]["owners"] == []
    assert legacy["requirement_channels"] == {"R1": "resource", "R2": "executable"}


def test_mixed_claims_and_multiple_node_owners_are_preserved():
    result = registry()
    node_ids = [key for key, value in result.items() if value["kind"] == "node"]
    boundary_id = next(key for key, value in result.items() if value["kind"] == "boundary")
    claims = coverage_claims_from_selections(requirements(), [
        {"requirement_id": "R1", "decision": "selected", "selected_candidate_ids": node_ids + [boundary_id]},
        {"requirement_id": "R2", "decision": "no_valid_candidate", "selected_candidate_ids": []},
    ], result)
    assert [x["kind"] for x in claims[0]["coverage_claims"]] == ["node", "boundary"]
    legacy = materialize_legacy_requirement_view(requirements(), claims, result)
    assert legacy["requirement_allocations"][0]["owners"] == ["scripts/a.py", "scripts/b.py"]


def test_preflight_defers_graph_claims_and_does_not_replan():
    result = registry(workflow_topology={"scripts/b.py": ["scripts/a.py"]})
    selected = [key for key, value in result.items() if value["kind"] in {"dataflow", "boundary"}]
    claims = coverage_claims_from_selections(requirements(), [
        {"requirement_id": "R1", "decision": "selected", "selected_candidate_ids": selected},
        {"requirement_id": "R2", "decision": "no_valid_candidate", "selected_candidate_ids": []},
    ], result)
    checked = validate_requirement_coverage_preflight(claims, result, items(), ["scripts/a.py", "scripts/b.py"])
    assert {x["status"] for x in checked} == {"deferred", "missing_structure"}
    assert next(x for x in checked if x["requirement_id"] == "R2")["issue_type"] == "requirement_coverage_unmapped"
    assert not should_replan_blueprint(checked, replan_count=0)


def test_only_first_blueprint_missing_structure_can_replan():
    result = [{"status": "missing_structure", "owner_stage": "blueprint"}]
    assert should_replan_blueprint(result, replan_count=0)
    assert not should_replan_blueprint(result, replan_count=1)
    assert not should_replan_blueprint([{"status": "deferred", "owner_stage": "responsibility_graph"}], replan_count=0)


def test_compiled_validation_reports_graph_specific_output_gap():
    result = registry(workflow_topology={"scripts/b.py": ["scripts/a.py"]})
    flow_id = next(key for key, value in result.items() if value["kind"] == "dataflow")
    output_id = next(key for key, value in result.items() if value["kind"] == "boundary" and value["direction"] == "output")
    claims = coverage_claims_from_selections(requirements(), [
        {"requirement_id": "R1", "decision": "selected", "selected_candidate_ids": [flow_id]},
        {"requirement_id": "R2", "decision": "selected", "selected_candidate_ids": [output_id]},
    ], result)
    checked = validate_compiled_coverage_claims(claims, result, items(), [
        {"from_node": "scripts/a.py", "from_output": "shared", "to_node": "scripts/b.py", "to_input": "shared"}
    ], CONTRACT)
    assert checked[0]["status"] == "satisfied"
    assert checked[1]["issue_type"] == "final_output_contract_gap"


def test_three_repeated_runs_are_identical():
    snapshots = []
    for _ in range(3):
        result = registry(workflow_topology={"scripts/b.py": ["scripts/a.py"]})
        snapshots.append((candidate_fingerprint(result), result))
    assert snapshots[0] == snapshots[1] == snapshots[2]


def test_selection_protocol_is_closed_and_empty_selection_is_retained():
    result = registry()
    claims = coverage_claims_from_selections(requirements(), [
        {"requirement_id": "R1", "decision": "no_valid_candidate", "selected_candidate_ids": []},
        {"requirement_id": "R2", "decision": "no_valid_candidate", "selected_candidate_ids": []},
    ], result)
    assert [item["decision"] for item in claims] == ["no_valid_candidate", "no_valid_candidate"]
    checked = validate_requirement_coverage_preflight(claims, result, items(), ["scripts/a.py", "scripts/b.py"])
    assert len(checked) == 2
    assert {item["issue_type"] for item in checked} == {"requirement_coverage_unmapped"}


def test_claims_project_to_graph_hints_and_conflicts_do_not_overwrite():
    result = registry(
        workflow_topology={"scripts/b.py": ["scripts/a.py"]},
        input_bindings=[{"binding_kind": "platform_parameter", "source_root": "payload",
                         "source_output": "payload", "target_node": "scripts/a.py", "target_input": "shared"}],
        final_output_bindings=[{"platform_slot": "result", "source_node": "scripts/b.py", "source_output": "shared"}],
    )
    selected = [key for key, value in result.items() if value["kind"] == "dataflow"]
    selected += [key for key, value in result.items() if value["kind"] == "boundary"]
    claims = coverage_claims_from_selections(requirements(), [
        {"requirement_id": "R1", "decision": "selected", "selected_candidate_ids": selected},
        {"requirement_id": "R2", "decision": "no_valid_candidate", "selected_candidate_ids": []},
    ], result)
    projected = project_coverage_claims_to_graph_hints(
        coverage_claims=claims, candidate_registry=result,
        workflow_topology={}, input_bindings=[], final_output_bindings=[],
    )
    assert projected["workflow_topology"] == {"scripts/b.py": ["scripts/a.py"]}
    assert projected["input_bindings"] and projected["final_output_bindings"]
    with __import__("pytest").raises(CoverageProjectionConflict):
        project_coverage_claims_to_graph_hints(
            coverage_claims=claims, candidate_registry=result, workflow_topology={},
            input_bindings=[{"binding_kind": "platform_parameter", "source_node": "platform_input_node",
                             "source_output": "other", "source_root": "other",
                             "target_node": "scripts/a.py", "target_input": "shared"}],
            final_output_bindings=[],
        )


def test_boundary_validation_is_port_exact():
    functions = [{"target_file": "scripts/a.py", "inputs": [],
                  "outputs": [{"name": "correct_output"}, {"name": "wrong_output"}]}]
    result = build_requirement_coverage_candidates(
        frozen_requirements=requirements(), frozen_blueprint={}, function_items=functions,
        allowed_function_item_targets=["scripts/a.py"], platform_contract=CONTRACT,
    )
    candidate_id = next(key for key, value in result.items()
                        if value["kind"] == "boundary" and value["node_port"] == "correct_output"
                        and value["platform_slot"] == "result")
    claims = coverage_claims_from_selections(requirements(), [
        {"requirement_id": "R1", "decision": "selected", "selected_candidate_ids": [candidate_id]},
        {"requirement_id": "R2", "decision": "no_valid_candidate", "selected_candidate_ids": []},
    ], result)
    checked = validate_compiled_coverage_claims(claims, result, functions, [{
        "from_node": "scripts/a.py", "from_output": "wrong_output",
        "to_node": "platform_output_node", "to_input": "result",
    }], CONTRACT)
    assert checked[0]["issue_type"] == "final_output_contract_gap"


def test_node_candidate_preserves_complete_function_item_structure():
    function = {
        "target_file": "scripts/a.py", "purpose": "p", "inputs": ["i"], "outputs": ["o"],
        "dependencies": ["scripts/upstream.py"], "required_capabilities": ["cap"],
        "forbidden_capabilities": ["forbidden"], "constraints": ["bounded"],
        "default_values": {"limit": 1}, "references": ["references/rules"],
    }
    result = build_requirement_coverage_candidates(
        frozen_requirements=requirements(), frozen_blueprint={}, function_items=[function],
        allowed_function_item_targets=["scripts/a.py"], platform_contract=CONTRACT,
    )
    node = next(value for value in result.values() if value["kind"] == "node")
    for key in function:
        assert node[key] == function[key]
