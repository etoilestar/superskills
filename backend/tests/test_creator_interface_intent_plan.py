import json
import pytest

from backend.services.creator.function_item_interface_plan import (
    CRITIC_SCHEMA, MULTIMODAL_INPUT_PROVENANCE_CONTRACT, PLATFORM_BOUNDARY_CONTRACT, InterfaceIntentPlanError, _compact_function_items,
    _interface_plan_prompt, build_graph_obligations_from_interfaces,
    apply_interface_patch,
    build_interface_repair_scope, collect_interface_plan_validation_issues,
    canonical_logical_binding_signatures,
    _include_previous_interface_plan,
    normalize_interface_review_issue, repair_interface_plan_semantically,
    plan_function_item_interfaces, review_interface_plan_semantically,
    validate_interface_intent_plan, validate_interface_repair_critic,
    build_canonical_interface_contract,
    collect_interface_contract_consistency_issues,
)


def test_canonical_input_contract_preserves_platform_field_identity():
    contract = build_canonical_interface_contract(
        plan={"interfaces": [p2m("I1", "scripts/unit_a.py", "primary_key_mapping_strategy", "primary_key_mapping_strategy")]},
        function_items=[item("scripts/unit_a.py", ["primary_key_mapping_strategy"], ["result_z"])],
        platform_contract={"platform_skill_boundary": {"input_envelope_fields": ["primary_key_mapping_strategy"]}},
    )
    binding = contract["interfaces"][0]
    assert binding["source"]["field"] == "primary_key_mapping_strategy"
    assert binding["target"]["field"] == "primary_key_mapping_strategy"
    assert collect_interface_contract_consistency_issues(
        interface_contract=contract,
        script_sources={"I1": 'value = args["primary_key_mapping_strategy"]'},
        command_variables={"I1": ["primary_key_mapping_strategy"]},
        runtime_bindings={"I1": ["primary_key_mapping_strategy"]},
    ) == []


@pytest.mark.parametrize(("output_name", "source_type", "target", "transform"), [
    ("report_json", "object", "text", "json_serialize"),
    ("report_path", "file_path", "file_outputs", "file_collect"),
])
def test_canonical_output_contract_retains_registered_transform(output_name, source_type, target, transform):
    interface = m2p("I1", "scripts/unit_a.py", output_name, target)
    interface["transform"] = transform
    contract = build_canonical_interface_contract(
        plan={"interfaces": [interface]},
        function_items=[item("scripts/unit_a.py", [], [{"port_id": output_name, "contract": {"type": source_type}}])],
        platform_contract={"platform_skill_boundary": {"final_output_fields": [target]}},
    )
    assert contract["interfaces"][0]["transform"] == transform


def test_consistency_rejects_undeclared_options_wrapper():
    contract = build_canonical_interface_contract(
        plan={"interfaces": [p2m("I1", "scripts/unit_a.py", "primary_key_mapping_strategy", "primary_key_mapping_strategy")]},
        function_items=[item("scripts/unit_a.py", ["primary_key_mapping_strategy"], ["result_z"])],
    )
    issues = collect_interface_contract_consistency_issues(
        interface_contract=contract,
        script_sources={"I1": 'value = args["options"]["primary_key_mapping_strategy"]'},
    )
    assert [issue["code"] for issue in issues] == ["interface_target_field_mismatch"]


def test_apply_interface_patch_preserves_existing_interfaces():
    current_interfaces = [
        {
            "interface_id": "I1",
            "source_platform_input": "input_files",
            "target_input": "input_files",
        },
        {
            "interface_id": "I2",
            "source_platform_input": "fields",
            "target_input": "fields",
        },
        {
            "interface_id": "I3",
            "source_output": "file_outputs",
            "target_platform_output": "file_outputs",
        },
    ]
    patches = [
        {
            "interface_id": "I1",
            "action": "modify",
            "changes": {"source_path": ["0"]},
        },
        {
            "action": "add",
            "interface": {
                "interface_id": "I4",
                "source_platform_input": "input_files",
                "source_path": ["1"],
                "target_input": "input_files",
            },
        },
    ]

    result = apply_interface_patch(current_interfaces, patches)
    ids = {item["interface_id"] for item in result}

    assert {"I1", "I2", "I3", "I4"} <= ids


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
            "severity": "blocking", "code": "disconnected_data_flow",
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
        assert issues[0]["details"]["code"] == "disconnected_data_flow"


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


@pytest.mark.asyncio
async def test_graph_projection_and_platform_inputs_can_feed_distinct_slots_of_one_member():
    items = [
        item("scripts/script1.py", [], ["report_data"]),
        item("scripts/script2.py", ["report_data", "font_size", "page_count"], ["result"]),
    ]
    graph = [{
        "from_node": "scripts/script1.py", "from_output": "report_data",
        "to_node": "scripts/script2.py", "to_input": "report_data",
    }]

    async def planner(messages, _model):
        payload = json.loads(messages[1]["content"])
        candidates = payload["semantic_mapping_candidates"]["input_mappings"]
        assert not any(value["target_input"] == "report_data" for value in candidates)
        assert {value["target_input"] for value in candidates} >= {"font_size", "page_count"}
        return json.dumps({"interfaces": [
            p2m("I1", "scripts/script2.py", "font_size", "font_size"),
            p2m("I2", "scripts/script2.py", "page_count", "page_count"),
            m2p("I3", "scripts/script2.py", "result", "result"),
        ]})

    contract = {"platform_skill_boundary": {
        "input_envelope_fields": ["font_size", "page_count"],
        "final_output_fields": ["result"], "required_final_output_fields": ["result"],
    }}
    result = await plan_function_item_interfaces(
        original_user_goal="render report", frozen_function_items=items,
        platform_contract=contract, responsibility_edges=graph,
        planner_model="planner", model_call=planner,
    )

    bindings = {(value["kind"], value.get("target_input")) for value in result["interfaces"]}
    assert bindings == {
        ("member_to_member", "report_data"),
        ("platform_to_member", "font_size"),
        ("platform_to_member", "page_count"),
        ("member_to_platform", None),
    }
    assert collect_interface_plan_validation_issues(
        plan=result, function_items=items, platform_contract=contract,
        responsibility_edges=graph,
    ) == []


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


@pytest.mark.asyncio
async def test_planner_uses_declared_serializer_for_structured_output_to_text_sink():
    items = [item(
        "scripts/unit_a.py", [],
        [{"port_id": "report_json", "description": "structured report", "contract": {"type": "object"}}],
    )]
    edge = m2p("I1", "scripts/unit_a.py", "report_json")
    edge["transform"] = "json_serialize"
    planned = {"interfaces": [edge]}

    async def planner(_messages, _model):
        return json.dumps(planned)

    async def reviewer(messages, _model):
        payload = json.loads(messages[-1]["content"])
        edge = payload["current_interface_plan"]["interfaces"][0]
        assert edge["target_platform_output"] == "text"
        assert edge["transform"] == "json_serialize"
        assert "Do not report that mapping as a type mismatch" in messages[0]["content"]
        return json.dumps({"passed": True, "issues": []})

    result = await plan_function_item_interfaces(
        original_user_goal="produce a report", frozen_function_items=items,
        platform_contract=platform(), planner_model="planner", model_call=planner,
        reviewer_model="reviewer", reviewer_model_call=reviewer,
    )

    assert result["interfaces"][0] == planned["interfaces"][0]
    assert collect_interface_plan_validation_issues(
        plan=result, function_items=items, platform_contract=platform(),
    ) == []


def test_structured_output_to_text_requires_declared_serializer():
    items = [item("scripts/unit_a.py", [], [{
        "port_id": "report_json", "contract": {"type": "object"},
    }])]
    plan = {"interfaces": [m2p("I1", "scripts/unit_a.py", "report_json")]}

    issues = collect_interface_plan_validation_issues(
        plan=plan, function_items=items, platform_contract=platform(),
    )

    assert [issue["code"] for issue in issues] == ["incompatible_platform_output_type"]


@pytest.mark.parametrize(
    ("source_name", "source_type", "target_name", "target_schema", "transform", "valid"),
    [
        ("report", "object", "text", {"type": "string"}, "json_serialize", True),
        ("artifact", "file_path", "file_outputs", {"type": "array", "items": {"type": "string"}}, "file_collect", True),
        ("report", "object", "text", {"type": "string"}, None, False),
        ("report", "string", "text", {"type": "string"}, "json_serialize", False),
        ("report", "object", "file_outputs", {"type": "array", "items": {"type": "string"}}, "json_serialize", False),
        ("internal_report", "object", "text_result", {"type": "string"}, "json_serialize", True),
    ],
)
def test_output_adapter_contract_closes_types_independently_of_port_names(
    source_name, source_type, target_name, target_schema, transform, valid,
):
    items = [item("scripts/unit_a.py", [], [{
        "port_id": source_name, "contract": {"type": source_type},
    }])]
    contract = {"platform_skill_boundary": {
        "input_envelope_fields": [],
        "final_output_fields": [{"name": target_name, "value_schema": target_schema}],
        "required_final_output_fields": [target_name],
    }}
    interface = m2p("I1", "scripts/unit_a.py", source_name, target_name)
    if transform is not None:
        interface["transform"] = transform

    issues = collect_interface_plan_validation_issues(
        plan={"interfaces": [interface]}, function_items=items,
        platform_contract=contract,
    )

    assert (issues == []) is valid
    if not valid:
        expected_code = (
            "incompatible_platform_output_transform"
            if transform is not None
            else "incompatible_platform_output_type"
        )
        assert [issue["code"] for issue in issues] == [expected_code]


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
    assert "independent top-level semantic source slots" in prompt
    assert "source_path is relative" in prompt
    assert "platform source must already be declared" in prompt


def test_declared_business_input_is_bound_as_a_flat_top_level_source():
    plan = {"interfaces": [
        p2m("I1", "scripts/unit_a.py", "primary_key", "primary_key"),
        m2p("I2", "scripts/unit_a.py"),
    ]}
    contract = {"platform_skill_boundary": {
        "input_envelope_fields": ["input_files", "primary_key"],
        "final_output_fields": ["text"], "required_final_output_fields": ["text"],
    }}
    assert collect_interface_plan_validation_issues(
        plan=plan,
        function_items=[item("scripts/unit_a.py", ["primary_key"], ["result_z"])],
        platform_contract=contract,
    ) == []

    invalid = {"interfaces": [
        {**p2m("I1", "scripts/unit_a.py", "primary_key", "fields"), "source_path": ["primary_key"]},
        m2p("I2", "scripts/unit_a.py"),
    ]}
    issues = collect_interface_plan_validation_issues(
        plan=invalid,
        function_items=[item("scripts/unit_a.py", ["primary_key"], ["result_z"])],
        platform_contract=contract,
    )
    assert any(issue["code"] == "unknown_platform_logical_input" for issue in issues)


def test_unclassified_input_envelope_source_remains_legal():
    plan = {"interfaces": [
        p2m("I1", "scripts/unit_a.py", "arbitrary_name", "payload"),
        m2p("I2", "scripts/unit_a.py"),
    ]}
    contract = {"platform_skill_boundary": {
        "input_envelope_fields": ["user_request", "payload"],
        "input_source_semantics": {
            "freeform_request": {"canonical": "user_request", "globally_required": False},
        },
        "final_output_fields": ["text"], "required_final_output_fields": ["text"],
    }}
    assert collect_interface_plan_validation_issues(
        plan=plan,
        function_items=[item("scripts/unit_a.py", ["arbitrary_name"], ["result_z"])],
        platform_contract=contract,
    ) == []


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


def test_reviewer_issue_has_explicit_severity_and_code():
    raw = {"severity": "blocking", "code": "type_mismatch", "message": "semantic mismatch", "affected_interfaces": ["I1"], "affected_inputs": [{"target_member": "scripts/unit_a.py", "target_input": "slot_x"}], "evidence": {"observed": "value_a", "expected": "value_b"}}
    issue = normalize_interface_review_issue(raw, [item("scripts/unit_a.py", ["slot_x"], ["value_a"])], {"interfaces": [p2m("I1", "scripts/unit_a.py")]})
    assert issue["severity"] == "blocking" and issue["code"] == "type_mismatch"


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
    async def planner(_messages, _model):
        return json.dumps({"patches": [{
            "interface_id": "I1", "action": "modify",
            "changes": {"source_path": ["value_x"]},
        }]})
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
        return json.dumps({"patches": []})

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
        return json.dumps({"patches": []})

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
        return json.dumps({"patches": []})

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
        return json.dumps({"patches": [{
            "interface_id": "I1", "action": "modify",
            "changes": {"source_path": ["y"]},
        }]})

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
    generator_responses = iter([
        {"patches": [{"action": "add", "interface": invalid["interfaces"][0]}]},
        {"patches": [{"interface_id": "I1", "action": "modify",
                       "changes": {"source_path": ["value_x"]}}]},
    ])
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
        return json.dumps({
            "patches": [{"action": "add", "interface": invalid["interfaces"][0]}],
        })

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
    responses = iter([
        "not-json",
        json.dumps({"patches": [{"action": "add", "interface": invalid["interfaces"][0]}]}),
        json.dumps({"patches": [{"interface_id": "I1", "action": "modify",
                                  "changes": {"source_path": ["value_x"]}}]}),
    ])
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
async def test_reviewer_prompt_applies_general_evidence_floor_after_deterministic_validation():
    items = [item("scripts/unit_a.py", [], [
        {"port_id": "report_md", "description": "human-readable Markdown report"},
        {"port_id": "file_outputs", "description": "generated file paths"},
    ])]
    plan = {"interfaces": [
        m2p("I1", "scripts/unit_a.py", "report_md", "markdown"),
        m2p("I2", "scripts/unit_a.py", "file_outputs", "file_outputs"),
    ]}

    async def reviewer(messages, _model):
        prompt = messages[0]["content"]
        assert "PRESUMPTION OF VALIDITY AND EVIDENCE FLOOR" in prompt
        assert "Do not infer a defect from a port name alone" in prompt
        assert "Apply this evidence rule uniformly to every binding" in prompt
        assert "Treat that membership as an established fact" in prompt
        assert "naming, schema shape, format labels, or" in prompt
        assert "never identifier membership" in prompt
        return json.dumps({"passed": True, "issues": []})

    contract = {"platform_skill_boundary": {"final_output_fields": [
        "markdown", "file_outputs",
    ]}}
    assert await review_interface_plan_semantically(
        original_user_goal="compare CSV files",
        frozen_function_items=items,
        interface_plan=plan,
        requirement_allocations=[],
        requirement_channels={},
        system_requirements=[],
        platform_contract=contract,
        reviewer_model="reviewer-test-model",
        model_call=reviewer,
    ) == []


@pytest.mark.asyncio
async def test_nonblocking_interface_review_issues_are_retained_without_failing(caplog):
    items = [item("scripts/unit_a.py", ["slot_x"], ["file_outputs"])]
    plan = {"interfaces": [p2m("I1", "scripts/unit_a.py"), m2p("I2", "scripts/unit_a.py", "file_outputs")]}

    async def reviewer(messages, _model):
        prompt = messages[0]["content"]
        assert "abstract file_outputs port may carry" in prompt
        assert "Evidence may come only from the" in prompt
        return json.dumps({"passed": True, "issues": [{
            "severity": "warning", "code": "output_description_clarity",
            "message": "The file format could be documented more explicitly.",
            "affected_interfaces": ["I2"], "affected_inputs": [],
            "evidence": {"observed": "abstract file output", "expected": "optional clearer documentation"},
        }]})

    with caplog.at_level("INFO"):
        issues = await review_interface_plan_semantically(
            original_user_goal="create a CSV reconciliation skill",
            frozen_function_items=items, interface_plan=plan,
            requirement_allocations=[], requirement_channels={}, system_requirements=[],
            platform_contract=platform(), reviewer_model="reviewer-test-model",
            model_call=reviewer,
        )
    assert issues[0]["severity"] == "warning"
    assert "blocking_issue_count=0 warning_issue_count=1 advisory_issue_count=0" in caplog.text


@pytest.mark.asyncio
async def test_deterministic_correction_finishes_before_semantic_review():
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
                "severity": "blocking", "code": "parameter_type_mismatch",
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
    assert not any(
        fact.get("source_stage") == "existing_binding_semantic_review"
        for fact in correction_facts
    )
    assert reviewer_modes == ["full"]


@pytest.mark.asyncio
async def test_invalid_existing_reference_is_corrected_before_semantic_review():
    items = [item("scripts/a.py", [], ["alpha"])]
    initial = {"interfaces": [
        m2m("I1", "scripts/missing.py", "alpha", "scripts/a.py", "alpha"),
        m2p("I2", "scripts/a.py", "alpha"),
    ]}
    corrected = {"interfaces": [m2p("I2", "scripts/a.py", "alpha")]}
    reviewer_modes = []

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
async def test_unknown_platform_output_is_deterministic_before_semantic_review():
    items = [item("scripts/a.py", [], ["report_pdf"])]
    initial = {"interfaces": [m2p("I1", "scripts/a.py", "report_pdf", "pdf_path")]}
    corrected = {"interfaces": [m2p("I1", "scripts/a.py", "report_pdf", "text")]}
    correction_facts = []
    events = []

    async def planner(messages, _model):
        events.append("planner")
        payload = json.loads(messages[-1]["content"])
        if "refinement_feedback" not in payload:
            return json.dumps(initial)
        correction_facts.extend(payload["refinement_feedback"]["acceptance_facts"])
        return json.dumps(corrected)

    async def reviewer(_messages, _model):
        events.append("reviewer")
        return json.dumps({"passed": True, "issues": []})

    assert await plan_function_item_interfaces(
        original_user_goal="produce a PDF report", frozen_function_items=items,
        platform_contract=platform(), planner_model="planner-test-model",
        model_call=planner, reviewer_model="reviewer-test-model",
        reviewer_model_call=reviewer,
    ) == corrected
    assert [fact["code"] for fact in correction_facts] == ["unknown_platform_logical_output"]
    assert correction_facts[0]["observed_value"] == "pdf_path"
    assert correction_facts[0]["expected_constraint"] == ["text"]
    assert events == ["planner", "planner", "reviewer"]


def test_prompts_define_structured_binding_and_canonical_input_semantics():
    import inspect
    from backend.services.creator import api

    interface_prompt = _interface_plan_prompt()
    assert "verify every structured binding in both directions" not in interface_prompt
    assert "identify the semantic value that\nslot requires" in interface_prompt
    source = inspect.getsource(api._generate_internal_blueprint_or_questions)
    assert "FUNCTIONITEM INPUT SEMANTICS" in source
    assert "upstream\nFunctionItem output" in source
    assert "SCRIPT-LEVEL FUNCTIONITEM CONTRACT" in source
    assert "collection-based inputs and outputs" in source
    assert "Do not expose internal script loops as external interfaces" in source


def test_interface_prompts_preserve_collection_level_script_boundaries():
    import inspect
    from backend.services.creator import function_item_interface_plan as module

    interface_prompt = _interface_plan_prompt()
    module_source = inspect.getsource(module)

    assert "Do not model repeated execution of the same script" in interface_prompt
    assert "using its collection input and output ports" in interface_prompt
    assert "Never expand an internal loop into\nper-item Interfaces" in interface_prompt
    assert "A valid Interface represents script-level data exchange" in module_source
    assert "Do not split one script\ninto multiple execution instances" in module_source
    assert "introduce artificial intermediate Interfaces" in module_source
