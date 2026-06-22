"""Deterministic Markdown frontmatter validation for Creator-generated files."""

from __future__ import annotations

import re
from typing import Any

import yaml

_SKILL_ALLOWED = {"name", "description", "license", "allowed-tools", "metadata"}
_SKILL_FORBIDDEN = {
    "trigger", "inputs", "outputs", "dependencies", "required_capabilities",
    "business_forbidden_capabilities", "references", "assets", "role", "path",
    "type", "scope", "workflow", "script_order", "resource_references",
}
_REFERENCE_ALLOWED = {"title", "description", "source", "license", "metadata"}
_REFERENCE_FORBIDDEN = {
    "trigger", "inputs", "outputs", "dependencies", "required_capabilities",
    "business_forbidden_capabilities", "assets", "references", "role", "path",
    "workflow", "script_order", "file_plan", "capabilities",
}

_FRONTMATTER_RE = re.compile(r"\A\ufeff?---\s*\r?\n(?P<yaml>[\s\S]*?)\r?\n---\s*(?:\r?\n|\Z)")


def parse_frontmatter(markdown: str) -> tuple[dict[str, Any], str, bool]:
    """Return ``(frontmatter, body, has_frontmatter)`` for a Markdown document."""
    text = markdown or ""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text.lstrip("\ufeff"), False
    raw = match.group("yaml")
    loaded = yaml.safe_load(raw) if raw.strip() else {}
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ValueError("frontmatter must be a YAML mapping")
    return {str(k): v for k, v in loaded.items()}, text[match.end():], True


def validate_skill_frontmatter(frontmatter: dict[str, Any]) -> list[str]:
    return _validate_frontmatter(frontmatter=frontmatter, allowed=_SKILL_ALLOWED, forbidden=_SKILL_FORBIDDEN, required={"name", "description"}, label="SKILL.md")


def validate_reference_frontmatter(frontmatter: dict[str, Any]) -> list[str]:
    return _validate_frontmatter(frontmatter=frontmatter, allowed=_REFERENCE_ALLOWED, forbidden=_REFERENCE_FORBIDDEN, required=set(), label="reference")


def validate_generic_frontmatter(frontmatter: dict[str, Any], *, allowed: set[str] | None = None, forbidden: set[str] | None = None) -> list[str]:
    return _validate_frontmatter(frontmatter=frontmatter, allowed=allowed or set(), forbidden=forbidden or set(), required=set(), label="markdown")


def apply_frontmatter_patch(markdown: str, new_frontmatter: dict[str, Any]) -> str:
    """Replace only the YAML frontmatter block, preserving the Markdown body."""
    _old, body, _has = parse_frontmatter(markdown)
    dumped = yaml.safe_dump(dict(new_frontmatter), allow_unicode=True, sort_keys=False).strip()
    return f"---\n{dumped}\n---\n{body}"


def _validate_frontmatter(*, frontmatter: dict[str, Any], allowed: set[str], forbidden: set[str], required: set[str], label: str) -> list[str]:
    issues: list[str] = []
    keys = {str(k) for k in frontmatter.keys()}
    missing = sorted(k for k in required if not str(frontmatter.get(k, "")).strip())
    if missing:
        issues.append(f"{label} frontmatter missing required keys: {', '.join(missing)}")
    forbidden_found = sorted(keys & forbidden)
    if forbidden_found:
        issues.append(f"{label} frontmatter contains Creator/internal keys: {', '.join(forbidden_found)}")
    if allowed:
        extra = sorted(keys - allowed)
        if extra:
            issues.append(f"{label} frontmatter contains unsupported top-level keys: {', '.join(extra)}")
    return issues
