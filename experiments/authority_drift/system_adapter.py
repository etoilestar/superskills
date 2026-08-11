"""Read-only adapter from Creator's existing structured plan representation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import Any

from .authority_snapshot import build_authority_snapshot


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    raise TypeError(f"Expected a structured SkillPlan value, got {type(value).__name__}")


def snapshot_skill_plan(skill_plan: Any, *, system_commit: str) -> dict[str, Any]:
    """Adapt a SkillPlan/dataclass/dict without parsing, running, or mutating it."""
    plan = _mapping(skill_plan)
    if not isinstance(plan.get("skill_name"), str) or not plan["skill_name"].strip():
        raise ValueError("SkillPlan requires a non-empty skill_name")
    for field in ("files", "function_items"):
        if field not in plan:
            raise ValueError(f"SkillPlan requires the {field} field")
        if not isinstance(plan[field], list):
            raise ValueError(f"SkillPlan.{field} must be a list")
    files = [_mapping(item) for item in plan["files"]]
    function_items = [_mapping(item) for item in plan["function_items"]]
    return build_authority_snapshot(
        system_commit=system_commit,
        skill_name=plan["skill_name"],
        file_plan=files,
        function_items=function_items,
    )
