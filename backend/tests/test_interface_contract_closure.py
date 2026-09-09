"""Minimal deterministic closure and edge-patch contract tests."""

import json

import pytest

from backend.services.creator.function_item_interface_plan import (
    INTERFACE_PATCH_SCHEMA,
    collect_interface_plan_validation_issues,
    interface_contract_closure_check,
    inject_optional_parameter_interfaces,
    plan_function_item_interfaces,
    validate_interface_plan_protocol,
    validate_interface_patch,
)
from backend.services.creator.responsibility_graph_expansion import (
    validate_responsibility_graph_candidate,
)


def items():
    return [{
        "target_file": "scripts/run.py", "role": "worker", "purpose": "run",
        "inputs": [
            {"port_id": "left", "role": "required_runtime_input", "contract": {"type": "string"}},
            {"port_id": "right", "role": "required_runtime_input", "contract": {"type": "string"}},
        ],
        "outputs": [{"port_id": "report", "role": "runtime_output", "contract": {"type": "string"}}],
        "constraints": [], "required_capabilities": [],
    }]


def platform():
    return {"platform_skill_boundary": {
        "input_envelope_fields": ["left", "right"],
        "input_schemas": {"left": {"type": "string"}, "right": {"type": "string"}},
        "final_output_fields": ["result"], "required_final_output_fields": ["result"],
        "output_sinks": {"result": {"semantic_type": "text", "accepted_source_types": ["text"]}},
    }}


def binding(iid, source, target):
    return {"interface_id": iid, "kind": "platform_to_member", "source_platform_input": source,
            "source_path": [], "target_member": "scripts/run.py", "target_input": target, "goal": "bind"}


def complete_plan(second_source="right"):
    return {"interfaces": [
        binding("I1", "left", "left"), binding("I2", second_source, "right"),
        {"interface_id": "I3", "kind": "member_to_platform", "source_member": "scripts/run.py",
         "source_output": "report", "target_platform_output": "result", "goal": "return"},
    ]}


def test_two_platform_inputs_close_two_member_slots():
    assert collect_interface_plan_validation_issues(
        plan=complete_plan(), function_items=items(), platform_contract=platform()) == []


def test_invalid_source_is_addressed_to_interface_id():
    issues = collect_interface_plan_validation_issues(
        plan=complete_plan("missing"), function_items=items(), platform_contract=platform())
    diagnostic = next(issue for issue in issues if issue["code"] == "interface_binding_invalid")
    assert diagnostic["interface_id"] == "I2"
    assert diagnostic["source"]["platform_input"] == "missing"


def test_replace_source_patch_restores_closure():
    broken = complete_plan("missing")
    violations = collect_interface_plan_validation_issues(
        plan=broken, function_items=items(), platform_contract=platform())
    repaired = validate_interface_patch(
        plan=broken, function_items=items(), platform_contract=platform(), violations=violations,
        patch={"operations": [{"op": "replace_source", "interface_id": "I2", "reason": "use frozen port",
                               "source_platform_input": "right", "source_path": []}]},
    )
    assert collect_interface_plan_validation_issues(
        plan=repaired, function_items=items(), platform_contract=platform()) == []


def test_patch_preserves_valid_interface_exactly():
    broken = complete_plan("missing")
    frozen = dict(broken["interfaces"][0])
    violations = collect_interface_plan_validation_issues(
        plan=broken, function_items=items(), platform_contract=platform())
    repaired = validate_interface_patch(
        plan=broken, function_items=items(), platform_contract=platform(), violations=violations,
        patch={"operations": [{"op": "replace_source", "interface_id": "I2", "reason": "use frozen port",
                               "source_platform_input": "right", "source_path": []}]},
    )
    assert repaired["interfaces"][0] == frozen


def test_optional_runtime_input_still_requires_declared_provenance():
    function_items = items()
    function_items[0]["inputs"][1]["role"] = "optional_runtime_input"
    plan = complete_plan()
    plan["interfaces"] = [edge for edge in plan["interfaces"] if edge["interface_id"] != "I2"]

    issues = collect_interface_plan_validation_issues(
        plan=plan, function_items=function_items, platform_contract=platform(),
    )
    assert any(issue["code"] == "missing_interface_contract" for issue in issues)


def test_backend_injects_hidden_fields_binding_for_optional_parameter():
    function_items = items()
    function_items[0]["inputs"][1]["role"] = "optional_runtime_input"
    plan = complete_plan()
    plan["interfaces"] = [edge for edge in plan["interfaces"] if edge["interface_id"] != "I2"]

    compiled = inject_optional_parameter_interfaces(plan, function_items=function_items)

    optional_edge = next(
        edge for edge in compiled["interfaces"] if edge.get("target_input") == "right"
    )
    assert optional_edge["source_platform_input"] == "fields"
    assert optional_edge["source_path"] == ["right"]
    assert collect_interface_plan_validation_issues(
        plan=compiled, function_items=function_items, platform_contract=platform(),
    ) == []

    edges = validate_responsibility_graph_candidate(
        function_items=function_items,
        platform_contract=platform(),
        interface_plan=compiled,
    )
    optional_edge = next(edge for edge in edges if edge["to_input"] == "right")
    assert optional_edge["from_output"] == "fields"
    assert optional_edge["constraints"] == [{
        "type": "platform_parameter_binding",
        "source_key": "right",
        "source_path": ["right"],
        "required": False,
    }]


@pytest.mark.asyncio
async def test_interface_model_never_sees_hidden_fields_or_optional_slot():
    function_items = items()
    function_items[0]["inputs"][1]["role"] = "optional_runtime_input"

    async def planner(messages, _model):
        payload = json.loads(messages[-1]["content"])
        boundary = payload["platform_contract"]["platform_skill_boundary"]
        assert "fields" not in boundary["input_envelope_fields"]
        assert [port["name"] for port in payload["function_items"][0]["inputs"]] == ["left"]
        slots = payload["frozen_interface_slots"]["members"]["scripts/run.py"]["inputs"]
        assert [port["name"] for port in slots] == ["left"]
        assert all(
            candidate["target_input"] != "right"
            for candidate in payload["semantic_mapping_candidates"]["input_mappings"]
        )
        return json.dumps({"interfaces": [
            binding("I1", "left", "left"),
            {"interface_id": "I3", "kind": "member_to_platform",
             "source_member": "scripts/run.py", "source_output": "report",
             "target_platform_output": "result", "goal": "return"},
        ]})

    result = await plan_function_item_interfaces(
        original_user_goal="run",
        frozen_function_items=function_items,
        platform_contract=platform(),
        planner_model="planner",
        model_call=planner,
    )

    hidden = next(edge for edge in result["interfaces"] if edge.get("target_input") == "right")
    assert hidden["source_platform_input"] == "fields"
    assert hidden["source_path"] == ["right"]


def test_defaulted_runtime_input_still_requires_declared_provenance():
    function_items = items()
    function_items[0]["default_values"] = {"right": []}
    plan = complete_plan()
    plan["interfaces"] = [edge for edge in plan["interfaces"] if edge["interface_id"] != "I2"]

    issues = collect_interface_plan_validation_issues(
        plan=plan, function_items=function_items, platform_contract=platform(),
    )
    assert any(issue["code"] == "missing_interface_contract" for issue in issues)


def test_required_and_derived_inputs_still_require_bindings():
    for role in ("required_runtime_input", "derived_input"):
        function_items = items()
        function_items[0]["inputs"][1]["role"] = role
        plan = complete_plan()
        plan["interfaces"] = [edge for edge in plan["interfaces"] if edge["interface_id"] != "I2"]

        issues = collect_interface_plan_validation_issues(
            plan=plan, function_items=function_items, platform_contract=platform(),
        )
        assert any(issue["code"] == "missing_interface_contract" for issue in issues)


def test_patch_schema_does_not_advertise_target_fields_for_replace_source():
    variants = INTERFACE_PATCH_SCHEMA["properties"]["operations"]["items"]["oneOf"]
    source_variants = [
        variant for variant in variants
        if variant["properties"]["op"].get("const") == "replace_source"
    ]

    assert source_variants
    assert all("target_member" not in variant["properties"] for variant in source_variants)
    assert all("target_input" not in variant["properties"] for variant in source_variants)


def _boundary_inputs(*names):
    return {"platform_skill_boundary": {
        "input_envelope_fields": list(names),
        "input_schemas": {
            name: {"type": "object" if name == "options" else "array" if name == "input_files" else "string"}
            for name in names
        },
        "final_output_fields": ["result"],
        "required_final_output_fields": ["result"],
        "output_sinks": {"result": {"value_schema": {"type": "string"}}},
    }}


def _closed_item(input_names):
    return [{
        "target_file": "scripts/run.py", "role": "worker", "purpose": "run",
        "inputs": [{"port_id": name, "role": "optional_runtime_input",
                    "contract": {"type": "object" if name == "options" else "array" if name == "input_files" else "string"}}
                   for name in input_names],
        "outputs": [{"port_id": "report", "role": "runtime_output", "contract": {"type": "string"}}],
        "constraints": [], "required_capabilities": [],
    }]


def _closed_plan(*names):
    interfaces = [{
        "interface_id": f"I{index}", "kind": "platform_to_member",
        "source_type": "platform_input", "source_platform_input": name,
        "source_path": [], "target_member": "scripts/run.py", "target_input": name,
    } for index, name in enumerate(names, 1)]
    interfaces.append({
        "interface_id": f"I{len(names) + 1}", "kind": "member_to_platform",
        "source_member": "scripts/run.py", "source_output": "report",
        "target_platform_output": "result",
    })
    return {"interfaces": interfaces}


def test_csv_empty_options_value_keeps_both_input_contracts():
    plan = _closed_plan("input_files", "options")
    assert interface_contract_closure_check(
        interface_plan=plan, function_items=_closed_item(["input_files", "options"]),
        platform_contract=_boundary_inputs("input_files", "options"),
    ) == []
    assert {edge["target_input"] for edge in plan["interfaces"] if edge["kind"] == "platform_to_member"} == {"input_files", "options"}


def test_pdf_options_remains_one_object_port():
    plan = validate_interface_plan_protocol(_closed_plan("user_request", "options"))
    inputs = {edge["target_input"] for edge in plan["interfaces"] if edge["kind"] == "platform_to_member"}
    assert inputs == {"user_request", "options"}
    assert not inputs.intersection({"font", "page_size", "pages"})


def test_two_images_remain_one_input_files_contract():
    plan = _closed_plan("input_files")
    assert interface_contract_closure_check(
        interface_plan=plan, function_items=_closed_item(["input_files"]),
        platform_contract=_boundary_inputs("input_files"),
    ) == []
    assert [edge["target_input"] for edge in plan["interfaces"] if edge["kind"] == "platform_to_member"] == ["input_files"]
