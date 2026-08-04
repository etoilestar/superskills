"""Goal-driven ResponsibilityGraph expansion using independent endpoint IDs."""

from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

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
    frontier: deque[dict] = field(default_factory=deque)
    resolved_obligation_ids: set[str] = field(default_factory=set)
    enqueued_inputs: set[tuple[str, str]] = field(default_factory=set)
    activation_order: list[str] = field(default_factory=list)
    next_obligation_number: int = 1
    terminal_references: list[dict] = field(default_factory=list)
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
    if isinstance(value, str) and value.strip().lower() not in {"", "unknown", "any", "object"}:
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
    for item in items:
        node_id = f"N{len(nodes) + 1:04d}"
        nodes.append({"node_id": node_id, "target_file": item["target_file"], "purpose": item.get("purpose", "")})
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
    return {"nodes": nodes, "script_outputs": outputs, "platform_inputs": platform_inputs, "platform_outputs": platform_outputs}


def _parse_object(text: str, code: str) -> dict:
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ResponsibilityGraphExpansionError("model response must be strict JSON", code=code) from exc
    if not isinstance(value, dict):
        raise ResponsibilityGraphExpansionError("model response must be a JSON object", code=code)
    return value


def validate_terminal_reference_protocol(*, response: Any) -> list[dict]:
    if not isinstance(response, dict) or set(response) != {"terminal_bindings"} or not isinstance(response["terminal_bindings"], list):
        raise ResponsibilityGraphExpansionError("terminal response must contain only terminal_bindings", code="invalid_terminal_reference_protocol")
    selections = response["terminal_bindings"]
    if not selections:
        raise ResponsibilityGraphExpansionError("at least one terminal binding is required", code="invalid_terminal_reference_protocol")
    for selection in selections:
        if not isinstance(selection, dict) or set(selection) != {"source_output_id", "platform_output_slot_id"} or not all(isinstance(selection.get(key), str) and selection[key] for key in ("source_output_id", "platform_output_slot_id")):
            raise ResponsibilityGraphExpansionError("terminal bindings must contain only endpoint IDs", code="invalid_terminal_reference_protocol")
    return [dict(value) for value in selections]


def validate_and_materialize_terminal_bindings(*, selections: list[dict], registry: dict, function_items: list[dict], platform_contract: dict) -> list[dict]:
    """Validate only selected endpoint pairs, then materialize terminal edges."""
    selections = validate_terminal_reference_protocol(
        response={"terminal_bindings": selections}
    )
    items = normalize_structured_function_items(function_items, source="graph_expansion")
    frozen = {item["target_file"] for item in items}
    outputs = {value["output_id"]: value for value in registry.get("script_outputs") or []}
    slots = {value["slot_id"]: value for value in registry.get("platform_outputs") or []}
    if not selections:
        raise ResponsibilityGraphExpansionError("at least one terminal binding is required", code="invalid_terminal_reference_protocol")
    used_slots: set[str] = set()
    edges = []
    for selection in selections:
        output = outputs.get(selection.get("source_output_id"))
        slot = slots.get(selection.get("platform_output_slot_id"))
        if output is None or output.get("target_file") not in frozen:
            raise ResponsibilityGraphExpansionError("unknown frozen script output ID", code="invalid_terminal_reference", details={"source_output_id": selection.get("source_output_id")})
        if slot is None:
            raise ResponsibilityGraphExpansionError("unknown platform output slot ID", code="invalid_terminal_reference", details={"platform_output_slot_id": selection.get("platform_output_slot_id")})
        if slot["slot_id"] in used_slots:
            raise ResponsibilityGraphExpansionError("a platform output slot may have only one source", code="duplicate_terminal_provenance")
        if _types_conflict(output.get("contract") or {}, slot.get("contract") or {}):
            raise ResponsibilityGraphExpansionError("selected terminal endpoints have conflicting types", code="terminal_type_conflict")
        used_slots.add(slot["slot_id"])
        edges.append(_edge(output["target_file"], output["port_id"], PLATFORM_OUTPUT_NODE, slot["field"]))
    required = _boundary(platform_contract).get("required_final_output_fields")
    if isinstance(required, list):
        required_fields = {_port(value)[0] for value in required if _port(value)[0]}
        selected_fields = {slots[value["platform_output_slot_id"]]["field"] for value in selections if value.get("platform_output_slot_id") in slots}
        missing = sorted(required_fields - selected_fields)
        if missing:
            raise ResponsibilityGraphExpansionError("terminal selection does not cover every required platform output", code="missing_required_terminal_binding", details={"missing_required_final_output_fields": missing})
    return edges


async def select_terminal_references(*, registry: dict, goal_context: dict, planner_model: str, model_call: ModelCall, validation_issue: dict | None = None) -> list[dict]:
    public_outputs = [{key: value[key] for key in ("output_id", "node_id", "node_purpose", "port_id", "description", "contract")} for value in registry["script_outputs"]]
    public_slots = [dict(value) for value in registry["platform_outputs"]]
    if not public_outputs or not public_slots:
        raise ResponsibilityGraphExpansionError("no terminal endpoint exists", code="unresolved_terminal_binding")
    payload: dict[str, Any] = {"goal_context": goal_context, "script_outputs": public_outputs, "platform_outputs": public_slots}
    if validation_issue:
        payload.update(validation_issue)
    prompt = "Select the minimum terminal endpoint references satisfying the final goal. Return strict JSON containing only terminal_bindings with source_output_id and platform_output_slot_id. Do not create endpoints, edges, or explanations."
    text = await model_call([{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], planner_model)
    return validate_terminal_reference_protocol(response=_parse_object(text, "invalid_terminal_reference_protocol"))


def enqueue_required_inputs_for_node(*, node_id: str, function_items: list[dict], state: GraphExpansionState) -> None:
    if node_id not in state.active_nodes:
        return
    item = next((value for value in normalize_structured_function_items(function_items, source="graph_expansion") if value["target_file"] == node_id), None)
    if item is None:
        return
    defaults = item.get("default_values") or {}
    incoming = {(edge["to_node"], edge["to_input"]) for edge in state.committed_edges}
    for input_name in item.get("inputs") or []:
        key = (node_id, str(input_name))
        if input_name in defaults or key in incoming or key in state.enqueued_inputs:
            continue
        state.frontier.append({"obligation_id": f"O{state.next_obligation_number:04d}", "target": {"node_id": node_id, "port_id": str(input_name)}, "target_context": {"node_purpose": item.get("purpose", ""), "input_name": str(input_name), "input_contract": {}}})
        state.next_obligation_number += 1
        state.enqueued_inputs.add(key)


def initialize_state_from_terminals(*, terminal_selections: list[dict], terminal_edges: list[dict], function_items: list[dict]) -> GraphExpansionState:
    state = GraphExpansionState(terminal_references=[dict(value) for value in terminal_selections], committed_edges=[dict(value) for value in terminal_edges])
    for edge in terminal_edges:
        node = edge["from_node"]
        if node not in state.active_nodes:
            state.active_nodes.add(node)
            state.activation_order.append(node)
            enqueue_required_inputs_for_node(node_id=node, function_items=function_items, state=state)
    return state


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


def validate_input_source_reference_protocol(*, obligation: dict, response: Any) -> dict:
    if not isinstance(response, dict) or set(response) != {"selection"} or not isinstance(response["selection"], dict):
        raise ResponsibilityGraphExpansionError("input response must contain only selection", code="invalid_input_reference_protocol")
    selection = response["selection"]
    if selection.get("obligation_id") != obligation.get("obligation_id") or selection.get("source_kind") not in {"script_output", "platform_input"}:
        raise ResponsibilityGraphExpansionError("selection must identify the current obligation and a valid source_kind", code="invalid_input_reference_protocol")
    expected = {"obligation_id", "source_kind", "source_output_id"} if selection["source_kind"] == "script_output" else {"obligation_id", "source_kind", "platform_input_slot_id", "path"}
    if set(selection) != expected:
        raise ResponsibilityGraphExpansionError("selection contains invalid fields for source_kind", code="invalid_input_reference_protocol")
    if selection["source_kind"] == "script_output" and (not isinstance(selection.get("source_output_id"), str) or not selection["source_output_id"]):
        raise ResponsibilityGraphExpansionError("source_output_id must be non-empty", code="invalid_input_reference_protocol")
    if selection["source_kind"] == "platform_input":
        path = selection.get("path")
        if not isinstance(selection.get("platform_input_slot_id"), str) or not selection["platform_input_slot_id"] or not isinstance(path, list) or any(not isinstance(value, str) or not value or value.lower() in _DANGEROUS_PATH_PARTS for value in path):
            raise ResponsibilityGraphExpansionError("platform input slot or path is invalid", code="invalid_input_reference_protocol")
    return dict(selection)


def validate_and_materialize_input_binding(*, obligation: dict, selection: dict, registry: dict, state: GraphExpansionState, function_items: list[dict]) -> dict:
    """Validate and materialize only the one source reference actually selected."""
    selection = validate_input_source_reference_protocol(
        obligation=obligation,
        response={"selection": selection},
    )
    items = normalize_structured_function_items(function_items, source="graph_expansion")
    item_by_node = {item["target_file"]: item for item in items}
    target = obligation.get("target") or {}
    target_node, target_input = str(target.get("node_id") or ""), str(target.get("port_id") or "")
    if target_node not in state.active_nodes or target_node not in item_by_node or target_input not in item_by_node[target_node]["inputs"]:
        raise ResponsibilityGraphExpansionError("obligation target is not an active declared input", code="invalid_binding_target")
    if any(edge["to_node"] == target_node and edge["to_input"] == target_input for edge in state.committed_edges):
        raise ResponsibilityGraphExpansionError("input already has provenance", code="duplicate_input_provenance")
    target_contract = (obligation.get("target_context") or {}).get("input_contract") or {}
    if selection["source_kind"] == "script_output":
        output = next((value for value in registry["script_outputs"] if value["output_id"] == selection["source_output_id"]), None)
        if output is None:
            raise ResponsibilityGraphExpansionError("unknown script output ID", code="invalid_input_reference")
        if output["target_file"] == target_node:
            raise ResponsibilityGraphExpansionError("self connection is forbidden", code="invalid_graph_endpoint")
        if _would_cycle(state.committed_edges, output["target_file"], target_node):
            raise ResponsibilityGraphExpansionError("selected source forms a directed cycle", code="responsibility_graph_cycle")
        if _types_conflict(output.get("contract") or {}, target_contract):
            raise ResponsibilityGraphExpansionError("selected endpoints have conflicting types", code="input_type_conflict")
        return _edge(output["target_file"], output["port_id"], target_node, target_input)
    slot = next((value for value in registry["platform_inputs"] if value["slot_id"] == selection["platform_input_slot_id"]), None)
    if slot is None:
        raise ResponsibilityGraphExpansionError("unknown platform input slot ID", code="invalid_input_reference")
    path = list(selection["path"])
    constraints = []
    if path:
        constraints.append({"type": "platform_parameter_binding", "source_key": ".".join(path), "source_path": path, "required": True})
    return _edge(PLATFORM_INPUT_NODE, slot["field"], target_node, target_input, constraints=constraints)


async def select_input_source_reference(*, obligation: dict, registry: dict, goal_context: dict, committed_edges: list[dict], planner_model: str, model_call: ModelCall, validation_issue: dict | None = None) -> dict:
    payload: dict[str, Any] = {"goal_context": goal_context, "current_partial_graph": {"committed_edges": committed_edges}, "obligation": {"obligation_id": obligation["obligation_id"], "target_node": obligation["target"]["node_id"], "target_input": obligation["target"]["port_id"], "target_context": obligation.get("target_context") or {}}, "script_outputs": [{key: value[key] for key in ("output_id", "node_id", "node_purpose", "port_id", "description", "contract")} for value in registry["script_outputs"]], "platform_inputs": [dict(value) for value in registry["platform_inputs"]]}
    if validation_issue:
        payload.update(validation_issue)
    prompt = "Select one source reference for only the current input. Return strict JSON containing only selection. Use source_kind script_output with source_output_id, or platform_input with platform_input_slot_id and an open string-array path. Do not return an edge or explanation."
    text = await model_call([{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], planner_model)
    return validate_input_source_reference_protocol(obligation=obligation, response=_parse_object(text, "invalid_input_reference_protocol"))


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
    unresolved = [(node, name) for node in state.activation_order for name in items[node]["inputs"] if name not in (items[node].get("default_values") or {}) and (node, name) not in incoming]
    if unresolved:
        raise ResponsibilityGraphExpansionError("active graph has unresolved inputs", code="unresolved_binding_obligation", details={"unresolved": unresolved})
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


async def expand_responsibility_graph(*, function_items: list[dict], platform_contract: dict, planner_model: str, goal_context: dict | None = None, model_call: ModelCall | None = None) -> list[dict]:
    """Select endpoint references and expand activated inputs one at a time."""
    if model_call is None:
        from ..creator_model_profiles import complete_creator_role_once
        async def model_call(messages: list[dict[str, str]], model: str) -> str:
            return await complete_creator_role_once(messages, "planner", fallback_model=model)
    normalized = normalize_structured_function_items(function_items, source="graph_expansion")
    context = dict(goal_context or {})
    registry = build_endpoint_registry(function_items=normalized, platform_contract=platform_contract)
    logger.info("[Creator][graph_expansion] script_output_count=%d platform_input_count=%d platform_output_count=%d", len(registry["script_outputs"]), len(registry["platform_inputs"]), len(registry["platform_outputs"]))
    model_calls = retries = 0
    async def counted(messages: list[dict[str, str]], model: str) -> str:
        nonlocal model_calls
        model_calls += 1
        return await model_call(messages, model)
    try:
        terminal_issue = None
        for terminal_attempt in range(2):
            try:
                terminal_selections = await select_terminal_references(
                    registry=registry, goal_context=context,
                    planner_model=planner_model, model_call=counted,
                    validation_issue=terminal_issue,
                )
                terminal_edges = validate_and_materialize_terminal_bindings(
                    selections=terminal_selections, registry=registry,
                    function_items=normalized, platform_contract=platform_contract,
                )
                break
            except ValueError as exc:
                if terminal_attempt:
                    raise ResponsibilityGraphExpansionError(
                        "terminal selection failed after one retry",
                        code="terminal_selection_failed",
                        details={"validation_error": str(exc)},
                    ) from exc
                retries += 1
                terminal_issue = {
                    "validation_issue": {
                        "code": getattr(exc, "code", "invalid_terminal_reference"),
                        "details": getattr(exc, "details", {}),
                    }
                }
        state = initialize_state_from_terminals(terminal_selections=terminal_selections, terminal_edges=terminal_edges, function_items=normalized)
        logger.info("[Creator][graph_expansion] frontier_size=%d active_node_count=%d", len(state.frontier), len(state.active_nodes))
        while state.frontier:
            obligation = state.frontier.popleft()
            issue = None
            for attempt in range(2):
                try:
                    selection = await select_input_source_reference(obligation=obligation, registry=registry, goal_context=context, committed_edges=state.committed_edges, planner_model=planner_model, model_call=counted, validation_issue=issue)
                    edge = validate_and_materialize_input_binding(obligation=obligation, selection=selection, registry=registry, state=state, function_items=normalized)
                    _validate_transaction(state.committed_edges + [edge], normalized)
                except ValueError as exc:
                    if attempt:
                        raise ResponsibilityGraphExpansionError("current input failed after one retry", code="graph_expansion_selection_failed", details={"obligation_id": obligation["obligation_id"], "validation_error": str(exc)}) from exc
                    retries += 1
                    issue = {"previous_selection": locals().get("selection", {}), "validation_issue": {"code": getattr(exc, "code", "invalid_input_reference"), "affected_obligation_id": obligation["obligation_id"]}}
                    continue
                state.committed_edges.append(edge)
                state.resolved_obligation_ids.add(obligation["obligation_id"])
                if edge["from_node"] != PLATFORM_INPUT_NODE and edge["from_node"] not in state.active_nodes:
                    state.active_nodes.add(edge["from_node"])
                    state.activation_order.append(edge["from_node"])
                    enqueue_required_inputs_for_node(node_id=edge["from_node"], function_items=normalized, state=state)
                break
            logger.info("[Creator][graph_expansion] frontier_size=%d committed_edge_count=%d active_node_count=%d", len(state.frontier), len(state.committed_edges), len(state.active_nodes))
        edges = _finalize_graph(state=state, function_items=normalized, terminal_edges=terminal_edges)
        logger.info("[Creator][graph_expansion] inactive_function_items=[] model_call_count=%d selection_retry_count=%d graph_valid=true", model_calls, retries)
        return edges
    except Exception:
        logger.info("[Creator][graph_expansion] model_call_count=%d selection_retry_count=%d graph_valid=false", model_calls, retries)
        raise
