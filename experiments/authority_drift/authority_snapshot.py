"""Canonical Authority Snapshot v1 for explicitly structured SkillPlans."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .system_adapter import to_plain_data


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def snapshot_skill_plan(structured_plan: Any, system_commit: str) -> dict[str, Any]:
    """Snapshot authority explicitly present in a Creator structured plan."""
    plan = to_plain_data(structured_plan)
    if not isinstance(plan, dict):
        raise TypeError("structured_plan must be a dict, dataclass, or pydantic model")
    skill_name = plan.get("skill_name")
    if not isinstance(skill_name, str) or not skill_name:
        raise ValueError("structured_plan.skill_name must be a non-empty string")
    files = plan.get("files", plan.get("file_plan", []))
    function_items = plan.get("function_items", [])
    if not isinstance(files, list) or not isinstance(function_items, list):
        raise ValueError("structured plan file plan and function_items must be lists")
    snapshot = {
        "snapshot_version": "1.0",
        "system_commit": system_commit,
        "skill_name": skill_name,
        "file_plan": files,
        "function_items": function_items,
    }
    snapshot["snapshot_hash"] = hashlib.sha256(_canonical_bytes(snapshot)).hexdigest()
    return snapshot
