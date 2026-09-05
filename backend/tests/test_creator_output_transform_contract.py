import pytest

from backend.services.creator.function_item_interface_plan import (
    InterfaceIntentPlanError,
    collect_interface_plan_validation_issues,
    validate_interface_patch_protocol,
    validate_interface_plan_protocol,
)


def _issues(source_type: str, accepted_source_types: list[str]):
    interface = {
        "interface_id": "I1", "kind": "member_to_platform",
        "source_member": "scripts/unit.py", "source_output": "member_output",
        "target_platform_output": "platform_output", "goal": "return result",
    }
    item = {
        "target_file": "scripts/unit.py", "role": "script", "purpose": "produce result",
        "inputs": [],
        "outputs": [{"port_id": "member_output", "contract": {"type": source_type}}],
        "default_values": {}, "required_capabilities": [], "constraints": [],
    }
    return collect_interface_plan_validation_issues(
        plan={"interfaces": [interface]}, function_items=[item],
        platform_contract={"platform_skill_boundary": {
            "final_output_fields": [{
                "name": "platform_output", "semantic_type": "text",
                "accepted_source_types": accepted_source_types,
                "value_schema": {"type": "string"},
            }],
            "required_final_output_fields": ["platform_output"],
        }},
    )


def test_object_output_can_bind_directly_to_text_platform_output():
    assert _issues("object", ["text", "object", "json"]) == []


def test_legacy_transform_is_read_but_removed_from_canonical_plan():
    plan = {"interfaces": [{
        "interface_id": "I1", "kind": "member_to_platform",
        "source_member": "scripts/unit.py", "source_output": "member_output",
        "target_platform_output": "platform_output", "goal": "return result",
        "transform": "historical_adapter",
    }]}
    assert "transform" not in validate_interface_plan_protocol(plan)["interfaces"][0]


def test_repair_protocol_rejects_replace_transform():
    with pytest.raises(InterfaceIntentPlanError) as exc_info:
        validate_interface_patch_protocol({"operations": [{
            "op": "replace_transform", "interface_id": "I1",
            "reason": "conversion is not interface planning", "transform": None,
        }]})
    assert exc_info.value.code == "invalid_interface_patch"
