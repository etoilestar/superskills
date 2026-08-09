"""SSOT and deterministic grounding for pre-freeze semantic reviews."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from .frozen_facts import CreatorFactsSnapshot

BLUEPRINT_REVIEW_EVIDENCE_CONTEXT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": [
        "requirement_channels", "requirement_allocations", "function_items",
        "file_plan", "resource_authority", "platform_contract",
    ],
    "properties": {
        "requirement_channels": {"type": "object"},
        "requirement_allocations": {"type": "array"},
        "function_items": {"type": "array"},
        "file_plan": {"type": "array"},
        "resource_authority": {"type": "object"},
        "platform_contract": {"type": "object"},
    },
}
ALLOWED_EVIDENCE_SOURCES = tuple(BLUEPRINT_REVIEW_EVIDENCE_CONTEXT_SCHEMA["required"])

EVIDENCE_REF_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["source", "ref", "field"],
    "properties": {
        "source": {"enum": list(ALLOWED_EVIDENCE_SOURCES)},
        "ref": {"type": "string", "minLength": 1},
        "field": {"type": "string", "minLength": 1},
    },
}

def _issue_schema(*, requirement: bool, targets: bool) -> dict[str, Any]:
    properties = {
        "evidence_refs": {"type": "array", "minItems": 1, "items": EVIDENCE_REF_SCHEMA},
        "required_condition": {"type": "string", "minLength": 1},
        "reason": {"type": "string", "minLength": 1},
    }
    required = list(properties)
    if requirement:
        properties["requirement_id"] = {"type": "string", "minLength": 1}
        required.insert(0, "requirement_id")
    if targets:
        properties["affected_targets"] = {"type": "array", "items": {"type": "string"}}
        required.insert(1 if requirement else 0, "affected_targets")
    return {"type": "object", "additionalProperties": False,
            "required": required, "properties": properties}

REVIEW_ISSUE_SCHEMAS = {
    "requirement_provenance": _issue_schema(requirement=True, targets=True),
    "function_item_contract": _issue_schema(requirement=False, targets=True),
    "constraint_semantics": _issue_schema(requirement=True, targets=False),
}

BLUEPRINT_SEMANTIC_REVIEW_SCHEMA = {
    "requirement_provenance": {
        "type": "object", "additionalProperties": False,
        "required": ["audited_requirement_ids", "audited_function_items", "issues"],
        "properties": {
            "audited_requirement_ids": {"type": "array", "items": {"type": "string"}},
            "audited_function_items": {"type": "array", "items": {"type": "string"}},
            "issues": {"type": "array", "items": REVIEW_ISSUE_SCHEMAS["requirement_provenance"]},
        },
    },
    "function_item_contract": {
        "type": "object", "additionalProperties": False,
        "required": ["audited_function_items", "issues"],
        "properties": {
            "audited_function_items": {"type": "array", "items": {"type": "string"}},
            "issues": {"type": "array", "items": REVIEW_ISSUE_SCHEMAS["function_item_contract"]},
        },
    },
    "constraint_semantics": {
        "type": "object", "additionalProperties": False,
        "required": ["audited_requirement_ids", "issues"],
        "properties": {
            "audited_requirement_ids": {"type": "array", "items": {"type": "string"}},
            "issues": {"type": "array", "items": REVIEW_ISSUE_SCHEMAS["constraint_semantics"]},
        },
    },
}

class ReviewSchemaError(ValueError):
    """JSON transport does not implement the shared machine contract."""

class ReviewEvidenceReferenceError(ValueError):
    """A syntactically valid model-selected fact reference does not exist."""

class ReviewAuditCoverageError(ValueError):
    """A reviewer omitted, duplicated, or invented an audit unit."""

class ReviewIdentityError(ValueError):
    """An issue identity is outside the frozen requirement/FunctionItem domain."""


def build_evidence_context(*, snapshot: CreatorFactsSnapshot) -> dict[str, Any]:
    """Copy only snapshot-owned facts; never infer roles or semantic meaning."""
    projection = snapshot.requirement_projection
    context = {
        "requirement_channels": {
            str(requirement_id): {"channel": channel}
            for requirement_id, channel in (projection.get("channels") or {}).items()
        },
        "requirement_allocations": deepcopy(projection.get("allocations") or []),
        "function_items": deepcopy(list(snapshot.function_items)),
        "file_plan": deepcopy(list(snapshot.file_plan)),
        "resource_authority": {"authority": deepcopy(snapshot.resource_authority)},
        "platform_contract": deepcopy(snapshot.platform_contract),
    }
    if set(ALLOWED_EVIDENCE_SOURCES) != set(context):
        raise AssertionError("evidence sources diverged from evidence_context keys")
    return context


def _identity_row(source: str, values: Any, ref: str) -> Any:
    if isinstance(values, Mapping):
        if ref not in values:
            raise ReviewEvidenceReferenceError(f"unknown {source} ref: {ref}")
        return values[ref]
    identity_fields = {
        "requirement_allocations": "requirement_id",
        "function_items": "target_file",
        "file_plan": "path",
    }
    identity_field = identity_fields.get(source)
    if not identity_field:
        raise ReviewEvidenceReferenceError(f"source is not reference-addressable: {source}")
    matches = [row for row in values if isinstance(row, Mapping)
               and str(row.get(identity_field) or "") == ref]
    if len(matches) != 1:
        raise ReviewEvidenceReferenceError(f"{source} ref must resolve exactly once: {ref}")
    return matches[0]


def resolve_evidence_ref(context: Mapping[str, Any], evidence_ref: Mapping[str, Any]) -> dict[str, Any]:
    """Project the exact observed value selected by source/ref/field."""
    if not isinstance(evidence_ref, Mapping) or set(evidence_ref) != {"source", "ref", "field"}:
        raise ReviewSchemaError("each evidence_refs item contains exactly source, ref, field")
    source, ref, field = (str(evidence_ref.get(key) or "").strip()
                          for key in ("source", "ref", "field"))
    if source not in context or source not in ALLOWED_EVIDENCE_SOURCES:
        raise ReviewEvidenceReferenceError(f"unknown evidence source: {source}")
    if not ref or not field:
        raise ReviewSchemaError("evidence ref and field must be non-empty")
    value = _identity_row(source, context[source], ref)
    for segment in field.split("."):
        if not isinstance(value, Mapping) or segment not in value:
            raise ReviewEvidenceReferenceError(f"unknown evidence field: {source}/{ref}/{field}")
        value = value[segment]
    return {"source": source, "ref": ref, "field": field,
            "observed": deepcopy(value)}


def _validate_receipt(actual: Any, expected: list[str], name: str) -> list[str]:
    if not isinstance(actual, list) or not all(isinstance(value, str) for value in actual):
        raise ReviewSchemaError(f"{name} must be a string array")
    if len(actual) != len(set(actual)) or set(actual) != set(expected):
        raise ReviewAuditCoverageError(
            f"invalid {name}: expected={expected}, actual={actual}"
        )
    return list(actual)


def validate_and_ground_review(
    review: Any, *, reviewer: str, evidence_context: Mapping[str, Any],
    expected_requirement_ids: list[str], expected_function_items: list[str],
    frozen_requirement_ids: list[str], frozen_function_items: list[str],
) -> dict[str, Any]:
    """Validate transport, identities, coverage, references, then ground facts."""
    schema = BLUEPRINT_SEMANTIC_REVIEW_SCHEMA[reviewer]
    if not isinstance(review, Mapping) or set(review) != set(schema["required"]):
        raise ReviewSchemaError(f"{reviewer} fields do not match its machine schema")
    normalized: dict[str, Any] = {}
    if "audited_requirement_ids" in schema["required"]:
        normalized["audited_requirement_ids"] = _validate_receipt(
            review.get("audited_requirement_ids"), expected_requirement_ids,
            "audited_requirement_ids",
        )
    if "audited_function_items" in schema["required"]:
        normalized["audited_function_items"] = _validate_receipt(
            review.get("audited_function_items"), expected_function_items,
            "audited_function_items",
        )
    issues = review.get("issues")
    if not isinstance(issues, list):
        raise ReviewSchemaError("issues must be an array")
    issue_schema = REVIEW_ISSUE_SCHEMAS[reviewer]
    grounded = []
    for index, issue in enumerate(issues):
        if not isinstance(issue, Mapping) or set(issue) != set(issue_schema["required"]):
            raise ReviewSchemaError(f"issue {index} fields do not match the {reviewer} schema")
        requirement_id = str(issue.get("requirement_id") or "").strip()
        if "requirement_id" in issue and requirement_id not in frozen_requirement_ids:
            raise ReviewIdentityError(f"unknown issue requirement_id: {requirement_id}")
        targets = issue.get("affected_targets", [])
        if "affected_targets" in issue:
            if not isinstance(targets, list) or any(not isinstance(value, str) for value in targets):
                raise ReviewSchemaError(f"issue {index} affected_targets must be a string array")
            unknown = sorted(set(targets) - set(frozen_function_items))
            if unknown:
                raise ReviewIdentityError(f"unknown issue affected_targets: {unknown}")
        refs = issue.get("evidence_refs")
        if not isinstance(refs, list) or not refs:
            raise ReviewSchemaError(f"issue {index} evidence_refs must be a non-empty array")
        if not str(issue.get("required_condition") or "").strip() or not str(issue.get("reason") or "").strip():
            raise ReviewSchemaError(f"issue {index} requires required_condition and reason")
        evidence = [resolve_evidence_ref(evidence_context, ref) for ref in refs]
        grounded.append({key: deepcopy(value) for key, value in issue.items()
                         if key != "evidence_refs"} | {"evidence": evidence})
    normalized["issues"] = grounded
    return normalized
