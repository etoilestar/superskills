"""FunctionItem Interface Intent Plan protocol for Creator graph planning.

The Blueprint already decomposes the complete system into frozen executable
FunctionItems. This module plans and validates only semantic interaction intents
between those already-frozen FunctionItems and the platform; it does not create
another subsystem decomposition or infer business semantics.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..skill_plan import GraphValidationError, normalize_structured_function_items

logger = logging.getLogger(__name__)
ModelCall = Callable[[list[dict[str, str]], str], Awaitable[str]]
INTERFACE_KINDS = {"platform_to_member", "member_to_member", "member_to_platform"}
INTERFACE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["interfaces"],
    "properties": {
        "interfaces": {
            "type": "array",
            "items": {
                "oneOf": [
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["interface_id", "kind", "goal", "target_member"],
                        "properties": {
                            "interface_id": {"type": "string", "minLength": 1},
                            "kind": {"const": "platform_to_member"},
                            "goal": {"type": "string", "minLength": 1},
                            "target_member": {"type": "string", "minLength": 1},
                        },
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["interface_id", "kind", "goal", "source_member", "target_member"],
                        "properties": {
                            "interface_id": {"type": "string", "minLength": 1},
                            "kind": {"const": "member_to_member"},
                            "goal": {"type": "string", "minLength": 1},
                            "source_member": {"type": "string", "minLength": 1},
                            "target_member": {"type": "string", "minLength": 1},
                        },
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["interface_id", "kind", "goal", "source_member"],
                        "properties": {
                            "interface_id": {"type": "string", "minLength": 1},
                            "kind": {"const": "member_to_platform"},
                            "goal": {"type": "string", "minLength": 1},
                            "source_member": {"type": "string", "minLength": 1},
                        },
                    },
                ]
            },
        }
    },
}


class InterfaceIntentPlanError(GraphValidationError):
    """Machine-readable Interface Intent Plan failure."""



def _strip_single_json_fence(text: str) -> str:
    stripped = str(text or "").strip()
    if not stripped.startswith("```") or not stripped.endswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 3 or lines[0].strip().lower() not in {"```", "```json"} or lines[-1].strip() != "```":
        return stripped
    return "\n".join(lines[1:-1]).strip()


def _parse_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(_strip_single_json_fence(text))
    except (TypeError, json.JSONDecodeError) as exc:
        raise InterfaceIntentPlanError("interface plan must be strict JSON", code="invalid_interface_plan_json", details={"path": "$"}) from exc
    if not isinstance(value, dict):
        raise InterfaceIntentPlanError("interface plan must be a JSON object", code="invalid_interface_plan_protocol", details={"path": "$"})
    return value


def _require_nonempty_string(value: dict[str, Any], key: str, code: str, path: str) -> str:
    raw = value.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise InterfaceIntentPlanError(f"{key} must be a non-empty string", code=code, details={"path": path})
    return raw.strip()


def _raise(message: str, code: str, *, path: str, **details: Any) -> None:
    payload = {"path": path, **{k: v for k, v in details.items() if v is not None}}
    raise InterfaceIntentPlanError(message, code=code, details=payload)


def _compact_function_items(function_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = normalize_structured_function_items(function_items, source="interface_intent_plan")
    return [
        {
            "target_file": item["target_file"],
            "purpose": item.get("purpose", ""),
            "inputs": item.get("inputs") or [],
            "outputs": item.get("outputs") or [],
            "dependencies": item.get("dependencies") or [],
        }
        for item in normalized
    ]


def validate_interface_intent_plan(*, plan: dict[str, Any], function_items: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate interface-intent protocol and references only."""
    if not isinstance(plan, dict) or set(plan) != {"interfaces"}:
        _raise("interface plan must contain only interfaces", "invalid_interface_plan_protocol", path="$")
    interfaces = plan.get("interfaces")
    if not isinstance(interfaces, list):
        _raise("interfaces must be a list", "invalid_interface_plan_protocol", path="$.interfaces")
    frozen_targets = {item["target_file"] for item in normalize_structured_function_items(function_items, source="interface_intent_plan")}
    seen_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    counts = {"platform_to_member": 0, "member_to_member": 0, "member_to_platform": 0}
    for index, raw_interface in enumerate(interfaces):
        path = f"$.interfaces[{index}]"
        if not isinstance(raw_interface, dict):
            _raise("interface must be an object", "invalid_interface_protocol", path=path)
        kind = raw_interface.get("kind")
        if kind not in INTERFACE_KINDS:
            _raise("interface kind is invalid", "invalid_interface_kind", path=f"{path}.kind")
        expected = {"interface_id", "kind", "goal", "target_member"} if kind == "platform_to_member" else {"interface_id", "kind", "goal", "source_member", "target_member"} if kind == "member_to_member" else {"interface_id", "kind", "goal", "source_member"}
        if set(raw_interface) != expected:
            _raise("interface fields do not match kind schema", "invalid_interface_protocol", path=path, expected=sorted(expected), observed=sorted(raw_interface))
        interface_id = _require_nonempty_string(raw_interface, "interface_id", "invalid_interface_protocol", f"{path}.interface_id")
        if interface_id in seen_ids:
            _raise("duplicate interface_id", "duplicate_interface_id", path=f"{path}.interface_id", interface_id=interface_id)
        seen_ids.add(interface_id)
        _require_nonempty_string(raw_interface, "goal", "invalid_interface_protocol", f"{path}.goal")
        interface = dict(raw_interface)
        if kind in {"member_to_member", "member_to_platform"}:
            source = _require_nonempty_string(raw_interface, "source_member", "invalid_interface_member", f"{path}.source_member")
            if source not in frozen_targets:
                _raise("source_member must reference a frozen FunctionItem", "unknown_interface_member", path=f"{path}.source_member", target=source)
        if kind in {"platform_to_member", "member_to_member"}:
            target = _require_nonempty_string(raw_interface, "target_member", "invalid_interface_member", f"{path}.target_member")
            if target not in frozen_targets:
                _raise("target_member must reference a frozen FunctionItem", "unknown_interface_member", path=f"{path}.target_member", target=target)
        if kind == "member_to_member" and interface["source_member"] == interface["target_member"]:
            _raise("member_to_member self connection is forbidden", "interface_self_connection", path=path, target=interface["source_member"])
        counts[kind] += 1
        normalized.append(interface)
    logger.info(
        "[Creator][interface_plan] interface_count=%d platform_input_interface_count=%d member_interface_count=%d platform_output_interface_count=%d",
        len(normalized), counts["platform_to_member"], counts["member_to_member"], counts["member_to_platform"],
    )
    return {"interfaces": normalized}


def build_graph_obligations_from_interfaces(*, interface_plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert declared interface intents to local graph obligations."""
    obligations: list[dict[str, Any]] = []
    for interface in interface_plan.get("interfaces") or []:
        obligation = {
            "obligation_id": f"O{len(obligations) + 1:04d}",
            "interface_id": interface["interface_id"],
            "goal": interface["goal"],
        }
        if interface["kind"] == "platform_to_member":
            obligation.update({"kind": "platform_to_script", "target_member": interface["target_member"]})
        elif interface["kind"] == "member_to_member":
            obligation.update({"kind": "script_to_script", "source_member": interface["source_member"], "target_member": interface["target_member"]})
        else:
            obligation.update({"kind": "script_to_platform", "source_member": interface["source_member"]})
        obligations.append(obligation)
    return obligations


def _interface_plan_prompt() -> str:
    return f"""
You are planning semantic interfaces between already-frozen executable FunctionItems.

The complete system has already been decomposed. Each supplied FunctionItem is one atomic executable subsystem.

Do not create another subsystem decomposition. Do not add, remove, rename, merge, split, group, or duplicate FunctionItems.

Your only task is to declare the necessary interaction directions:
1. platform input to a FunctionItem;
2. one FunctionItem to another FunctionItem;
3. a FunctionItem to platform output.

Each interaction must appear exactly once as one interface object.

Do not repeat FunctionItem purposes, inputs, outputs, dependencies, capabilities, execution commands, paths, argv contracts, placeholders, or port declarations.
Do not select endpoint IDs. Do not generate ResponsibilityEdges. Do not infer from filenames, suffixes, roles, or business keyword tables.
Use the Blueprint goal, FunctionItem purposes, declared inputs/outputs, requirement allocations, and platform contract as semantic evidence.

Return only strict JSON matching this schema. No markdown unless the transport wraps the single JSON object in one json fence.
Schema:
{json.dumps(INTERFACE_SCHEMA, ensure_ascii=False)}
""".strip()


async def _reformat_interface_plan_response(*, raw_response: str, validation_error: InterfaceIntentPlanError, planner_model: str, model_call: ModelCall) -> dict[str, Any]:
    logger.info("[Creator][interface_protocol_repair] attempt=1 error_paths=%s", [validation_error.details.get("path", "$")])
    prompt = "Return only a corrected JSON object that matches the supplied schema. Do not reinterpret the Blueprint and do not add semantic conclusions; only reformat the original response."
    payload = {"schema": INTERFACE_SCHEMA, "raw_response": raw_response, "validation_error": {"code": validation_error.code, "details": validation_error.details, "message": str(validation_error)}}
    text = await model_call([{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}], planner_model)
    return _parse_object(text)


async def plan_function_item_interfaces(*, original_user_goal: str, frozen_blueprint: str, frozen_function_items: list[dict[str, Any]], requirement_allocations: list[dict[str, Any]] | None = None, requirement_channels: dict[str, str] | None = None, platform_contract: dict[str, Any] | None = None, planner_model: str, model_call: ModelCall) -> dict[str, Any]:
    """Ask the model for interaction intents between frozen FunctionItems."""
    payload = {
        "system_goal": original_user_goal,
        "function_items": _compact_function_items(frozen_function_items),
        "executable_requirement_allocations": [
            allocation
            for allocation in (requirement_allocations or [])
            if requirement_channels and requirement_channels.get(str(allocation.get("requirement_id") or "")) == "executable"
        ],
        "platform_contract": platform_contract or {},
    }
    raw_response = await model_call([{"role": "system", "content": _interface_plan_prompt()}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}], planner_model)
    try:
        return validate_interface_intent_plan(plan=_parse_object(raw_response), function_items=frozen_function_items)
    except InterfaceIntentPlanError as exc:
        if exc.code in {"invalid_interface_plan_json", "invalid_interface_plan_protocol", "invalid_interface_protocol", "invalid_interface_kind"}:
            reformatted = await _reformat_interface_plan_response(raw_response=raw_response, validation_error=exc, planner_model=planner_model, model_call=model_call)
            return validate_interface_intent_plan(plan=reformatted, function_items=frozen_function_items)
        raise


async def repair_interface_intents(*, original_user_goal: str, frozen_blueprint: str, frozen_function_items: list[dict[str, Any]], current_interface_plan: dict[str, Any], validation_errors: list[dict[str, Any]], affected_members: list[str] | None = None, missing_platform_output_fields: list[str] | None = None, requirement_allocations: list[dict[str, Any]] | None = None, requirement_channels: dict[str, str] | None = None, platform_contract: dict[str, Any] | None = None, planner_model: str, model_call: ModelCall) -> dict[str, Any]:
    """Run one local semantic repair of interface intents."""
    logger.info("[Creator][interface_semantic_repair] affected_members=%s missing_platform_output_fields=%s", affected_members or [], missing_platform_output_fields or [])
    prompt = "Preserve every existing interface that is unrelated to affected_members or missing_platform_output_fields. Only add, delete, or modify interfaces required to resolve the supplied validation errors. Do not change FunctionItems. Do not redesign the complete interface plan. Return only JSON matching the interface schema."
    payload = {
        "system_goal": original_user_goal,
        "function_items": _compact_function_items(frozen_function_items),
        "current_interface_plan": current_interface_plan,
        "validation_errors": validation_errors,
        "affected_members": affected_members or [],
        "missing_platform_output_fields": missing_platform_output_fields or [],
        "executable_requirement_allocations": [
            allocation
            for allocation in (requirement_allocations or [])
            if requirement_channels and requirement_channels.get(str(allocation.get("requirement_id") or "")) == "executable"
        ],
        "platform_contract": platform_contract or {},
        "schema": INTERFACE_SCHEMA,
    }
    text = await model_call([{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}], planner_model)
    return validate_interface_intent_plan(plan=_parse_object(text), function_items=frozen_function_items)

