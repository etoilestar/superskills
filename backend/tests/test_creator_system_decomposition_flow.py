import json

import pytest

from backend.services.creator.api import (
    validate_final_executable_requirement_ownership,
    validate_frozen_function_item_structure,
)
from backend.services.creator.function_item_interface_plan import plan_function_item_interfaces
from backend.services.creator.responsibility_graph_expansion import expand_responsibility_graph
from backend.services.platform_io_contract import build_platform_io_contract


def item(target_file, purpose, inputs, outputs):
    return {
        "target_file": target_file,
        "role": "script",
        "purpose": purpose,
        "inputs": inputs,
        "outputs": outputs,
        "required_capabilities": [],
        "constraints": [],
        "default_values": {},
    }


def allocation(requirement_id, owners):
    return {
        "requirement_id": requirement_id,
        "requirement": f"requirement-{requirement_id}",
        "owners": owners,
        "evidence": {"responsibility": "declared by planner", "outputs": [], "capabilities": []},
    }


@pytest.mark.asyncio
async def test_system_decomposition_flow_reconciles_owner_then_builds_graph_and_continues():
    calls: list[str] = []
    code_generation_called = False
    e2e_called = False
    frozen_function_items = [
        item("scripts/a.py", "atomic subgoal 1", ["input_1"], ["output_1"]),
        item("scripts/b.py", "atomic subgoal 2", ["input_1"], ["output_1"]),
    ]
    initial_allocations = [allocation("R1", ["scripts/a.py"]), allocation("R2", [])]
    requirement_channels = {"R1": "executable", "R2": "executable"}

    calls.append("blueprint_function_item_binding")
    decomposition = validate_frozen_function_item_structure(
        function_items=frozen_function_items,
        requirement_allocations=initial_allocations,
    )
    assert decomposition["targets"] == ["scripts/a.py", "scripts/b.py"]
    assert all(value["purpose"] for value in frozen_function_items)
    assert frozen_function_items[0]["purpose"] != frozen_function_items[1]["purpose"]

    calls.append("requirement_allocation")
    calls.append("semantic_review")
    calls.append("allocation_reconciliation")
    repaired_allocations = [allocation("R1", ["scripts/a.py"]), allocation("R2", ["scripts/b.py"])]
    ownership = validate_final_executable_requirement_ownership(
        requirement_allocations=repaired_allocations,
        requirement_channels=requirement_channels,
        allowed_owner_targets=["scripts/a.py", "scripts/b.py"],
    )
    calls.append("final_ownership_validation")
    assert ownership["ownership_valid"] is True

    captured_interface_payload = {}

    async def interface_model(messages, _model):
        calls.append("interface_intent_planner")
        captured_interface_payload.update(json.loads(messages[-1]["content"]))
        return json.dumps({
            "interfaces": [
                {"interface_id": "I0001", "kind": "platform_to_member", "goal": "runtime input", "target_member": "scripts/a.py"},
                {"interface_id": "I0002", "kind": "member_to_member", "goal": "member handoff", "source_member": "scripts/a.py", "target_member": "scripts/b.py"},
                {"interface_id": "I0003", "kind": "member_to_platform", "goal": "final output", "source_member": "scripts/b.py"},
            ]
        })

    interface_plan = await plan_function_item_interfaces(
        original_user_goal="complete user goal",
        frozen_function_items=frozen_function_items,
        requirement_allocations=repaired_allocations,
        requirement_channels=requirement_channels,
        platform_contract=build_platform_io_contract(),
        planner_model="p",
        model_call=interface_model,
    )
    assert {value["target_file"] for value in captured_interface_payload["function_items"]} == {"scripts/a.py", "scripts/b.py"}
    assert not {"subsystems", "subsystem_links", "members"} & set(captured_interface_payload)

    endpoint_payloads = []

    async def endpoint_model(messages, _model):
        calls.append("endpoint_planner")
        payload = json.loads(messages[-1]["content"])
        endpoint_payloads.append(payload)
        obligation = payload["obligation"]
        if obligation["kind"] == "platform_to_script":
            return json.dumps({"source_id": payload["platform_inputs"][0]["slot_id"], "target_id": payload["target_member_inputs"][0]["input_id"], "path": []})
        if obligation["kind"] == "script_to_script":
            return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": payload["target_member_inputs"][0]["input_id"]})
        slot = next(value for value in payload["platform_outputs"] if value["field"] == "text")
        return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": slot["slot_id"]})

    edges = await expand_responsibility_graph(
        function_items=frozen_function_items,
        platform_contract=build_platform_io_contract(),
        planner_model="p",
        goal_context={"system_goal": "complete user goal"},
        model_call=endpoint_model,
        interface_plan=interface_plan,
    )
    assert len(edges) == 3
    assert endpoint_payloads
    assert all("binding_candidates" not in payload for payload in endpoint_payloads)
    assert all("legacy_" + "goal_expansion" not in json.dumps(payload) for payload in endpoint_payloads)
    assert all("terminal_" + "bindings" not in payload and "frontier" not in payload for payload in endpoint_payloads)

    calls.append("code_generation")
    code_generation_called = True
    calls.append("e2e")
    e2e_called = True

    assert code_generation_called is True
    assert e2e_called is True
    assert calls[:6] == [
        "blueprint_function_item_binding",
        "requirement_allocation",
        "semantic_review",
        "allocation_reconciliation",
        "final_ownership_validation",
        "interface_intent_planner",
    ]
    assert "endpoint_planner" in calls
    assert calls[-2:] == ["code_generation", "e2e"]
