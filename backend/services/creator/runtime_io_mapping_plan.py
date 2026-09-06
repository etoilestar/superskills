"""Plan and execute approved runtime output representation mappings.

This layer deliberately sits after semantic Interface planning.  It may choose
how an already-bound value is represented at the platform boundary, but it may
not change either side of that binding.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..platform_io_contract import get_platform_output_sink, value_matches_platform_schema

ModelCall = Callable[[list[dict[str, str]], str], Awaitable[str]]

RUNTIME_CAPABILITY_SUMMARY: dict[str, Any] = {
    "supported_mappings": [
        {"from": "object", "to": "file_outputs", "operation": "serialize_json_file", "mode": "serialize_to_file"},
        {"from": "array[string]", "to": "file_outputs", "operation": "collect_file_paths", "mode": "collect_file_paths"},
        {"from": "object", "to": "text", "operation": "json_stringify", "mode": "json_stringify"},
    ]
}


class RuntimeIOMappingError(ValueError):
    def __init__(self, message: str, *, code: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def runtime_capability_summary() -> dict[str, Any]:
    """Return a copy so callers cannot mutate the runtime's authority list."""
    return json.loads(json.dumps(RUNTIME_CAPABILITY_SUMMARY))


def _schema_kind(schema: dict[str, Any]) -> str:
    kind = str(schema.get("type") or "unknown")
    if kind == "array" and isinstance(schema.get("items"), dict):
        return f"array[{schema['items'].get('type', 'unknown')}]"
    return kind


def _port_schema(port: dict[str, Any]) -> dict[str, Any]:
    schema = port.get("schema") or port.get("contract") or port.get("value_schema")
    if isinstance(schema, dict) and schema:
        return dict(schema)
    raw = str(port.get("type") or "unknown")
    if raw.startswith("list["):
        return {"type": "array", "items": {"type": raw[5:-1]}}
    return {"type": raw}


def _parse_model_json(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.I)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeIOMappingError("runtime mapping planner returned invalid JSON", code="runtime_io_mapping_planning_failure") from exc
    if not isinstance(value, dict):
        raise RuntimeIOMappingError("runtime mapping plan must be an object", code="runtime_io_mapping_planning_failure")
    return value


def validate_runtime_io_mapping_plan(
    plan: dict[str, Any], *, function_items: list[dict[str, Any]],
    platform_contract: dict[str, Any], interface_plan: dict[str, Any],
    capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Prove every mapping preserves frozen source and target contracts."""
    if set(plan) != {"mappings"} or not isinstance(plan.get("mappings"), list):
        raise RuntimeIOMappingError("plan must contain only mappings", code="runtime_io_mapping_planning_failure")
    ports: dict[tuple[str, str], dict[str, Any]] = {}
    for item in function_items:
        member = str(item.get("target_file") or item.get("member") or "")
        for output in item.get("outputs") or []:
            if isinstance(output, dict):
                name = str(output.get("name") or output.get("port_id") or output.get("field") or "")
                ports[(member, name)] = _port_schema(output)
    bindings = {
        (str(i.get("source_member") or ""), str(i.get("source_output") or ""), str(i.get("target_platform_output") or ""))
        for i in interface_plan.get("interfaces") or [] if i.get("kind") == "member_to_platform"
    }
    supported = {(c.get("from"), c.get("to"), c.get("operation"), c.get("mode")) for c in (capabilities or runtime_capability_summary()).get("supported_mappings", [])}
    seen: set[tuple[str, str, str]] = set()
    for mapping in plan["mappings"]:
        if not isinstance(mapping, dict) or not isinstance(mapping.get("source"), dict) or not isinstance(mapping.get("target"), dict):
            raise RuntimeIOMappingError("invalid mapping record", code="runtime_io_mapping_planning_failure")
        source, target = mapping["source"], mapping["target"]
        key = (str(source.get("member") or ""), str(source.get("output") or ""), str(target.get("platform_output") or ""))
        if key not in bindings or key in seen:
            raise RuntimeIOMappingError("mapping must match one frozen terminal binding", code="runtime_io_mapping_planning_failure", details={"binding": key})
        source_schema = ports.get(key[:2])
        sink = get_platform_output_sink(platform_contract, key[2])
        if source_schema is None or sink is None or source.get("schema") != source_schema or target.get("schema") != sink["value_schema"]:
            raise RuntimeIOMappingError("mapping changed a frozen schema or sink", code="runtime_io_mapping_planning_failure", details={"binding": key})
        operation, mode = str(mapping.get("operation") or ""), str(mapping.get("mode") or "")
        if (_schema_kind(source_schema), key[2], operation, mode) not in supported:
            raise RuntimeIOMappingError("mapping is not supported by runtime capabilities", code="runtime_io_mapping_planning_failure", details={"binding": key, "operation": operation})
        if mode == "serialize_to_file" and not str(mapping.get("artifact_name") or "").endswith(".json"):
            raise RuntimeIOMappingError("JSON file mapping requires artifact_name ending in .json", code="runtime_io_mapping_planning_failure")
        seen.add(key)
    return plan


def _mapping_prompt() -> str:
    return """You are the Runtime IO Mapping Planner. Decide representation mapping only for the supplied frozen source->target bindings.
Return strict JSON: {\"mappings\":[{\"source\":{\"member\":...,\"output\":...,\"schema\":...},\"target\":{\"platform_output\":...,\"schema\":...},\"mode\":...,\"operation\":...,\"artifact_name\":...,\"reason\":...}]}.
Use only an exactly declared runtime capability. Do not modify a source schema or platform contract, invent a sink, or perform business-field transformation. Directly compatible bindings need no mapping record."""


async def plan_runtime_io_mappings(
    *, canonical_interface_contract: dict[str, Any], function_items: list[dict[str, Any]],
    platform_contract: dict[str, Any], planner_model: str, model_call: ModelCall,
    capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caps = capabilities or runtime_capability_summary()
    terminal = []
    for interface in canonical_interface_contract.get("interfaces") or []:
        if interface.get("kind") != "member_to_platform":
            continue
        member, output, sink_name = str(interface.get("source_member") or ""), str(interface.get("source_output") or ""), str(interface.get("target_platform_output") or "")
        item = next((x for x in function_items if str(x.get("target_file") or x.get("member") or "") == member), {})
        port = next((x for x in item.get("outputs") or [] if isinstance(x, dict) and str(x.get("name") or x.get("port_id") or x.get("field") or "") == output), {})
        sink = get_platform_output_sink(platform_contract, sink_name)
        source_schema = _port_schema(port)
        if sink and source_schema != sink["value_schema"]:
            terminal.append({"source": {"member": member, "output": output, "schema": source_schema}, "target": {"platform_output": sink_name, "schema": sink["value_schema"]}})
    if not terminal:
        return {"mappings": []}
    raw = await model_call([{"role": "system", "content": _mapping_prompt()}, {"role": "user", "content": json.dumps({"bindings": terminal, "runtime_capability_summary": caps}, ensure_ascii=False)}], planner_model)
    return validate_runtime_io_mapping_plan(_parse_model_json(raw), function_items=function_items, platform_contract=platform_contract, interface_plan=canonical_interface_contract, capabilities=caps)


def execute_runtime_io_mapping(
    *, member: str, output: str, platform_output: str, value: Any,
    mapping_plan: dict[str, Any] | None, output_dir: str | Path,
) -> Any:
    """Execute one approved mapping; never infer an operation from the value."""
    mapping = next((m for m in (mapping_plan or {}).get("mappings", []) if m.get("source", {}).get("member") == member and m.get("source", {}).get("output") == output and m.get("target", {}).get("platform_output") == platform_output), None)
    if mapping is None:
        raise RuntimeIOMappingError("runtime IO mapping plan is missing", code="runtime_io_mapping_missing", details={"member": member, "output": output, "platform_output": platform_output})
    operation = mapping.get("operation")
    source_schema = mapping.get("source", {}).get("schema", {})
    declared = any(
        capability.get("from") == _schema_kind(source_schema)
        and capability.get("to") == platform_output
        and capability.get("operation") == operation
        and capability.get("mode") == mapping.get("mode")
        for capability in RUNTIME_CAPABILITY_SUMMARY["supported_mappings"]
    )
    if not declared:
        raise RuntimeIOMappingError("mapping operation is not declared by runtime capabilities", code="runtime_io_mapping_planning_failure", details={"operation": operation})
    if operation == "serialize_json_file":
        root = Path(output_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        path = (root / Path(str(mapping["artifact_name"])).name).resolve()
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        return [str(path)]
    if operation == "json_stringify":
        return json.dumps(value, ensure_ascii=False)
    if operation == "collect_file_paths":
        return list(value)
    raise RuntimeIOMappingError("approved mapping operation is unavailable", code="runtime_io_mapping_planning_failure", details={"operation": operation})
