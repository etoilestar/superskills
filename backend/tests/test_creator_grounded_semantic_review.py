import json

import pytest

from backend.services.creator import api
from backend.services.creator.frozen_facts import CreatorFactsSnapshot
from backend.services.creator.semantic_review_contracts import (
    ALLOWED_EVIDENCE_SOURCES,
    BLUEPRINT_REVIEW_EVIDENCE_CONTEXT_SCHEMA,
    BLUEPRINT_SEMANTIC_REVIEW_SCHEMA,
    ReviewAuditCoverageError,
    ReviewEvidenceReferenceError,
    ReviewIdentityError,
    ReviewSchemaError,
    build_evidence_context,
    validate_and_ground_review,
)


def _snapshot():
    return CreatorFactsSnapshot.from_mutable(
        confirmed_requirements=["requirements"],
        requirement_projection={
            "channels": {"R1": "resource", "R2": "executable", "R3": "direct"},
            "allocations": [
                {"requirement_id": "R1", "requirement": "Use no assets", "owners": [], "evidence": {}},
                {"requirement_id": "R2", "requirement": "Run A", "owners": ["scripts/a.py"], "evidence": {}},
                {"requirement_id": "R3", "requirement": "Return directly", "owners": [], "evidence": {}},
            ],
        },
        function_items=[{"target_file": "scripts/a.py", "responsibility": "Run A", "inputs": ["payload"], "outputs": ["result"], "dependencies": []}],
        file_plan=[
            {"path": "scripts/a.py", "file_type": "script", "asset_source": ""},
            {"path": "references/guide.md", "file_type": "reference", "asset_source": "bundled"},
        ],
        resource_authority={"authoritative_references": ["references/guide.md"], "authoritative_assets": [], "allowed_resources": ["references/guide.md"]},
        platform_contract={"platform_skill_boundary": {"input_envelope_fields": ["payload"], "final_output_fields": ["text"]}},
    )


def _validate(review, reviewer, reqs, funcs):
    context = build_evidence_context(snapshot=_snapshot())
    return validate_and_ground_review(
        review, reviewer=reviewer, evidence_context=context,
        expected_requirement_ids=reqs, expected_function_items=funcs,
        frozen_requirement_ids=["R1", "R2", "R3"],
        frozen_function_items=["scripts/a.py"],
    )


def _provenance(ref):
    return {"audited_requirement_ids": ["R2"], "audited_function_items": ["scripts/a.py"], "issues": [{"requirement_id": "R2", "affected_targets": ["scripts/a.py"], "evidence_refs": [ref], "required_condition": "Owner fulfills requirement.", "reason": "Semantic relationship."}]}


def test_sources_equal_snapshot_projection_and_prompt_aliases_are_absent():
    context = build_evidence_context(snapshot=_snapshot())
    assert set(ALLOWED_EVIDENCE_SOURCES) == set(context)
    assert set(BLUEPRINT_REVIEW_EVIDENCE_CONTEXT_SCHEMA["required"]) == set(context)
    assert context["platform_contract"] and context["file_plan"]
    serialized = json.dumps(BLUEPRINT_SEMANTIC_REVIEW_SCHEMA)
    assert "observed" not in serialized
    assert "current_blueprint" not in serialized
    assert "frozen_function_items" not in serialized


def test_channel_and_owner_observed_are_backend_grounded():
    grounded = _validate(_provenance({"source": "requirement_channels", "ref": "R1", "field": "channel"}), "requirement_provenance", ["R2"], ["scripts/a.py"])
    assert grounded["issues"][0]["evidence"][0]["observed"] == "resource"
    grounded = _validate(_provenance({"source": "requirement_allocations", "ref": "R2", "field": "owners"}), "requirement_provenance", ["R2"], ["scripts/a.py"])
    assert grounded["issues"][0]["evidence"][0]["observed"] == ["scripts/a.py"]


def test_file_plan_resource_and_platform_grounding_use_snapshot_values():
    cases = [
        ({"source": "file_plan", "ref": "references/guide.md", "field": "file_type"}, "reference"),
        ({"source": "resource_authority", "ref": "authority", "field": "authoritative_assets"}, []),
        ({"source": "platform_contract", "ref": "platform_skill_boundary", "field": "final_output_fields"}, ["text"]),
    ]
    for ref, observed in cases:
        review = {"audited_requirement_ids": ["R1", "R3"], "issues": [{"requirement_id": "R1", "evidence_refs": [ref], "violated_fact_ref": ref, "required_condition": "Constraint holds.", "reason": "Check structured fact."}]}
        grounded = _validate(review, "constraint_semantics", ["R1", "R3"], [])
        assert grounded["issues"][0]["evidence"][0]["observed"] == observed


def test_all_three_receipts_are_exact_duplicate_free_domains():
    _validate({"audited_requirement_ids": ["R1", "R3"], "issues": []}, "constraint_semantics", ["R1", "R3"], [])
    _validate({"audited_function_items": ["scripts/a.py"], "issues": []}, "function_item_contract", [], ["scripts/a.py"])
    with pytest.raises(ReviewAuditCoverageError):
        _validate({"audited_requirement_ids": ["R1"], "issues": []}, "constraint_semantics", ["R1", "R3"], [])
    with pytest.raises(ReviewAuditCoverageError):
        _validate({"audited_requirement_ids": ["R2"], "audited_function_items": [], "issues": []}, "requirement_provenance", ["R2"], ["scripts/a.py"])


def test_unknown_issue_identities_and_evidence_refs_are_rejected():
    bad_target = _provenance({"source": "requirement_allocations", "ref": "R2", "field": "owners"})
    bad_target["issues"][0]["affected_targets"] = ["scripts/nonexistent.py"]
    with pytest.raises(ReviewIdentityError):
        _validate(bad_target, "requirement_provenance", ["R2"], ["scripts/a.py"])
    bad_requirement = _provenance({"source": "requirement_allocations", "ref": "R2", "field": "owners"})
    bad_requirement["issues"][0]["requirement_id"] = "R404"
    with pytest.raises(ReviewIdentityError):
        _validate(bad_requirement, "requirement_provenance", ["R2"], ["scripts/a.py"])
    with pytest.raises(ReviewEvidenceReferenceError):
        _validate(_provenance({"source": "requirement_allocations", "ref": "R404", "field": "owners"}), "requirement_provenance", ["R2"], ["scripts/a.py"])


def test_issue_identities_must_belong_to_the_reviewer_audit_domain():
    wrong_requirement = _provenance({"source": "requirement_allocations", "ref": "R1", "field": "owners"})
    wrong_requirement["issues"][0]["requirement_id"] = "R1"  # frozen, but not executable
    with pytest.raises(ReviewIdentityError):
        _validate(wrong_requirement, "requirement_provenance", ["R2"], ["scripts/a.py"])
    wrong_target = _provenance({"source": "requirement_allocations", "ref": "R2", "field": "owners"})
    wrong_target["audited_function_items"] = []
    with pytest.raises(ReviewIdentityError):
        _validate(wrong_target, "requirement_provenance", ["R2"], [])


def test_model_observed_is_a_schema_error():
    review = _provenance({"source": "requirement_allocations", "ref": "R2", "field": "owners", "observed": []})
    with pytest.raises(ReviewSchemaError):
        _validate(review, "requirement_provenance", ["R2"], ["scripts/a.py"])


@pytest.mark.asyncio
async def test_transport_repair_bad_ref_returns_to_fresh_semantic_attempt(monkeypatch):
    calls = []

    async def complete(messages, _role, **kwargs):
        stage = kwargs["stage"]
        calls.append(stage)
        if calls == ["function_item_contract"]:
            return json.dumps({"issues": []})  # malformed transport
        if stage == "semantic_review_transport":
            return json.dumps({
                "audited_function_items": ["scripts/a.py"],
                "issues": [{
                    "affected_targets": ["scripts/a.py"],
                    "evidence_refs": [{"source": "function_items", "ref": "scripts/missing.py", "field": "inputs"}],
                    "required_condition": "Inputs are conjunctive.", "reason": "Contract check.",
                }],
            })
        return json.dumps({"audited_function_items": ["scripts/a.py"], "issues": []})

    monkeypatch.setattr(api, "complete_creator_role_once", complete)
    context = build_evidence_context(snapshot=_snapshot())
    result = await api._run_grounded_blueprint_reviewer(
        reviewer="function_item_contract", evidence_context=context,
        expected_requirement_ids=[], expected_function_items=["scripts/a.py"],
        frozen_requirement_ids=["R1", "R2", "R3"],
        frozen_function_items=["scripts/a.py"], explanatory_blueprint="prose",
        planner_model="test",
    )
    assert result["issues"] == []
    assert calls == ["function_item_contract", "semantic_review_transport", "function_item_contract"]


@pytest.mark.asyncio
async def test_transport_repair_bad_coverage_returns_to_fresh_semantic_attempt(monkeypatch):
    calls = []

    async def complete(_messages, _role, **kwargs):
        stage = kwargs["stage"]
        calls.append(stage)
        if stage == "semantic_review_transport":
            return json.dumps({"audited_requirement_ids": ["R1"], "issues": []})
        if calls == ["constraint_semantics"]:
            return json.dumps({"issues": []})
        return json.dumps({"audited_requirement_ids": ["R1", "R3"], "issues": []})

    monkeypatch.setattr(api, "complete_creator_role_once", complete)
    result = await api._run_grounded_blueprint_reviewer(
        reviewer="constraint_semantics", evidence_context=build_evidence_context(snapshot=_snapshot()),
        expected_requirement_ids=["R1", "R3"], expected_function_items=[],
        frozen_requirement_ids=["R1", "R2", "R3"], frozen_function_items=["scripts/a.py"],
        explanatory_blueprint="prose", planner_model="test",
    )
    assert result["audited_requirement_ids"] == ["R1", "R3"]
    assert calls == ["constraint_semantics", "semantic_review_transport", "constraint_semantics"]


@pytest.mark.asyncio
async def test_three_reviewer_funnel_covers_channels_and_blocks_alias_before_freeze(monkeypatch):
    snapshot = _snapshot()

    async def complete(_messages, _role, **kwargs):
        if kwargs["stage"] == "requirement_provenance":
            return json.dumps({"audited_requirement_ids": ["R2"], "audited_function_items": ["scripts/a.py"], "issues": []})
        if kwargs["stage"] == "constraint_semantics":
            return json.dumps({"audited_requirement_ids": ["R1", "R3"], "issues": []})
        assert kwargs["stage"] == "function_item_contract"
        return json.dumps({
            "audited_function_items": ["scripts/a.py"],
            "issues": [{
                "affected_targets": ["scripts/a.py"],
                "evidence_refs": [{"source": "function_items", "ref": "scripts/a.py", "field": "inputs"}],
                "required_condition": "Required inputs represent distinct simultaneous runtime values.",
                "reason": "The declared inputs are alternative aliases for one value.",
            }],
        })

    monkeypatch.setattr(api, "complete_creator_role_once", complete)
    review = await api._review_blueprint_semantic_closure(
        request=api.PreparePlanRequest(user_request="generic"),
        blueprint_text="non-authoritative prose", facts_snapshot=snapshot,
        planner_model="test",
    )
    assert review["passed"] is False
    assert review["audited_requirement_ids"] == ["R2"]
    assert review["audited_constraint_requirement_ids"] == ["R1", "R3"]
    assert review["audited_provenance_function_items"] == ["scripts/a.py"]
    assert review["issues"][0]["repair_scope"] == "blueprint"
    assert review["issues"][0]["evidence"][0]["observed"] == ["payload"]


@pytest.mark.asyncio
async def test_constraint_authority_comes_from_violated_fact_not_supporting_evidence(monkeypatch):
    async def complete(_messages, _role, **kwargs):
        if kwargs["stage"] == "requirement_provenance":
            return json.dumps({"audited_requirement_ids": ["R2"], "audited_function_items": ["scripts/a.py"], "issues": []})
        if kwargs["stage"] == "function_item_contract":
            return json.dumps({"audited_function_items": ["scripts/a.py"], "issues": []})
        return json.dumps({"audited_requirement_ids": ["R1", "R3"], "issues": [{
            "requirement_id": "R1",
            "evidence_refs": [{"source": "file_plan", "ref": "references/guide.md", "field": "file_type"}],
            "violated_fact_ref": {"source": "resource_authority", "ref": "authority", "field": "authoritative_assets"},
            "required_condition": "Resource authority must represent the confirmed constraint.",
            "reason": "The authoritative resource fact contradicts the constraint.",
        }]})

    monkeypatch.setattr(api, "complete_creator_role_once", complete)
    review = await api._review_blueprint_semantic_closure(
        request=api.PreparePlanRequest(user_request="generic"),
        blueprint_text="prose", facts_snapshot=_snapshot(), planner_model="test",
    )
    assert review["issues"][0]["repair_scope"] == "resource_authority"
    assert review["issues"][0]["violated_fact"]["observed"] == []


@pytest.mark.asyncio
async def test_future_stage_only_constraint_is_not_blocking(monkeypatch):
    async def complete(_messages, _role, **kwargs):
        if kwargs["stage"] == "requirement_provenance":
            return json.dumps({"audited_requirement_ids": ["R2"], "audited_function_items": ["scripts/a.py"], "issues": []})
        if kwargs["stage"] == "function_item_contract":
            return json.dumps({"audited_function_items": ["scripts/a.py"], "issues": []})
        # Runtime-only verification has no current structured contradiction.
        return json.dumps({"audited_requirement_ids": ["R1", "R3"], "issues": []})

    monkeypatch.setattr(api, "complete_creator_role_once", complete)
    review = await api._review_blueprint_semantic_closure(
        request=api.PreparePlanRequest(user_request="verify only after runtime"),
        blueprint_text="no future evidence yet", facts_snapshot=_snapshot(), planner_model="test",
    )
    assert review["passed"] is True
    assert review["issues"] == []
