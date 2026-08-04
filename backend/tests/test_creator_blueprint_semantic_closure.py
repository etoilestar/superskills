import ast
import json
from pathlib import Path

import pytest

from backend.services.creator import api
from backend.services.creator.contracts import (
    validate_blueprint_semantic_review,
    validate_requirement_allocations,
)


def _allocation(requirement_id, owners):
    return {
        "requirement_id": requirement_id,
        "requirement": f"Complete capability {requirement_id}",
        "owners": owners,
        "evidence": {"responsibility": "完成责任", "outputs": ["result"], "capabilities": ["operation"]},
    }


def _review_issue(
    issue_type="requirement_uncovered", requirement_id="R1", *,
    blocking_now=True, repair_scope="blueprint", affected_targets=None,
    evidence_stage="blueprint", evidence=None, expected_fact="required fact",
    reason="Current supplied fact is missing.", repair_guidance="Repair it.",
    **extra,
):
    if evidence is None and blocking_now:
        evidence = [{
            "source": "function_item", "target": "scripts/a.py",
            "field": "outputs", "observed": [],
        }]
    return {
        "issue_type": issue_type, "requirement_id": requirement_id,
        "blocking_now": blocking_now, "evidence_stage": evidence_stage,
        "repair_scope": repair_scope,
        "affected_targets": affected_targets or [], "evidence": evidence or [],
        "expected_fact": expected_fact if blocking_now else "",
        "reason": reason,
        "repair_guidance": repair_guidance if blocking_now else "",
        **extra,
    }


def _blueprint(entries):
    skill_entry = _entry("SKILL.md", "describe workflow", "skill_overview")
    return """## 📋 Skill 架构蓝图
### 基本信息
- **Skill 名称**: closure-test
### I/O 契约
- **输入**: request
- **输出**: result
- **触发词**: run
### 目录结构
[当前 Skill 根目录]
### 工作流逻辑
1. run
### SkillPlan / 文件职责计划
%s
### 宿主执行方式
- **直接回答**: result
- **需要脚本/命令**: run
- **禁止隐式执行**: yes
- **执行后回答**: result
### 资源清单
- [ ] none
""" % "\n".join([skill_entry, *entries])


def _entry(path, purpose="run responsibility", role="generic_script"):
    return f"""- path: `{path}`
  role: {role}
  purpose: {purpose}
  inputs: []
  outputs: [result]
  dependencies: []
  required_capabilities: []
  forbidden_capabilities: []
  references: []
  constraints: []"""


def test_semantic_review_normalizer_has_single_definition():
    source = Path(api.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    definitions = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_normalize_semantic_review_against_allocations"
    ]
    assert len(definitions) == 1
    assert [argument.arg for argument in definitions[0].args.args] == [
        "review", "requirement_allocations", "requirement_channels"
    ]
    assert definitions[0].args.defaults == []


def test_semantic_review_normalizer_accepts_required_channel_map():
    result = api._normalize_semantic_review_against_allocations(
        {"passed": True, "issues": []},
        [_allocation("R1", [])],
        {"R1": "executable"},
    )
    assert result == {"passed": True, "issues": [], "deferred_checks": []}


def test_one_function_item_may_own_multiple_requirements():
    result = validate_requirement_allocations(
        [_allocation("R1", ["scripts/a.py"]), _allocation("R2", ["scripts/a.py"])],
        allowed_owner_targets=["scripts/a.py"],
    )
    assert [item["owners"] for item in result] == [["scripts/a.py"], ["scripts/a.py"]]


def test_multiple_function_items_may_cooperate_on_one_requirement():
    result = validate_requirement_allocations(
        [_allocation("R1", ["scripts/a.py", "scripts/b.py"])],
        allowed_owner_targets=["scripts/a.py", "scripts/b.py"],
    )
    assert result[0]["owners"] == ["scripts/a.py", "scripts/b.py"]


@pytest.mark.parametrize("invalid_owner", [
    "SKILL.md",
    "references/r.md",
    "assets/a.bin",
    "scripts/not-exists.py",
    "a.py",
    "generic_script",
    "text_generation",
])
def test_invalid_owner_is_rejected_without_path_guessing(invalid_owner):
    with pytest.raises(ValueError, match="outside current FunctionItem domain"):
        validate_requirement_allocations(
            [_allocation("R1", [invalid_owner])],
            allowed_owner_targets=["scripts/a.py", "scripts/b.py"],
        )


def test_owner_domain_contains_only_function_item_targets():
    for invalid_owner in ("SKILL.md", "references/r.opaque"):
        with pytest.raises(ValueError, match="FunctionItem domain"):
            validate_requirement_allocations(
                [_allocation("R1", [invalid_owner])],
                allowed_owner_targets=["scripts/a.py"],
            )
    assert validate_requirement_allocations(
        [_allocation("R1", ["scripts/a.py"])],
        allowed_owner_targets=["scripts/a.py"],
    )[0]["owners"] == ["scripts/a.py"]


def test_empty_owner_is_allowed_during_allocation():
    allocations = validate_requirement_allocations(
        [_allocation("R1", [])], allowed_owner_targets=["scripts/a.py"]
    )
    assert allocations[0]["owners"] == []


@pytest.mark.parametrize(
    ("channels", "expected"),
    [
        ({"R1": "direct"}, (0, 0, 1)),
        ({"R1": "resource"}, (0, 1, 0)),
        ({"R1": "executable", "R2": "resource"}, (1, 1, 0)),
        ({"R1": "executable", "R2": "resource", "R3": "direct"}, (1, 1, 1)),
    ],
)
def test_requirement_channels_count_per_requirement(channels, expected):
    summary = api._requirement_channel_summary(channels)
    assert (
        summary["executable_requirement_count"],
        summary["resource_requirement_count"],
        summary["direct_requirement_count"],
    ) == expected


def test_no_executable_allocations_do_not_create_ownerless_issue():
    allocations = [_allocation("R1", []), _allocation("R2", [])]
    review = api._normalize_semantic_review_against_allocations(
        {"passed": True, "issues": []}, allocations,
        {"R1": "resource", "R2": "direct"},
    )
    assert review == {"passed": True, "issues": [], "deferred_checks": []}


@pytest.mark.parametrize("channels", [
    {},
    {"R1": "direct", "R2": "resource"},
    {"R1": "unknown"},
])
def test_requirement_channel_protocol_rejects_missing_extra_or_invalid(channels):
    with pytest.raises(api.PreparePlanProtocolError):
        api._validate_requirement_channels(channels, [_allocation("R1", [])])


@pytest.mark.asyncio
@pytest.mark.parametrize("change_channel", [False, True])
async def test_reconciliation_cannot_modify_owned_entry_or_channel(monkeypatch, change_channel):
    allocations = [_allocation("R1", ["scripts/a.py"]), _allocation("R2", [])]
    changed = [
        {**allocations[0], "evidence": {
            "responsibility": "changed", "outputs": [], "capabilities": []}},
        allocations[1],
    ]
    channels = {"R1": "executable", "R2": "executable"}

    async def complete(*_args, **_kwargs):
        returned_channels = dict(channels)
        if change_channel:
            returned_channels["R2"] = "direct"
        return json.dumps({
            "requirement_allocations": changed,
            "requirement_channels": returned_channels,
        })

    monkeypatch.setattr(api, "complete_creator_role_once", complete)
    with pytest.raises(api.PreparePlanProtocolError):
        await api._reconcile_requirement_allocations(
            request=api.PreparePlanRequest(user_request="Complete capability A"),
            blueprint_text="blueprint",
            function_items=[{"target_file": "scripts/a.py"}],
            requirement_allocations=allocations,
            requirement_channels=channels,
            semantic_review={"passed": False, "issues": []},
            planner_model="test",
        )


@pytest.mark.asyncio
async def test_requirement_planner_keeps_ownerless_core_requirement(monkeypatch):
    captured = []

    async def complete(messages, *_args, **_kwargs):
        captured.append(messages[0]["content"])
        return json.dumps({"requirement_allocations": [
            _allocation("R1", ["scripts/a.py"]),
            _allocation("R2", ["scripts/b.py"]),
            _allocation("R3", []),
        ], "requirement_channels": {
            "R1": "executable", "R2": "executable", "R3": "executable",
        }})

    monkeypatch.setattr(api, "complete_creator_role_once", complete)
    allocations = await api._plan_requirement_allocations(
        request=api.PreparePlanRequest(user_request="用户要求完成 A、B、C"),
        blueprint_text="blueprint",
        function_items=[
            {"target_file": "scripts/a.py"},
            {"target_file": "scripts/b.py"},
        ],
        planner_model="test",
    )

    assert allocations["requirement_allocations"][2]["owners"] == []
    assert allocations["requirement_channels"]["R3"] == "executable"
    planner_prompt = captured[0]
    assert "return owners=[]" in planner_prompt
    assert "do not omit" in planner_prompt
    assert "do not force an unrelated owner" in planner_prompt
    assert "Do not add, remove, rename, or modify FilePlan" in planner_prompt
    assert "negative constraint" in planner_prompt
    normalized_prompt = " ".join(planner_prompt.split())
    assert "do not mechanically assign every constraint" in normalized_prompt
    assert "Backend performs no keyword" in normalized_prompt
    assert "prohibitions, and responsibility boundaries" in planner_prompt
    assert "Do not return owners=[] merely because" in planner_prompt


@pytest.mark.asyncio
async def test_empty_function_item_domain_does_not_override_executable_channel(monkeypatch):
    async def complete(*_args, **_kwargs):
        return json.dumps({
            "requirement_allocations": [_allocation("R1", [])],
            "requirement_channels": {"R1": "executable"},
        })

    monkeypatch.setattr(api, "complete_creator_role_once", complete)
    projection = await api._plan_executable_requirement_allocations(
        request=api.PreparePlanRequest(user_request="Complete capability A"),
        blueprint_text="blueprint", function_items=[], planner_model="test",
    )
    assert projection["requirement_channels"] == {"R1": "executable"}
    assert projection["requirement_allocations"][0]["owners"] == []


def test_requirement_ids_are_unique_and_requirements_non_empty():
    with pytest.raises(ValueError, match="duplicate requirement_id"):
        validate_requirement_allocations(
            [_allocation("R1", ["scripts/a.py"]), _allocation("R1", ["scripts/a.py"])],
            allowed_owner_targets=["scripts/a.py"],
        )
    blank = _allocation("R2", ["scripts/a.py"])
    blank["requirement"] = ""
    with pytest.raises(ValueError, match="requirement must be non-empty"):
        validate_requirement_allocations([blank], allowed_owner_targets=["scripts/a.py"])


@pytest.mark.asyncio
async def test_reviewer_unknown_requirement_is_protocol_invalid(monkeypatch):
    async def complete(messages, *_args, **_kwargs):
        payload = json.loads(messages[1]["content"])
        assert payload["user_requirement"] == "用户需要完成 A、B、C 三项责任"
        assert len(payload["requirement_allocations"]) == 2
        return json.dumps({"passed": False, "issues": [
            _review_issue(requirement_id="R3")
        ], "deferred_checks": []})
    monkeypatch.setattr(api, "complete_creator_role_once", complete)
    with pytest.raises(api.PreparePlanProtocolError, match="allocation domain"):
        await api._review_blueprint_semantic_closure(
            request=api.PreparePlanRequest(user_request="用户需要完成 A、B、C 三项责任"),
            blueprint_text="blueprint",
            requirement_allocations=[_allocation("R1", ["scripts/a.py"]), _allocation("R2", ["scripts/a.py"])],
            function_items=[{"target_file": "scripts/a.py"}], requirement_channels={"R1": "executable", "R2": "executable"}, planner_model="test",
        )


@pytest.mark.asyncio
async def test_resource_semantic_conflict_is_reported_by_reviewer_not_suffix_logic(monkeypatch):
    async def complete(*_args, **_kwargs):
        return json.dumps({"passed": False, "issues": [_review_issue(
            issue_type="resource_semantic_conflict", requirement_id="",
            repair_scope="resource", affected_targets=["scripts/a.py"],
            resource="static/content.opaque",
        )], "deferred_checks": []})
    monkeypatch.setattr(api, "complete_creator_role_once", complete)
    review = await api._review_blueprint_semantic_closure(
        request=api.PreparePlanRequest(user_request="完成责任"), blueprint_text="dependency without provenance",
        function_items=[{"target_file": "scripts/a.py"}],
        requirement_allocations=[_allocation("R1", ["scripts/a.py"])],
        requirement_channels={"R1": "executable"}, planner_model="test",
    )
    assert review["issues"][0]["resource"] == "static/content.opaque"


@pytest.mark.asyncio
async def test_semantic_pass_normalizes_ownerless_requirement_to_issue(monkeypatch):
    async def complete(*_args, **_kwargs):
        return json.dumps({"passed": True, "issues": [], "deferred_checks": []})

    monkeypatch.setattr(api, "complete_creator_role_once", complete)
    review = await api._review_blueprint_semantic_closure(
        request=api.PreparePlanRequest(user_request="Complete capability A"),
        blueprint_text="blueprint",
        function_items=[{"target_file": "scripts/a.py"}],
        requirement_allocations=[_allocation("R1", [])],
        requirement_channels={"R1": "executable"},
        planner_model="test",
    )
    assert review == {"passed": True, "issues": [], "deferred_checks": []}


def test_normalization_does_not_duplicate_reported_ownerless_coverage_issue():
    issue = {
        "issue_type": "requirement_partially_covered",
        "requirement_id": "R1",
        "affected_targets": [],
        "reason": "Incomplete coverage.",
        "repair_guidance": "Clarify responsibility.",
    }
    normalized = api._normalize_semantic_review_against_allocations(
        {"passed": False, "issues": [issue, dict(issue)]},
        [_allocation("R1", [])],
        {"R1": "executable"},
    )
    assert normalized == {"passed": False, "issues": [issue], "deferred_checks": []}


@pytest.mark.parametrize(
    "owners",
    [["scripts/a.py"], ["scripts/a.py", "scripts/b.py"]],
)
def test_normalization_preserves_legal_owned_allocations(owners):
    allocations = [_allocation("R1", owners)]
    normalized = api._normalize_semantic_review_against_allocations(
        {"passed": True, "issues": []}, allocations, {"R1": "executable"}
    )
    assert normalized == {"passed": True, "issues": [], "deferred_checks": []}
    assert allocations[0]["owners"] == owners


def test_normalization_does_not_infer_ownerless_issues():
    normalized = api._normalize_semantic_review_against_allocations(
        {"passed": True, "issues": []},
        [_allocation("R1", []), _allocation("R2", [])],
        {"R1": "executable", "R2": "executable"},
    )
    assert normalized == {"passed": True, "issues": [], "deferred_checks": []}


def test_normalization_preserves_mixed_reviewer_issues():
    issues = [
        {"issue_type": "responsibility_mismatch", "requirement_id": "R1"},
        {"issue_type": "resource_semantic_conflict", "requirement_id": ""},
        {"issue_type": "requirement_uncovered", "requirement_id": "R1"},
    ]
    normalized = api._normalize_semantic_review_against_allocations(
        {"passed": False, "issues": issues}, [_allocation("R1", [])],
        {"R1": "executable"},
    )
    assert normalized["issues"] == issues


def test_normalization_does_not_mutate_inputs():
    review = {"passed": True, "issues": []}
    allocations = [_allocation("R1", [])]
    original_review = json.loads(json.dumps(review))
    original_allocations = json.loads(json.dumps(allocations))
    api._normalize_semantic_review_against_allocations(
        review, allocations, {"R1": "executable"}
    )
    assert review == original_review
    assert allocations == original_allocations


@pytest.mark.asyncio
@pytest.mark.parametrize("mutator", [
    lambda items: [*items, _allocation("R2", [])],
    lambda items: [],
    lambda items: [{**items[0], "requirement_id": "R2"}],
    lambda items: [{**items[0], "requirement": "Changed capability"}],
    lambda items: [items[1], items[0]],
])
async def test_reconciliation_cannot_change_requirement_set(monkeypatch, mutator):
    allocations = [_allocation("R1", []), _allocation("R2", ["scripts/a.py"])]

    async def complete(*_args, **_kwargs):
        return json.dumps({
            "requirement_allocations": mutator(allocations),
            "requirement_channels": {"R1": "executable", "R2": "executable"},
        })

    monkeypatch.setattr(api, "complete_creator_role_once", complete)
    with pytest.raises((ValueError, api.PreparePlanProtocolError)):
        await api._reconcile_requirement_allocations(
            request=api.PreparePlanRequest(user_request="Complete capability A"),
            blueprint_text="blueprint",
            function_items=[{"target_file": "scripts/a.py"}],
            requirement_allocations=allocations,
            requirement_channels={"R1": "executable", "R2": "executable"},
            semantic_review={"passed": False, "issues": []},
            planner_model="test",
        )


def test_semantic_review_rejects_unknown_target_exactly():
    with pytest.raises(ValueError, match="outside current FunctionItem domain"):
        validate_blueprint_semantic_review(
            {"passed": False, "issues": [_review_issue(
                issue_type="responsibility_mismatch",
                affected_targets=["scripts/not_exists.py"],
            )], "deferred_checks": []},
            allowed_function_item_targets=["scripts/exists.py"],
            supplied_requirement_ids=["R1"],
        )


def test_resource_conflict_may_have_no_affected_target():
    review = validate_blueprint_semantic_review(
        {"passed": False, "issues": [_review_issue(
            issue_type="resource_semantic_conflict", repair_scope="resource",
            affected_targets=[], resource="resources/r.opaque",
        )], "deferred_checks": []},
        allowed_function_item_targets=["scripts/a.py"],
        supplied_requirement_ids=["R1"],
    )
    assert review["issues"][0]["affected_targets"] == []


def test_summary_file_sets_are_authoritative_file_plan_projection():
    summary = api.PreparePlanReviewSummary(
        files_to_create_or_update=["extra.file"], assets_to_upload=["wrong.asset"]
    )
    files = [
        api.FileSpecOut(path="scripts/a.py", purpose="work", file_type="script", required=True, can_skip=False),
        api.FileSpecOut(path="resources/upload.opaque", purpose="static", file_type="asset", asset_source="user_upload", required=True, can_skip=False),
        api.FileSpecOut(path="references/info.opaque", purpose="guidance", file_type="reference", asset_source="user_upload", required=True, can_skip=False),
    ]
    api._sync_prepare_summary_files_from_skill_plan(summary, files)
    assert summary.files_to_create_or_update == [item.path for item in files]
    assert summary.assets_to_upload == ["resources/upload.opaque"]


@pytest.mark.asyncio
async def test_localized_replan_prompt_requires_minimal_change(monkeypatch):
    calls = []
    async def complete(messages, *_args, **_kwargs):
        calls.append(messages)
        return json.dumps({
            "internal_blueprint_text": after,
            "changed_targets": ["scripts/a.py"], "added_targets": [], "changed_resources": [],
        })
    monkeypatch.setattr(api, "complete_creator_role_once", complete)
    before = _blueprint([_entry("scripts/a.py"), _entry("scripts/b.py")])
    after = _blueprint([_entry("scripts/a.py", "revised responsibility"), _entry("scripts/b.py")])
    result = await api._replan_blueprint_for_semantic_closure(
        request=api.PreparePlanRequest(user_request="完成 A、B、C"), blueprint_text=before,
        function_items=[], requirement_allocations=[], blocking_issues=[{
            "issue_type": "responsibility_mismatch", "affected_targets": ["scripts/a.py"],
        }], planner_model="test",
    )
    assert result == after.strip()
    assert len(calls) == 1
    assert "minimum Blueprint facts" in calls[0][0]["content"]


def test_replan_scope_rejects_unrelated_existing_target_change():
    before = _blueprint([_entry("scripts/a.py"), _entry("scripts/b.py"), _entry("scripts/c.py")])
    after = _blueprint([_entry("scripts/a.py", "revised"), _entry("scripts/b.py", "unrelated"), _entry("scripts/c.py")])
    with pytest.raises(api.PreparePlanProtocolError, match="outside blocking issue scope"):
        api._validate_blueprint_semantic_replan_scope(
            before_blueprint_text=before, after_blueprint_text=after,
            blocking_issues=[{"issue_type": "responsibility_mismatch", "affected_targets": ["scripts/a.py"]}],
            patch_manifest={"changed_targets": ["scripts/a.py", "scripts/b.py"], "added_targets": [], "changed_resources": []},
        )


def test_replan_scope_allows_affected_target_only():
    before = _blueprint([_entry("scripts/a.py"), _entry("scripts/b.py")])
    after = _blueprint([_entry("scripts/a.py", "revised"), _entry("scripts/b.py")])
    api._validate_blueprint_semantic_replan_scope(
        before_blueprint_text=before, after_blueprint_text=after,
        blocking_issues=[{"issue_type": "responsibility_mismatch", "affected_targets": ["scripts/a.py"]}],
        patch_manifest={"changed_targets": ["scripts/a.py"], "added_targets": [], "changed_resources": []},
    )


def test_uncovered_scope_allows_addition_but_preserves_existing_targets():
    before = _blueprint([_entry("scripts/a.py")])
    after = _blueprint([_entry("scripts/a.py"), _entry("scripts/b.py")])
    api._validate_blueprint_semantic_replan_scope(
        before_blueprint_text=before, after_blueprint_text=after,
        blocking_issues=[{"issue_type": "requirement_uncovered", "affected_targets": []}],
        patch_manifest={"changed_targets": [], "added_targets": ["scripts/b.py"], "changed_resources": []},
    )


def test_uncovered_scope_rejects_deletion_and_replacement():
    before = _blueprint([_entry("scripts/a.py"), _entry("scripts/c.py")])
    after = _blueprint([_entry("scripts/b.py")])
    with pytest.raises(api.PreparePlanProtocolError, match="removing paths"):
        api._validate_blueprint_semantic_replan_scope(
            before_blueprint_text=before, after_blueprint_text=after,
            blocking_issues=[{"issue_type": "requirement_uncovered", "affected_targets": []}],
            patch_manifest={"changed_targets": [], "added_targets": ["scripts/b.py"], "changed_resources": []},
        )


def test_uncovered_scope_allows_explicit_existing_target_clarification():
    before = _blueprint([_entry("scripts/a.py")])
    after = _blueprint([_entry("scripts/a.py", "revised")])
    api._validate_blueprint_semantic_replan_scope(
        before_blueprint_text=before,
        after_blueprint_text=after,
        blocking_issues=[{"issue_type": "requirement_uncovered", "affected_targets": []}],
        patch_manifest={
            "changed_targets": ["scripts/a.py"],
            "added_targets": [],
            "changed_resources": [],
        },
    )


@pytest.mark.asyncio
async def test_real_ownerless_replan_clarifies_existing_function_item(monkeypatch):
    before = _blueprint([_entry("scripts/a.py")])
    clarified = _entry("scripts/a.py", "Capability A responsibility").replace(
        "inputs: []", "inputs: [request]"
    ).replace("outputs: [result]", "outputs: [capability_result]")
    after = _blueprint([clarified])

    async def complete(*_args, **_kwargs):
        return json.dumps({
            "internal_blueprint_text": after,
            "changed_targets": ["scripts/a.py"],
            "added_targets": [],
            "changed_resources": [],
        })

    monkeypatch.setattr(api, "complete_creator_role_once", complete)
    result = await api._replan_blueprint_for_semantic_closure(
        request=api.PreparePlanRequest(user_request="Complete capability A"),
        blueprint_text=before,
        function_items=[{"target_file": "scripts/a.py"}],
        requirement_allocations=[_allocation("R1", [])],
        blocking_issues=[{"issue_type": "requirement_uncovered", "requirement_id": "R1", "affected_targets": []}],
        planner_model="test",
    )
    assert result == after.strip()
    assert "scripts/b.py" not in result


@pytest.mark.asyncio
async def test_real_ownerless_replan_clarifies_multiple_existing_function_items(monkeypatch):
    before = _blueprint([_entry("scripts/a.py"), _entry("scripts/b.py")])
    after = _blueprint([
        _entry("scripts/a.py", "Produce shared capability data"),
        _entry("scripts/b.py", "Consume shared capability data"),
    ])

    async def complete(*_args, **_kwargs):
        return json.dumps({
            "internal_blueprint_text": after,
            "changed_targets": ["scripts/a.py", "scripts/b.py"],
            "added_targets": [],
            "changed_resources": [],
        })

    monkeypatch.setattr(api, "complete_creator_role_once", complete)
    result = await api._replan_blueprint_for_semantic_closure(
        request=api.PreparePlanRequest(user_request="Complete capability A"),
        blueprint_text=before,
        function_items=[{"target_file": "scripts/a.py"}, {"target_file": "scripts/b.py"}],
        requirement_allocations=[_allocation("R1", [])],
        blocking_issues=[{"issue_type": "requirement_uncovered", "requirement_id": "R1", "affected_targets": []}],
        planner_model="test",
    )
    assert result == after.strip()


@pytest.mark.asyncio
async def test_real_ownerless_replan_allows_minimum_new_responsibility_carrier(monkeypatch):
    before = _blueprint([_entry("scripts/a.py")])
    after = _blueprint([_entry("scripts/a.py"), _entry("scripts/c.py", "Capability A responsibility")])

    async def complete(*_args, **_kwargs):
        return json.dumps({
            "internal_blueprint_text": after,
            "changed_targets": [],
            "added_targets": ["scripts/c.py"],
            "changed_resources": [],
        })

    monkeypatch.setattr(api, "complete_creator_role_once", complete)
    result = await api._replan_blueprint_for_semantic_closure(
        request=api.PreparePlanRequest(user_request="Complete capability A"),
        blueprint_text=before,
        function_items=[{"target_file": "scripts/a.py"}],
        requirement_allocations=[_allocation("R1", [])],
        blocking_issues=[{"issue_type": "requirement_uncovered", "requirement_id": "R1", "affected_targets": []}],
        planner_model="test",
    )
    assert result == after.strip()


def test_ownerless_replan_rejects_unrelated_resource_change():
    before = _blueprint([_entry("scripts/a.py")])
    after = _blueprint([
        _entry("scripts/a.py"),
        _entry("references/r.md", "Unrelated resource", "reference"),
    ])
    with pytest.raises(api.PreparePlanProtocolError, match="resources outside"):
        api._validate_blueprint_semantic_replan_scope(
            before_blueprint_text=before,
            after_blueprint_text=after,
            blocking_issues=[{"issue_type": "requirement_uncovered", "requirement_id": "R1", "affected_targets": []}],
            patch_manifest={
                "changed_targets": [],
                "added_targets": [],
                "changed_resources": ["references/r.md"],
            },
        )


def test_replan_uses_actual_noop_instead_of_false_manifest():
    before = _blueprint([_entry("scripts/a.py")])
    actual = api._validate_blueprint_semantic_replan_scope(
        before_blueprint_text=before,
        after_blueprint_text=before,
        blocking_issues=[{
            "issue_type": "requirement_uncovered",
            "requirement_id": "R1",
            "affected_targets": [],
        }],
        patch_manifest={
            "changed_targets": ["scripts/a.py"],
            "added_targets": [],
            "changed_resources": [],
        },
    )
    assert actual["actual_changed_targets"] == []
    assert actual["actual_added_targets"] == []
    assert actual["actual_changed_resources"] == []


@pytest.mark.parametrize("old,new", [
    ("role: generic_script", "role: alternate_role"),
    ("required_capabilities: []", "required_capabilities: [capability_a]"),
    ("forbidden_capabilities: []", "forbidden_capabilities: [capability_b]"),
    ("references: []", "references: [references/r.md]"),
])
def test_ownerless_replan_rejects_non_clarification_fields(old, new):
    before = _blueprint([_entry("scripts/a.py")])
    after = before.replace(old, new)
    with pytest.raises(api.PreparePlanProtocolError, match="scope|FunctionItem domain"):
        api._validate_blueprint_semantic_replan_scope(
            before_blueprint_text=before,
            after_blueprint_text=after,
            blocking_issues=[{
                "issue_type": "requirement_uncovered",
                "requirement_id": "R1",
                "affected_targets": [],
            }],
            patch_manifest={
                "changed_targets": ["scripts/a.py"],
                "added_targets": [], "changed_resources": [],
            },
        )


def test_pre_graph_protocol_rejects_random_unsupplied_owner_target():
    random_target = "scripts/q9x_unknown.py"
    with pytest.raises(ValueError, match="outside current FunctionItem domain"):
        validate_blueprint_semantic_review(
            {"passed": False, "issues": [_review_issue(
                issue_type="responsibility_mismatch",
                affected_targets=[random_target],
            )], "deferred_checks": []},
            allowed_function_item_targets=["scripts/a.py"],
            supplied_requirement_ids=["R1"],
        )


def test_pre_graph_blocking_issue_requires_current_evidence():
    issue = _review_issue(evidence=[])
    issue["evidence"] = []
    with pytest.raises(ValueError, match="requires evidence"):
        validate_blueprint_semantic_review(
            {"passed": False, "issues": [issue], "deferred_checks": []},
            allowed_function_item_targets=["scripts/a.py"],
            supplied_requirement_ids=["R1"],
        )


def test_deferred_graph_check_is_nonblocking():
    deferred = _review_issue(
        issue_type="deferred_verification", blocking_now=False,
        repair_scope="none", evidence_stage="graph", evidence=[],
        reason="Requires graph evidence.",
    )
    review = validate_blueprint_semantic_review(
        {"passed": True, "issues": [], "deferred_checks": [deferred]},
        allowed_function_item_targets=["scripts/a.py", "scripts/b.py"],
        supplied_requirement_ids=["R1"],
    )
    assert review["passed"] is True
    assert review["deferred_checks"][0]["evidence_stage"] == "graph"


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["direct", "resource"])
async def test_ownerless_non_executable_channel_remains_passed(monkeypatch, channel):
    async def complete(messages, *_args, **_kwargs):
        payload = json.loads(messages[1]["content"])
        assert payload["requirement_channels"] == {"R1": channel}
        assert payload["requirement_allocations"][0]["owners"] == []
        return json.dumps({"passed": True, "issues": [], "deferred_checks": []})

    monkeypatch.setattr(api, "complete_creator_role_once", complete)
    review = await api._review_blueprint_semantic_closure(
        request=api.PreparePlanRequest(user_request="opaque requirement"),
        blueprint_text="blueprint",
        function_items=[{"target_file": "scripts/a.py"}],
        requirement_allocations=[_allocation("R1", [])],
        requirement_channels={"R1": channel},
        planner_model="test",
    )
    assert review == {"passed": True, "issues": [], "deferred_checks": []}


@pytest.mark.asyncio
async def test_reviewer_deferred_relationship_does_not_fail_review(monkeypatch):
    deferred = _review_issue(
        issue_type="deferred_verification", blocking_now=False,
        repair_scope="none", evidence_stage="graph", evidence=[],
        reason="Final verification requires ResponsibilityGraph evidence.",
    )

    async def complete(*_args, **_kwargs):
        return json.dumps({"passed": True, "issues": [], "deferred_checks": [deferred]})

    monkeypatch.setattr(api, "complete_creator_role_once", complete)
    review = await api._review_blueprint_semantic_closure(
        request=api.PreparePlanRequest(user_request="opaque relationship"),
        blueprint_text="blueprint",
        function_items=[{"target_file": "scripts/a.py"}, {"target_file": "scripts/b.py"}],
        requirement_allocations=[_allocation("R1", ["scripts/a.py", "scripts/b.py"])],
        requirement_channels={"R1": "executable"},
        planner_model="test",
    )
    assert review["passed"] is True
    assert review["issues"] == []
    assert len(review["deferred_checks"]) == 1


def test_actual_noop_projection_ignores_nonsemantic_blueprint_wording():
    before = _blueprint([_entry("scripts/a.py")])
    after = before.replace("### 目录结构", "### 目录结构\n")
    assert api._semantic_projection_facts(before) == api._semantic_projection_facts(after)


@pytest.mark.asyncio
async def test_semantic_reviewer_protocol_retries_once_locally(monkeypatch):
    calls = []

    async def complete(messages, *_args, **_kwargs):
        payload = json.loads(messages[1]["content"])
        calls.append(payload)
        if len(calls) == 1:
            return json.dumps({"passed": True, "issues": []})
        assert "protocol_error" in payload
        return json.dumps({"passed": True, "issues": [], "deferred_checks": []})

    monkeypatch.setattr(api, "complete_creator_role_once", complete)
    review = await api._review_blueprint_semantic_closure(
        request=api.PreparePlanRequest(user_request="opaque"),
        blueprint_text="blueprint",
        function_items=[{"target_file": "scripts/a.py"}],
        requirement_allocations=[_allocation("R1", ["scripts/a.py"])],
        requirement_channels={"R1": "executable"},
        planner_model="test",
    )
    assert review["passed"] is True
    assert len(calls) == 2
    assert calls[0]["requirement_allocations"] == calls[1]["requirement_allocations"]


@pytest.mark.asyncio
async def test_allocation_reconcile_may_change_explicitly_routed_channel(monkeypatch):
    allocations = [_allocation("R1", ["scripts/a.py"]), _allocation("R2", ["scripts/a.py"])]
    channels = {"R1": "executable", "R2": "executable"}

    async def complete(messages, *_args, **_kwargs):
        payload = json.loads(messages[1]["content"])
        assert payload["repairable_requirement_ids"] == ["R2"]
        reconciled = [dict(allocations[0]), {**allocations[1], "owners": []}]
        return json.dumps({
            "requirement_allocations": reconciled,
            "requirement_channels": {"R1": "executable", "R2": "direct"},
        })

    monkeypatch.setattr(api, "complete_creator_role_once", complete)
    result = await api._reconcile_requirement_allocations(
        request=api.PreparePlanRequest(user_request="opaque"),
        blueprint_text="blueprint",
        function_items=[{"target_file": "scripts/a.py"}],
        requirement_allocations=allocations,
        requirement_channels=channels,
        semantic_review={"passed": False, "issues": [_review_issue(
            requirement_id="R2", repair_scope="allocation",
        )], "deferred_checks": []},
        planner_model="test",
    )
    assert result["requirement_channels"] == {"R1": "executable", "R2": "direct"}
    assert result["requirement_allocations"][0] == allocations[0]
    assert result["requirement_allocations"][1]["owners"] == []
