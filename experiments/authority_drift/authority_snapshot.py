"""Deterministic, deliberately narrow Authority Snapshot v1 serialization."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from typing import Any


SNAPSHOT_VERSION = "1.0"
FILE_PLAN_FIELDS = (
    "path", "file_type", "file_kind", "asset_source", "required", "can_skip"
)
FUNCTION_ITEM_FIELDS = (
    "target_file", "role", "purpose", "inputs", "outputs", "default_values",
    "required_capabilities", "constraints",
)
# Canonical defaults mirror backend.services.skill_plan.SkillPlanEntry.  Fields
# without an entry here are required by the existing structured plan contract.
FILE_PLAN_DEFAULTS = {
    "file_kind": "config",
    "asset_source": "",
    "required": True,
    "can_skip": False,
}
# normalize_structured_function_items defines only default_values as optional.
FUNCTION_ITEM_DEFAULTS = {"default_values": {}}


def _canonicalize(value: Any) -> Any:
    """Return a detached JSON value with stable dict and list ordering."""
    if isinstance(value, Mapping):
        return {
            str(key): _canonicalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_canonicalize(item) for item in value]
        return sorted(items, key=_canonical_json)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        return _canonicalize(value.model_dump())
    raise TypeError(f"Authority Snapshot values must be JSON-compatible, got {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _project_item(
    item: Mapping[str, Any],
    *,
    fields: tuple[str, ...],
    defaults: Mapping[str, Any],
    identity: str,
    label: str,
) -> dict[str, Any]:
    if identity not in item or not isinstance(item[identity], str) or not item[identity].strip():
        raise ValueError(f"{label} requires a non-empty {identity}")
    missing = [field for field in fields if field not in item and field not in defaults]
    if missing:
        raise ValueError(f"{label} missing required fields: {', '.join(missing)}")
    projected = {
        field: copy.deepcopy(item[field] if field in item else defaults[field])
        for field in fields
    }
    return _canonicalize(projected)


def build_authority_snapshot(
    *,
    system_commit: str,
    skill_name: str,
    file_plan: list[Mapping[str, Any]],
    function_items: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a snapshot only from the explicitly allow-listed authority fields."""
    files = [
        _project_item(item, fields=FILE_PLAN_FIELDS, defaults=FILE_PLAN_DEFAULTS,
                      identity="path", label="file_plan item")
        for item in file_plan
    ]
    functions = [
        _project_item(item, fields=FUNCTION_ITEM_FIELDS, defaults=FUNCTION_ITEM_DEFAULTS,
                      identity="target_file", label="function_item")
        for item in function_items
    ]
    files.sort(key=lambda item: (str(item["path"]), _canonical_json(item)))
    functions.sort(key=lambda item: (str(item["target_file"]), _canonical_json(item)))

    payload = {
        "snapshot_version": SNAPSHOT_VERSION,
        "system_commit": system_commit,
        "skill_name": skill_name,
        "file_plan": files,
        "function_items": functions,
    }
    payload["snapshot_hash"] = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return payload


def snapshot_json(snapshot: Mapping[str, Any]) -> str:
    """Serialize a completed snapshot using the v1 canonical JSON encoding."""
    return _canonical_json(snapshot)
