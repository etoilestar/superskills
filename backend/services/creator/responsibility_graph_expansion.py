"""Interface-intent ResponsibilityGraph expansion using scoped endpoint IDs."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .function_item_interface_plan import (
    AUTHORITY_CONTRACT,
    build_graph_obligations_from_interfaces,
    runtime_input_source_facts,
    required_platform_output_fields,
)
from ..skill_plan import GraphValidationError, normalize_structured_function_items, validate_structured_responsibility_edge_transport
from ..platform_io_contract import get_platform_output_sink, normalize_platform_output_sinks

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
                facts = runtime_input_source_facts(raw_input, item.get("default_values"))
                inputs.append({"input_id": f"IN{len(inputs) + 1:04d}", "node_id": node_id, "target_file": item["target_file"], "port_id": port_id, "node_purpose": item.get("purpose", ""), "description": description, "contract": contract, **facts})
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
    for sink in normalize_platform_output_sinks(platform_contract):
        platform_outputs.append({"slot_id": f"POUT{len(platform_outputs) + 1:04d}", "field": sink["name"], "description": "", "contract": sink["value_schema"], "sink_contract": sink})
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


def _validate_transaction(edges: list[dict], function_items: list[dict], platform_contract: dict | None = None) -> None:
    validate_structured_responsibility_edge_transport(edges, function_items=function_items, source="graph_expansion", platform_contract=platform_contract)
    incoming: set[tuple[str, str]] = set()
    for edge in edges:
        others = [value for value in edges if value is not edge]
        if _would_cycle(others, str(edge["from_node"]), str(edge["to_node"])):
            raise ResponsibilityGraphExpansionError("responsibility graph contains a directed cycle", code="responsibility_graph_cycle")
        if edge["to_node"] != PLATFORM_OUTPUT_NODE:
            key = (str(edge["to_node"]), str(edge["to_input"]))
            if key in incoming:
                raise ResponsibilityGraphExpansionError("ordinary logical input has duplicate provenance", code="duplicate_input_provenance", details={"target_member": key[0], "target_input": key[1]})
            incoming.add(key)


def _finalize_graph(*, state: GraphExpansionState, function_items: list[dict], terminal_edges: list[dict], platform_contract: dict | None = None) -> list[dict]:
    _validate_transaction(state.committed_edges, function_items, platform_contract)
    actual_terminals = [edge for edge in state.committed_edges if edge["to_node"] == PLATFORM_OUTPUT_NODE]
    if actual_terminals != terminal_edges or not actual_terminals:
        raise ResponsibilityGraphExpansionError("terminal set changed during expansion", code="invalid_terminal_closure")
    items = {item["target_file"]: item for item in normalize_structured_function_items(function_items, source="graph_expansion")}
    incoming = {(edge["to_node"], edge["to_input"]) for edge in state.committed_edges}
    unresolved = []
    for node in state.activation_order:
        for raw_input in items[node].get("inputs") or []:
            port_id = _port(raw_input)[0]
            facts = runtime_input_source_facts(raw_input, items[node].get("default_values"))
            if port_id and facts["runtime_source_required"] and (node, port_id) not in incoming:
                unresolved.append({"target": node, "input_id": port_id, "required": True, "default_present": False})
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



def _validate_interface_selection_protocol(*, obligation: dict, response: Any) -> dict:
    if not isinstance(response, dict):
        raise ResponsibilityGraphExpansionError(
            "endpoint selection must be a JSON object",
            code="invalid_interface_endpoint_protocol",
            details={
                "path": "$",
                "expected_type": "object",
                "observed_type": type(response).__name__,
                "observed_value": response,
                "observed_response": response,
            },
        )

    status = response.get("status")
    if status == "unbound":
        if set(response) != {"status", "reason"} or not isinstance(response.get("reason"), str) or not response["reason"].strip():
            raise ResponsibilityGraphExpansionError("unbound selection has invalid protocol", code="invalid_interface_endpoint_protocol", details={"path": "$", "observed_response": response})
        return {"status": "unbound", "reason": response["reason"].strip()}
    if status != "bound":
        raise ResponsibilityGraphExpansionError("endpoint selection status must be bound or unbound", code="invalid_interface_endpoint_protocol", details={"path": "$.status", "observed_response": response})

    kind = obligation.get("kind")
    expected = {"status", "source_id", "target_id", "source_path"} if kind == "platform_to_script" else {"status", "source_id", "target_id"}

    if set(response) != expected:
        raise ResponsibilityGraphExpansionError(
            "endpoint selection fields do not match the current interface kind",
            code="invalid_interface_endpoint_protocol",
            details={
                "path": "$",
                "expected_fields": sorted(expected),
                "observed_fields": sorted(response),
                "observed_value": response,
                "observed_response": response,
            },
        )

    source_id = response.get("source_id")
    target_id = response.get("target_id")

    if not isinstance(source_id, str) or not source_id:
        raise ResponsibilityGraphExpansionError(
            "source_id must be a non-empty string",
            code="invalid_interface_endpoint_protocol",
            details={
                "path": "$.source_id",
                "expected_type": "non-empty string",
                "observed_type": type(source_id).__name__,
                "observed_value": source_id,
                "observed_response": response,
            },
        )

    if not isinstance(target_id, str) or not target_id:
        raise ResponsibilityGraphExpansionError(
            "target_id must be a non-empty string",
            code="invalid_interface_endpoint_protocol",
            details={
                "path": "$.target_id",
                "expected_type": "non-empty string",
                "observed_type": type(target_id).__name__,
                "observed_value": target_id,
                "observed_response": response,
            },
        )

    if kind == "platform_to_script":
        source_path = response.get("source_path")

        if not isinstance(source_path, list):
            raise ResponsibilityGraphExpansionError(
                "source_path must be a JSON array of zero or more non-empty strings",
                code="invalid_interface_endpoint_protocol",
                details={
                    "path": "$.source_path",
                    "expected_type": "array<string>",
                    "observed_type": type(source_path).__name__,
                    "observed_value": source_path,
                    "observed_response": response,
                    "direct_binding_example": [],
                },
            )

        invalid_parts = [
            part
            for part in source_path
            if (
                not isinstance(part, str)
                or not part
                or part.lower() in _DANGEROUS_PATH_PARTS
            )
        ]

        if invalid_parts:
            raise ResponsibilityGraphExpansionError(
                "source_path contains an invalid path component",
                code="invalid_interface_endpoint_protocol",
                details={
                    "path": "$.source_path",
                    "expected_type": "array of safe non-empty strings",
                    "observed_type": "array",
                    "observed_value": source_path,
                    "invalid_parts": invalid_parts,
                    "observed_response": response,
                },
            )

    return dict(response)

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
        source_path = list(selection["source_path"])
        constraints = []
        if source_path:
            constraints.append({"type": "platform_parameter_binding", "source_key": ".".join(source_path), "source_path": source_path, "required": True})
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


def _resolve_declared_logical_binding(*, obligation: dict, registry: dict) -> dict:
    """Resolve frozen logical port identities to opaque registry IDs."""
    kind = obligation["kind"]
    if kind == "platform_to_script":
        source = next((value for value in registry["platform_inputs"] if value["field"] == obligation["source_platform_input"]), None)
        target = next((value for value in registry["script_inputs"] if value["target_file"] == obligation["target_member"] and value["port_id"] == obligation["target_input"]), None)
        selection = {"status": "bound", "source_id": source["slot_id"] if source else "", "target_id": target["input_id"] if target else "", "source_path": list(obligation["source_path"])}
    elif kind == "script_to_platform":
        source = next((value for value in registry["script_outputs"] if value["target_file"] == obligation["source_member"] and value["port_id"] == obligation["source_output"]), None)
        target = next((value for value in registry["platform_outputs"] if value["field"] == obligation["target_platform_output"]), None)
        selection = {"status": "bound", "source_id": source["output_id"] if source else "", "target_id": target["slot_id"] if target else ""}
    else:
        source = next((value for value in registry["script_outputs"] if value["target_file"] == obligation["source_member"] and value["port_id"] == obligation["source_output"]), None)
        target = next((value for value in registry["script_inputs"] if value["target_file"] == obligation["target_member"] and value["port_id"] == obligation["target_input"]), None)
        selection = {"status": "bound", "source_id": source["output_id"] if source else "", "target_id": target["input_id"] if target else ""}
    if source is None or target is None:
        raise ResponsibilityGraphExpansionError("declared logical binding has no endpoint identity", code="interface_endpoint_unbound", details={"interface_id": obligation.get("interface_id", ""), "goal": obligation.get("goal", ""), "logical_binding": obligation})
    logger.info("[Creator][graph_materialization] interface_id=%s logical_source_ref=%s resolved_source_endpoint=%s logical_target_ref=%s resolved_target_endpoint=%s", obligation.get("interface_id", ""), obligation.get("source_output") or obligation.get("source_platform_input"), selection["source_id"], obligation.get("target_input") or obligation.get("target_platform_output"), selection["target_id"])
    return selection


def _validate_platform_terminal_edges(*, terminal_edges: list[dict], platform_contract: dict) -> None:
    selected_fields: set[str] = set()
    for edge in terminal_edges:
        field = str(edge.get("to_input") or "")
        sink = get_platform_output_sink(platform_contract, field)
        if field in selected_fields and (not sink or sink["cardinality"] == "one"):
            raise ResponsibilityGraphExpansionError("a platform output slot may have only one source", code="duplicate_terminal_provenance", details={"platform_output_field": field})
        selected_fields.add(field)
    legal_fields = {sink["name"] for sink in normalize_platform_output_sinks(platform_contract)}
    required_fields = required_platform_output_fields(platform_contract)
    invalid_required = sorted(required_fields - legal_fields)
    if invalid_required:
        raise ResponsibilityGraphExpansionError("required platform output is outside legal domain", code="invalid_required_platform_output", details={"invalid_required_final_output_fields": invalid_required, "legal_final_output_fields": sorted(legal_fields)})
    if not terminal_edges:
        details: dict[str, Any] = {"missing_required_final_output_fields": sorted(required_fields)}
        if not required_fields:
            details["missing_platform_output_interface"] = True
        raise ResponsibilityGraphExpansionError("interface plan must declare at least one platform output", code="interface_plan_incomplete", details=details)
    missing = sorted(required_fields - selected_fields)
    if missing:
        raise ResponsibilityGraphExpansionError("terminal selection does not cover every required platform output", code="interface_plan_incomplete", details={"missing_required_final_output_fields": missing})


def validate_responsibility_graph_candidate(
    *, function_items: list[dict], platform_contract: dict, interface_plan: dict,
) -> list[dict]:
    """Materialize and authoritatively validate an Interface Plan's graph."""
    normalized = normalize_structured_function_items(function_items, source="graph_expansion")
    registry = build_endpoint_registry(
        function_items=normalized, platform_contract=platform_contract,
    )
    logger.info(
        "[Creator][graph_expansion] mode=function_item_interface_expansion script_output_count=%d platform_input_count=%d platform_output_count=%d",
        len(registry["script_outputs"]), len(registry["platform_inputs"]), len(registry["platform_outputs"]),
    )
    obligations = build_graph_obligations_from_interfaces(interface_plan=interface_plan)
    logger.info("[Creator][graph_expansion] mode=function_item_interface_expansion obligation_count=%d", len(obligations))
    item_by_target = {item["target_file"]: item for item in normalized}
    state = GraphExpansionState(active_nodes=set(item_by_target), activation_order=list(item_by_target))
    model_calls = 0
    for obligation in obligations:
        selection = _resolve_declared_logical_binding(obligation=obligation, registry=registry)
        edge = _materialize_interface_obligation(obligation=obligation, selection=selection, registry=registry, state=state)
        _validate_transaction(state.committed_edges + [edge], normalized, platform_contract)
        state.committed_edges.append(edge)
    terminals = [edge for edge in state.committed_edges if edge["to_node"] == PLATFORM_OUTPUT_NODE]
    _validate_platform_terminal_edges(terminal_edges=terminals, platform_contract=platform_contract)
    edges = _finalize_graph(state=state, function_items=normalized, terminal_edges=terminals, platform_contract=platform_contract)
    logger.info("[Creator][graph_expansion] inactive_function_items=[] model_call_count=%d selection_retry_count=0 graph_valid=true", model_calls)
    return edges

async def expand_responsibility_graph(*, function_items: list[dict], platform_contract: dict, planner_model: str, goal_context: dict | None = None, model_call: ModelCall | None = None, interface_plan: dict) -> list[dict]:
    """Select endpoint references from a required FunctionItem interface plan."""
    return validate_responsibility_graph_candidate(
        function_items=function_items, platform_contract=platform_contract,
        interface_plan=interface_plan,
    )
