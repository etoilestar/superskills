import json

import pytest

from backend.services.creator.semantic_review_contracts import (
    ALLOWED_EVIDENCE_SOURCES,
    BLUEPRINT_REVIEW_EVIDENCE_CONTEXT_SCHEMA,
    BLUEPRINT_SEMANTIC_REVIEW_SCHEMA,
    ReviewAuditCoverageError,
    ReviewEvidenceReferenceError,
    ReviewSchemaError,
    build_evidence_context,
    validate_and_ground_review,
)


def _context():
    return build_evidence_context(
        requirement_channels={"R1": "resource", "R2": "executable"},
        requirement_allocations=[
            {"requirement_id": "R1", "requirement": "Use a resource", "owners": [], "evidence": {}},
            {"requirement_id": "R2", "requirement": "Run A", "owners": ["scripts/a.py"], "evidence": {}},
        ],
        function_items=[{
            "target_file": "scripts/a.py", "responsibility": "Run A",
            "inputs": ["payload"], "outputs": ["result"], "dependencies": [],
        }],
        platform_contract={"boundary": {"input": "user_payload"}},
    )


def _review(ref):
    return {
        "audited_requirement_ids": ["R2"],
        "issues": [{
            "requirement_id": "R2", "affected_targets": ["scripts/a.py"],
            "evidence_refs": [ref], "required_condition": "The owner must fulfill R2.",
            "reason": "Semantic relationship needs review.",
        }],
    }


def test_allowed_sources_are_exactly_payload_evidence_keys_and_platform_is_available():
    context = _context()
    assert set(ALLOWED_EVIDENCE_SOURCES) == set(context)
    assert set(BLUEPRINT_REVIEW_EVIDENCE_CONTEXT_SCHEMA["required"]) == set(context)
    assert context["platform_contract"]
    serialized = json.dumps(BLUEPRINT_SEMANTIC_REVIEW_SCHEMA)
    assert "current_blueprint" not in serialized
    assert "frozen_function_items" not in serialized


def test_grounded_channel_cannot_be_forged_by_reviewer():
    grounded = validate_and_ground_review(
        _review({"source": "requirement_channels", "ref": "R1", "field": "channel"}),
        reviewer="requirement_provenance", evidence_context=_context(),
        expected_audit_domain=["R2"],
    )
    assert grounded["issues"][0]["evidence"][0]["observed"] == "resource"


def test_grounded_owner_cannot_be_forged_by_reviewer():
    grounded = validate_and_ground_review(
        _review({"source": "requirement_allocations", "ref": "R2", "field": "owners"}),
        reviewer="requirement_provenance", evidence_context=_context(),
        expected_audit_domain=["R2"],
    )
    assert grounded["issues"][0]["evidence"][0]["observed"] == ["scripts/a.py"]
    assert "observed" not in BLUEPRINT_SEMANTIC_REVIEW_SCHEMA["requirement_provenance"]["properties"]["issues"]["items"]["properties"]


def test_model_observed_key_and_unresolvable_reference_are_rejected():
    review = _review({"source": "requirement_allocations", "ref": "R2", "field": "owners", "observed": []})
    with pytest.raises(ReviewSchemaError):
        validate_and_ground_review(review, reviewer="requirement_provenance", evidence_context=_context(), expected_audit_domain=["R2"])
    review = _review({"source": "requirement_allocations", "ref": "R404", "field": "owners"})
    with pytest.raises(ReviewEvidenceReferenceError):
        validate_and_ground_review(review, reviewer="requirement_provenance", evidence_context=_context(), expected_audit_domain=["R2"])


def test_audit_coverage_is_exact_and_duplicate_free():
    review = _review({"source": "requirement_allocations", "ref": "R2", "field": "owners"})
    review["audited_requirement_ids"] = ["R2", "R2"]
    with pytest.raises(ReviewAuditCoverageError):
        validate_and_ground_review(review, reviewer="requirement_provenance", evidence_context=_context(), expected_audit_domain=["R2"])


def test_function_item_contract_receipt_covers_frozen_domain():
    review = {"audited_function_items": ["scripts/a.py"], "issues": []}
    grounded = validate_and_ground_review(
        review, reviewer="function_item_contract", evidence_context=_context(),
        expected_audit_domain=["scripts/a.py"],
    )
    assert grounded == review
