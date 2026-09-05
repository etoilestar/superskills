"""Business-agnostic regression tests for the shared Interface contract."""

from backend.services.creator.function_item_interface_plan import (
    build_canonical_interface_contract,
    collect_interface_contract_consistency_issues,
    collect_interface_plan_validation_issues,
)


def port(name, role, semantic="value", schema_type="string"):
    return {"port_id": name, "role": role,
            "contract": {"type": schema_type, "semantic_type": semantic}}


def member(path, inputs, outputs):
    return {"target_file": path, "role": "worker", "purpose": "transform value",
            "inputs": inputs, "outputs": outputs, "constraints": [],
            "required_capabilities": []}


def boundary(*inputs):
    return {"platform_skill_boundary": {
        "input_envelope_fields": list(inputs),
        "input_schemas": {name: {"type": "string", "semantic_type": "value"} for name in inputs},
        "final_output_fields": ["result"],
    }}


def p2m(source, target="scripts/a.py", slot="input"):
    return {"interface_id": "I1", "kind": "platform_to_member",
            "source_platform_input": source, "source_path": [],
            "target_member": target, "target_input": slot, "goal": "transfer declared value"}


def m2m(source="scripts/a.py", output="value", target="scripts/b.py", slot="input"):
    return {"interface_id": "I1", "kind": "member_to_member", "source_member": source,
            "source_output": output, "target_member": target, "target_input": slot,
            "goal": "transfer declared value"}


def issues(plan, items, contract=None):
    return collect_interface_plan_validation_issues(
        plan={"interfaces": plan}, function_items=items,
        platform_contract=contract or boundary("external"),
    )


def test_required_input_with_compatible_source_passes():
    items = [member("scripts/a.py", [port("input", "required_runtime_input")], [port("value", "runtime_output")])]
    assert not [x for x in issues([p2m("external")], items) if x["error_type"] != "binding_error" or x["code"] != "missing_platform_terminal"]


def test_optional_input_without_source_passes():
    items = [member("scripts/a.py", [port("input", "optional_runtime_input")], [port("value", "runtime_output")])]
    assert not any(x["code"] == "uncovered_required_logical_input" for x in issues([], items))


def test_optional_input_with_wrong_source_fails_provenance():
    items = [member("scripts/a.py", [port("input", "optional_runtime_input", "wanted")], [port("value", "runtime_output")])]
    contract = boundary("external")
    contract["platform_skill_boundary"]["input_schemas"]["external"]["semantic_type"] = "other"
    assert any(x["error_type"] == "provenance_error" for x in issues([p2m("external")], items, contract))


def test_derived_input_from_preceding_intermediate_output_passes():
    items = [
        member("scripts/a.py", [], [port("value", "intermediate_output")]),
        member("scripts/b.py", [port("input", "derived_input")], [port("done", "runtime_output")]),
    ]
    assert not any(x["error_type"] == "provenance_error" for x in issues([m2m()], items))


def test_derived_input_from_platform_fails():
    items = [member("scripts/a.py", [port("input", "derived_input")], [port("value", "runtime_output")])]
    assert any(x["error_type"] == "provenance_error" for x in issues([p2m("external")], items))


def test_object_to_text_direct_binding_passes():
    items = [member("scripts/a.py", [], [port("value", "runtime_output", schema_type="object")])]
    plan = {"interfaces": [{"interface_id": "I1", "kind": "member_to_platform",
        "source_member": "scripts/a.py", "source_output": "value",
        "target_platform_output": "result", "goal": "return result"}]}
    contract = {"platform_skill_boundary": {"final_output_fields": ["result"],
        "output_sinks": {"result": {"value_schema": {"type": "string"}}}}}
    assert collect_interface_plan_validation_issues(plan=plan, function_items=items, platform_contract=contract) == []


def test_cross_stage_contract_divergence_fails():
    items = [member("scripts/a.py", [port("input", "required_runtime_input")], [port("value", "runtime_output")])]
    canonical = build_canonical_interface_contract(
        plan={"interfaces": [p2m("external")]}, function_items=items,
        platform_contract=boundary("external"))
    divergent = {"interfaces": []}
    found = collect_interface_contract_consistency_issues(
        interface_contract=canonical,
        artifact_contracts={"SKILL.md": canonical, "command": canonical,
                            "script": divergent, "runtime binding": canonical})
    assert [x["artifact"] for x in found] == ["script"]
