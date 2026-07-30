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
    files = [_mapping(item) for item in plan.get("files", [])]
    function_items = [_mapping(item) for item in plan.get("function_items", [])]
    return build_authority_snapshot(
        system_commit=system_commit,
        skill_name=plan.get("skill_name", ""),
        file_plan=files,
        function_items=function_items,
    )
