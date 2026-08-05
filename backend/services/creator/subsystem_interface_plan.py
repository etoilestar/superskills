"""Subsystem Interface Plan protocol for Creator graph planning.

This module intentionally validates only structure, references, ownership, and
scope relationships. It does not infer business semantics, subsystem taxonomy,
or endpoint bindings.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

from ..skill_plan import GraphValidationError, normalize_structured_function_items

logger = logging.getLogger(__name__)
ModelCall = Callable[[list[dict[str, str]], str], Awaitable[str]]


class SubsystemInterfacePlanError(GraphValidationError):
    """Machine-readable Subsystem Interface Plan protocol failure."""


def _strip_single_json_fence(text: str) -> str:
    stripped = str(text or "").strip()
    if not stripped.startswith("```") or not stripped.endswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 3 or not lines[0].strip().lower() in {"```", "```json"} or lines[-1].strip() != "```":
        return stripped
    return "\n".join(lines[1:-1]).strip()


def _parse_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(_strip_single_json_fence(text))
    except (TypeError, json.JSONDecodeError) as exc:
        raise SubsystemInterfacePlanError("subsystem plan must be strict JSON", code="invalid_subsystem_plan_json") from exc
    if not isinstance(value, dict):
        raise SubsystemInterfacePlanError("subsystem plan must be a JSON object", code="invalid_subsystem_plan_protocol")
    return value


def _require_keys(value: Any, *, keys: set[str], code: str, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise SubsystemInterfacePlanError(f"{label} has invalid fields", code=code)
    return value


def _require_nonempty_string(value: dict[str, Any], key: str, code: str) -> str:
    raw = value.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise SubsystemInterfacePlanError(f"{key} must be a non-empty string", code=code, details={"field": key})
    return raw.strip()


def _error(message: str, code: str, **details: Any) -> None:
    raise SubsystemInterfacePlanError(message, code=code, details={k: v for k, v in details.items() if v is not None})


def _validate_protocol_shape(plan: dict[str, Any]) -> None:
    _require_keys(plan, keys={"subsystems", "subsystem_links"}, code="invalid_subsystem_plan_protocol", label="subsystem plan")
    if not isinstance(plan["subsystems"], list) or not plan["subsystems"]:
        _error("subsystems must be a non-empty list", "invalid_subsystem_plan_protocol")
    if not isinstance(plan["subsystem_links"], list):
        _error("subsystem_links must be a list", "invalid_subsystem_plan_protocol")


def validate_subsystem_interface_plan(*, plan: dict[str, Any], function_items: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate Subsystem Interface Plan structure and references only."""
    _validate_protocol_shape(plan)
    normalized_items = normalize_structured_function_items(function_items, source="subsystem_interface_plan")
    frozen_targets = {item["target_file"] for item in normalized_items}
    subsystem_ids: set[str] = set()
    link_ids: set[str] = set()
    interface_ids: set[str] = set()
    member_owner: dict[str, str] = {}
    interfaces: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
    normalized_subsystems: list[dict[str, Any]] = []

    for raw_subsystem in plan["subsystems"]:
        subsystem = _require_keys(raw_subsystem, keys={"subsystem_id", "goal", "members", "internal_interfaces", "external_inputs", "external_outputs"}, code="invalid_subsystem_protocol", label="subsystem")
        subsystem_id = _require_nonempty_string(subsystem, "subsystem_id", "invalid_subsystem_protocol")
        if subsystem_id in subsystem_ids:
            _error("duplicate subsystem_id", "duplicate_subsystem_id", subsystem_id=subsystem_id)
        subsystem_ids.add(subsystem_id)
        _require_nonempty_string(subsystem, "goal", "invalid_subsystem_protocol")
        members = subsystem.get("members")
        if not isinstance(members, list) or not members or any(not isinstance(m, str) or not m.strip() for m in members):
            _error("members must be a non-empty string list", "invalid_subsystem_members", subsystem_id=subsystem_id)
        member_set = set(members)
        unknown = sorted(member_set - frozen_targets)
        if unknown:
            _error("subsystem references unknown FunctionItem", "unknown_function_item", subsystem_id=subsystem_id, targets=unknown)
        duplicates = [target for target, count in Counter(members).items() if count > 1]
        if duplicates:
            _error("subsystem contains duplicate members", "duplicate_subsystem_member", subsystem_id=subsystem_id, targets=duplicates)
        for member in members:
            if member in member_owner:
                _error("FunctionItem belongs to multiple subsystems", "function_item_multiple_subsystems", target=member, subsystem_ids=[member_owner[member], subsystem_id])
            member_owner[member] = subsystem_id

        for field in ("internal_interfaces", "external_inputs", "external_outputs"):
            if not isinstance(subsystem.get(field), list):
                _error(f"{field} must be a list", "invalid_subsystem_protocol", subsystem_id=subsystem_id, field=field)

        for raw_interface in subsystem["internal_interfaces"]:
            interface = _require_keys(raw_interface, keys={"interface_id", "goal", "producer_member", "consumer_member"}, code="invalid_internal_interface_protocol", label="internal interface")
            interface_id = _require_nonempty_string(interface, "interface_id", "invalid_internal_interface_protocol")
            if interface_id in interface_ids: _error("duplicate interface_id", "duplicate_interface_id", interface_id=interface_id)
            interface_ids.add(interface_id); _require_nonempty_string(interface, "goal", "invalid_internal_interface_protocol")
            producer = _require_nonempty_string(interface, "producer_member", "invalid_internal_interface_protocol")
            consumer = _require_nonempty_string(interface, "consumer_member", "invalid_internal_interface_protocol")
            if producer not in member_set or consumer not in member_set:
                _error("internal interface member must belong to current subsystem", "invalid_internal_interface_member", subsystem_id=subsystem_id, interface_id=interface_id)
            interfaces[(subsystem_id, interface_id)] = ("internal", interface)

        for raw_interface in subsystem["external_inputs"]:
            interface = _require_keys(raw_interface, keys={"interface_id", "goal", "consumer_member", "source_scope"}, code="invalid_external_input_protocol", label="external input")
            interface_id = _require_nonempty_string(interface, "interface_id", "invalid_external_input_protocol")
            if interface_id in interface_ids: _error("duplicate interface_id", "duplicate_interface_id", interface_id=interface_id)
            interface_ids.add(interface_id); _require_nonempty_string(interface, "goal", "invalid_external_input_protocol")
            consumer = _require_nonempty_string(interface, "consumer_member", "invalid_external_input_protocol")
            if consumer not in member_set: _error("external input consumer_member must belong to current subsystem", "invalid_external_input_member", subsystem_id=subsystem_id, interface_id=interface_id)
            if interface.get("source_scope") not in {"platform", "subsystem"}: _error("external input source_scope is invalid", "invalid_external_input_scope", subsystem_id=subsystem_id, interface_id=interface_id)
            interfaces[(subsystem_id, interface_id)] = ("external_input", interface)

        for raw_interface in subsystem["external_outputs"]:
            interface = _require_keys(raw_interface, keys={"interface_id", "goal", "producer_member", "target_scope"}, code="invalid_external_output_protocol", label="external output")
            interface_id = _require_nonempty_string(interface, "interface_id", "invalid_external_output_protocol")
            if interface_id in interface_ids: _error("duplicate interface_id", "duplicate_interface_id", interface_id=interface_id)
            interface_ids.add(interface_id); _require_nonempty_string(interface, "goal", "invalid_external_output_protocol")
            producer = _require_nonempty_string(interface, "producer_member", "invalid_external_output_protocol")
            if producer not in member_set: _error("external output producer_member must belong to current subsystem", "invalid_external_output_member", subsystem_id=subsystem_id, interface_id=interface_id)
            if interface.get("target_scope") not in {"platform", "subsystem"}: _error("external output target_scope is invalid", "invalid_external_output_scope", subsystem_id=subsystem_id, interface_id=interface_id)
            interfaces[(subsystem_id, interface_id)] = ("external_output", interface)
        normalized_subsystems.append(dict(subsystem))

    missing = sorted(frozen_targets - set(member_owner))
    if missing:
        _error("FunctionItem is not covered by any subsystem", "uncovered_function_item", targets=missing)
    extra = sorted(set(member_owner) - frozen_targets)
    if extra:
        _error("unknown FunctionItem assigned to subsystem", "unknown_function_item", targets=extra)

    source_links: Counter[tuple[str, str]] = Counter()
    target_links: Counter[tuple[str, str]] = Counter()
    normalized_links: list[dict[str, Any]] = []
    for raw_link in plan["subsystem_links"]:
        link = _require_keys(raw_link, keys={"link_id", "source_subsystem_id", "source_interface_id", "target_subsystem_id", "target_interface_id", "transfer_goal"}, code="invalid_subsystem_link_protocol", label="subsystem link")
        link_id = _require_nonempty_string(link, "link_id", "invalid_subsystem_link_protocol")
        if link_id in link_ids: _error("duplicate link_id", "duplicate_link_id", link_id=link_id)
        link_ids.add(link_id); _require_nonempty_string(link, "transfer_goal", "invalid_subsystem_link_protocol")
        source_key = (_require_nonempty_string(link, "source_subsystem_id", "invalid_subsystem_link_protocol"), _require_nonempty_string(link, "source_interface_id", "invalid_subsystem_link_protocol"))
        target_key = (_require_nonempty_string(link, "target_subsystem_id", "invalid_subsystem_link_protocol"), _require_nonempty_string(link, "target_interface_id", "invalid_subsystem_link_protocol"))
        source = interfaces.get(source_key); target = interfaces.get(target_key)
        if source is None: _error("subsystem link source interface does not exist", "unknown_link_source_interface", link_id=link_id)
        if target is None: _error("subsystem link target interface does not exist", "unknown_link_target_interface", link_id=link_id)
        if source[0] != "external_output": _error("subsystem link source must be an external_output", "invalid_link_source_interface", link_id=link_id)
        if target[0] != "external_input": _error("subsystem link target must be an external_input", "invalid_link_target_interface", link_id=link_id)
        source_links[source_key] += 1; target_links[target_key] += 1
        normalized_links.append(dict(link))

    for key, (kind, interface) in interfaces.items():
        if kind == "external_input":
            if interface["source_scope"] == "platform" and target_links[key]: _error("platform external_input must not be linked from a subsystem", "invalid_external_input_scope_link", subsystem_id=key[0], interface_id=key[1])
            if interface["source_scope"] == "subsystem" and target_links[key] != 1: _error("subsystem external_input must be provided by exactly one subsystem_link", "unlinked_external_input", subsystem_id=key[0], interface_id=key[1])
        if kind == "external_output":
            if interface["target_scope"] == "platform" and source_links[key]: _error("platform external_output must not link to another subsystem", "invalid_external_output_scope_link", subsystem_id=key[0], interface_id=key[1])
            if interface["target_scope"] == "subsystem" and not source_links[key]: _error("subsystem external_output must be used by at least one subsystem_link", "unlinked_external_output", subsystem_id=key[0], interface_id=key[1])

    validated = {"subsystems": normalized_subsystems, "subsystem_links": normalized_links}
    logger.info("[Creator][subsystem_plan] subsystem_count=%d member_count=%d internal_interface_count=%d external_input_count=%d external_output_count=%d subsystem_link_count=%d", len(normalized_subsystems), len(member_owner), sum(len(s["internal_interfaces"]) for s in normalized_subsystems), sum(len(s["external_inputs"]) for s in normalized_subsystems), sum(len(s["external_outputs"]) for s in normalized_subsystems), len(normalized_links))
    return validated


def _short_goal(goal: str) -> str:
    return goal[:120] + ("..." if len(goal) > 120 else "")


def build_graph_obligations_from_subsystems(*, subsystem_plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert declared subsystem interfaces to graph obligations without semantic inference."""
    subsystems = {s["subsystem_id"]: s for s in subsystem_plan.get("subsystems") or []}
    external_outputs = {(s["subsystem_id"], i["interface_id"]): i for s in subsystems.values() for i in s.get("external_outputs") or []}
    external_inputs = {(s["subsystem_id"], i["interface_id"]): i for s in subsystems.values() for i in s.get("external_inputs") or []}
    obligations: list[dict[str, Any]] = []
    def add(obligation: dict[str, Any]) -> None:
        obligation["obligation_id"] = f"O{len(obligations) + 1:04d}"
        obligations.append(obligation)
        logger.info("[Creator][subsystem_obligation] obligation_id=%s subsystem_id=%s interface_id=%s goal=%s source_scope=%s target_scope=%s", obligation.get("obligation_id"), obligation.get("subsystem_id") or obligation.get("target_subsystem_id") or "", obligation.get("interface_id") or obligation.get("target_interface_id") or "", _short_goal(str(obligation.get("goal") or "")), obligation.get("allowed_source_scope") or obligation.get("source_scope") or "", obligation.get("target_scope") or obligation.get("allowed_target_scope") or "")
    for subsystem in subsystem_plan.get("subsystems") or []:
        sid = subsystem["subsystem_id"]
        for interface in subsystem.get("external_inputs") or []:
            if interface["source_scope"] == "platform":
                add({"kind": "platform_to_script", "subsystem_id": sid, "interface_id": interface["interface_id"], "goal": interface["goal"], "target_member": interface["consumer_member"], "target_scope": "function_input", "allowed_source_scope": "platform"})
        for interface in subsystem.get("internal_interfaces") or []:
            add({"kind": "script_to_script", "subsystem_id": sid, "interface_id": interface["interface_id"], "goal": interface["goal"], "source_member": interface["producer_member"], "target_member": interface["consumer_member"], "allowed_source_scope": "declared_source_member", "allowed_target_scope": "declared_target_member"})
        for interface in subsystem.get("external_outputs") or []:
            if interface["target_scope"] == "platform":
                add({"kind": "script_to_platform", "subsystem_id": sid, "interface_id": interface["interface_id"], "goal": interface["goal"], "source_member": interface["producer_member"], "target_scope": "platform_output"})
    for link in subsystem_plan.get("subsystem_links") or []:
        source = external_outputs[(link["source_subsystem_id"], link["source_interface_id"])]
        target = external_inputs[(link["target_subsystem_id"], link["target_interface_id"])]
        add({"kind": "script_to_script", "source_subsystem_id": link["source_subsystem_id"], "source_interface_id": link["source_interface_id"], "target_subsystem_id": link["target_subsystem_id"], "target_interface_id": link["target_interface_id"], "goal": link["transfer_goal"], "source_member": source["producer_member"], "target_member": target["consumer_member"], "allowed_source_scope": "declared_source_member", "allowed_target_scope": "declared_target_member"})
    return obligations


async def plan_subsystem_interfaces(*, original_user_goal: str, frozen_blueprint: str, frozen_function_items: list[dict[str, Any]], requirement_allocations: list[dict[str, Any]] | None = None, requirement_channels: dict[str, str] | None = None, platform_contract: dict[str, Any] | None = None, validation_errors: list[dict[str, Any]] | None = None, planner_model: str, model_call: ModelCall) -> dict[str, Any]:
    """Ask the model for a Subsystem Interface Plan and validate it."""
    prompt = """
You are decomposing one frozen Skill Blueprint into semantic execution subsystems before concrete endpoint binding.
A subsystem is a coherent sub-goal boundary containing one or more supplied FunctionItems.
Determine the minimum coherent subsystem decomposition, exact supplied FunctionItem membership, semantic interfaces inside each subsystem, semantic interfaces crossing subsystem boundaries, and platform-facing subsystem interfaces.
Do not produce endpoint IDs, ResponsibilityEdges, platform paths, code, new files, or modified FunctionItems. Return strict JSON containing only subsystems and subsystem_links.
Do not add, remove, rename, split, merge, or duplicate FunctionItems. Every executable FunctionItem must belong to exactly one subsystem; static references/assets are not executable members.
Do not infer from filenames, suffixes, roles, fixed business keyword tables, fixed layer taxonomies, or fixed subsystem counts. Use the original goal, Blueprint responsibilities, FunctionItem purposes, inputs, outputs, dependencies, requirement allocations, requirement channels, and platform I/O descriptions as evidence.
Do not mechanically create one subsystem per FunctionItem. Do not force FunctionItems into one subsystem merely to minimize subsystem count. Use the minimum decomposition needed to express distinct execution sub-goals.
""".strip()
    payload: dict[str, Any] = {"original_user_goal": original_user_goal, "frozen_blueprint": frozen_blueprint, "frozen_function_items": frozen_function_items, "requirement_allocations": requirement_allocations or [], "requirement_channels": requirement_channels or {}, "platform_contract": platform_contract or {}}
    issue = None
    initial_validation_errors = list(validation_errors or [])
    for attempt in range(2):
        request_payload = dict(payload)
        if initial_validation_errors or issue:
            request_payload["validation_errors"] = initial_validation_errors + ([issue] if issue else [])
            logger.info("[Creator][subsystem_plan_repair] attempt=%d error_codes=%s", attempt, [value.get("code") for value in request_payload["validation_errors"]])
        text = await model_call([{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(request_payload, ensure_ascii=False, default=str)}], planner_model)
        try:
            return validate_subsystem_interface_plan(plan=_parse_object(text), function_items=frozen_function_items)
        except SubsystemInterfacePlanError as exc:
            if attempt:
                raise
            issue = {"code": exc.code, "details": getattr(exc, "details", {}), "message": str(exc), "instruction": "Repair only the subsystem/interface declarations. Do not modify Blueprint or FunctionItems."}
    raise SubsystemInterfacePlanError("subsystem planner produced no plan", code="invalid_subsystem_plan_protocol")


async def repair_subsystem_interfaces(**kwargs: Any) -> dict[str, Any]:
    """Compatibility wrapper for one local Subsystem Plan repair cycle."""
    return await plan_subsystem_interfaces(**kwargs)
