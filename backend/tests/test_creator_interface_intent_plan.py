import json
import pytest

from backend.services.creator.function_item_interface_plan import (
    CRITIC_SCHEMA, InterfaceIntentPlanError, _compact_function_items,
    _interface_plan_prompt, build_graph_obligations_from_interfaces,
    build_interface_repair_scope, collect_interface_plan_validation_issues,
    canonical_logical_binding_signatures,
    normalize_interface_review_issue, repair_interface_plan_semantically,
    plan_function_item_interfaces, review_interface_plan_semantically,
    validate_interface_intent_plan, validate_interface_repair_critic,
)


def item(name, inputs, outputs):
    return {"target_file": name, "role": "script", "purpose": f"purpose {name}", "inputs": inputs, "outputs": outputs, "default_values": {}, "required_capabilities": [], "constraints": []}


def p2m(iid, target, target_input="slot_x", source="payload"):
    return {"interface_id": iid, "kind": "platform_to_member", "source_platform_input": source, "source_path": [], "target_member": target, "target_input": target_input, "goal": "supply the declared receiving slot"}


def m2m(iid, source, output, target, target_input):
    return {"interface_id": iid, "kind": "member_to_member", "source_member": source, "source_output": output, "target_member": target, "target_input": target_input, "goal": "supply the declared receiving slot"}


def m2p(iid, source, output="result_z", target="text"):
    return {"interface_id": iid, "kind": "member_to_platform", "source_member": source, "source_output": output, "target_platform_output": target, "goal": "deliver the declared platform result"}


def platform():
    return {"platform_skill_boundary": {"input_envelope_fields": ["payload"], "final_output_fields": ["text"]
, "required_final_output_fields": ["text"]
}}


def test_interface_schema_has_explicit_logical_bindings():
    items = [item("scripts/unit_a.py", [{"port_id": "slot_x", "description": "logical input", "contract": {"type": "string"}}], [{"port_id": "result_z", "description": "logical output", "contract": {"type": "string"}}])]
    plan = {"interfaces": [p2m("I1", "scripts/unit_a.py"), m2p("I2", "scripts/unit_a.py")]}
    assert validate_interface_intent_plan(plan=plan, function_items=items) == plan
    obligations = build_graph_obligations_from_interfaces(interface_plan=plan)
    assert obligations[0]["source_platform_input"] == "payload"
    assert obligations[0]["target_input"] == "slot_x"
    assert obligations[1]["source_output"] == "result_z"
    assert obligations[1]["target_platform_output"] == "text"


def test_canonical_binding_signature_ignores_goal_id_and_order_but_not_binding():
    before = {"interfaces": [p2m("I1", "scripts/unit_a.py"), m2p("I2", "scripts/unit_a.py")]}
    presentation_only = json.loads(json.dumps(before))
    presentation_only["interfaces"].reverse()
    presentation_only["interfaces"][0]["interface_id"] = "I9"
    presentation_only["interfaces"][0]["goal"] = "value_x"
    assert canonical_logical_binding_signatures(before) == canonical_logical_binding_signatures(presentation_only)
    changed = json.loads(json.dumps(before))
    changed["interfaces"][0]["source_path"] = ["value_y"]
    assert canonical_logical_binding_signatures(before) != canonical_logical_binding_signatures(changed)


def test_compact_ports_preserve_semantic_metadata():
    compact = _compact_function_items([item("scripts/unit_a.py", [{"port_id": "slot_x", "description": "x", "contract": {"type": "string"}}], [{"port_id": "value_a", "description": "a", "contract": {"type": "string"}}])])[0]
    assert compact["inputs"][0] == {"name": "slot_x", "description": "x", "contract": {"type": "string"}, "required": True, "default_present": False, "runtime_source_required": True}
    assert compact["outputs"][0] == {"name": "value_a", "description": "a", "contract": {"type": "string"}}


def test_interface_closure_finds_missing_slot_before_graph():
    items = [item("scripts/unit_a.py", ["slot_x", "slot_y"], ["result_z"])]
    issues = collect_interface_plan_validation_issues(plan={"interfaces": [p2m("I1", "scripts/unit_a.py", "slot_x"), m2p("I2", "scripts/unit_a.py")]}, function_items=items, platform_contract=platform())
    assert [issue["code"] for issue in issues] == ["uncovered_required_logical_input"]
    assert issues[0]["observed_value"]["target_input"] == "slot_y"


def test_broad_goal_does_not_cover_another_structured_slot():
    interface = p2m("I1", "scripts/unit_a.py", "slot_x")
    interface["goal"] = "supply slot_x and slot_y and everything else"
    issues = collect_interface_plan_validation_issues(plan={"interfaces": [interface, m2p("I2", "scripts/unit_a.py")]}, function_items=[item("scripts/unit_a.py", ["slot_x", "slot_y"], ["result_z"])], platform_contract=platform())
    assert any(issue["observed_value"].get("target_input") == "slot_y" for issue in issues)


def test_optional_and_default_inputs_do_not_require_interfaces():
    inputs = [{"port_id": "slot_x", "required": False}, {"port_id": "slot_y", "default": "v"}]
    issues = collect_interface_plan_validation_issues(plan={"interfaces": [m2p("I1", "scripts/unit_a.py")]}, function_items=[item("scripts/unit_a.py", inputs, ["result_z"])], platform_contract=platform())
    assert issues == []


def test_reusable_output_can_cover_multiple_receiving_slots():
    items = [item("scripts/unit_a.py", ["slot_x"], ["value_a"]), item("scripts/unit_b.py", ["slot_x", "slot_y"], ["result_z"])]
    plan = {"interfaces": [p2m("I1", "scripts/unit_a.py"), m2m("I2", "scripts/unit_a.py", "value_a", "scripts/unit_b.py", "slot_x"), m2m("I3", "scripts/unit_a.py", "value_a", "scripts/unit_b.py", "slot_y"), m2p("I4", "scripts/unit_b.py")]}
    assert collect_interface_plan_validation_issues(plan=plan, function_items=items, platform_contract=platform()) == []


def test_unknown_logical_port_is_rejected():
    with pytest.raises(InterfaceIntentPlanError) as raised:
        validate_interface_intent_plan(plan={"interfaces": [p2m("I1", "scripts/unit_a.py", "missing")]}, function_items=[item("scripts/unit_a.py", ["slot_x"], ["value_a"])])
    assert raised.value.code == "unknown_interface_logical_port"


def test_critic_protocol_is_two_strings_only():
    value = {"diagnosis": "root cause", "required_postcondition": "all required slots are faithfully bound"}
    assert CRITIC_SCHEMA == {"diagnosis": "string", "required_postcondition": "string"}
    assert validate_interface_repair_critic(value, validation_issues=[], current_interface_plan={}, frozen_function_items=[], repair_scope={}) == value


def test_reviewer_issue_has_no_taxonomy():
    raw = {"message": "semantic mismatch", "affected_interfaces": ["I1"], "affected_inputs": [{"target_member": "scripts/unit_a.py", "target_input": "slot_x"}], "evidence": {"observed": "value_a", "expected": "value_b"}}
    issue = normalize_interface_review_issue(raw, [item("scripts/unit_a.py", ["slot_x"], ["value_a"])], {"interfaces": [p2m("I1", "scripts/unit_a.py")]})
    assert "code" not in issue and "category" not in issue


@pytest.mark.asyncio
async def test_unknown_semantic_defect_runs_generic_repair_and_revalidation():
    items = [item("scripts/unit_a.py", ["slot_x"], ["result_z"])]
    current, repaired = {"interfaces": [p2m("I1", "scripts/unit_a.py"), m2p("I2", "scripts/unit_a.py", target="text")]}, {"interfaces": [p2m("I1", "scripts/unit_a.py"), m2p("I2", "scripts/unit_a.py")]}
    repaired["interfaces"][0]["source_path"] = ["value_x"]
    issue = {"message": "novel semantic defect", "affected_interfaces": ["I1"], "affected_inputs": [{"target_member": "scripts/unit_a.py", "target_input": "slot_x"}], "evidence": {"observed": "wrong", "expected": "correct"}, "details": {}, "stage": "review", "path": "$", "interface_id": "I1"}
    reviewer_calls = 0
    async def reviewer(messages, _model):
        nonlocal reviewer_calls
        reviewer_calls += 1
        payload = json.loads(messages[-1]["content"])
        return json.dumps({"diagnosis": "root cause", "required_postcondition": "correct binding semantics"} if "validation_issues" in payload else {"passed": True, "issues": []})
    async def planner(_messages, _model): return json.dumps(repaired)
    result = await repair_interface_plan_semantically(original_user_goal="g", frozen_function_items=items, current_interface_plan=current, validation_issues=[issue], repair_scope=build_interface_repair_scope([issue]), platform_contract=platform(), planner_model="planner-test-model", model_call=planner, reviewer_model="reviewer-test-model", reviewer_model_call=reviewer)
    assert result == repaired and reviewer_calls == 2


def test_planner_prompt_separates_logical_ports_from_endpoint_ids():
    prompt = _interface_plan_prompt()
    assert "Structured logical binding fields are authoritative" in prompt
    assert "opaque endpoint IDs" in prompt
    assert "Do not choose by field-name similarity alone" in prompt
    assert "final_output_fields defines the legal platform-output domain" in prompt
    assert "Do not treat every legal final_output_field as required" in prompt


def test_required_default_matrix_and_default_values_agree_with_closure():
    values = [
        ({"port_id": "slot_required", "required": True}, True),
        ({"port_id": "slot_optional", "required": False}, False),
        ({"port_id": "slot_inline", "default": "x"}, False),
    ]
    function_item = item("scripts/unit_a.py", [value for value, _ in values] + [{"port_id": "slot_map"}], ["result_z"])
    function_item["default_values"] = {"slot_map": "x"}
    compact = _compact_function_items([function_item])[0]
    assert {value["name"]: value["runtime_source_required"] for value in compact["inputs"]} == {"slot_required": True, "slot_optional": False, "slot_inline": False, "slot_map": False}
    issues = collect_interface_plan_validation_issues(plan={"interfaces": [m2p("I1", "scripts/unit_a.py")]}, function_items=[function_item], platform_contract=platform())
    assert [issue["observed_value"]["target_input"] for issue in issues if issue["code"] == "uncovered_required_logical_input"] == ["slot_required"]


def test_required_platform_outputs_are_explicit_subset_only():
    contract = {"platform_skill_boundary": {"input_envelope_fields": ["payload"], "final_output_fields": ["text", "image_path", "file_path"], "required_final_output_fields": ["text"]}}
    interfaces = {"interfaces": [p2m("I1", "scripts/unit_a.py"), m2p("I2", "scripts/unit_a.py", target="text")]}
    assert collect_interface_plan_validation_issues(plan=interfaces, function_items=[item("scripts/unit_a.py", ["slot_x"], ["result_z"])], platform_contract=contract) == []


def test_missing_required_output_declaration_does_not_require_all_legal_outputs():
    contract = {"platform_skill_boundary": {"input_envelope_fields": ["payload"], "final_output_fields": ["text", "image_path"]}}
    interfaces = {"interfaces": [p2m("I1", "scripts/unit_a.py"), m2p("I2", "scripts/unit_a.py", target="text")]}
    assert collect_interface_plan_validation_issues(plan=interfaces, function_items=[item("scripts/unit_a.py", ["slot_x"], ["result_z"])], platform_contract=contract) == []


def test_required_platform_output_must_be_in_legal_domain():
    contract = {"platform_skill_boundary": {"input_envelope_fields": ["payload"], "final_output_fields": ["text"], "required_final_output_fields": ["missing"]}}
    issues = collect_interface_plan_validation_issues(plan={"interfaces": [m2p("I1", "scripts/unit_a.py", target="text")]}, function_items=[item("scripts/unit_a.py", [], ["result_z"])], platform_contract=contract)
    assert any(issue["code"] == "invalid_required_platform_output" for issue in issues)


def test_duplicate_receiving_slot_is_rejected_without_aggregation_contract():
    items = [item("scripts/unit_a.py", ["slot_x"], ["value_a"]), item("scripts/unit_b.py", ["slot_y"], ["result_z"])]
    plan = {"interfaces": [p2m("I1", "scripts/unit_a.py"), m2m("I2", "scripts/unit_a.py", "value_a", "scripts/unit_b.py", "slot_y"), p2m("I3", "scripts/unit_b.py", "slot_y"), m2p("I4", "scripts/unit_b.py")]}
    assert any(issue["code"] == "duplicate_logical_input_provenance" for issue in collect_interface_plan_validation_issues(plan=plan, function_items=items, platform_contract=platform()))


def test_aggregation_marker_does_not_bypass_single_provenance_contract():
    target_input = {"port_id": "slot_y", "contract": {"aggregation": True}}
    items = [item("scripts/unit_a.py", ["slot_x"], ["value_a"]), item("scripts/unit_b.py", [target_input], ["result_z"])]
    plan = {"interfaces": [p2m("I1", "scripts/unit_a.py"), m2m("I2", "scripts/unit_a.py", "value_a", "scripts/unit_b.py", "slot_y"), p2m("I3", "scripts/unit_b.py", "slot_y"), m2p("I4", "scripts/unit_b.py")]}
    issues = collect_interface_plan_validation_issues(plan=plan, function_items=items, platform_contract=platform())
    assert any(issue["code"] == "duplicate_logical_input_provenance" for issue in issues)


@pytest.mark.asyncio
async def test_generator_may_repair_declared_source_path():
    items = [item("scripts/unit_a.py", ["slot_x"], ["result_z"])]
    current = {"interfaces": [p2m("I1", "scripts/unit_a.py"), m2p("I2", "scripts/unit_a.py")]}
    current["interfaces"][0]["source_path"] = ["x"]
    repaired = json.loads(json.dumps(current))
    repaired["interfaces"][0]["source_path"] = ["y"]
    issue = {"message": "nested platform value is semantically inconsistent", "affected_interfaces": ["I1"], "affected_inputs": [{"target_member": "scripts/unit_a.py", "target_input": "slot_x"}], "evidence": {"observed": "x", "expected": "confirmed nested value"}, "details": {}, "stage": "review", "path": "$", "interface_id": "I1"}
    generator_prompts = []
    reviewer_prompts = []

    async def reviewer(messages, _model):
        reviewer_prompts.append(messages[0]["content"])
        payload = json.loads(messages[-1]["content"])
        return json.dumps({"diagnosis": "the nested value is wrong", "required_postcondition": "bind the confirmed nested value"} if "validation_issues" in payload else {"passed": True, "issues": []})

    async def planner(messages, _model):
        generator_prompts.append(messages[0]["content"])
        return json.dumps(repaired)

    result = await repair_interface_plan_semantically(original_user_goal="g", frozen_function_items=items, current_interface_plan=current, validation_issues=[issue], repair_scope=build_interface_repair_scope([issue]), platform_contract=platform(), planner_model="planner-test-model", model_call=planner, reviewer_model="reviewer-test-model", reviewer_model_call=reviewer)
    assert result["interfaces"][0]["source_path"] == ["y"]
    assert "source_path is part of the declared logical binding" in generator_prompts[0]
    assert "Do not add opaque endpoint IDs or Graph edges" in generator_prompts[0]
    assert "Do not add source paths" not in generator_prompts[0]
    for header in ("PLATFORM OUTPUT CONTRACT", "RUNTIME INPUT PROVENANCE CONTRACT"):
        assert header in reviewer_prompts[0]
        assert header in generator_prompts[0]

@pytest.mark.asyncio
async def test_planner_protocol_repair_runs_once_and_preserves_semantics():
    from backend.services.creator.function_item_interface_plan import plan_function_item_interfaces
    items = [item("scripts/unit_a.py", ["slot_x"], ["result_z"])]
    valid = {"interfaces": [p2m("I1", "scripts/unit_a.py"), m2p("I2", "scripts/unit_a.py")]}
    responses = iter(["not-json", json.dumps(valid)])
    calls = 0
    async def planner(_messages, _model):
        nonlocal calls
        calls += 1
        return next(responses)
    assert await plan_function_item_interfaces(original_user_goal="g", frozen_function_items=items, platform_contract=platform(), planner_model="planner-test-model", model_call=planner) == valid
    assert calls == 2


@pytest.mark.asyncio
async def test_valid_json_missing_source_path_uses_planner_correction_not_reformatter():
    from backend.services.creator.function_item_interface_plan import plan_function_item_interfaces
    items = [item("scripts/unit_a.py", ["slot_x"], ["result_z"])]
    valid = {"interfaces": [p2m("I1", "scripts/unit_a.py"), m2p("I2", "scripts/unit_a.py")]}
    missing = json.loads(json.dumps(valid))
    del missing["interfaces"][0]["source_path"]
    prompts = []
    responses = iter([json.dumps(missing), json.dumps(valid)])
    async def planner(messages, _model):
        prompts.append(messages[0]["content"])
        return next(responses)
    assert await plan_function_item_interfaces(original_user_goal="g", frozen_function_items=items, platform_contract=platform(), planner_model="planner-test-model", model_call=planner) == valid
    assert len(prompts) == 2
    assert "INTERFACE PLAN CORRECTION" in prompts[1]
    assert "PROTOCOL REPAIR AUTHORITY" not in prompts[1]


@pytest.mark.asyncio
async def test_planner_correction_uses_second_attempt_for_staged_residual():
    from backend.services.creator.function_item_interface_plan import plan_function_item_interfaces
    items = [item("scripts/unit_a.py", ["slot_x"], ["value_x"]),
             item("scripts/unit_b.py", ["slot_x"], ["result_z"])]
    initial = {"interfaces": [p2m("I1", "scripts/unit_a.py"), m2p("I3", "scripts/unit_b.py")]}
    del initial["interfaces"][0]["source_path"]
    first = {"interfaces": [p2m("I1", "scripts/unit_a.py"), m2p("I3", "scripts/unit_b.py")]}
    complete = {"interfaces": [p2m("I1", "scripts/unit_a.py"),
                               m2m("I2", "scripts/unit_a.py", "value_x", "scripts/unit_b.py", "slot_x"),
                               m2p("I3", "scripts/unit_b.py")]}
    planner_responses = iter([initial, first, complete])
    correction_payloads = []
    reviewer_calls = 0

    async def planner(messages, _model):
        value = next(planner_responses)
        if "INTERFACE PLAN CORRECTION" in messages[0]["content"]:
            correction_payloads.append(json.loads(messages[-1]["content"]))
        return json.dumps(value)

    async def reviewer(_messages, _model):
        nonlocal reviewer_calls
        reviewer_calls += 1
        return json.dumps({"passed": True, "issues": []})

    result = await plan_function_item_interfaces(
        original_user_goal="g", frozen_function_items=items,
        platform_contract=platform(), planner_model="planner-test-model",
        model_call=planner, reviewer_model="reviewer-test-model",
        reviewer_model_call=reviewer,
    )
    assert result == complete
    assert [value["refinement_feedback"]["attempt"] for value in correction_payloads] == [1, 2]
    assert correction_payloads[1]["previous_interface_plan"] == first
    assert any(value["code"] == "uncovered_required_logical_input"
               for value in correction_payloads[1]["refinement_feedback"]["acceptance_facts"])
    # Each deterministic-invalid candidate is now audited as well as the final plan.
    assert reviewer_calls == 3


@pytest.mark.asyncio
async def test_planner_correction_no_progress_uses_full_budget():
    from backend.services.creator.function_item_interface_plan import plan_function_item_interfaces
    items = [item("scripts/unit_a.py", ["slot_x", "slot_y"], ["result_z"])]
    initial = {"interfaces": [p2m("I1", "scripts/unit_a.py"), m2p("I2", "scripts/unit_a.py")]}
    presentation_only = json.loads(json.dumps(initial))
    presentation_only["interfaces"].reverse()
    presentation_only["interfaces"][0]["interface_id"] = "I9"
    presentation_only["interfaces"][0]["goal"] = "value_x"
    repeated = json.loads(json.dumps(presentation_only))
    responses = iter([initial, presentation_only, repeated])
    calls = 0

    async def planner(_messages, _model):
        nonlocal calls
        calls += 1
        return json.dumps(next(responses))

    with pytest.raises(InterfaceIntentPlanError) as raised:
        await plan_function_item_interfaces(
            original_user_goal="g", frozen_function_items=items,
            platform_contract=platform(), planner_model="planner-test-model",
            model_call=planner,
        )
    assert raised.value.code == "interface_plan_deterministic_closure_failed"
    assert raised.value.details["semantic_changed"] is False
    assert calls == 3


@pytest.mark.asyncio
async def test_planner_correction_hard_stops_after_two_residual_attempts():
    from backend.services.creator.function_item_interface_plan import plan_function_item_interfaces
    items = [item("scripts/unit_a.py", ["slot_x", "slot_y"], ["result_z"])]
    initial = {"interfaces": [p2m("I1", "scripts/unit_a.py"), m2p("I2", "scripts/unit_a.py")]}
    del initial["interfaces"][0]["source_path"]
    first = {"interfaces": [p2m("I1", "scripts/unit_a.py"), m2p("I2", "scripts/unit_a.py")]}
    second = json.loads(json.dumps(first))
    second["interfaces"][0]["source_path"] = ["value_x"]
    responses = iter([initial, first, second])
    calls = 0

    async def planner(_messages, _model):
        nonlocal calls
        calls += 1
        return json.dumps(next(responses))

    with pytest.raises(InterfaceIntentPlanError) as raised:
        await plan_function_item_interfaces(
            original_user_goal="g", frozen_function_items=items,
            platform_contract=platform(), planner_model="planner-test-model",
            model_call=planner,
        )
    assert raised.value.code == "interface_plan_deterministic_closure_failed"
    assert raised.value.details["correction_attempts"] == 2
    assert calls == 3


@pytest.mark.asyncio
async def test_generator_schema_invalid_candidate_becomes_second_attempt_residual():
    items = [item("scripts/unit_a.py", ["slot_x"], ["result_z"])]
    current = {"interfaces": [p2m("I1", "scripts/unit_a.py"), m2p("I2", "scripts/unit_a.py")]}
    invalid = json.loads(json.dumps(current))
    del invalid["interfaces"][0]["source_path"]
    repaired = json.loads(json.dumps(current))
    repaired["interfaces"][0]["source_path"] = ["value_x"]
    issue = {"message": "semantic mismatch", "affected_interfaces": ["I1"],
             "affected_inputs": [{"target_member": "scripts/unit_a.py", "target_input": "slot_x"}],
             "evidence": {"observed": "value_x", "expected": "value_y"},
             "details": {}, "stage": "review", "path": "$", "interface_id": "I1"}
    generator_responses = iter([invalid, repaired])
    generator_payloads = []
    critic_calls = reviewer_calls = 0

    async def critic_or_reviewer(messages, _model):
        nonlocal critic_calls, reviewer_calls
        payload = json.loads(messages[-1]["content"])
        if "validation_issues" in payload:
            critic_calls += 1
            return json.dumps({"diagnosis": "semantic mismatch", "required_postcondition": "binding is semantically valid"})
        reviewer_calls += 1
        return json.dumps({"passed": True, "issues": []})

    async def generator(messages, _model):
        generator_payloads.append(json.loads(messages[-1]["content"]))
        return json.dumps(next(generator_responses))

    result = await repair_interface_plan_semantically(
        original_user_goal="g", frozen_function_items=items,
        current_interface_plan=current, validation_issues=[issue],
        repair_scope=build_interface_repair_scope([issue]),
        platform_contract=platform(), planner_model="planner-test-model",
        model_call=generator, reviewer_model="reviewer-test-model",
        reviewer_model_call=critic_or_reviewer,
    )
    assert result == repaired
    assert critic_calls == 1
    assert len(generator_payloads) == 2
    assert generator_payloads[1]["current_interface_plan"] == invalid
    assert generator_payloads[1]["refinement_feedback"]["acceptance_facts"][0]["code"] == "invalid_interface_protocol"
    assert reviewer_calls == 1


@pytest.mark.asyncio
async def test_generator_schema_invalid_hard_stops_after_two_attempts():
    items = [item("scripts/unit_a.py", ["slot_x"], ["result_z"])]
    current = {"interfaces": [p2m("I1", "scripts/unit_a.py"), m2p("I2", "scripts/unit_a.py")]}
    invalid = json.loads(json.dumps(current))
    del invalid["interfaces"][0]["source_path"]
    issue = {"message": "semantic mismatch", "affected_interfaces": ["I1"],
             "affected_inputs": [{"target_member": "scripts/unit_a.py", "target_input": "slot_x"}],
             "evidence": {"observed": "value_x", "expected": "value_y"},
             "details": {}, "stage": "review", "path": "$", "interface_id": "I1"}
    critic_calls = generator_calls = 0

    async def critic(_messages, _model):
        nonlocal critic_calls
        critic_calls += 1
        return json.dumps({"diagnosis": "semantic mismatch", "required_postcondition": "binding is semantically valid"})

    async def generator(_messages, _model):
        nonlocal generator_calls
        generator_calls += 1
        return json.dumps(invalid)

    with pytest.raises(InterfaceIntentPlanError) as raised:
        await repair_interface_plan_semantically(
            original_user_goal="g", frozen_function_items=items,
            current_interface_plan=current, validation_issues=[issue],
            repair_scope=build_interface_repair_scope([issue]),
            platform_contract=platform(), planner_model="planner-test-model",
            model_call=generator, reviewer_model="reviewer-test-model",
            reviewer_model_call=critic,
        )
    assert raised.value.code == "semantic_issues_remain"
    assert critic_calls == 1
    assert generator_calls == 2


@pytest.mark.asyncio
async def test_generator_transport_repair_preserves_schema_residual_for_attempt_two():
    items = [item("scripts/unit_a.py", ["slot_x"], ["result_z"])]
    current = {"interfaces": [p2m("I1", "scripts/unit_a.py"), m2p("I2", "scripts/unit_a.py")]}
    invalid = json.loads(json.dumps(current))
    del invalid["interfaces"][0]["source_path"]
    repaired = json.loads(json.dumps(current))
    repaired["interfaces"][0]["source_path"] = ["value_x"]
    issue = {"message": "semantic mismatch", "affected_interfaces": ["I1"],
             "affected_inputs": [{"target_member": "scripts/unit_a.py", "target_input": "slot_x"}],
             "evidence": {"observed": "value_x", "expected": "value_y"},
             "details": {}, "stage": "review", "path": "$", "interface_id": "I1"}
    responses = iter(["not-json", json.dumps(invalid), json.dumps(repaired)])
    prompts = []

    async def critic_or_reviewer(messages, _model):
        payload = json.loads(messages[-1]["content"])
        return json.dumps({"diagnosis": "semantic mismatch", "required_postcondition": "binding is semantically valid"} if "validation_issues" in payload else {"passed": True, "issues": []})

    async def generator_or_reformatter(messages, _model):
        prompts.append(messages[0]["content"])
        return next(responses)

    result = await repair_interface_plan_semantically(
        original_user_goal="g", frozen_function_items=items,
        current_interface_plan=current, validation_issues=[issue],
        repair_scope=build_interface_repair_scope([issue]),
        platform_contract=platform(), planner_model="planner-test-model",
        model_call=generator_or_reformatter, reviewer_model="reviewer-test-model",
        reviewer_model_call=critic_or_reviewer,
    )
    assert result == repaired
    assert sum("PROTOCOL REPAIR AUTHORITY" in value for value in prompts) == 1
    assert len(prompts) == 3


@pytest.mark.asyncio
async def test_reviewer_protocol_repair_runs_on_reviewer_route():
    from backend.services.creator.function_item_interface_plan import review_interface_plan_semantically
    items = [item("scripts/unit_a.py", ["slot_x"], ["result_z"])]
    plan = {"interfaces": [p2m("I1", "scripts/unit_a.py"), m2p("I2", "scripts/unit_a.py")]}
    responses = iter(["bad", json.dumps({"passed": True, "issues": []})])
    models = []
    prompts = []
    async def reviewer(messages, model):
        models.append(model)
        prompts.append(messages[0]["content"])
        return next(responses)
    assert await review_interface_plan_semantically(original_user_goal="g", frozen_function_items=items, interface_plan=plan, requirement_allocations=[], requirement_channels={}, system_requirements=[], platform_contract=platform(), reviewer_model="reviewer-test-model", model_call=reviewer) == []
    assert models == ["reviewer-test-model", "reviewer-test-model"]
    assert "final_output_fields defines the legal platform-output domain" in prompts[0]
    assert "Do not treat every legal final_output_field as required" in prompts[0]


@pytest.mark.asyncio
async def test_correction_receives_uncovered_slot_and_existing_binding_semantic_fact():
    items = [
        item("scripts/a.py", [], [
            {"port_id": "alpha", "description": "alpha value"},
            {"port_id": "beta", "description": "beta value"},
        ]),
        item("scripts/b.py", [{"port_id": "beta", "description": "beta value"}], ["result"]),
        item("scripts/c.py", [{"port_id": "gamma", "description": "gamma value"}], ["result"]),
    ]
    initial = {"interfaces": [
        m2m("I1", "scripts/a.py", "alpha", "scripts/b.py", "beta"),
        m2p("I2", "scripts/c.py", "result"),
    ]}
    corrected = {"interfaces": [
        m2m("I1", "scripts/a.py", "beta", "scripts/b.py", "beta"),
        p2m("I3", "scripts/c.py", "gamma"),
        m2p("I2", "scripts/c.py", "result"),
    ]}
    correction_facts = []

    async def planner(messages, _model):
        payload = json.loads(messages[-1]["content"])
        if "refinement_feedback" not in payload:
            return json.dumps(initial)
        correction_facts.extend(payload["refinement_feedback"]["acceptance_facts"])
        return json.dumps(corrected)

    async def reviewer(messages, _model):
        payload = json.loads(messages[-1]["content"])
        binding = payload["current_interface_plan"]["interfaces"][0]
        if binding.get("source_output") == "alpha":
            return json.dumps({"passed": False, "issues": [{
                "message": "declared alpha does not satisfy beta",
                "affected_interfaces": ["I1"],
                "affected_inputs": [{"target_member": "scripts/b.py", "target_input": "beta"}],
                "evidence": {"observed": "alpha", "expected": "beta"},
            }]})
        return json.dumps({"passed": True, "issues": []})

    result = await plan_function_item_interfaces(
        original_user_goal="transform values", frozen_function_items=items,
        platform_contract=platform(), planner_model="planner-test-model",
        model_call=planner, reviewer_model="reviewer-test-model",
        reviewer_model_call=reviewer,
    )
    assert result == corrected
    assert any(fact.get("code") == "uncovered_required_logical_input" for fact in correction_facts)
    semantic = [fact for fact in correction_facts if fact.get("source_stage") == "existing_binding_semantic_review"]
    assert semantic == [{
        "source_stage": "existing_binding_semantic_review",
        "interface_id": "I1", "current_binding": initial["interfaces"][0],
        "message": "declared alpha does not satisfy beta",
        "expected_constraint": "Declared semantic source must satisfy declared receiving slot.",
    }]


@pytest.mark.asyncio
async def test_early_semantic_review_protocol_failure_fails_open():
    items = [item("scripts/a.py", ["alpha", "gamma"], ["result"])]
    initial = {"interfaces": [p2m("I1", "scripts/a.py", "alpha"), m2p("I2", "scripts/a.py", "result")]}
    corrected = {"interfaces": [p2m("I1", "scripts/a.py", "alpha"), p2m("I3", "scripts/a.py", "gamma"), m2p("I2", "scripts/a.py", "result")]}
    reviewer_responses = iter(["bad-json", "still-bad"])
    correction_facts = []

    async def planner(messages, _model):
        payload = json.loads(messages[-1]["content"])
        if "refinement_feedback" not in payload:
            return json.dumps(initial)
        correction_facts.extend(payload["refinement_feedback"]["acceptance_facts"])
        return json.dumps(corrected)

    async def reviewer(_messages, _model):
        try:
            return next(reviewer_responses)
        except StopIteration:
            return json.dumps({"passed": True, "issues": []})

    assert await plan_function_item_interfaces(
        original_user_goal="process alpha and gamma", frozen_function_items=items,
        platform_contract=platform(), planner_model="planner-test-model",
        model_call=planner, reviewer_model="reviewer-test-model",
        reviewer_model_call=reviewer,
    ) == corrected
    assert [fact["code"] for fact in correction_facts] == ["uncovered_required_logical_input"]


def test_prompts_define_structured_binding_and_canonical_input_semantics():
    import inspect
    from backend.services.creator import api

    interface_prompt = _interface_plan_prompt()
    assert "verify every structured binding in both directions" not in interface_prompt
    assert "identify the semantic value that\nslot requires" in interface_prompt
    source = inspect.getsource(api._generate_internal_blueprint_or_questions)
    assert "FUNCTIONITEM INPUT SEMANTICS" in source
    assert "upstream\nFunctionItem output" in source
