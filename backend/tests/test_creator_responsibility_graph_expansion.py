import json

import pytest

from backend.services.creator.responsibility_graph_expansion import (
    ResponsibilityGraphExpansionError,
    build_endpoint_registry,
    expand_responsibility_graph,
)
from backend.services.platform_io_contract import build_platform_io_contract


def item(node, inputs, outputs, defaults=None):
    return {"target_file": node, "role": "script", "purpose": f"purpose-{node}", "inputs": inputs, "outputs": outputs, "required_capabilities": [], "constraints": [], "default_values": defaults or {}}


def contract(required=None):
    boundary = {"input_envelope_fields": ["fields", "options"], "final_output_fields": ["text", "pdf_path", "image_path", "docx_path"]}
    if required is not None:
        boundary["required_final_output_fields"] = required
    return {"platform_skill_boundary": boundary}


def plan(*interfaces):
    return {"interfaces": list(interfaces)}


def p2m(interface_id, target):
    return {"interface_id": interface_id, "kind": "platform_to_member", "goal": "runtime input", "target_member": target}


def m2m(interface_id, source, target):
    return {"interface_id": interface_id, "kind": "member_to_member", "goal": "member result", "source_member": source, "target_member": target}


def m2p(interface_id, source):
    return {"interface_id": interface_id, "kind": "member_to_platform", "goal": "platform output", "source_member": source}


def test_registry_has_independent_stable_endpoints_without_cartesian_product():
    items = [item(f"scripts/n{i}.py", [], [f"o{i}"]) for i in range(10)]
    platform = {"platform_skill_boundary": {"input_envelope_fields": [f"i{i}" for i in range(3)], "final_output_fields": [f"p{i}" for i in range(10)]}}
    first = build_endpoint_registry(function_items=items, platform_contract=platform)
    second = build_endpoint_registry(function_items=items, platform_contract=platform)
    assert first == second
    assert len(first["nodes"]) == len(first["script_outputs"]) == len(first["platform_outputs"]) == 10
    assert first["nodes"][0]["node_id"] == "N0001"
    assert first["script_outputs"][-1]["output_id"] == "OUT0010"
    assert first["platform_outputs"][-1]["slot_id"] == "POUT0010"
    assert all("edge" not in value for group in first.values() for value in group)


def test_one_hundred_outputs_register_no_edge_candidates():
    items = [item(f"scripts/n{i}.py", [], [f"o{i}"]) for i in range(100)]
    registry = build_endpoint_registry(function_items=items, platform_contract=contract())
    assert len(registry["script_outputs"]) == 100
    assert not any("candidate_id" in value or "edge" in value for value in registry["script_outputs"])


@pytest.mark.asyncio
async def test_two_script_interface_expansion_uses_endpoint_references_without_global_candidates():
    items = [item("scripts/a.py", ["a_in"], ["a_out"]), item("scripts/b.py", ["b_in"], ["b_out"])]
    calls = []

    async def model(messages, _model):
        payload = json.loads(messages[-1]["content"])
        calls.append(payload)
        obligation = payload["obligation"]
        if obligation["kind"] == "platform_to_script":
            slot = next(value for value in payload["platform_inputs"] if value["field"] == "fields")
            return json.dumps({"source_id": slot["slot_id"], "target_id": payload["target_member_inputs"][0]["input_id"], "path": ["future_91ab"]})
        if obligation["kind"] == "script_to_platform":
            slot = next(value for value in payload["platform_outputs"] if value["field"] == "text")
            return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": slot["slot_id"]})
        return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": payload["target_member_inputs"][0]["input_id"]})

    edges = await expand_responsibility_graph(function_items=items, platform_contract=build_platform_io_contract(), planner_model="p", goal_context={"system_goal": "random"}, model_call=model, interface_plan=plan(p2m("I0001", "scripts/a.py"), m2m("I0002", "scripts/a.py", "scripts/b.py"), m2p("I0003", "scripts/b.py")))
    assert [(edge["from_node"], edge["to_node"]) for edge in edges] == [("platform_input_node", "scripts/a.py"), ("scripts/a.py", "scripts/b.py"), ("scripts/b.py", "platform_output_node")]
    member_call = next(call for call in calls if call["obligation"]["kind"] == "script_to_script")
    assert "script_outputs" not in member_call
    assert "binding_candidates" not in member_call


@pytest.mark.asyncio
async def test_interface_plan_missing_is_not_legacy_fallback():
    with pytest.raises(TypeError):
        await expand_responsibility_graph(function_items=[item("scripts/a.py", [], ["z"])], platform_contract=contract(), planner_model="p", model_call=lambda *_: "{}", goal_context={})


@pytest.mark.asyncio
async def test_endpoint_selection_retries_only_current_interface():
    items = [item("scripts/a.py", ["x"], ["z"])]
    calls = []

    async def model(messages, _model):
        payload = json.loads(messages[-1]["content"])
        calls.append(payload)
        if payload["obligation"]["kind"] == "platform_to_script":
            if len(calls) == 1:
                return json.dumps({"source_id": "PIN9999", "target_id": payload["target_member_inputs"][0]["input_id"], "path": []})
            return json.dumps({"source_id": payload["platform_inputs"][0]["slot_id"], "target_id": payload["target_member_inputs"][0]["input_id"], "path": []})
        slot = next(value for value in payload["platform_outputs"] if value["field"] == "text")
        return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": slot["slot_id"]})

    await expand_responsibility_graph(function_items=items, platform_contract=contract(), planner_model="p", model_call=model, goal_context={}, interface_plan=plan(p2m("I0001", "scripts/a.py"), m2p("I0002", "scripts/a.py")))
    assert calls[1]["validation_error"]["code"] == "invalid_interface_endpoint_reference"


@pytest.mark.asyncio
async def test_structured_port_defaults_and_unresolved_inputs_use_port_ids():
    items = [item("scripts/a.py", [{"port_id": "provided", "description": ""}, {"port_id": "defaulted", "description": ""}], ["z"], defaults={"defaulted": "ok"})]

    async def model(messages, _model):
        payload = json.loads(messages[-1]["content"])
        if payload["obligation"]["kind"] == "platform_to_script":
            return json.dumps({"source_id": payload["platform_inputs"][0]["slot_id"], "target_id": payload["target_member_inputs"][0]["input_id"], "path": []})
        slot = next(value for value in payload["platform_outputs"] if value["field"] == "text")
        return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": slot["slot_id"]})

    edges = await expand_responsibility_graph(function_items=items, platform_contract=contract(), planner_model="p", model_call=model, goal_context={}, interface_plan=plan(p2m("I0001", "scripts/a.py"), m2p("I0002", "scripts/a.py")))
    assert ("scripts/a.py", "provided") in {(edge["to_node"], edge["to_input"]) for edge in edges}

    missing_items = [item("scripts/a.py", [{"port_id": "first"}, {"port_id": "second"}], ["z"])]
    with pytest.raises(ResponsibilityGraphExpansionError) as raised:
        await expand_responsibility_graph(function_items=missing_items, platform_contract=contract(), planner_model="p", model_call=model, goal_context={}, interface_plan=plan(p2m("I0001", "scripts/a.py"), m2p("I0002", "scripts/a.py")))
    assert raised.value.code == "interface_plan_incomplete"
    assert raised.value.details["uncovered_inputs"] == [{"target": "scripts/a.py", "input_id": "second"}]


def test_structured_port_description_and_contract_are_preserved_in_registry():
    items = [
        item(
            "scripts/a.py",
            [{"port_id": "input_1", "description": "input description", "contract": {"type": "object"}}],
            [{"port_id": "output_1", "description": "output description", "contract": {"type": "string"}}],
        )
    ]
    registry = build_endpoint_registry(function_items=items, platform_contract=contract())
    assert registry["script_inputs"][0]["description"] == "input description"
    assert registry["script_inputs"][0]["contract"] == {"type": "object"}
    assert registry["script_outputs"][0]["description"] == "output description"
    assert registry["script_outputs"][0]["contract"] == {"type": "string"}


@pytest.mark.asyncio
async def test_endpoint_payload_keeps_structured_port_metadata_and_type_conflicts_use_contract():
    items = [
        item(
            "scripts/a.py",
            [{"port_id": "input_1", "description": "input description", "contract": {"type": "object"}}],
            [{"port_id": "output_1", "description": "output description", "contract": {"type": "object"}}],
        ),
        item(
            "scripts/b.py",
            [{"port_id": "input_1", "description": "target description", "contract": {"type": "string"}}],
            ["output_1"],
        ),
    ]
    captured = []

    async def model(messages, _model):
        payload = json.loads(messages[-1]["content"])
        captured.append(payload)
        obligation = payload["obligation"]
        if obligation["kind"] == "platform_to_script":
            return json.dumps({"source_id": payload["platform_inputs"][0]["slot_id"], "target_id": payload["target_member_inputs"][0]["input_id"], "path": []})
        if obligation["kind"] == "script_to_script":
            return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": payload["target_member_inputs"][0]["input_id"]})
        slot = next(value for value in payload["platform_outputs"] if value["field"] == "text")
        return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": slot["slot_id"]})

    with pytest.raises(ResponsibilityGraphExpansionError) as raised:
        await expand_responsibility_graph(function_items=items, platform_contract=contract(), planner_model="p", model_call=model, goal_context={}, interface_plan=plan(p2m("I0001", "scripts/a.py"), m2m("I0002", "scripts/a.py", "scripts/b.py"), m2p("I0003", "scripts/b.py")))
    assert raised.value.code == "interface_endpoint_type_conflict"
    member_call = next(call for call in captured if call["obligation"]["kind"] == "script_to_script")
    assert member_call["source_member_outputs"][0]["description"] == "output description"
    assert member_call["source_member_outputs"][0]["contract"] == {"type": "object"}
    assert member_call["target_member_inputs"][0]["description"] == "target description"
    assert member_call["target_member_inputs"][0]["contract"] == {"type": "string"}


@pytest.mark.asyncio
async def test_missing_platform_output_reports_required_fields_without_terminal_edges():
    items = [item("scripts/a.py", [], ["output_1"])]
    platform = {"platform_skill_boundary": {"input_envelope_fields": ["fields"], "final_output_fields": ["text", "pdf_path"], "required_final_output_fields": ["text", "pdf_path"]}}

    with pytest.raises(ResponsibilityGraphExpansionError) as raised:
        await expand_responsibility_graph(function_items=items, platform_contract=platform, planner_model="p", model_call=lambda *_: "{}", goal_context={}, interface_plan=plan())
    assert raised.value.code == "interface_plan_incomplete"
    assert raised.value.details["missing_required_final_output_fields"] == ["pdf_path", "text"]
