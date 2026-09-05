import pytest

from backend.services.creator.function_item_interface_plan import (
    build_interface_repair_scope,
    collect_interface_plan_validation_issues,
)


def _issues(source_type, target, transform):
    contracts = {
        "text_result": {
            "name": "text_result", "semantic_type": "text",
            "accepted_source_types": ["string", "text", "json", "object"],
            "allowed_transforms": ["text_render"],
            "value_schema": {"type": "string"},
        },
        "file_output": {
            "name": "file_output", "semantic_type": "file",
            "accepted_source_types": ["artifact", "file", "json", "object"],
            "allowed_transforms": ["json_serialize", "text_render"],
            "value_schema": {"type": "string"},
        },
    }
    interface = {
        "interface_id": "I1", "kind": "member_to_platform",
        "source_member": "scripts/unit.py", "source_output": "arbitrary_name",
        "target_platform_output": target, "goal": "return result",
    }
    if transform:
        interface["transform"] = transform
    item = {
        "target_file": "scripts/unit.py", "role": "script", "purpose": "produce result",
        "inputs": [], "outputs": [{"port_id": "arbitrary_name", "contract": {"type": source_type}}],
        "default_values": {}, "required_capabilities": [], "constraints": [],
    }
    return collect_interface_plan_validation_issues(
        plan={"interfaces": [interface]}, function_items=[item],
        platform_contract={"platform_skill_boundary": {
            "final_output_fields": [contracts[target]],
            "required_final_output_fields": [target],
        }},
    )


@pytest.mark.parametrize(("source_type", "target", "transform", "valid"), [
    ("object", "text_result", "json_serialize", False),
    ("object", "text_result", "text_render", True),
    ("file", "file_output", None, True),
    ("artifact", "text_result", None, False),
    ("json", "file_output", None, False),
    ("json", "file_output", "json_serialize", True),
])
def test_output_validation_uses_source_transform_result_and_target_contract(
    source_type, target, transform, valid,
):
    assert (_issues(source_type, target, transform) == []) is valid


def test_transform_failure_is_structured_for_targeted_repair():
    issue = _issues("artifact", "text_result", "file_collect")[0]
    reason = build_interface_repair_scope([issue])["failed_validation_reason"][0]
    assert reason == {
        "error": "transform_result_type_mismatch", "source_type": "artifact",
        "transform": "file_collect", "result_type": "file", "target_type": "text",
    }


@pytest.mark.parametrize(("target", "transform", "valid"), [
    ("json_output", None, True),
    ("text_output", "json_serialize", False),
    ("file_output", "json_serialize", True),
])
def test_structured_output_contract_matrix(target, transform, valid):
    contracts = {
        "json_output": {"name": "json_output", "semantic_type": "json", "accepted_source_types": ["object", "json"], "allowed_transforms": [], "value_schema": {"type": "object"}},
        "text_output": {"name": "text_output", "semantic_type": "text", "accepted_source_types": ["text", "object"], "allowed_transforms": ["text_render"], "value_schema": {"type": "string"}},
        "file_output": {"name": "file_output", "semantic_type": "file", "accepted_source_types": ["object"], "allowed_transforms": ["json_serialize"], "value_schema": {"type": "string"}},
    }
    interface = {"interface_id": "I1", "kind": "member_to_platform", "source_member": "scripts/unit.py", "source_output": "result", "target_platform_output": target, "goal": "return"}
    if transform:
        interface["transform"] = transform
    item = {"target_file": "scripts/unit.py", "role": "script", "purpose": "produce", "inputs": [], "outputs": [{"port_id": "result", "contract": {"type": "object"}}], "default_values": {}, "required_capabilities": [], "constraints": []}
    issues = collect_interface_plan_validation_issues(plan={"interfaces": [interface]}, function_items=[item], platform_contract={"platform_skill_boundary": {"final_output_fields": [contracts[target]], "required_final_output_fields": [target]}})
    assert (issues == []) is valid
    if not valid:
        assert issues[0]["code"] == "OUTPUT_CONTRACT_INCONSISTENCY"
