import json
import pytest

from backend.services.creator.responsibility_graph_expansion import (
    GraphExpansionState, ResponsibilityGraphExpansionError,
    build_endpoint_registry, expand_responsibility_graph,
    validate_and_materialize_input_binding,
    validate_and_materialize_terminal_bindings,
    validate_input_source_reference_protocol,
    validate_terminal_reference_protocol,
)
from backend.services.platform_io_contract import build_platform_io_contract


def item(node, inputs, outputs, defaults=None):
    return {"target_file": node, "role": "script", "purpose": f"purpose-{node}", "inputs": inputs, "outputs": outputs, "required_capabilities": [], "constraints": [], "default_values": defaults or {}}


def contract(required=None):
    boundary = {"input_envelope_fields": ["fields", "options"], "final_output_fields": ["text", "pdf_path", "image_path", "docx_path"]}
    if required is not None:
        boundary["required_final_output_fields"] = required
    return {"platform_skill_boundary": boundary}


def obligation(node="scripts/b.py", port="x"):
    return {"obligation_id": "O0001", "target": {"node_id": node, "port_id": port}, "target_context": {"input_contract": {}}}


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


def test_terminal_direct_references_materialize_only_selected_pair_and_required_coverage():
    items = [item("scripts/a.py", [], ["z"])]
    platform = contract(["pdf_path", "docx_path"])
    registry = build_endpoint_registry(function_items=items, platform_contract=platform)
    output_id = registry["script_outputs"][0]["output_id"]
    slots = {value["field"]: value["slot_id"] for value in registry["platform_outputs"]}
    with pytest.raises(ResponsibilityGraphExpansionError, match="cover every required"):
        validate_and_materialize_terminal_bindings(selections=[{"source_output_id": output_id, "platform_output_slot_id": slots["pdf_path"]}], registry=registry, function_items=items, platform_contract=platform)
    edges = validate_and_materialize_terminal_bindings(selections=[
        {"source_output_id": output_id, "platform_output_slot_id": slots["pdf_path"]},
        {"source_output_id": output_id, "platform_output_slot_id": slots["docx_path"]},
    ], registry=registry, function_items=items, platform_contract=platform)
    assert [edge["to_input"] for edge in edges] == ["pdf_path", "docx_path"]


def test_script_output_reference_materializes_one_edge_and_rejects_cycle():
    items = [item("scripts/a.py", ["a_in"], ["a"]), item("scripts/b.py", ["x"], ["b"])]
    registry = build_endpoint_registry(function_items=items, platform_contract=contract())
    state = GraphExpansionState(active_nodes={"scripts/b.py"})
    selected = {"obligation_id": "O0001", "source_kind": "script_output", "source_output_id": registry["script_outputs"][0]["output_id"]}
    edge = validate_and_materialize_input_binding(obligation=obligation(), selection=selected, registry=registry, state=state, function_items=items)
    assert (edge["from_node"], edge["to_node"]) == ("scripts/a.py", "scripts/b.py")
    state.committed_edges = [edge]
    state.active_nodes.add("scripts/a.py")
    reverse = {**selected, "source_output_id": registry["script_outputs"][1]["output_id"]}
    with pytest.raises(ResponsibilityGraphExpansionError) as raised:
        validate_and_materialize_input_binding(obligation=obligation("scripts/a.py", "a_in"), selection=reverse, registry=registry, state=state, function_items=items)
    assert raised.value.code == "responsibility_graph_cycle"


@pytest.mark.parametrize("slot_field,path", [
    ("fields", ["page_count"]),
    ("options", ["future_8f31", "setting_91ab"]),
    ("options", []),
])
def test_platform_open_paths_need_no_parameter_vocabulary(slot_field, path):
    items = [item("scripts/b.py", ["x"], ["z"])]
    registry = build_endpoint_registry(function_items=items, platform_contract=contract())
    slot = next(value for value in registry["platform_inputs"] if value["field"] == slot_field)
    state = GraphExpansionState(active_nodes={"scripts/b.py"})
    edge = validate_and_materialize_input_binding(obligation=obligation(), selection={"obligation_id": "O0001", "source_kind": "platform_input", "platform_input_slot_id": slot["slot_id"], "path": path}, registry=registry, state=state, function_items=items)
    assert edge["from_output"] == slot_field
    assert (edge["constraints"][0]["source_path"] if path else []) == path


@pytest.mark.parametrize("path", [[""], ["__proto__"], ["constructor"], "not-list"])
def test_invalid_platform_paths_are_rejected(path):
    with pytest.raises(ResponsibilityGraphExpansionError):
        validate_input_source_reference_protocol(obligation=obligation(), response={"selection": {"obligation_id": "O0001", "source_kind": "platform_input", "platform_input_slot_id": "PIN0001", "path": path}})


def test_unknown_endpoint_ids_and_extra_edge_fields_are_rejected():
    with pytest.raises(ResponsibilityGraphExpansionError):
        validate_terminal_reference_protocol(response={"terminal_bindings": [{"source_output_id": "OUT9999", "platform_output_slot_id": "POUT9999", "edge": {}}]})
    items = [item("scripts/b.py", ["x"], ["z"])]
    registry = build_endpoint_registry(function_items=items, platform_contract=contract())
    with pytest.raises(ResponsibilityGraphExpansionError):
        validate_and_materialize_input_binding(obligation=obligation(), selection={"obligation_id": "O0001", "source_kind": "script_output", "source_output_id": "OUT9999"}, registry=registry, state=GraphExpansionState(active_nodes={"scripts/b.py"}), function_items=items)


def scripted_model(terminal_node, terminal_port, terminal_field, input_sources, calls=None):
    remaining = list(input_sources)
    async def model(messages, _model):
        payload = json.loads(messages[-1]["content"])
        if calls is not None: calls.append(payload)
        if "platform_outputs" in payload and "obligation" not in payload:
            output = next(value for value in payload["script_outputs"] if value["node_purpose"] == f"purpose-{terminal_node}" and value["port_id"] == terminal_port)
            slot = next(value for value in payload["platform_outputs"] if value["field"] == terminal_field)
            return json.dumps({"terminal_bindings": [{"source_output_id": output["output_id"], "platform_output_slot_id": slot["slot_id"]}]})
        kind, marker, path = remaining.pop(0)
        if kind == "script_output":
            output = next(value for value in payload["script_outputs"] if value["node_purpose"] == f"purpose-{marker[0]}" and value["port_id"] == marker[1])
            selection = {"obligation_id": payload["obligation"]["obligation_id"], "source_kind": kind, "source_output_id": output["output_id"]}
        else:
            slot = next(value for value in payload["platform_inputs"] if value["field"] == marker)
            selection = {"obligation_id": payload["obligation"]["obligation_id"], "source_kind": kind, "platform_input_slot_id": slot["slot_id"], "path": path}
        return json.dumps({"selection": selection})
    return model


@pytest.mark.asyncio
async def test_two_script_goal_driven_expansion_uses_endpoint_references():
    items = [item("scripts/a.py", ["a_in"], ["a_out"]), item("scripts/b.py", ["b_in"], ["b_out"])]
    calls = []
    edges = await expand_responsibility_graph(function_items=items, platform_contract=build_platform_io_contract(), planner_model="p", goal_context={"user_request": "random"}, model_call=scripted_model("scripts/b.py", "b_out", "text", [("script_output", ("scripts/a.py", "a_out"), []), ("platform_input", "fields", ["future_91ab"])], calls))
    assert [(edge["from_node"], edge["to_node"]) for edge in edges] == [("scripts/b.py", "platform_output_node"), ("scripts/a.py", "scripts/b.py"), ("platform_input_node", "scripts/a.py")]
    assert all("terminal_candidates" not in call and "binding_candidates" not in call for call in calls)


@pytest.mark.asyncio
async def test_terminal_protocol_retries_with_same_independent_registry():
    items = [item("scripts/a.py", [], ["z"])]
    calls = []
    async def model(messages, _model):
        payload = json.loads(messages[-1]["content"]); calls.append(payload)
        if len(calls) == 1: return json.dumps({"terminal_binding_ids": ["legacy"]})
        return json.dumps({"terminal_bindings": [{"source_output_id": payload["script_outputs"][0]["output_id"], "platform_output_slot_id": payload["platform_outputs"][0]["slot_id"]}]})
    await expand_responsibility_graph(function_items=items, platform_contract=contract(), planner_model="p", model_call=model, goal_context={})
    assert calls[0]["script_outputs"] == calls[1]["script_outputs"]
    assert calls[0]["platform_outputs"] == calls[1]["platform_outputs"]
    assert calls[1]["validation_issue"]["code"] == "invalid_terminal_reference_protocol"
