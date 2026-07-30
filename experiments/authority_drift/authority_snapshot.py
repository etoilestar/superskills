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


def _canonicalize(value: Any) -> Any:
    """Return a detached JSON value with stable dict and list ordering."""
    if isinstance(value, Mapping):
        return {
            str(key): _canonicalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_canonicalize(item) for item in value]
        return sorted(items, key=_canonical_json)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        return _canonicalize(value.model_dump())
    raise TypeError(f"Authority Snapshot values must be JSON-compatible, got {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_authority_snapshot(
    *,
    system_commit: str,
    skill_name: str,
    file_plan: list[Mapping[str, Any]],
    function_items: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a snapshot only from the explicitly allow-listed authority fields."""
    files = [
        _canonicalize({field: copy.deepcopy(item.get(field)) for field in FILE_PLAN_FIELDS})
        for item in file_plan
    ]
    functions = [
        _canonicalize({field: copy.deepcopy(item.get(field)) for field in FUNCTION_ITEM_FIELDS})
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
