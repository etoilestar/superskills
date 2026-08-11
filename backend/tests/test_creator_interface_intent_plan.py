import json
import pytest

from backend.services.creator.function_item_interface_plan import (
    CRITIC_SCHEMA, MULTIMODAL_INPUT_PROVENANCE_CONTRACT, PLATFORM_BOUNDARY_CONTRACT, InterfaceIntentPlanError, _compact_function_items,
    _interface_plan_prompt, build_graph_obligations_from_interfaces,
    build_interface_repair_scope, collect_interface_plan_validation_issues,
    canonical_logical_binding_signatures,
    existing_binding_references_valid,
    _include_previous_interface_plan,
    normalize_interface_review_issue, repair_interface_plan_semantically,
    plan_function_item_interfaces, review_interface_plan_semantically,
    validate_interface_intent_plan, validate_interface_repair_critic,
)


@pytest.mark.parametrize("acceptance_facts", [
    [{"code": "abstract_failure", "observed_value": "x", "expected_constraint": "y"}],
    [{"code": "unrelated_condition", "observed_value": "left", "expected_constraint": "right"}],
])
def test_no_progress_restart_is_independent_of_acceptance_fact_type(acceptance_facts):
    feedback = {
        "acceptance_facts": acceptance_facts,
        "progress": {"semantic_changed": False},
    }
    assert _include_previous_interface_plan(feedback) is False
    feedback["progress"]["semantic_changed"] = True
    assert _include_previous_interface_plan(feedback) is True


def test_platform_boundary_contract_standard_internal_data_flow():
    items = [
        item("scripts/a.py", ["request"], ["alpha"]),
        item("scripts/b.py", ["alpha"], ["result"]),
    ]
    contract = {"platform_skill_boundary": {
        "input_envelope_fields": ["request"],
        "final_output_fields": ["result"],
        "required_final_output_fields": ["result"],
    }}
    plan = {"interfaces": [
        p2m("I1", "scripts/a.py", "request", "request"),
        m2m("I2", "scripts/a.py", "alpha", "scripts/b.py", "alpha"),
        m2p("I3", "scripts/b.py", "result", "result"),
    ]}
    assert collect_interface_plan_validation_issues(
        plan=plan, function_items=items, platform_contract=contract,
    ) == []


@pytest.mark.asyncio
async def test_platform_boundary_contract_reviewer_rejects_platform_as_internal_relay():
    items = [
        item("scripts/a.py", ["request"], [{"port_id": "alpha", "description": "derived semantic value"}]),
        item("scripts/b.py", [{"port_id": "alpha", "description": "derived semantic value from A"}], ["result"]),
    ]
    candidate = {"interfaces": [
        p2m("I1", "scripts/a.py", "request", "request"),
        m2p("I2", "scripts/a.py", "alpha", "result"),
        p2m("I3", "scripts/b.py", "alpha", "request"),
    ]}

    async def reviewer(messages, _model):
        assert PLATFORM_BOUNDARY_CONTRACT in messages[0]["content"]
        return json.dumps({"passed": False, "issues": [{
            "message": "The external request does not provide A's derived alpha value.",
            "affected_interfaces": ["I3"],
            "affected_inputs": [{"target_member": "scripts/b.py", "target_input": "alpha"}],
            "evidence": {"observed": "platform request", "expected": "alpha produced by A"},
        }]})

    for review_mode in ("existing_bindings_only", "full"):
        issues = await review_interface_plan_semantically(
            original_user_goal="transform a request in two stages",
            frozen_function_items=items, interface_plan=candidate,
            requirement_allocations=None, requirement_channels=None,
            system_requirements=None,
            platform_contract={"platform_skill_boundary": {
                "input_envelope_fields": ["request"], "final_output_fields": ["result"]}},
            reviewer_model="reviewer", model_call=reviewer,
            review_mode=review_mode,
        )
        assert len(issues) == 1
        assert "code" not in issues[0]["details"]


def test_platform_boundary_contract_allows_nested_platform_source():
    items = [item("scripts/b.py", ["data"], ["result"])]
    plan = {"interfaces": [
        {**p2m("I1", "scripts/b.py", "data", "payload"), "source_path": ["data"]},
        m2p("I2", "scripts/b.py", "result", "result"),
    ]}
    contract = {"platform_skill_boundary": {
        "input_envelope_fields": ["payload"], "final_output_fields": ["result"],
        "required_final_output_fields": ["result"],
    }}
    assert collect_interface_plan_validation_issues(
        plan=plan, function_items=items, platform_contract=contract,
    ) == []


@pytest.mark.asyncio
async def test_platform_boundary_contract_planner_uses_semantics_with_randomized_identifiers():
    items = [
        item("scripts/a.py", ["request"], [{"port_id": "zeta_83", "description": "normalized value for the next stage"}]),
        item("scripts/b.py", [{"port_id": "q17", "description": "normalized value emitted by the first stage"}], ["result"]),
    ]
    expected = {"interfaces": [
        p2m("I1", "scripts/a.py", "request", "request"),
        m2m("I2", "scripts/a.py", "zeta_83", "scripts/b.py", "q17"),
        m2p("I3", "scripts/b.py", "result", "result"),
    ]}

    incomplete = {"interfaces": [
        p2m("I1", "scripts/a.py", "request", "request"),
        m2p("I3", "scripts/b.py", "result", "result"),
    ]}
    planner_calls = 0

    async def planner(messages, _model):
        nonlocal planner_calls
        planner_calls += 1
        prompt = messages[0]["content"]
        assert PLATFORM_BOUNDARY_CONTRACT in prompt
        if planner_calls == 1:
            assert "For each receiving slot, first identify where" in prompt
            return json.dumps(incomplete)
        assert "INTERFACE PLAN CORRECTION" in prompt
        return json.dumps(expected)

    result = await plan_function_item_interfaces(
        original_user_goal="run two semantic transformation stages",
        frozen_function_items=items,
        platform_contract={"platform_skill_boundary": {
            "input_envelope_fields": ["request"], "final_output_fields": ["result"],
            "required_final_output_fields": ["result"]}},
        planner_model="planner", model_call=planner,
    )
    assert planner_calls == 2
    assert result["interfaces"][1]["kind"] == "member_to_member"


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


def test_optional_input_may_keep_a_valid_binding_and_prompt_assigns_semantic_authority():
    inputs = [{"port_id": "arbitrary_name", "required": False}]
    plan = {"interfaces": [
        p2m("I1", "scripts/unit_a.py", "arbitrary_name", "input_files"),
        m2p("I2", "scripts/unit_a.py"),
    ]}
    contract = {"platform_skill_boundary": {
        "input_envelope_fields": ["user_request", "input_files"],
        "final_output_fields": ["text"], "required_final_output_fields": ["text"],
    }}
    assert collect_interface_plan_validation_issues(
        plan=plan, function_items=[item("scripts/unit_a.py", inputs, ["result_z"])],
        platform_contract=contract,
    ) == []
    prompt = _interface_plan_prompt()
    assert MULTIMODAL_INPUT_PROVENANCE_CONTRACT in prompt
    assert "receiver-local interface identity" in prompt
    assert "fields.<current-skill-declared-param>" in prompt


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


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_code", ["graph_failure_alpha", "graph_failure_beta"])
async def test_graph_triggered_repair_requires_authoritative_graph_acceptance(
    monkeypatch, failure_code,
):
    from backend.services.creator import responsibility_graph_expansion as graph_expansion

    items = [item("scripts/unit_q.py", ["slot_k"], ["value_r"])]
    candidate = {"interfaces": [
        p2m("I1", "scripts/unit_q.py", "slot_k"),
        m2p("I2", "scripts/unit_q.py", "value_r"),
    ]}
    issue = {"message": "observed graph condition", "affected_interfaces": [],
             "affected_inputs": [], "evidence": {"observed": "blocked"},
             "details": {}, "stage": "graph_validation", "path": "$.interfaces"}
    payloads = []

    def reject(**_kwargs):
        raise graph_expansion.ResponsibilityGraphExpansionError(
            "observed invalid graph state", code=failure_code,
            details={"observation": "constraint remains false"},
        )

    monkeypatch.setattr(graph_expansion, "validate_responsibility_graph_candidate", reject)

    async def critic_or_reviewer(messages, _model):
        payload = json.loads(messages[-1]["content"])
        return json.dumps(
            {"diagnosis": "graph condition remains invalid", "required_postcondition": "graph condition is valid"}
            if "validation_issues" in payload else {"passed": True, "issues": []}
        )

    async def generator(messages, _model):
        payloads.append(json.loads(messages[-1]["content"]))
        return json.dumps(candidate)

    with pytest.raises(InterfaceIntentPlanError) as raised:
        await repair_interface_plan_semantically(
            original_user_goal="process an abstract payload", frozen_function_items=items,
            current_interface_plan=candidate, validation_issues=[issue],
            repair_scope=build_interface_repair_scope([issue]),
            platform_contract=platform(), repair_stage="graph_expansion_feedback",
            planner_model="planner-test-model", model_call=generator,
            reviewer_model="reviewer-test-model", reviewer_model_call=critic_or_reviewer,
        )

    assert raised.value.code == "semantic_issues_remain"
    assert raised.value.details["remaining_issues"][0] == {
        "code": failure_code,
        "message": "observed invalid graph state",
        "details": {"observation": "constraint remains false"},
        "stage": "graph_validation",
    }
    assert payloads[1]["refinement_feedback"]["acceptance_facts"][0]["code"] == failure_code


@pytest.mark.asyncio
async def test_graph_triggered_repair_accepts_after_authoritative_graph_validation(monkeypatch):
    from backend.services.creator import responsibility_graph_expansion as graph_expansion

    items = [item("scripts/unit_q.py", ["slot_k"], ["value_r"])]
    candidate = {"interfaces": [
        p2m("I1", "scripts/unit_q.py", "slot_k"),
        m2p("I2", "scripts/unit_q.py", "value_r"),
    ]}
    calls = 0

    def accept(**_kwargs):
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(graph_expansion, "validate_responsibility_graph_candidate", accept)

    async def critic_or_reviewer(messages, _model):
        payload = json.loads(messages[-1]["content"])
        return json.dumps(
            {"diagnosis": "graph condition failed", "required_postcondition": "graph condition is valid"}
            if "validation_issues" in payload else {"passed": True, "issues": []}
        )

    async def generator(_messages, _model):
        return json.dumps(candidate)

    result = await repair_interface_plan_semantically(
        original_user_goal="process an abstract payload", frozen_function_items=items,
        current_interface_plan=candidate,
        validation_issues=[{"message": "graph condition", "details": {}}],
        repair_scope=build_interface_repair_scope([]), platform_contract=platform(),
        repair_stage="graph_expansion_feedback", planner_model="planner-test-model",
        model_call=generator, reviewer_model="reviewer-test-model",
        reviewer_model_call=critic_or_reviewer,
    )
    assert result == candidate
    assert calls == 1


@pytest.mark.asyncio
async def test_interface_only_repair_does_not_run_graph_acceptance(monkeypatch):
    from backend.services.creator import responsibility_graph_expansion as graph_expansion

    monkeypatch.setattr(
        graph_expansion, "validate_responsibility_graph_candidate",
        lambda **_kwargs: pytest.fail("ordinary Interface repair must not construct a graph"),
    )
    items = [item("scripts/unit_q.py", ["slot_k"], ["value_r"])]
    candidate = {"interfaces": [
        p2m("I1", "scripts/unit_q.py", "slot_k"),
        m2p("I2", "scripts/unit_q.py", "value_r"),
    ]}

    async def critic_or_reviewer(messages, _model):
        payload = json.loads(messages[-1]["content"])
        return json.dumps(
            {"diagnosis": "Interface condition failed", "required_postcondition": "Interface condition is valid"}
            if "validation_issues" in payload else {"passed": True, "issues": []}
        )

    async def generator(_messages, _model):
        return json.dumps(candidate)

    assert await repair_interface_plan_semantically(
        original_user_goal="process an abstract payload", frozen_function_items=items,
        current_interface_plan=candidate,
        validation_issues=[{"message": "Interface condition", "details": {}}],
        repair_scope=build_interface_repair_scope([]), platform_contract=platform(),
        planner_model="planner-test-model", model_call=generator,
        reviewer_model="reviewer-test-model", reviewer_model_call=critic_or_reviewer,
    ) == candidate


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
    reviewer_modes = []

    async def planner(messages, _model):
        value = next(planner_responses)
        if "INTERFACE PLAN CORRECTION" in messages[0]["content"]:
            correction_payloads.append(json.loads(messages[-1]["content"]))
        return json.dumps(value)

    async def reviewer(messages, _model):
        reviewer_modes.append(
            "existing" if "EXISTING-BINDING-ONLY MODE" in messages[0]["content"] else "full"
        )
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
    assert reviewer_modes == ["existing", "full"]


@pytest.mark.asyncio
async def test_planner_correction_no_progress_reconstructs_without_previous_candidate():
    from backend.services.creator.function_item_interface_plan import plan_function_item_interfaces
    items = [item("scripts/unit_a.py", ["slot_x", "slot_y"], ["result_z"])]
    initial = {"interfaces": [p2m("I1", "scripts/unit_a.py"), m2p("I2", "scripts/unit_a.py")]}
    presentation_only = json.loads(json.dumps(initial))
    presentation_only["interfaces"].reverse()
    presentation_only["interfaces"][0]["interface_id"] = "I9"
    presentation_only["interfaces"][0]["goal"] = "value_x"
    complete = {"interfaces": [
        p2m("I1", "scripts/unit_a.py", "slot_x"),
        p2m("I3", "scripts/unit_a.py", "slot_y"),
        m2p("I2", "scripts/unit_a.py"),
    ]}
    responses = iter([initial, presentation_only, complete])
    calls = 0
    correction_payloads = []

    async def planner(messages, _model):
        nonlocal calls
        calls += 1
        if "INTERFACE PLAN CORRECTION" in messages[0]["content"]:
            correction_payloads.append(json.loads(messages[-1]["content"]))
        return json.dumps(next(responses))

    result = await plan_function_item_interfaces(
        original_user_goal="g", frozen_function_items=items,
        platform_contract=platform(), planner_model="planner-test-model",
        model_call=planner,
    )
    assert result == complete
    assert calls == 3
    assert correction_payloads[0]["previous_interface_plan"] == initial
    assert correction_payloads[1]["refinement_feedback"]["progress"]["semantic_changed"] is False
    assert "previous_interface_plan" not in correction_payloads[1]


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
    reviewer_modes = []

    async def planner(messages, _model):
        payload = json.loads(messages[-1]["content"])
        if "refinement_feedback" not in payload:
            return json.dumps(initial)
        correction_facts.extend(payload["refinement_feedback"]["acceptance_facts"])
        return json.dumps(corrected)

    async def reviewer(messages, _model):
        reviewer_modes.append(
            "existing" if "EXISTING-BINDING-ONLY MODE" in messages[0]["content"] else "full"
        )
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
    assert reviewer_modes == ["existing", "full"]


@pytest.mark.asyncio
async def test_invalid_existing_reference_skips_early_semantic_audit():
    items = [item("scripts/a.py", [], ["alpha"])]
    initial = {"interfaces": [
        m2m("I1", "scripts/missing.py", "alpha", "scripts/a.py", "alpha"),
        m2p("I2", "scripts/a.py", "alpha"),
    ]}
    corrected = {"interfaces": [m2p("I2", "scripts/a.py", "alpha")]}
    reviewer_modes = []

    assert not existing_binding_references_valid(
        plan=initial, function_items=items, platform_contract=platform(),
    )

    async def planner(messages, _model):
        payload = json.loads(messages[-1]["content"])
        return json.dumps(corrected if "refinement_feedback" in payload else initial)

    async def reviewer(messages, _model):
        reviewer_modes.append(
            "existing" if "EXISTING-BINDING-ONLY MODE" in messages[0]["content"] else "full"
        )
        return json.dumps({"passed": True, "issues": []})

    assert await plan_function_item_interfaces(
        original_user_goal="produce alpha", frozen_function_items=items,
        platform_contract=platform(), planner_model="planner-test-model",
        model_call=planner, reviewer_model="reviewer-test-model",
        reviewer_model_call=reviewer,
    ) == corrected
    assert reviewer_modes == ["full"]


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
