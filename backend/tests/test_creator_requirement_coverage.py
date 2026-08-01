from backend.services.creator.requirement_coverage import (
    build_requirement_coverage_candidates,
    candidate_fingerprint,
    coverage_claims_from_selections,
    extract_frozen_requirements,
    materialize_legacy_requirement_view,
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
        {"requirement_id": "R1", "selected_candidate_ids": resource_ids},
        {"requirement_id": "R2", "selected_candidate_ids": constraint_ids},
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
        {"requirement_id": "R1", "selected_candidate_ids": node_ids + [boundary_id]},
        {"requirement_id": "R2", "selected_candidate_ids": []},
    ], result)
    assert [x["kind"] for x in claims[0]["coverage_claims"]] == ["node", "boundary"]
    legacy = materialize_legacy_requirement_view(requirements(), claims, result)
    assert legacy["requirement_allocations"][0]["owners"] == ["scripts/a.py", "scripts/b.py"]


def test_preflight_defers_graph_claims_and_does_not_replan():
    result = registry(workflow_topology={"scripts/b.py": ["scripts/a.py"]})
    selected = [key for key, value in result.items() if value["kind"] in {"dataflow", "boundary"}]
    claims = coverage_claims_from_selections(requirements(), [
        {"requirement_id": "R1", "selected_candidate_ids": selected},
        {"requirement_id": "R2", "selected_candidate_ids": []},
    ], result)
    checked = validate_requirement_coverage_preflight(claims, result, items(), ["scripts/a.py", "scripts/b.py"])
    assert checked and {x["status"] for x in checked} == {"deferred"}
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
        {"requirement_id": "R1", "selected_candidate_ids": [flow_id]},
        {"requirement_id": "R2", "selected_candidate_ids": [output_id]},
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
