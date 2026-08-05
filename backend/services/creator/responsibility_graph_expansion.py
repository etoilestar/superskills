"""Interface-intent ResponsibilityGraph expansion using scoped endpoint IDs."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .function_item_interface_plan import build_graph_obligations_from_interfaces
from ..skill_plan import GraphValidationError, normalize_structured_function_items, validate_structured_responsibility_edge_transport

logger = logging.getLogger(__name__)
PLATFORM_INPUT_NODE = "platform_input_node"
PLATFORM_OUTPUT_NODE = "platform_output_node"
_EDGE_PURPOSE = "Bind a declared source endpoint to a required target endpoint."
_DANGEROUS_PATH_PARTS = {"__proto__", "prototype", "constructor"}
ModelCall = Callable[[list[dict[str, str]], str], Awaitable[str]]


class ResponsibilityGraphExpansionError(GraphValidationError):
    """A machine-readable endpoint-reference or graph failure."""


@dataclass
class GraphExpansionState:
    active_nodes: set[str] = field(default_factory=set)
    committed_edges: list[dict] = field(default_factory=list)
    activation_order: list[str] = field(default_factory=list)
    inactive_function_items: list[str] = field(default_factory=list)


def _boundary(contract: dict[str, Any]) -> dict[str, Any]:
    value = contract.get("platform_skill_boundary", contract)
    return value if isinstance(value, dict) else {}


def _port(value: Any) -> tuple[str, str, dict[str, Any]]:
    if isinstance(value, dict):
        name = str(value.get("port_id") or value.get("id") or value.get("name") or value.get("field") or "").strip()
        description = str(value.get("description") or "")
        contract = value.get("contract") if isinstance(value.get("contract"), dict) else {}
        return name, description, dict(contract)
    return str(value or "").strip(), "", {}


def _definite_type(contract: dict[str, Any]) -> str | None:
    value = contract.get("type") if isinstance(contract, dict) else None
    if isinstance(value, str) and value.strip().lower() not in {"", "unknown", "any"}:
        return value.strip().lower()
    return None


def _types_conflict(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_type, right_type = _definite_type(left), _definite_type(right)
    return bool(left_type and right_type and left_type != right_type)


def _edge(from_node: str, from_output: str, to_node: str, to_input: str, *, constraints: list[dict] | None = None) -> dict:
    return {"from_node": from_node, "from_output": from_output, "to_node": to_node, "to_input": to_input, "purpose": _EDGE_PURPOSE, "constraints": list(constraints or [])}



def build_endpoint_registry(*, function_items: list[dict], platform_contract: dict) -> dict:
    """Register declared endpoints independently, in normalized declaration order."""
    items = normalize_structured_function_items(function_items, source="graph_expansion")
    nodes: list[dict] = []
    outputs: list[dict] = []
    inputs: list[dict] = []
    for item in items:
        node_id = f"N{len(nodes) + 1:04d}"
        nodes.append({"node_id": node_id, "target_file": item["target_file"], "purpose": item.get("purpose", "")})
        for raw_input in item.get("inputs") or []:
            port_id, description, contract = _port(raw_input)
            if port_id:
                inputs.append({"input_id": f"IN{len(inputs) + 1:04d}", "node_id": node_id, "target_file": item["target_file"], "port_id": port_id, "node_purpose": item.get("purpose", ""), "description": description, "contract": contract})
        for raw_output in item.get("outputs") or []:
            port_id, description, contract = _port(raw_output)
            if port_id:
                outputs.append({"output_id": f"OUT{len(outputs) + 1:04d}", "node_id": node_id, "target_file": item["target_file"], "port_id": port_id, "node_purpose": item.get("purpose", ""), "description": description, "contract": contract})
    boundary = _boundary(platform_contract)
    platform_inputs = []
    for raw_slot in boundary.get("input_envelope_fields") or []:
        field_name, description, contract = _port(raw_slot)
        if field_name:
            platform_inputs.append({"slot_id": f"PIN{len(platform_inputs) + 1:04d}", "field": field_name, "description": description, "contract": contract})
    platform_outputs = []
    for raw_slot in boundary.get("final_output_fields") or []:
        field_name, description, contract = _port(raw_slot)
        if field_name:
            platform_outputs.append({"slot_id": f"POUT{len(platform_outputs) + 1:04d}", "field": field_name, "description": description, "contract": contract})
    return {"nodes": nodes, "script_inputs": inputs, "script_outputs": outputs, "platform_inputs": platform_inputs, "platform_outputs": platform_outputs}


def _strip_single_json_fence(text: str) -> str:
    stripped = str(text or "").strip()
    if not stripped.startswith("```") or not stripped.endswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 3 or lines[0].strip().lower() not in {"```", "```json"} or lines[-1].strip() != "```":
        return stripped
    return "\n".join(lines[1:-1]).strip()


def _parse_object(text: str, code: str) -> dict:
    try:
        value = json.loads(_strip_single_json_fence(text))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ResponsibilityGraphExpansionError("model response must be strict JSON", code=code) from exc
    if not isinstance(value, dict):
        raise ResponsibilityGraphExpansionError("model response must be a JSON object", code=code)
    return value


def _would_cycle(edges: list[dict], source: str, target: str) -> bool:
    if source == PLATFORM_INPUT_NODE or target == PLATFORM_OUTPUT_NODE:
        return False
    adjacency: dict[str, set[str]] = {}
    for edge in edges:
        adjacency.setdefault(str(edge["from_node"]), set()).add(str(edge["to_node"]))
    pending, seen = [target], set()
    while pending:
        node = pending.pop()
        if node == source:
            return True
        if node not in seen:
            seen.add(node)
            pending.extend(adjacency.get(node, ()))
    return False


def _validate_transaction(edges: list[dict], function_items: list[dict]) -> None:
    validate_structured_responsibility_edge_transport(edges, function_items=function_items, source="graph_expansion")
    incoming: set[tuple[str, str]] = set()
    for edge in edges:
        others = [value for value in edges if value is not edge]
        if _would_cycle(others, str(edge["from_node"]), str(edge["to_node"])):
            raise ResponsibilityGraphExpansionError("responsibility graph contains a directed cycle", code="responsibility_graph_cycle")
        if edge["to_node"] != PLATFORM_OUTPUT_NODE:
            key = (edge["to_node"], edge["to_input"])
            if key in incoming:
                raise ResponsibilityGraphExpansionError("input has duplicate provenance", code="duplicate_input_provenance")
            incoming.add(key)


def _finalize_graph(*, state: GraphExpansionState, function_items: list[dict], terminal_edges: list[dict]) -> list[dict]:
    _validate_transaction(state.committed_edges, function_items)
    actual_terminals = [edge for edge in state.committed_edges if edge["to_node"] == PLATFORM_OUTPUT_NODE]
    if actual_terminals != terminal_edges or not actual_terminals:
        raise ResponsibilityGraphExpansionError("terminal set changed during expansion", code="invalid_terminal_closure")
    items = {item["target_file"]: item for item in normalize_structured_function_items(function_items, source="graph_expansion")}
    incoming = {(edge["to_node"], edge["to_input"]) for edge in state.committed_edges}
    unresolved = []
    for node in state.activation_order:
        defaults = items[node].get("default_values") or {}
        for raw_input in items[node].get("inputs") or []:
            port_id = _port(raw_input)[0]
            if port_id and port_id not in defaults and (node, port_id) not in incoming:
                unresolved.append({"target": node, "input_id": port_id})
    if unresolved:
        raise ResponsibilityGraphExpansionError("interface plan does not cover all required FunctionItem inputs", code="interface_plan_incomplete", details={"uncovered_inputs": unresolved})
    for node in state.active_nodes:
        pending, seen = [node], set()
        while pending and PLATFORM_OUTPUT_NODE not in seen:
            current = pending.pop()
            seen.add(current)
            pending.extend(edge["to_node"] for edge in state.committed_edges if edge["from_node"] == current and edge["to_node"] not in seen)
        if PLATFORM_OUTPUT_NODE not in seen:
            raise ResponsibilityGraphExpansionError("active node cannot reach a terminal", code="inactive_graph_component", details={"target": node})
    state.inactive_function_items = sorted(set(items) - state.active_nodes)
    if state.inactive_function_items:
        diagnostic = {"issue_type": "inactive_frozen_function_items", "targets": state.inactive_function_items, "reason": "Frozen FunctionItems were not selected on any path to a required terminal."}
        raise ResponsibilityGraphExpansionError(json.dumps(diagnostic, ensure_ascii=False), code="inactive_frozen_function_items", details=diagnostic)
    return list(state.committed_edges)



def _public_script_inputs(registry: dict, member: str) -> list[dict]:
    return [{key: value[key] for key in ("input_id", "node_id", "node_purpose", "port_id", "description", "contract")} for value in registry["script_inputs"] if value["target_file"] == member]


def _public_script_outputs(registry: dict, member: str) -> list[dict]:
    return [{key: value[key] for key in ("output_id", "node_id", "node_purpose", "port_id", "description", "contract")} for value in registry["script_outputs"] if value["target_file"] == member]


def _validate_interface_selection_protocol(*, obligation: dict, response: Any) -> dict:
    if not isinstance(response, dict):
        raise ResponsibilityGraphExpansionError("endpoint selection must be a JSON object", code="invalid_interface_endpoint_protocol")
    kind = obligation.get("kind")
    expected = {"source_id", "target_id", "path"} if kind == "platform_to_script" else {"source_id", "target_id"}
    if set(response) != expected:
        raise ResponsibilityGraphExpansionError("endpoint selection contains invalid fields", code="invalid_interface_endpoint_protocol")
    if not isinstance(response.get("source_id"), str) or not response["source_id"] or not isinstance(response.get("target_id"), str) or not response["target_id"]:
        raise ResponsibilityGraphExpansionError("endpoint IDs must be non-empty strings", code="invalid_interface_endpoint_protocol")
    if kind == "platform_to_script":
        path = response.get("path")
        if not isinstance(path, list) or any(not isinstance(value, str) or not value or value.lower() in _DANGEROUS_PATH_PARTS for value in path):
            raise ResponsibilityGraphExpansionError("platform path is invalid", code="invalid_interface_endpoint_protocol")
    return dict(response)


async def _select_interface_endpoint_reference(*, obligation: dict, registry: dict, goal_context: dict, committed_edges: list[dict], planner_model: str, model_call: ModelCall, validation_issue: dict | None = None) -> dict:
    kind = obligation["kind"]
    payload: dict[str, Any] = {"goal_context": goal_context, "current_partial_graph": {"committed_edges": committed_edges}, "obligation": obligation}
    if kind == "platform_to_script":
        payload["platform_inputs"] = [dict(value) for value in registry["platform_inputs"]]
        payload["target_member_inputs"] = _public_script_inputs(registry, obligation["target_member"])
        prompt = "Select endpoint IDs only for this declared interface intent. Return strict JSON with exactly source_id, target_id, and path. Choose source_id from platform_inputs and target_id from target_member_inputs. Do not return an edge, wrapper, obligation_id, or explanation."
    elif kind == "script_to_platform":
        payload["source_member_outputs"] = _public_script_outputs(registry, obligation["source_member"])
        payload["platform_outputs"] = [dict(value) for value in registry["platform_outputs"]]
        prompt = "Select endpoint IDs only for this declared interface intent. Return strict JSON with exactly source_id and target_id. Choose source_id from source_member_outputs and target_id from platform_outputs. Do not return an edge, wrapper, obligation_id, or explanation."
    else:
        payload["source_member_outputs"] = _public_script_outputs(registry, obligation["source_member"])
        payload["target_member_inputs"] = _public_script_inputs(registry, obligation["target_member"])
        prompt = "Select endpoint IDs only for this declared interface intent. Return strict JSON with exactly source_id and target_id. Choose source_id from source_member_outputs and target_id from target_member_inputs. Do not return an edge, wrapper, obligation_id, or explanation."
    if validation_issue:
        payload.update(validation_issue)
    text = await model_call([{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}], planner_model)
    return _validate_interface_selection_protocol(obligation=obligation, response=_parse_object(text, "invalid_interface_endpoint_protocol"))


def _materialize_interface_obligation(*, obligation: dict, selection: dict, registry: dict, state: GraphExpansionState) -> dict:
    selection = _validate_interface_selection_protocol(obligation=obligation, response=selection)
    kind = obligation["kind"]
    if kind == "platform_to_script":
        source = next((value for value in registry["platform_inputs"] if value["slot_id"] == selection["source_id"]), None)
        target = next((value for value in registry["script_inputs"] if value["input_id"] == selection["target_id"] and value["target_file"] == obligation["target_member"]), None)
        if source is None or target is None:
            raise ResponsibilityGraphExpansionError("selected endpoint is outside declared interface obligation scope", code="invalid_interface_endpoint_reference")
        if _types_conflict(source.get("contract") or {}, target.get("contract") or {}):
            raise ResponsibilityGraphExpansionError("selected endpoints have conflicting types", code="interface_endpoint_type_conflict")
        constraints = [{"type": "platform_parameter_binding", "source_key": ".".join(selection["path"]), "source_path": list(selection["path"]), "required": True}] if selection["path"] else []
        return _edge(PLATFORM_INPUT_NODE, source["field"], target["target_file"], target["port_id"], constraints=constraints)
    if kind == "script_to_platform":
        source = next((value for value in registry["script_outputs"] if value["output_id"] == selection["source_id"] and value["target_file"] == obligation["source_member"]), None)
        target = next((value for value in registry["platform_outputs"] if value["slot_id"] == selection["target_id"]), None)
        if source is None or target is None:
            raise ResponsibilityGraphExpansionError("selected endpoint is outside declared interface obligation scope", code="invalid_interface_endpoint_reference")
        if _types_conflict(source.get("contract") or {}, target.get("contract") or {}):
            raise ResponsibilityGraphExpansionError("selected endpoints have conflicting types", code="interface_endpoint_type_conflict")
        return _edge(source["target_file"], source["port_id"], PLATFORM_OUTPUT_NODE, target["field"])
    source = next((value for value in registry["script_outputs"] if value["output_id"] == selection["source_id"] and value["target_file"] == obligation["source_member"]), None)
    target = next((value for value in registry["script_inputs"] if value["input_id"] == selection["target_id"] and value["target_file"] == obligation["target_member"]), None)
    if source is None or target is None:
        raise ResponsibilityGraphExpansionError("selected endpoint is outside declared interface obligation scope", code="invalid_interface_endpoint_reference")
    if source["target_file"] == target["target_file"]:
        raise ResponsibilityGraphExpansionError("self connection is forbidden", code="invalid_graph_endpoint")
    if _would_cycle(state.committed_edges, source["target_file"], target["target_file"]):
        raise ResponsibilityGraphExpansionError("selected source forms a directed cycle", code="responsibility_graph_cycle")
    if _types_conflict(source.get("contract") or {}, target.get("contract") or {}):
        raise ResponsibilityGraphExpansionError("selected endpoints have conflicting types", code="interface_endpoint_type_conflict")
    return _edge(source["target_file"], source["port_id"], target["target_file"], target["port_id"])


def _validate_platform_terminal_edges(*, terminal_edges: list[dict], platform_contract: dict) -> None:
    selected_fields: set[str] = set()
    for edge in terminal_edges:
        field = str(edge.get("to_input") or "")
        if field in selected_fields:
            raise ResponsibilityGraphExpansionError("a platform output slot may have only one source", code="duplicate_terminal_provenance", details={"platform_output_field": field})
        selected_fields.add(field)
    required = _boundary(platform_contract).get("required_final_output_fields")
    required_fields = {_port(value)[0] for value in required if _port(value)[0]} if isinstance(required, list) else set()
    if not terminal_edges:
        details: dict[str, Any] = {"missing_required_final_output_fields": sorted(required_fields)}
        if not required_fields:
            details["missing_platform_output_interface"] = True
        raise ResponsibilityGraphExpansionError("interface plan must declare at least one platform output", code="interface_plan_incomplete", details=details)
    missing = sorted(required_fields - selected_fields)
    if missing:
        raise ResponsibilityGraphExpansionError("terminal selection does not cover every required platform output", code="interface_plan_incomplete", details={"missing_required_final_output_fields": missing})


async def _expand_from_interface_plan(*, normalized: list[dict], platform_contract: dict, registry: dict, interface_plan: dict, planner_model: str, goal_context: dict, model_call: ModelCall) -> list[dict]:
    obligations = build_graph_obligations_from_interfaces(interface_plan=interface_plan)
    logger.info("[Creator][graph_expansion] mode=function_item_interface_expansion obligation_count=%d", len(obligations))
    item_by_target = {item["target_file"]: item for item in normalized}
    state = GraphExpansionState(active_nodes=set(item_by_target), activation_order=list(item_by_target))
    retries = model_calls = 0
    async def counted(messages: list[dict[str, str]], model: str) -> str:
        nonlocal model_calls
        model_calls += 1
        return await model_call(messages, model)
    for obligation in obligations:
        issue = None
        for attempt in range(2):
            try:
                selection = await _select_interface_endpoint_reference(obligation=obligation, registry=registry, goal_context=goal_context, committed_edges=state.committed_edges, planner_model=planner_model, model_call=counted, validation_issue=issue)
                edge = _materialize_interface_obligation(obligation=obligation, selection=selection, registry=registry, state=state)
                _validate_transaction(state.committed_edges + [edge], normalized)
            except ValueError as exc:
                if attempt:
                    if isinstance(exc, ResponsibilityGraphExpansionError):
                        raise exc
                    raise ResponsibilityGraphExpansionError("current interface obligation failed after one retry", code="graph_expansion_selection_failed", details={"obligation_id": obligation["obligation_id"], "validation_error": str(exc)}) from exc
                retries += 1
                issue = {"current_goal": obligation.get("goal", ""), "previous_selection": locals().get("selection", {}), "validation_error": {"code": getattr(exc, "code", "invalid_interface_endpoint_reference"), "details": getattr(exc, "details", {})}, "instruction": "Replace only the endpoint selection for the current interface."}
                continue
            state.committed_edges.append(edge)
            break
    terminals = [edge for edge in state.committed_edges if edge["to_node"] == PLATFORM_OUTPUT_NODE]
    _validate_platform_terminal_edges(terminal_edges=terminals, platform_contract=platform_contract)
    edges = _finalize_graph(state=state, function_items=normalized, terminal_edges=terminals)
    logger.info("[Creator][graph_expansion] inactive_function_items=[] model_call_count=%d selection_retry_count=%d graph_valid=true", model_calls, retries)
    return edges

async def expand_responsibility_graph(*, function_items: list[dict], platform_contract: dict, planner_model: str, goal_context: dict | None = None, model_call: ModelCall | None = None, interface_plan: dict) -> list[dict]:
    """Select endpoint references from a required FunctionItem interface plan."""
    if model_call is None:
        from ..creator_model_profiles import complete_creator_role_once
        async def model_call(messages: list[dict[str, str]], model: str) -> str:
            return await complete_creator_role_once(messages, "planner", fallback_model=model)
    normalized = normalize_structured_function_items(function_items, source="graph_expansion")
    context = dict(goal_context or {})
    registry = build_endpoint_registry(function_items=normalized, platform_contract=platform_contract)
    logger.info(
        "[Creator][graph_expansion] mode=function_item_interface_expansion script_output_count=%d platform_input_count=%d platform_output_count=%d",
        len(registry["script_outputs"]), len(registry["platform_inputs"]), len(registry["platform_outputs"]),
    )
    return await _expand_from_interface_plan(
        normalized=normalized, platform_contract=platform_contract, registry=registry,
        interface_plan=interface_plan, planner_model=planner_model,
        goal_context=context, model_call=model_call,
    )
