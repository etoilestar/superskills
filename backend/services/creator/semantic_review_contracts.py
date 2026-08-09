"""Single source of truth for grounded Blueprint semantic reviews.

The model selects fact references.  This module validates those references and
projects their exact values from the authoritative payload; it never chooses a
semantic source or changes a Blueprint fact.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, Mapping


BLUEPRINT_REVIEW_EVIDENCE_CONTEXT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "requirement_channels", "requirement_allocations", "function_items",
        "blueprint_facts", "platform_contract",
    ],
    "properties": {
        "requirement_channels": {"type": "object"},
        "requirement_allocations": {"type": "array"},
        "function_items": {"type": "array"},
        "blueprint_facts": {"type": "object"},
        "platform_contract": {"type": "object"},
    },
}

EVIDENCE_REF_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["source", "ref", "field"],
    "properties": {
        "source": {"enum": BLUEPRINT_REVIEW_EVIDENCE_CONTEXT_SCHEMA["required"]},
        "ref": {"type": "string", "minLength": 1},
        "field": {"type": "string", "minLength": 1},
    },
}

ISSUE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["requirement_id", "affected_targets", "evidence_refs", "required_condition", "reason"],
    "properties": {
        "requirement_id": {"type": "string"},
        "affected_targets": {"type": "array", "items": {"type": "string"}},
        "evidence_refs": {"type": "array", "minItems": 1, "items": EVIDENCE_REF_SCHEMA},
        "required_condition": {"type": "string", "minLength": 1},
        "reason": {"type": "string", "minLength": 1},
    },
}

BLUEPRINT_SEMANTIC_REVIEW_SCHEMA = {
    "requirement_provenance": {
        "type": "object", "additionalProperties": False,
        "required": ["audited_requirement_ids", "issues"],
        "properties": {
            "audited_requirement_ids": {"type": "array", "items": {"type": "string"}},
            "issues": {"type": "array", "items": ISSUE_SCHEMA},
        },
    },
    "function_item_contract": {
        "type": "object", "additionalProperties": False,
        "required": ["audited_function_items", "issues"],
        "properties": {
            "audited_function_items": {"type": "array", "items": {"type": "string"}},
            "issues": {"type": "array", "items": ISSUE_SCHEMA},
        },
    },
}

ALLOWED_EVIDENCE_SOURCES = tuple(BLUEPRINT_REVIEW_EVIDENCE_CONTEXT_SCHEMA["required"])


class ReviewSchemaError(ValueError):
    """The JSON transport does not implement the shared machine contract."""


class ReviewEvidenceReferenceError(ValueError):
    """A syntactically valid model-selected fact reference does not exist."""


class ReviewAuditCoverageError(ValueError):
    """The reviewer silently omitted or duplicated an audit unit."""


def build_blueprint_facts(function_items: list[dict[str, Any]]) -> dict[str, Any]:
    """Project parser-owned structured facts without interpreting prose."""
    items = deepcopy(function_items)
    return {
        "files": [str(item.get("target_file") or "") for item in items],
        "function_items": items,
        "dependencies": {
            str(item.get("target_file") or ""): deepcopy(item.get("dependencies") or [])
            for item in items
        },
        "resources": {
            str(item.get("target_file") or ""): deepcopy(item.get("resources") or [])
            for item in items
        },
        "platform_io": {},
    }


def build_evidence_context(*, requirement_channels: dict[str, str],
                           requirement_allocations: list[dict[str, Any]],
                           function_items: list[dict[str, Any]],
                           platform_contract: dict[str, Any]) -> dict[str, Any]:
    context = {
        "requirement_channels": {
            str(requirement_id): {"channel": channel}
            for requirement_id, channel in requirement_channels.items()
        },
        "requirement_allocations": deepcopy(requirement_allocations),
        "function_items": deepcopy(function_items),
        "blueprint_facts": build_blueprint_facts(function_items),
        "platform_contract": deepcopy(platform_contract),
    }
    assert set(ALLOWED_EVIDENCE_SOURCES) == set(context)
    return context


def _identity_row(source: str, values: Any, ref: str) -> Any:
    if isinstance(values, Mapping):
        if ref not in values:
            raise ReviewEvidenceReferenceError(f"unknown {source} ref: {ref}")
        return values[ref]
    identity_field = "requirement_id" if source == "requirement_allocations" else "target_file"
    matches = [row for row in values if isinstance(row, Mapping) and str(row.get(identity_field) or "") == ref]
    if len(matches) != 1:
        raise ReviewEvidenceReferenceError(f"{source} ref must resolve exactly once: {ref}")
    return matches[0]


def resolve_evidence_ref(context: Mapping[str, Any], evidence_ref: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve source/ref/field to the exact observed value (identity projection)."""
    if set(evidence_ref) != {"source", "ref", "field"}:
        raise ReviewSchemaError("each evidence_refs item contains exactly source, ref, field")
    source, ref, field = (str(evidence_ref[key] or "").strip() for key in ("source", "ref", "field"))
    if source not in context or source not in ALLOWED_EVIDENCE_SOURCES:
        raise ReviewEvidenceReferenceError(f"unknown evidence source: {source}")
    if not ref or not field:
        raise ReviewSchemaError("evidence ref and field must be non-empty")
    value = _identity_row(source, context[source], ref)
    for segment in field.split("."):
        if not isinstance(value, Mapping) or segment not in value:
            raise ReviewEvidenceReferenceError(f"unknown evidence field: {source}/{ref}/{field}")
        value = value[segment]
    return {"source": source, "ref": ref, "field": field, "observed": deepcopy(value)}


def validate_and_ground_review(review: Any, *, reviewer: str,
                               evidence_context: Mapping[str, Any],
                               expected_audit_domain: Iterable[str]) -> dict[str, Any]:
    schema = BLUEPRINT_SEMANTIC_REVIEW_SCHEMA[reviewer]
    if not isinstance(review, Mapping) or set(review) != set(schema["required"]):
        raise ReviewSchemaError(f"{reviewer} review fields do not match its machine schema")
    receipt_key = "audited_requirement_ids" if reviewer == "requirement_provenance" else "audited_function_items"
    receipt, issues = review.get(receipt_key), review.get("issues")
    if not isinstance(receipt, list) or not all(isinstance(value, str) for value in receipt):
        raise ReviewSchemaError(f"{receipt_key} must be a string array")
    expected, actual = list(expected_audit_domain), list(receipt)
    if len(actual) != len(set(actual)) or set(actual) != set(expected):
        raise ReviewAuditCoverageError(f"invalid reviewer audit coverage: expected={expected}, actual={actual}")
    if not isinstance(issues, list):
        raise ReviewSchemaError("issues must be an array")
    grounded = []
    required = set(ISSUE_SCHEMA["required"])
    for index, issue in enumerate(issues):
        if not isinstance(issue, Mapping) or set(issue) != required:
            raise ReviewSchemaError(f"issue {index} fields do not match the machine schema")
        if not isinstance(issue["affected_targets"], list) or not isinstance(issue["evidence_refs"], list):
            raise ReviewSchemaError(f"issue {index} arrays are invalid")
        if not issue["evidence_refs"] or not str(issue["required_condition"]).strip() or not str(issue["reason"]).strip():
            raise ReviewSchemaError(f"issue {index} requires evidence_refs, required_condition, and reason")
        evidence = [resolve_evidence_ref(evidence_context, ref) for ref in issue["evidence_refs"]]
        grounded.append({key: deepcopy(value) for key, value in issue.items() if key != "evidence_refs"} | {"evidence": evidence})
    return {receipt_key: actual, "issues": grounded}
