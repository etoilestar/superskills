import pytest
from backend.services.creator.responsibility_graph_expansion import (
    ResponsibilityGraphExpansionError, build_endpoint_registry,
    expand_responsibility_graph,
)


def item(name, inputs, outputs):
    return {"target_file": name, "role": "script", "purpose": name, "inputs": inputs, "outputs": outputs, "default_values": {}, "required_capabilities": [], "constraints": []}


def platform():
    return {"platform_skill_boundary": {"input_envelope_fields": ["payload"], "final_output_fields": ["text"]
, "required_final_output_fields": ["text"]
}}


def test_registry_keeps_logical_and_opaque_id_layers_distinct():
    registry = build_endpoint_registry(function_items=[item("scripts/unit_a.py", ["slot_x"], ["value_a"])], platform_contract=platform())
    assert registry["script_inputs"][0]["port_id"] == "slot_x"
    assert registry["script_inputs"][0]["input_id"].startswith("IN")
    assert registry["script_outputs"][0]["port_id"] == "value_a"
    assert registry["script_outputs"][0]["output_id"].startswith("OUT")


@pytest.mark.asyncio
async def test_graph_preserves_interface_logical_identity_without_model_binding():
    items = [item("scripts/unit_a.py", ["slot_x"], ["value_a"]), item("scripts/unit_b.py", ["slot_y"], ["result_z"])]
    interfaces = {"interfaces": [
        {"interface_id": "I1", "kind": "platform_to_member", "source_platform_input": "payload", "source_path": [], "target_member": "scripts/unit_a.py", "target_input": "slot_x", "goal": "root input"},
        {"interface_id": "I2", "kind": "member_to_member", "source_member": "scripts/unit_a.py", "source_output": "value_a", "target_member": "scripts/unit_b.py", "target_input": "slot_y", "goal": "handoff"},
        {"interface_id": "I3", "kind": "member_to_platform", "source_member": "scripts/unit_b.py", "source_output": "result_z", "target_platform_output": "text", "goal": "result"},
    ]}
    calls = 0
    async def model(*_args):
        nonlocal calls
        calls += 1
        raise AssertionError("deterministic logical-port resolution must not call Binder")
    edges = await expand_responsibility_graph(function_items=items, platform_contract=platform(), planner_model="p", model_call=model, interface_plan=interfaces)
    assert calls == 0
    assert [(edge["from_node"], edge["from_output"], edge["to_node"], edge["to_input"]) for edge in edges] == [
        ("platform_input_node", "payload", "scripts/unit_a.py", "slot_x"),
        ("scripts/unit_a.py", "value_a", "scripts/unit_b.py", "slot_y"),
        ("scripts/unit_b.py", "result_z", "platform_output_node", "text"),
    ]


@pytest.mark.asyncio
async def test_reusable_logical_output_fans_out_without_candidate_search():
    items = [item("scripts/unit_a.py", ["slot_x"], ["value_a"]), item("scripts/unit_b.py", ["slot_x"], ["result_z"]), item("scripts/unit_c.py", ["slot_y"], ["result_z"])]
    plan = {"interfaces": [
        {"interface_id": "I1", "kind": "platform_to_member", "source_platform_input": "payload", "source_path": [], "target_member": "scripts/unit_a.py", "target_input": "slot_x", "goal": "root"},
        {"interface_id": "I2", "kind": "member_to_member", "source_member": "scripts/unit_a.py", "source_output": "value_a", "target_member": "scripts/unit_b.py", "target_input": "slot_x", "goal": "fanout a"},
        {"interface_id": "I3", "kind": "member_to_member", "source_member": "scripts/unit_a.py", "source_output": "value_a", "target_member": "scripts/unit_c.py", "target_input": "slot_y", "goal": "fanout b"},
        {"interface_id": "I4", "kind": "member_to_platform", "source_member": "scripts/unit_b.py", "source_output": "result_z", "target_platform_output": "text", "goal": "result"},
    ]}
    with pytest.raises(ResponsibilityGraphExpansionError) as raised:
        await expand_responsibility_graph(function_items=items, platform_contract=platform(), planner_model="p", interface_plan=plan)
    assert raised.value.code == "inactive_graph_component"
    # Both fan-out edges were materialized before terminal reachability rejected unit_c.


@pytest.mark.asyncio
async def test_missing_declared_logical_port_returns_structured_unbound():
    items = [item("scripts/unit_a.py", ["slot_x"], ["value_a"])]
    plan = {"interfaces": [{"interface_id": "I1", "kind": "platform_to_member", "source_platform_input": "payload", "source_path": [], "target_member": "scripts/unit_a.py", "target_input": "missing", "goal": "invalid"}]}
    with pytest.raises(ResponsibilityGraphExpansionError) as raised:
        await expand_responsibility_graph(function_items=items, platform_contract=platform(), planner_model="p", interface_plan=plan)
    assert raised.value.code == "interface_endpoint_unbound"
    assert raised.value.details["logical_binding"]["target_input"] == "missing"

@pytest.mark.asyncio
async def test_nested_source_path_materializes_constraint():
    items = [item("scripts/unit_a.py", ["slot_x"], ["result_z"])]
    plan = {"interfaces": [
        {"interface_id": "I1", "kind": "platform_to_member", "source_platform_input": "payload", "source_path": ["x", "y"], "target_member": "scripts/unit_a.py", "target_input": "slot_x", "goal": "nested value"},
        {"interface_id": "I2", "kind": "member_to_platform", "source_member": "scripts/unit_a.py", "source_output": "result_z", "target_platform_output": "text", "goal": "result"},
    ]}
    edges = await expand_responsibility_graph(function_items=items, platform_contract=platform(), planner_model="p", interface_plan=plan)
    assert edges[0]["constraints"] == [{"type": "platform_parameter_binding", "source_key": "x.y", "source_path": ["x", "y"], "required": True}]


def test_invalid_source_path_protocol_is_rejected():
    from backend.services.creator.function_item_interface_plan import validate_interface_plan_protocol, InterfaceIntentPlanError
    invalid = {"interfaces": [{"interface_id": "I1", "kind": "platform_to_member", "source_platform_input": "payload", "source_path": [""], "target_member": "scripts/unit_a.py", "target_input": "slot_x", "goal": "x"}]}
    with pytest.raises(InterfaceIntentPlanError):
        validate_interface_plan_protocol(invalid)


@pytest.mark.asyncio
async def test_type_conflict_does_not_search_for_another_source():
    items = [item("scripts/unit_a.py", [{"port_id": "slot_x", "contract": {"type": "string"}}], [{"port_id": "result_z", "contract": {"type": "string"}}])]
    contract = {"platform_skill_boundary": {"input_envelope_fields": [{"field": "payload", "contract": {"type": "object"}}], "final_output_fields": ["text"], "required_final_output_fields": ["text"]}}
    plan = {"interfaces": [{"interface_id": "I1", "kind": "platform_to_member", "source_platform_input": "payload", "source_path": [], "target_member": "scripts/unit_a.py", "target_input": "slot_x", "goal": "x"}, {"interface_id": "I2", "kind": "member_to_platform", "source_member": "scripts/unit_a.py", "source_output": "result_z", "target_platform_output": "text", "goal": "x"}]}
    with pytest.raises(ResponsibilityGraphExpansionError) as raised:
        await expand_responsibility_graph(function_items=items, platform_contract=contract, planner_model="p", interface_plan=plan)
    assert raised.value.code == "interface_endpoint_type_conflict"


@pytest.mark.asyncio
async def test_duplicate_graph_provenance_is_deterministically_rejected():
    items = [item("scripts/unit_a.py", ["slot_x"], ["value_a"]), item("scripts/unit_b.py", ["slot_x"], ["value_b"]), item("scripts/unit_c.py", [{"port_id": "slot_y", "contract": {"aggregation": True}}], ["result_z"])]
    plan = {"interfaces": [
        {"interface_id": "I1", "kind": "platform_to_member", "source_platform_input": "payload", "source_path": [], "target_member": "scripts/unit_a.py", "target_input": "slot_x", "goal": "x"},
        {"interface_id": "I2", "kind": "platform_to_member", "source_platform_input": "payload", "source_path": [], "target_member": "scripts/unit_b.py", "target_input": "slot_x", "goal": "x"},
        {"interface_id": "I3", "kind": "member_to_member", "source_member": "scripts/unit_a.py", "source_output": "value_a", "target_member": "scripts/unit_c.py", "target_input": "slot_y", "goal": "x"},
        {"interface_id": "I4", "kind": "member_to_member", "source_member": "scripts/unit_b.py", "source_output": "value_b", "target_member": "scripts/unit_c.py", "target_input": "slot_y", "goal": "x"},
        {"interface_id": "I5", "kind": "member_to_platform", "source_member": "scripts/unit_c.py", "source_output": "result_z", "target_platform_output": "text", "goal": "x"},
    ]}
    with pytest.raises(ResponsibilityGraphExpansionError) as raised:
        await expand_responsibility_graph(function_items=items, platform_contract=platform(), planner_model="p", interface_plan=plan)
    assert raised.value.code == "duplicate_input_provenance"

@pytest.mark.asyncio
async def test_directed_cycle_remains_rejected():
    items = [item("scripts/unit_a.py", ["slot_x"], ["value_a"]), item("scripts/unit_b.py", ["slot_y"], ["value_b"])]
    plan = {"interfaces": [
        {"interface_id": "I1", "kind": "member_to_member", "source_member": "scripts/unit_a.py", "source_output": "value_a", "target_member": "scripts/unit_b.py", "target_input": "slot_y", "goal": "x"},
        {"interface_id": "I2", "kind": "member_to_member", "source_member": "scripts/unit_b.py", "source_output": "value_b", "target_member": "scripts/unit_a.py", "target_input": "slot_x", "goal": "x"},
        {"interface_id": "I3", "kind": "member_to_platform", "source_member": "scripts/unit_b.py", "source_output": "value_b", "target_platform_output": "text", "goal": "x"},
    ]}
    with pytest.raises(ResponsibilityGraphExpansionError) as raised:
        await expand_responsibility_graph(function_items=items, platform_contract=platform(), planner_model="p", interface_plan=plan)
    assert raised.value.code == "responsibility_graph_cycle"


@pytest.mark.asyncio
async def test_multiple_explicit_required_outputs_materialize_terminals():
    items = [item("scripts/unit_a.py", ["slot_x"], ["result_a", "result_b"])]
    contract = {"platform_skill_boundary": {"input_envelope_fields": ["payload"], "final_output_fields": ["text", "image_path"], "required_final_output_fields": ["text", "image_path"]}}
    plan = {"interfaces": [
        {"interface_id": "I1", "kind": "platform_to_member", "source_platform_input": "payload", "source_path": [], "target_member": "scripts/unit_a.py", "target_input": "slot_x", "goal": "x"},
        {"interface_id": "I2", "kind": "member_to_platform", "source_member": "scripts/unit_a.py", "source_output": "result_a", "target_platform_output": "text", "goal": "x"},
        {"interface_id": "I3", "kind": "member_to_platform", "source_member": "scripts/unit_a.py", "source_output": "result_b", "target_platform_output": "image_path", "goal": "x"},
    ]}
    edges = await expand_responsibility_graph(function_items=items, platform_contract=contract, planner_model="p", interface_plan=plan)
    assert {(edge["to_node"], edge["to_input"]) for edge in edges if edge["to_node"] == "platform_output_node"} == {("platform_output_node", "text"), ("platform_output_node", "image_path")}
