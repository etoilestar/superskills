import json

import pytest

from backend.services.creator.responsibility_graph_expansion import (
    ResponsibilityGraphExpansionError,
    expand_responsibility_graph,
)
from backend.services.creator.function_item_interface_plan import (
    InterfaceIntentPlanError,
    build_graph_obligations_from_interfaces,
    plan_function_item_interfaces,
    repair_interface_intents,
    validate_interface_intent_plan,
)
from backend.services.platform_io_contract import build_platform_io_contract


def item(node, inputs, outputs):
    return {"target_file": node, "role": "script", "purpose": f"purpose-{node}", "inputs": inputs, "outputs": outputs, "required_capabilities": [], "constraints": [], "default_values": {}}


def plan(*interfaces):
    return {"interfaces": list(interfaces)}


def p2m(interface_id, target, goal="runtime input"):
    return {"interface_id": interface_id, "kind": "platform_to_member", "goal": goal, "target_member": target}


def m2m(interface_id, source, target, goal="member result"):
    return {"interface_id": interface_id, "kind": "member_to_member", "goal": goal, "source_member": source, "target_member": target}


def m2p(interface_id, source, goal="platform output"):
    return {"interface_id": interface_id, "kind": "member_to_platform", "goal": goal, "source_member": source}


def fenced_json(value):
    return "```json\n" + json.dumps(value) + "\n```"


def test_interface_plan_rejects_unknown_member_self_connection_and_extra_fields():
    items = [item("scripts/a.py", ["input_1"], ["output_1"])]
    with pytest.raises(InterfaceIntentPlanError) as raised:
        validate_interface_intent_plan(plan=plan({**p2m("I0001", "scripts/missing.py"), "extra": True}), function_items=items)
    assert raised.value.code == "invalid_interface_protocol"

    with pytest.raises(InterfaceIntentPlanError) as raised:
        validate_interface_intent_plan(plan=plan(m2m("I0001", "scripts/a.py", "scripts/a.py")), function_items=items)
    assert raised.value.code == "interface_self_connection"


def test_build_graph_obligations_from_interfaces_preserves_member_scope():
    interface_plan = validate_interface_intent_plan(
        plan=plan(p2m("I0001", "scripts/a.py"), m2m("I0002", "scripts/a.py", "scripts/b.py"), m2p("I0003", "scripts/b.py")),
        function_items=[item("scripts/a.py", ["input_1"], ["output_1"]), item("scripts/b.py", ["input_1"], ["output_1"])],
    )
    obligations = build_graph_obligations_from_interfaces(interface_plan=interface_plan)
    assert [ob["kind"] for ob in obligations] == ["platform_to_script", "script_to_script", "script_to_platform"]
    assert obligations[1]["source_member"] == "scripts/a.py"
    assert obligations[1]["target_member"] == "scripts/b.py"
    assert "subsystem_id" not in obligations[1]


async def scripted_endpoint_model(messages, _model):
    payload = json.loads(messages[-1]["content"])
    obligation = payload["obligation"]
    if obligation["kind"] == "platform_to_script":
        return json.dumps({"source_id": payload["platform_inputs"][0]["slot_id"], "target_id": payload["target_member_inputs"][0]["input_id"], "path": []})
    if obligation["kind"] == "script_to_platform":
        slot = next(slot for slot in payload["platform_outputs"] if slot["field"] == "text")
        return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": slot["slot_id"]})
    return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": payload["target_member_inputs"][0]["input_id"]})


@pytest.mark.asyncio
async def test_single_function_item_platform_to_member_to_platform_graph_valid():
    items = [item("scripts/a.py", ["input_1"], ["output_1"])]
    edges = await expand_responsibility_graph(function_items=items, platform_contract=build_platform_io_contract(), planner_model="p", goal_context={}, model_call=scripted_endpoint_model, interface_plan=plan(p2m("I0001", "scripts/a.py"), m2p("I0002", "scripts/a.py")))
    assert [(edge["from_node"], edge["to_node"]) for edge in edges] == [("platform_input_node", "scripts/a.py"), ("scripts/a.py", "platform_output_node")]


@pytest.mark.asyncio
async def test_three_serial_function_items_use_scoped_endpoint_binding():
    items = [item("scripts/a.py", ["input_1"], ["output_1"]), item("scripts/b.py", ["input_1"], ["output_1"]), item("scripts/c.py", ["input_1"], ["output_1"])]
    calls = []

    async def model(messages, model_name):
        calls.append(json.loads(messages[-1]["content"]))
        return await scripted_endpoint_model(messages, model_name)

    await expand_responsibility_graph(function_items=items, platform_contract=build_platform_io_contract(), planner_model="p", goal_context={}, model_call=model, interface_plan=plan(p2m("I0001", "scripts/a.py"), m2m("I0002", "scripts/a.py", "scripts/b.py"), m2m("I0003", "scripts/b.py", "scripts/c.py"), m2p("I0004", "scripts/c.py")))
    member_call = next(call for call in calls if call["obligation"]["interface_id"] == "I0002")
    assert [value["port_id"] for value in member_call["source_member_outputs"]] == ["output_1"]
    assert [value["port_id"] for value in member_call["target_member_inputs"]] == ["input_1"]
    assert "script_outputs" not in member_call


@pytest.mark.asyncio
async def test_branch_converge_multi_input_and_multi_platform_output_shapes():
    items = [item("scripts/a.py", ["input_1"], ["output_1"]), item("scripts/b.py", ["input_1"], ["output_1"]), item("scripts/c.py", ["input_1", "input_2"], ["output_1", "output_2"])]
    platform = {"platform_skill_boundary": {"input_envelope_fields": ["fields", "options"], "final_output_fields": ["text", "pdf_path"], "required_final_output_fields": ["text", "pdf_path"]}}
    terminal_fields = iter(["text", "pdf_path"])

    async def model(messages, _model):
        payload = json.loads(messages[-1]["content"])
        obligation = payload["obligation"]
        if obligation["kind"] == "platform_to_script":
            return json.dumps({"source_id": payload["platform_inputs"][0]["slot_id"], "target_id": payload["target_member_inputs"][0]["input_id"], "path": []})
        if obligation["kind"] == "script_to_platform":
            field = next(terminal_fields)
            slot = next(slot for slot in payload["platform_outputs"] if slot["field"] == field)
            return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": slot["slot_id"]})
        index = 0 if obligation["interface_id"] == "I0003" else 1
        return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": payload["target_member_inputs"][index]["input_id"]})

    edges = await expand_responsibility_graph(function_items=items, platform_contract=platform, planner_model="p", goal_context={}, model_call=model, interface_plan=plan(p2m("I0001", "scripts/a.py"), p2m("I0002", "scripts/b.py"), m2m("I0003", "scripts/a.py", "scripts/c.py"), m2m("I0004", "scripts/b.py", "scripts/c.py"), m2p("I0005", "scripts/c.py"), m2p("I0006", "scripts/c.py")))
    assert len([edge for edge in edges if edge["to_node"] == "platform_output_node"]) == 2


@pytest.mark.asyncio
async def test_same_output_can_feed_member_and_platform_when_interfaces_declare_both():
    items = [item("scripts/a.py", ["input_1"], ["output_1"]), item("scripts/b.py", ["input_1"], ["output_1"])]
    terminal_fields = iter(["text", "pdf_path"])

    async def model(messages, _model):
        payload = json.loads(messages[-1]["content"])
        obligation = payload["obligation"]
        if obligation["kind"] == "platform_to_script":
            return json.dumps({"source_id": payload["platform_inputs"][0]["slot_id"], "target_id": payload["target_member_inputs"][0]["input_id"], "path": []})
        if obligation["kind"] == "script_to_platform":
            field = next(terminal_fields)
            slot = next(slot for slot in payload["platform_outputs"] if slot["field"] == field)
            return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": slot["slot_id"]})
        return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": payload["target_member_inputs"][0]["input_id"]})

    edges = await expand_responsibility_graph(function_items=items, platform_contract=build_platform_io_contract(), planner_model="p", goal_context={}, model_call=model, interface_plan=plan(p2m("I0001", "scripts/a.py"), m2m("I0002", "scripts/a.py", "scripts/b.py"), m2p("I0003", "scripts/a.py"), m2p("I0004", "scripts/b.py")))
    assert ("scripts/a.py", "platform_output_node") in {(edge["from_node"], edge["to_node"]) for edge in edges}
    assert ("scripts/a.py", "scripts/b.py") in {(edge["from_node"], edge["to_node"]) for edge in edges}


@pytest.mark.asyncio
async def test_plan_and_endpoint_selection_accept_single_json_fence_and_protocol_reformat():
    items = [item("scripts/a.py", ["input_1"], ["output_1"])]
    valid_plan = plan(p2m("I0001", "scripts/a.py"), m2p("I0002", "scripts/a.py"))
    responses = iter([json.dumps({"interfaces": [{**p2m("I0001", "scripts/a.py"), "extra": "bad"}, m2p("I0002", "scripts/a.py")]}), fenced_json(valid_plan)])

    async def planner_model(_messages, _model):
        return next(responses)

    assert await plan_function_item_interfaces(original_user_goal="g", frozen_blueprint="b", frozen_function_items=items, planner_model="p", model_call=planner_model) == valid_plan

    async def endpoint_model(messages, _model):
        payload = json.loads(messages[-1]["content"])
        if payload["obligation"]["kind"] == "platform_to_script":
            return fenced_json({"source_id": payload["platform_inputs"][0]["slot_id"], "target_id": payload["target_member_inputs"][0]["input_id"], "path": []})
        slot = next(slot for slot in payload["platform_outputs"] if slot["field"] == "text")
        return fenced_json({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": slot["slot_id"]})

    edges = await expand_responsibility_graph(function_items=items, platform_contract=build_platform_io_contract(), planner_model="p", goal_context={}, model_call=endpoint_model, interface_plan=valid_plan)
    assert edges[-1]["to_node"] == "platform_output_node"


@pytest.mark.asyncio
async def test_missing_input_and_missing_platform_output_raise_interface_or_terminal_errors():
    items = [item("scripts/a.py", ["input_1", "input_2"], ["output_1"])]
    with pytest.raises(ResponsibilityGraphExpansionError) as raised:
        await expand_responsibility_graph(function_items=items, platform_contract=build_platform_io_contract(), planner_model="p", goal_context={}, model_call=scripted_endpoint_model, interface_plan=plan(p2m("I0001", "scripts/a.py"), m2p("I0002", "scripts/a.py")))
    assert raised.value.code == "interface_plan_incomplete"

    platform = {"platform_skill_boundary": {"input_envelope_fields": ["fields"], "final_output_fields": ["text", "pdf_path"], "required_final_output_fields": ["text", "pdf_path"]}}
    single = [item("scripts/a.py", [], ["output_1"])]
    with pytest.raises(ResponsibilityGraphExpansionError) as raised:
        await expand_responsibility_graph(function_items=single, platform_contract=platform, planner_model="p", goal_context={}, model_call=scripted_endpoint_model, interface_plan=plan(m2p("I0001", "scripts/a.py")))
    assert raised.value.code == "interface_plan_incomplete"


@pytest.mark.asyncio
async def test_repair_interface_intents_returns_validated_repaired_plan():
    items = [item("scripts/a.py", ["input_1"], ["output_1"])]
    repaired = plan(p2m("I0001", "scripts/a.py"), m2p("I0002", "scripts/a.py"))

    async def model(messages, _model):
        payload = json.loads(messages[-1]["content"])
        assert payload["current_interface_plan"] == {"interfaces": []}
        assert payload["affected_members"] == ["scripts/a.py"]
        assert payload["validation_errors"][0]["code"] == "interface_plan_incomplete"
        return json.dumps(repaired)

    assert await repair_interface_intents(original_user_goal="g", frozen_blueprint="b", frozen_function_items=items, current_interface_plan={"interfaces": []}, validation_errors=[{"code": "interface_plan_incomplete"}], affected_members=["scripts/a.py"], missing_platform_output_fields=[], planner_model="p", model_call=model) == repaired


def allocation(requirement_id, owners):
    return {"requirement_id": requirement_id, "requirement": f"requirement-{requirement_id}", "owners": owners, "evidence": {"responsibility": "r", "outputs": [], "capabilities": []}}


def test_frozen_function_item_subgoal_validation_requires_purpose_and_executable_owners():
    from backend.services.creator.api import PreparePlanProtocolError, validate_frozen_function_item_subgoals

    items = [item("scripts/a.py", ["input_1"], ["output_1"])]
    summary = validate_frozen_function_item_subgoals(function_items=items, requirement_allocations=[allocation("R1", ["scripts/a.py"])], requirement_channels={"R1": "executable"})
    assert summary["decomposition_valid"] is True
    assert summary["targets"] == ["scripts/a.py"]

    bad_purpose = [{**items[0], "purpose": ""}]
    with pytest.raises((PreparePlanProtocolError, ValueError)):
        validate_frozen_function_item_subgoals(function_items=bad_purpose, requirement_allocations=[allocation("R1", ["scripts/a.py"])], requirement_channels={"R1": "executable"})

    with pytest.raises(PreparePlanProtocolError):
        validate_frozen_function_item_subgoals(function_items=items, requirement_allocations=[allocation("R1", [])], requirement_channels={"R1": "executable"})


def test_interface_planner_payload_is_compact_and_uses_existing_function_items_only():
    items = [item("scripts/a.py", ["input_1"], ["output_1"])]
    captured = {}

    async def model(messages, _model):
        captured.update(json.loads(messages[-1]["content"]))
        return json.dumps(plan(p2m("I0001", "scripts/a.py"), m2p("I0002", "scripts/a.py")))

    import asyncio
    asyncio.run(plan_function_item_interfaces(original_user_goal="goal", frozen_blueprint="SHOULD_NOT_BE_SENT", frozen_function_items=items, requirement_allocations=[allocation("R1", ["scripts/a.py"]), allocation("R2", [])], requirement_channels={"R1": "executable", "R2": "direct"}, planner_model="p", model_call=model))
    assert set(captured) == {"system_goal", "function_items", "executable_requirement_allocations", "platform_contract"}
    assert captured["function_items"][0]["target_file"] == "scripts/a.py"
    assert [value["requirement_id"] for value in captured["executable_requirement_allocations"]] == ["R1"]
