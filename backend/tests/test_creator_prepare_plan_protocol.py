import json
import re

import pytest
from fastapi import HTTPException

from backend.services.creator import api
from backend.services.creator.common import AnalyzeBlueprintResponse, AssetRequirementOut, FileSpecOut


def _request(**kwargs):
    data = {"user_request": "做一个工具", "human_feedback": "", "model": None}
    data.update(kwargs)
    return api.PreparePlanRequest(**data)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("second_issue", "expected_repeated"),
    [
        ({"code": "issue_a", "path": "scripts/a.py", "message": "still invalid"}, True),
        ({"code": "issue_b", "path": "workflow", "message": "new issue"}, False),
    ],
)
async def test_blueprint_repair_marks_only_truly_repeated_issues_unresolved(
    monkeypatch, second_issue, expected_repeated,
):
    initial_issue = {"code": "issue_a", "path": "scripts/a.py", "message": "invalid field"}
    prompts = []
    responses = iter(["candidate one", "candidate two"])

    async def fake_complete(messages, *_args, **_kwargs):
        prompts.append(json.loads(messages[1]["content"]))
        return json.dumps({"internal_blueprint_text": next(responses)})

    validation_results = iter([[second_issue], []])
    monkeypatch.setattr(api, "complete_creator_role_once", fake_complete)
    monkeypatch.setattr(api, "_prepare_repair_candidate_is_valid", lambda _candidate: True)
    monkeypatch.setattr(api, "_normalize_prepare_blueprint_references", lambda candidate: candidate)
    monkeypatch.setattr(api, "_preflight_prepare_blueprint_text", lambda _candidate: next(validation_results))

    repaired = await api._repair_prepare_blueprint_protocol(
        request=_request(),
        blueprint_text="original blueprint",
        protocol_errors=[initial_issue],
    )

    assert repaired == "candidate two"
    repeated = prompts[1]["remaining_issues_from_previous_repair"]
    assert bool(repeated) is expected_repeated
    assert ("remains unresolved" in prompts[1]["repair_directive"]) is expected_repeated


async def _async_result(value):
    return value


def _ready_blueprint(paths="- path: `SKILL.md`\n  role: skill_overview\n  inputs: [user_request]\n  outputs: [workflow]\n  dependencies: []\n  required_capabilities: []\n  forbidden_capabilities: [hidden_runtime_protocol]\n  references: []"):
    return f"""## 📋 Skill 架构蓝图
### 基本信息
- **Skill 名称**: demo-skill
### I/O 契约
- **输入**: runtime text
- **输出**: JSON
- **触发词**: 处理文本
### 目录结构
[当前 Skill 根目录]
├── SKILL.md
### 工作流逻辑
1. 处理输入
### SkillPlan / 文件职责计划
{paths}
### 宿主执行方式
- **直接回答**: 返回结果
- **需要脚本/命令**: 无
- **禁止隐式执行**: 遵守
- **执行后回答**: 返回
### 资源清单
- [ ] 无
"""


def _plan(path="SKILL.md", assets=None):
    return AnalyzeBlueprintResponse(
        skill_name="demo-skill",
        files=[FileSpecOut(path=path, purpose="overview", required=True, can_skip=False)],
        warnings=[],
        asset_requirements=assets or [],
        blueprint_text=_ready_blueprint(),
    )


def test_prepare_question_protocol_helpers_require_options_and_supplement():
    questions = ["输入来源希望支持哪种？A. 粘贴文本 B. 上传文件", "还有其他需要补充的要求吗？A. 没有 B. 有，我补充说明"]
    assert api._prepare_questions_have_options(questions)
    assert api._prepare_questions_include_supplement_check(questions)


def test_prepare_questions_multiple_model_questions_are_truncated_to_one():
    questions = api._normalize_prepare_clarifying_questions([
        "输入来源希望支持哪种？A. 粘贴文本 B. 上传文件",
        "输出格式希望是哪种？A. JSON B. Markdown",
        "还有其他需要补充的要求吗？A. 没有 B. 有，我补充说明",
    ])
    assert questions == ["输入来源希望支持哪种？A. 粘贴文本 B. 上传文件"]
    assert api._prepare_questions_have_options(questions)


def test_prepare_questions_do_not_auto_append_supplement_question():
    questions = api._normalize_prepare_clarifying_questions(["输出格式希望是哪种？A. JSON B. Markdown"])
    assert len(questions) == 1
    assert "补充" not in questions[0]


def test_prepare_question_without_options_gets_options_on_same_question_only():
    questions = api._normalize_prepare_clarifying_questions(["输入是什么？"])
    assert len(questions) == 1
    assert questions[0].startswith("输入是什么？")
    assert api._prepare_questions_have_options(questions)


def test_prepare_single_supplement_question_is_preserved():
    questions = api._normalize_prepare_clarifying_questions([api._PREPARE_SUPPLEMENT_QUESTION])
    assert questions == [api._PREPARE_SUPPLEMENT_QUESTION]


def test_preflight_rejects_asset_placeholder_and_directory_paths():
    assert any(i["code"] == "invalid_asset_placeholder_path" for i in api._preflight_prepare_blueprint_text(_ready_blueprint("- path: `assets/<name.ext>`\n  role: asset\n  source: user_upload")))
    assert any(i["code"] == "invalid_asset_directory_path" for i in api._preflight_prepare_blueprint_text(_ready_blueprint("- path: `assets/`\n  role: asset\n  source: user_upload")))


def test_preflight_does_not_treat_prose_path_as_skillplan_path():
    text = _ready_blueprint("- path: `SKILL.md`\n  role: skill_overview") + "\n运行时每次上传的用户输入文件 assets/input.pdf\n"
    codes = {i["code"] for i in api._preflight_prepare_blueprint_text(text)}
    assert "directory_or_text_path_missing_from_skill_plan" not in codes


def test_preflight_protocol_example_does_not_expand_strict_fileplan():
    text = _ready_blueprint(
        "- path: `SKILL.md`\n  role: skill_overview\n"
        "- path: `scripts/x.py`\n  role: script"
    ) + "\n宿主执行方式：reference 文件使用普通相对路径，例如 `references/example_reference.md`\n"

    assert api._preflight_prepare_blueprint_text(text) == []


def test_preflight_reference_field_still_requires_declared_skillplan_path():
    text = _ready_blueprint(
        "- path: `scripts/x.py`\n"
        "  role: script\n"
        "  dependencies: []\n"
        "  references: [references/actual.md]"
    )

    codes = {issue["code"] for issue in api._preflight_prepare_blueprint_text(text)}
    assert "reference_missing_from_skill_plan" in codes


def test_preflight_directory_display_does_not_expand_strict_fileplan():
    text = _ready_blueprint() + "\n目录示例：\n├── references/\n└── assets/\n具体示例 `references/example.md`\n"

    assert api._preflight_prepare_blueprint_text(text) == []


def test_preflight_resource_source_uses_structured_parser_for_both_field_names():
    asset = _ready_blueprint(
        "- path: `assets/static.resource`\n  role: asset\n  asset_source: user_upload"
    )
    reference = _ready_blueprint(
        "- path: `references/guide.resource`\n  role: reference\n  source: bundled"
    )

    assert "asset_missing_source" not in {
        issue["code"] for issue in api._preflight_prepare_blueprint_text(asset)
    }
    assert "reference_static_source_conflict" in {
        issue["code"] for issue in api._preflight_prepare_blueprint_text(reference)
    }


@pytest.mark.asyncio
async def test_needs_clarification_response_has_one_optioned_question(monkeypatch):
    async def fake_generate(_request):
        return {"status": "needs_clarification", "clarifying_questions": ["输入是什么？", api._PREPARE_SUPPLEMENT_QUESTION]}
    monkeypatch.setattr(api, "_generate_internal_blueprint_or_questions", fake_generate)
    resp = await api.prepare_plan(_request())
    assert resp.status == "needs_clarification"
    assert len(resp.clarifying_questions) == 1
    assert api._prepare_questions_have_options(resp.clarifying_questions)
    assert resp.clarifying_questions[0].startswith("输入是什么？")


@pytest.mark.asyncio
async def test_under_limit_needs_clarification_drops_model_review_summary(monkeypatch):
    async def fake_generate(_request):
        return {
            "status": "needs_clarification",
            "clarifying_questions": ["输入来源？A. 粘贴文本 B. 上传文件"],
            "review_summary": {"goal": "不应提前展示", "input": "x", "output": "y", "risks": ["hidden"]},
        }
    monkeypatch.setattr(api, "_generate_internal_blueprint_or_questions", fake_generate)
    resp = await api.prepare_plan(_request())
    assert resp.status == "needs_clarification"
    assert resp.clarifying_questions == ["输入来源？A. 粘贴文本 B. 上传文件"]
    assert resp.review_summary == api.PreparePlanReviewSummary()


@pytest.mark.asyncio
async def test_direct_ready_first_returns_creation_points_confirmation(monkeypatch):
    async def fake_generate(_request):
        return {"status": "ready", "internal_blueprint_text": _ready_blueprint(), "skill_name": "demo-skill"}
    async def fake_summary(**kwargs):
        return api.PreparePlanReviewSummary(goal="目标功能", input="运行时输入", output="JSON", risks=["hidden risk"])
    async def fake_analyze(_request):
        raise AssertionError("analyze_blueprint should not run before creation points confirmation")

    monkeypatch.setattr(api, "_generate_internal_blueprint_or_questions", fake_generate)
    monkeypatch.setattr(api, "_prepare_summarize_confirmed_requirements", fake_summary)
    monkeypatch.setattr(api, "analyze_blueprint", fake_analyze)

    resp = await api.prepare_plan(_request())

    assert resp.status == "needs_clarification"
    assert resp.prepare_stage == "creation_points_confirmation"
    assert resp.clarifying_questions == [api._PREPARE_SUPPLEMENT_QUESTION]
    assert resp.review_summary.goal == "目标功能"
    assert resp.review_summary.risks == []
    assert resp.files == []


@pytest.mark.asyncio
async def test_feedback_wants_supplement_blocks_ready(monkeypatch):
    async def fake_generate(_request):
        return {"status": "ready", "internal_blueprint_text": _ready_blueprint()}
    async def fake_analyze(_request):
        raise AssertionError("analyze_blueprint should not be called before supplement content exists")
    monkeypatch.setattr(api, "_generate_internal_blueprint_or_questions", fake_generate)
    monkeypatch.setattr(api, "analyze_blueprint", fake_analyze)
    resp = await api.prepare_plan(_request(human_feedback="问题：还有其他需要补充的要求吗？\n选择：B. 有，我补充说明"))
    assert resp.status == "needs_clarification"
    assert len(resp.clarifying_questions) == 1
    assert "请补充你的其他要求" in resp.clarifying_questions[0]


@pytest.mark.asyncio
async def test_asset_placeholder_ready_returns_confirmation_not_blocked(monkeypatch):
    async def fake_generate(_request):
        return {"status": "ready", "internal_blueprint_text": _ready_blueprint("- path: `assets/<name.ext>`\n  role: asset\n  source: user_upload")}
    async def no_repair(**kwargs):
        return kwargs["blueprint_text"]
    monkeypatch.setattr(api, "_generate_internal_blueprint_or_questions", fake_generate)
    monkeypatch.setattr(api, "_repair_prepare_blueprint_protocol", no_repair)
    resp = await api.prepare_plan(_request())
    assert resp.status == "needs_clarification"
    assert resp.review_summary.risks == []
    assert "要点" in resp.clarifying_questions[0] or "按这些" in resp.clarifying_questions[0]


@pytest.mark.asyncio
async def test_analyze_blueprint_error_returns_confirmation_not_blocked(monkeypatch):
    async def fake_generate(_request):
        return {"status": "ready", "internal_blueprint_text": _ready_blueprint()}
    async def fake_analyze(_request):
        raise HTTPException(status_code=400, detail="invalid blueprint")
    async def no_repair(**kwargs):
        return kwargs["blueprint_text"]
    monkeypatch.setattr(api, "_generate_internal_blueprint_or_questions", fake_generate)
    monkeypatch.setattr(api, "analyze_blueprint", fake_analyze)
    monkeypatch.setattr(api, "_repair_prepare_blueprint_protocol", no_repair)
    resp = await api.prepare_plan(_request())
    assert resp.status == "needs_clarification"


@pytest.mark.asyncio
async def test_ready_uses_strict_analyze_plan_and_filters_dynamic_paths(monkeypatch):
    calls = []
    async def fake_generate(_request):
        return {"status": "ready", "internal_blueprint_text": _ready_blueprint()}
    async def fake_analyze(request):
        calls.append(request)
        return _plan(path="SKILL.md")
    monkeypatch.setattr(api, "_generate_internal_blueprint_or_questions", fake_generate)
    monkeypatch.setattr(api, "analyze_blueprint", fake_analyze)
    resp = await api.prepare_plan(_request(human_feedback="A. 没有，按上面的选择继续"))
    assert resp.status == "ready"
    assert calls and calls[0].strict is True
    assert resp.review_summary.files_to_create_or_update == ["SKILL.md"]
    assert not any("<" in p or p == "assets/" for p in resp.review_summary.files_to_create_or_update)
    assert resp.review_summary.assets_to_upload == []

@pytest.mark.asyncio
async def test_limit_reached_returns_summary_confirmation_not_business_question(monkeypatch):
    async def fake_generate(_request):
        return {"status": "needs_clarification", "clarifying_questions": ["输出格式？A. JSON B. Markdown"]}
    async def fake_summary(**kwargs):
        return api.PreparePlanReviewSummary(goal="目标功能", input="运行时输入", output="JSON", risks=["should drop"])
    monkeypatch.setattr(api, "_generate_internal_blueprint_or_questions", fake_generate)
    monkeypatch.setattr(api, "_prepare_summarize_confirmed_requirements", fake_summary)
    history = [
        {"role": "assistant", "content": "我还需要确认一个必要信息：\n输入？A. 文本 B. 文件"},
        {"role": "assistant", "content": "我还需要确认一个必要信息：\n输出？A. JSON B. Markdown"},
    ]
    resp = await api.prepare_plan(_request(conversation_history=history))
    assert resp.status == "needs_clarification"
    assert "补充" in resp.clarifying_questions[0]
    assert resp.review_summary.goal == "目标功能"


@pytest.mark.asyncio
async def test_supplement_content_resummarizes_and_asks_confirmation(monkeypatch):
    async def fake_generate(_request):
        return {"status": "needs_clarification", "clarifying_questions": ["输入？A. 文本 B. 文件"]}
    async def fake_summary(**kwargs):
        return api.PreparePlanReviewSummary(goal="更新后的创建要点", input="补充后的输入", output="JSON", risks=[])
    monkeypatch.setattr(api, "_generate_internal_blueprint_or_questions", fake_generate)
    monkeypatch.setattr(api, "_prepare_summarize_confirmed_requirements", fake_summary)
    resp = await api.prepare_plan(_request(human_feedback="问题：以上创建要点是否还需要补充？\n选择：B. 有，我补充说明\n补充：增加 CSV 输入"))
    assert resp.status == "needs_clarification"
    assert resp.review_summary.goal == "更新后的创建要点"
    assert "更新创建要点" in resp.clarifying_questions[0]


@pytest.mark.asyncio
async def test_supplement_limit_still_requires_creation_points_confirmation(monkeypatch):
    calls = []
    async def fake_generate(_request):
        return {"status": "needs_clarification", "clarifying_questions": ["输入？A. 文本 B. 文件"]}
    async def fake_summary(**kwargs):
        return api.PreparePlanReviewSummary(goal="最终要点", input="输入", output="JSON", risks=[])
    async def fake_blueprint(**kwargs):
        return {"status": "ready", "internal_blueprint_text": _ready_blueprint(), "skill_name": "demo-skill"}
    async def fake_analyze(request):
        calls.append(request)
        return _plan(path="SKILL.md")
    monkeypatch.setattr(api, "_generate_internal_blueprint_or_questions", fake_generate)
    monkeypatch.setattr(api, "_prepare_summarize_confirmed_requirements", fake_summary)
    monkeypatch.setattr(api, "_generate_internal_blueprint_from_confirmed_summary", fake_blueprint)
    monkeypatch.setattr(api, "analyze_blueprint", fake_analyze)
    history = [{"role": "user", "content": "补充：第一次补充"}]
    resp = await api.prepare_plan(_request(conversation_history=history, human_feedback="补充：第二次补充"))
    assert resp.status == "needs_clarification"
    assert resp.prepare_stage == "creation_points_confirmation"
    assert "补充" in resp.clarifying_questions[0]
    assert calls == []


@pytest.mark.asyncio
async def test_limit_reached_sets_creation_points_stage_and_strips_risks(monkeypatch):
    async def fake_generate(_request):
        return {"status": "needs_clarification", "clarifying_questions": ["输出？A. JSON B. Markdown"]}
    async def fake_summary(**kwargs):
        return api.PreparePlanReviewSummary(goal="目标功能", input="运行时输入", output="JSON", risks=["不展示"])
    monkeypatch.setattr(api, "_generate_internal_blueprint_or_questions", fake_generate)
    monkeypatch.setattr(api, "_prepare_summarize_confirmed_requirements", fake_summary)
    history = [
        {"role": "assistant", "content": "我还需要确认一个必要信息：输入？A. 文本 B. 文件"},
        {"role": "assistant", "content": "我还需要确认一个必要信息：输出？A. JSON B. Markdown"},
    ]
    resp = await api.prepare_plan(_request(conversation_history=history))
    assert resp.prepare_stage == "creation_points_confirmation"
    assert resp.review_summary.goal == "目标功能"
    assert resp.review_summary.risks == []


@pytest.mark.asyncio
async def test_confirm_after_supplement_generates_blueprint(monkeypatch):
    calls = []
    async def fake_generate(_request):
        return {"status": "needs_clarification", "clarifying_questions": ["输入？A. 文本 B. 文件"]}
    async def fake_summary(**kwargs):
        return api.PreparePlanReviewSummary(goal="确认后的要点", input="输入", output="JSON", risks=[])
    async def fake_blueprint(**kwargs):
        return {"status": "ready", "internal_blueprint_text": _ready_blueprint(), "skill_name": "demo-skill"}
    async def fake_analyze(request):
        calls.append(request)
        return _plan(path="SKILL.md")
    monkeypatch.setattr(api, "_generate_internal_blueprint_or_questions", fake_generate)
    monkeypatch.setattr(api, "_prepare_summarize_confirmed_requirements", fake_summary)
    monkeypatch.setattr(api, "_generate_internal_blueprint_from_confirmed_summary", fake_blueprint)
    monkeypatch.setattr(api, "analyze_blueprint", fake_analyze)
    resp = await api.prepare_plan(_request(human_feedback="问题：已根据补充内容更新创建要点。是否按这些要点继续？\n选择：A. 没有其他补充，按这些要点继续"))
    assert resp.status == "ready"
    assert resp.prepare_stage == "ready"
    assert calls and calls[0].strict is True

@pytest.mark.asyncio
async def test_prepare_action_confirm_ignores_full_question_ab_text(monkeypatch):
    calls = []
    async def fake_generate(_request):
        return {"status": "needs_clarification", "clarifying_questions": ["输入？A. 文本 B. 文件"]}
    async def fake_summary(**kwargs):
        return api.PreparePlanReviewSummary(goal="确认后的要点", input="输入", output="JSON", risks=[])
    async def fake_blueprint(**kwargs):
        return {"status": "ready", "internal_blueprint_text": _ready_blueprint(), "skill_name": "demo-skill"}
    async def fake_analyze(request):
        calls.append(request)
        return _plan(path="SKILL.md")
    monkeypatch.setattr(api, "_generate_internal_blueprint_or_questions", fake_generate)
    monkeypatch.setattr(api, "_prepare_summarize_confirmed_requirements", fake_summary)
    monkeypatch.setattr(api, "_generate_internal_blueprint_from_confirmed_summary", fake_blueprint)
    monkeypatch.setattr(api, "analyze_blueprint", fake_analyze)
    feedback = "问题：以上创建要点是否还需要补充？A. 没有，按这些要点继续 B. 有，我补充说明\n选择：A. 没有，按这些要点继续"
    resp = await api.prepare_plan(_request(human_feedback=feedback, prepare_action="confirm"))
    assert resp.status == "ready"
    assert resp.prepare_stage == "ready"
    assert calls
    assert "请补充你的其他要求" not in resp.clarifying_questions


@pytest.mark.asyncio
async def test_prepare_action_request_supplement_ignores_a_text_and_skips_analyze(monkeypatch):
    async def fake_generate(_request):
        raise AssertionError("model should not run while waiting for supplement text")
    monkeypatch.setattr(api, "_generate_internal_blueprint_or_questions", fake_generate)
    feedback = "问题：以上创建要点是否还需要补充？A. 没有，按这些要点继续 B. 有，我补充说明\n选择：B. 有，我补充说明"
    function_items = [_abstract_function_item("scripts/a.py")]
    responsibility_edges = [{"from_node":"scripts/a.py","from_output":"semantic_result","to_node":"platform_output_node","to_input":"text","purpose":"deliver","constraints":[]}]
    resp = await api.prepare_plan(_request(
        human_feedback=feedback,
        prepare_action="request_supplement",
        function_items=function_items,
        responsibility_edges=responsibility_edges,
    ))
    assert resp.status == "needs_clarification"
    assert "请补充你的其他要求" in resp.clarifying_questions[0]
    assert resp.files == []
    assert resp.function_items == function_items
    assert resp.responsibility_edges == responsibility_edges


@pytest.mark.asyncio
async def test_prepare_action_submit_supplement_resummarizes_without_analyze(monkeypatch):
    async def fake_generate(_request):
        raise AssertionError("model blueprint generation should not run for submitted supplement confirmation")
    async def fake_summary(**kwargs):
        return api.PreparePlanReviewSummary(goal="更新后的创建要点", input="补充后的输入", output="JSON", risks=[])
    monkeypatch.setattr(api, "_generate_internal_blueprint_or_questions", fake_generate)
    monkeypatch.setattr(api, "_prepare_summarize_confirmed_requirements", fake_summary)
    resp = await api.prepare_plan(_request(human_feedback="补充：增加 CSV 输入", prepare_action="submit_supplement"))
    assert resp.status == "needs_clarification"
    assert resp.prepare_stage == "supplement_confirmation"
    assert resp.review_summary.goal == "更新后的创建要点"
    assert resp.files == []


def test_legacy_choice_line_prevents_question_option_b_from_overriding_a():
    feedback = "问题：以上创建要点是否还需要补充？A. 没有，按这些要点继续 B. 有，我补充说明\n选择：A. 没有，按这些要点继续"
    request = _request(human_feedback=feedback)
    assert api._prepare_user_confirmed_no_more_supplement(request)
    assert not api._prepare_feedback_wants_supplement(request)


def _file(path, *, asset_source=""):
    return FileSpecOut(path=path, purpose="test", required=True, can_skip=False, asset_source=asset_source)


@pytest.mark.asyncio
async def test_ready_syncs_review_summary_files_from_skill_plan_references(monkeypatch):
    async def fake_generate(_request):
        return {
            "status": "ready",
            "internal_blueprint_text": _ready_blueprint(),
            "review_summary": {"files_to_create_or_update": ["SKILL.md", "scripts/process.py"]},
        }

    async def fake_analyze(_request):
        return AnalyzeBlueprintResponse(
            skill_name="demo-skill",
            files=[_file("SKILL.md"), _file("scripts/process.py"), _file("references/output-patterns.md")],
            warnings=[],
            asset_requirements=[],
            blueprint_text=_ready_blueprint(),
        )

    monkeypatch.setattr(api, "_generate_internal_blueprint_or_questions", fake_generate)
    monkeypatch.setattr(api, "analyze_blueprint", fake_analyze)

    resp = await api.prepare_plan(_request(human_feedback="A. 没有，按上面的选择继续"))

    assert resp.status == "ready"
    assert resp.review_summary.files_to_create_or_update == ["SKILL.md", "scripts/process.py", "references/output-patterns.md"]
    assert [f.path for f in resp.files] == ["SKILL.md", "scripts/process.py", "references/output-patterns.md"]


@pytest.mark.asyncio
async def test_ready_does_not_add_summary_hallucinated_file_to_execution_plan(monkeypatch):
    async def fake_generate(_request):
        return {
            "status": "ready",
            "internal_blueprint_text": _ready_blueprint(),
            "review_summary": {"files_to_create_or_update": ["SKILL.md", "scripts/extra.py"]},
        }

    async def fake_analyze(_request):
        return AnalyzeBlueprintResponse(
            skill_name="demo-skill",
            files=[_file("SKILL.md")],
            warnings=[],
            asset_requirements=[],
            blueprint_text=_ready_blueprint(),
        )

    monkeypatch.setattr(api, "_generate_internal_blueprint_or_questions", fake_generate)
    monkeypatch.setattr(api, "analyze_blueprint", fake_analyze)

    resp = await api.prepare_plan(_request(human_feedback="A. 没有，按上面的选择继续"))

    assert resp.status == "ready"
    assert resp.review_summary.files_to_create_or_update == ["SKILL.md"]
    assert [f.path for f in resp.files] == ["SKILL.md"]
    assert any(w.get("code") == "summary_files_not_in_skill_plan" and "scripts/extra.py" in w.get("files", []) for w in resp.warnings)


def test_summary_hallucinated_asset_is_not_projected_to_uploads():
    summary = api.PreparePlanReviewSummary(
        files_to_create_or_update=["SKILL.md"],
        assets_to_upload=["assets/hallucinated.png"],
    )

    warnings = api._sync_prepare_summary_files_from_skill_plan(
        summary,
        [_file("SKILL.md")],
    )

    assert summary.assets_to_upload == []
    assert any(w.get("code") == "summary_asset_not_in_file_plan" and "assets/hallucinated.png" in w.get("files", []) for w in warnings)


def test_required_user_upload_asset_missing_is_detectable_before_ready():
    missing = api._required_file_plan_user_upload_asset_paths(
        [_file("SKILL.md"), _file("assets/template.png", asset_source="user_upload")]
    )

    assert missing == ["assets/template.png"]


@pytest.mark.asyncio
async def test_prepare_plan_blocks_ready_when_required_user_upload_asset_missing(monkeypatch):
    blueprint = _ready_blueprint(_skill_plan_block(""))

    async def fake_generate(_request):
        return {
            "status": "ready",
            "internal_blueprint_text": blueprint,
            "skill_name": "demo-skill",
            "review_summary": {
                "files_to_create_or_update": ["SKILL.md"],
                "assets_to_upload": ["assets/template.png", "assets/hallucinated.png"],
            },
        }

    async def fake_analyze(_request):
        return AnalyzeBlueprintResponse(
            skill_name="demo-skill",
            files=[
                _file("SKILL.md"),
                _file("assets/template.png", asset_source="user_upload"),
            ],
            warnings=[],
            asset_requirements=[AssetRequirementOut(path="assets/template.png", source="user_explicit", required=True)],
            blueprint_text=blueprint,
        )

    monkeypatch.setattr(api, "_generate_internal_blueprint_or_questions", fake_generate)
    monkeypatch.setattr(api, "analyze_blueprint", fake_analyze)

    resp = await api.prepare_plan(
        _request(
            prepare_action="confirm",
            previous_blueprint_text=blueprint,
            human_feedback="A. 没有，按上面的选择继续",
            uploaded_files=[],
            function_items=[],
            responsibility_edges=[],
        )
    )

    assert resp.status != "ready"
    assert resp.prepare_stage == "asset_upload_required"
    assert resp.review_summary.assets_to_upload == ["assets/template.png"]
    assert any(
        isinstance(blocker, dict)
        and blocker.get("code") == "required_asset_not_uploaded"
        for blocker in resp.creation_blockers
    )


def test_no_user_upload_asset_keeps_assets_to_upload_empty():
    summary = api.PreparePlanReviewSummary(assets_to_upload=["assets/bundled.png"])

    api._sync_prepare_summary_files_from_skill_plan(
        summary,
        [_file("SKILL.md"), _file("assets/bundled.png", asset_source="bundled")],
    )

    assert summary.assets_to_upload == []


def test_bundled_asset_requires_real_inventory_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "bundled_skills_path", tmp_path)
    files = [_file("assets/template.png", asset_source="bundled")]
    assert api._unresolved_bundled_asset_paths(files, skill_name="demo") == ["assets/template.png"]
    asset = tmp_path / "demo" / "assets" / "template.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"real bundled source")
    assert api._unresolved_bundled_asset_paths(files, skill_name="demo") == []


def test_sync_prepare_summary_files_filters_directories_and_dynamic_paths():
    summary = api.PreparePlanReviewSummary(files_to_create_or_update=["SKILL.md"])
    warnings = api._sync_prepare_summary_files_from_skill_plan(summary, [
        _file("SKILL.md"),
        _file("scripts/"),
        _file("references/"),
        _file("assets/"),
        _file("scripts/${name}.py"),
        _file("references/[file].md"),
        _file("assets/logo.png"),
        _file("assets/bundled.png", asset_source="bundled"),
        _file("references/output-patterns.md"),
    ])

    assert warnings == []
    assert summary.files_to_create_or_update == ["SKILL.md", "assets/bundled.png", "references/output-patterns.md"]
    assert "scripts/" not in summary.files_to_create_or_update
    assert "references/" not in summary.files_to_create_or_update
    assert "assets/" not in summary.files_to_create_or_update
    assert not any("${" in path or "[" in path for path in summary.files_to_create_or_update)


def _blueprint_with_reference_mention(reference_line: str) -> str:
    return _ready_blueprint() + f"\n{reference_line}\n"


def _assert_normalized_reference_block(text: str, path: str):
    normalized = api._normalize_prepare_blueprint_references(text)
    assert f"- path: `{path}`" in normalized
    assert normalized.index(f"- path: `{path}`") < normalized.index("### 宿主执行方式")
    block_match = re.search(rf"(?ms)^- path: `{re.escape(path)}`\n(?P<block>.*?)(?=^- path:|^### |\Z)", normalized)
    assert block_match, normalized
    block = block_match.group("block")
    for field in [
        "role: reference",
        "inputs: []",
        "outputs: []",
        "dependencies: []",
        "required_capabilities: []",
        "forbidden_capabilities: []",
        "references: []",
    ]:
        assert field in block
    assert not any(i["code"] == "directory_or_text_path_missing_from_skill_plan" and i["path"] == path for i in api._preflight_prepare_blueprint_text(normalized))


def test_normalize_prepare_references_adds_dependency_reference_to_skill_plan():
    text = _ready_blueprint("- path: `scripts/process.py`\n  role: script\n  inputs: []\n  outputs: []\n  dependencies: [references/test-dependency-boundary.md]\n  required_capabilities: []\n  forbidden_capabilities: []\n  references: []")
    _assert_normalized_reference_block(text, "references/test-dependency-boundary.md")


def test_normalize_prepare_references_adds_references_field_reference_to_skill_plan():
    text = _ready_blueprint("- path: `scripts/process.py`\n  role: script\n  inputs: []\n  outputs: []\n  dependencies: []\n  required_capabilities: []\n  forbidden_capabilities: []\n  references: [references/test-reference-boundary.md]")
    _assert_normalized_reference_block(text, "references/test-reference-boundary.md")


def test_normalize_prepare_references_is_idempotent():
    path = "references/test-idempotent-boundary.md"
    text = _ready_blueprint(f"- path: `scripts/process.py`\n  role: script\n  inputs: []\n  outputs: []\n  dependencies: [{path}]\n  required_capabilities: []\n  forbidden_capabilities: []\n  references: []")
    once = api._normalize_prepare_blueprint_references(text)
    twice = api._normalize_prepare_blueprint_references(once)
    assert once == twice
    assert twice.count(f"- path: `{path}`") == 1


def test_normalize_prepare_references_does_not_add_incidental_mentions():
    text = _blueprint_with_reference_mention("资源展示：- [ ] references/test-incidental-resource.md\n正文提到 `references/test-incidental-prose.md`，但 SkillPlan 没有显式 dependencies/references。")
    normalized = api._normalize_prepare_blueprint_references(text)
    assert "- path: `references/test-incidental-resource.md`" not in normalized
    assert "- path: `references/test-incidental-prose.md`" not in normalized
    assert normalized == text.strip()


def test_normalize_prepare_references_ignores_wildcards_and_placeholders():
    text = _ready_blueprint("- path: `scripts/process.py`\n  role: script\n  inputs: []\n  outputs: []\n  dependencies: [references/*.md, references/<name>.md, references/[file].md, references/]\n  required_capabilities: []\n  forbidden_capabilities: []\n  references: []")
    normalized = api._normalize_prepare_blueprint_references(text)
    assert "- path: `references/*.md`" not in normalized
    assert "- path: `references/<name>.md`" not in normalized
    assert "- path: `references/[file].md`" not in normalized
    assert normalized == text.strip()


def test_preflight_missing_skill_plan_message_includes_path():
    text = _ready_blueprint("- path: `scripts/process.py`\n  role: script\n  inputs: []\n  outputs: []\n  dependencies: [references/test-missing-preflight.md]\n  required_capabilities: []\n  forbidden_capabilities: []\n  references: []")
    issues = api._preflight_prepare_blueprint_text(text)
    issue = next(i for i in issues if i["code"] == "dependency_missing_from_skill_plan")
    assert issue["path"] == "references/test-missing-preflight.md"
    assert "references/test-missing-preflight.md" in issue["message"]


def _script_plan_block(path: str) -> str:
    return (
        f"- path: `{path}`\n"
        "  role: generic_script\n"
        "  inputs: []\n"
        "  outputs: [result]\n"
        "  dependencies: []\n"
        "  required_capabilities: []\n"
        "  forbidden_capabilities: []\n"
        "  references: []\n"
        "  constraints: []"
    )


def _skill_plan_block(extra_blocks: str) -> str:
    return (
        "- path: `SKILL.md`\n"
        "  role: skill_overview\n"
        "  inputs: []\n"
        "  outputs: []\n"
        "  dependencies: []\n"
        "  required_capabilities: []\n"
        "  forbidden_capabilities: []\n"
        "  references: []\n"
        "  constraints: []\n"
        f"{extra_blocks}"
    )


def _ready_payload(blueprint: str) -> dict:
    return {
        "status": "ready",
        "clarifying_questions": [],
        "review_summary": {},
        "internal_blueprint_text": blueprint,
        "skill_name": "demo",
        "blockers": [],
    }


def _function_item(path: str) -> dict:
    return {
        "target_file": path,
        "role": "generic_script",
        "purpose": f"run {path}",
        "inputs": [],
        "outputs": ["result"],
        "required_capabilities": [],
        "constraints": [],
    }


def _output_edge(path: str) -> dict:
    return {
        "from_node": path,
        "from_output": "result",
        "to_node": "platform_output_node",
        "to_input": "file_outputs",
        "purpose": "deliver result",
        "constraints": [],
    }


def _semantic_closure_response(messages):
    """Return default semantic protocol responses without consuming graph mocks."""
    import json

    system = str(messages[0].get("content") or "") if messages else ""
    if "requirement coverage projection" in system:
        payload = json.loads(messages[1]["content"])
        owners = [
            item["target_file"]
            for item in payload.get("function_items") or []
            if item.get("target_file")
        ]
        return {"requirement_allocations": [{
            "requirement_id": "R1", "requirement": "完成核心责任",
            "owners": owners,
            "evidence": {"responsibility": "完成责任", "outputs": [], "capabilities": []},
        }]}
    if "semantic coverage Reviewer" in system:
        return {"passed": True, "issues": []}
    return None


def _mock_creator_completion(monkeypatch, fake_complete):
    async def fake_role(messages, role, fallback_model):
        return await fake_complete(messages, fallback_model)

    monkeypatch.setattr(api, "complete_chat_once", fake_complete)
    monkeypatch.setattr(api, "complete_creator_role_once", fake_role)


class _SemanticClosureGraphCalled(BaseException):
    pass


def _with_script_purpose(blueprint, target, purpose):
    marker = f"- path: `{target}`\n  role: generic_script"
    return blueprint.replace(marker, marker + f"\n  purpose: {purpose}")


async def _run_semantic_closure_until_graph(
    monkeypatch, *, reviews, replanned_blueprint=None, initial_blueprint=None,
    allocation_responses=None,
):
    import json

    initial = initial_blueprint or _ready_blueprint(
        _skill_plan_block("\n" + _script_plan_block("scripts/a.py"))
    )
    allocations = 0
    review_calls = 0
    replan_calls = 0
    graph_calls = 0
    blueprint_calls = 0

    async def initial_planner(messages, model):
        return json.dumps(_ready_payload(initial))

    async def semantic_models(messages, role, fallback_model):
        nonlocal allocations, review_calls, replan_calls, blueprint_calls
        system = str(messages[0].get("content") or "")
        if "requirement coverage projection" in system:
            allocations += 1
            if allocation_responses is not None:
                return json.dumps(allocation_responses[allocations - 1])
            return json.dumps({"requirement_allocations": [{
                "requirement_id": "R1", "requirement": "完成核心责任",
                "owners": ["scripts/a.py"],
                "evidence": {"responsibility": "完成责任", "outputs": [], "capabilities": []},
            }]})
        if "semantic coverage Reviewer" in system:
            result = reviews[review_calls]
            review_calls += 1
            return json.dumps(result)
        if "localized semantic replan" in system:
            replan_calls += 1
            return json.dumps(replanned_blueprint)
        if blueprint_calls == 0:
            blueprint_calls += 1
            return json.dumps(_ready_payload(initial))
        raise AssertionError("Unexpected model call occurred before graph binding sentinel")

    async def graph(**kwargs):
        nonlocal graph_calls
        graph_calls += 1
        raise _SemanticClosureGraphCalled()

    monkeypatch.setattr(api, "complete_chat_once", initial_planner)
    monkeypatch.setattr(api, "complete_creator_role_once", semantic_models)
    monkeypatch.setattr(api, "_bind_executable_responsibility_plan", graph)
    try:
        await api._generate_internal_blueprint_or_questions(_request())
    except _SemanticClosureGraphCalled:
        pass
    return {
        "initial": initial, "allocations": allocations, "reviews": review_calls,
        "replans": replan_calls, "graphs": graph_calls,
    }


@pytest.mark.asyncio
async def test_semantic_closure_pass_calls_graph_once_without_replan(monkeypatch):
    result = await _run_semantic_closure_until_graph(
        monkeypatch, reviews=[{"passed": True, "issues": []}],
    )
    assert result == {**result, "allocations": 1, "reviews": 1, "replans": 0, "graphs": 1}


@pytest.mark.asyncio
async def test_semantic_closure_replans_once_then_calls_graph(monkeypatch):
    initial = _ready_blueprint(_skill_plan_block("\n" + _script_plan_block("scripts/a.py")))
    revised = _with_script_purpose(initial, "scripts/a.py", "revised responsibility")
    result = await _run_semantic_closure_until_graph(
        monkeypatch,
        reviews=[
            {"passed": False, "issues": [{
                "issue_type": "responsibility_mismatch", "requirement_id": "R1",
                "affected_targets": ["scripts/a.py"], "reason": "mismatch", "repair_guidance": "revise",
            }]},
            {"passed": True, "issues": []},
        ],
        replanned_blueprint={
            "internal_blueprint_text": revised, "changed_targets": ["scripts/a.py"],
            "added_targets": [], "changed_resources": [],
        },
    )
    assert (result["allocations"], result["reviews"], result["replans"], result["graphs"]) == (2, 2, 1, 1)


@pytest.mark.asyncio
async def test_semantic_closure_second_failure_never_calls_graph(monkeypatch):
    initial = _ready_blueprint(_skill_plan_block("\n" + _script_plan_block("scripts/a.py")))
    revised = _with_script_purpose(initial, "scripts/a.py", "revised responsibility")
    failed = {"passed": False, "issues": [{
        "issue_type": "responsibility_mismatch", "requirement_id": "R1",
        "affected_targets": ["scripts/a.py"], "reason": "mismatch", "repair_guidance": "revise",
    }]}
    with pytest.raises(api.PreparePlanProtocolError, match="exactly one localized replan"):
        await _run_semantic_closure_until_graph(
            monkeypatch, reviews=[failed, failed], replanned_blueprint={
                "internal_blueprint_text": revised, "changed_targets": ["scripts/a.py"],
                "added_targets": [], "changed_resources": [],
            },
        )


@pytest.mark.asyncio
async def test_semantic_closure_scope_drift_never_calls_graph(monkeypatch):
    initial = _ready_blueprint(_skill_plan_block(
        "\n" + _script_plan_block("scripts/a.py") + "\n" + _script_plan_block("scripts/b.py")
    ))
    drifted = _with_script_purpose(
        _with_script_purpose(initial, "scripts/a.py", "revised responsibility"),
        "scripts/b.py", "unrelated change",
    )
    failed = {"passed": False, "issues": [{
        "issue_type": "responsibility_mismatch", "requirement_id": "R1",
        "affected_targets": ["scripts/a.py"], "reason": "mismatch", "repair_guidance": "revise",
    }]}
    with pytest.raises(api.PreparePlanProtocolError, match="outside blocking issue scope"):
        await _run_semantic_closure_until_graph(
            monkeypatch, reviews=[failed], initial_blueprint=initial, replanned_blueprint={
                "internal_blueprint_text": drifted,
                "changed_targets": ["scripts/a.py", "scripts/b.py"],
                "added_targets": [], "changed_resources": [],
            },
        )


@pytest.mark.asyncio
async def test_ownerless_allocation_drives_replan_then_graph(monkeypatch):
    initial = _ready_blueprint(_skill_plan_block(
        "\n" + _script_plan_block("scripts/a.py") + "\n" + _script_plan_block("scripts/b.py")
    ))
    revised = _ready_blueprint(_skill_plan_block(
        "\n" + _script_plan_block("scripts/a.py")
        + "\n" + _script_plan_block("scripts/b.py")
        + "\n" + _script_plan_block("scripts/c.py")
    ))
    evidence = {"responsibility": "", "outputs": [], "capabilities": []}
    first_allocations = {"requirement_allocations": [
        {"requirement_id": "R1", "requirement": "完成 A", "owners": ["scripts/a.py"], "evidence": evidence},
        {"requirement_id": "R2", "requirement": "完成 B", "owners": ["scripts/b.py"], "evidence": evidence},
        {"requirement_id": "R3", "requirement": "完成 C", "owners": [], "evidence": evidence},
    ]}
    second_allocations = {"requirement_allocations": [
        *first_allocations["requirement_allocations"][:2],
        {"requirement_id": "R3", "requirement": "完成 C", "owners": ["scripts/c.py"], "evidence": evidence},
    ]}
    result = await _run_semantic_closure_until_graph(
        monkeypatch,
        initial_blueprint=initial,
        allocation_responses=[first_allocations, second_allocations],
        reviews=[
            {"passed": False, "issues": [{
                "issue_type": "requirement_uncovered", "requirement_id": "R3",
                "affected_targets": [], "reason": "no owner", "repair_guidance": "add responsibility",
            }]},
            {"passed": True, "issues": []},
        ],
        replanned_blueprint={
            "internal_blueprint_text": revised,
            "changed_targets": [], "added_targets": ["scripts/c.py"], "changed_resources": [],
        },
    )
    assert (result["allocations"], result["reviews"], result["replans"], result["graphs"]) == (2, 2, 1, 1)


@pytest.mark.asyncio
async def test_binding_and_convergence_are_edge_only_over_frozen_function_items(monkeypatch):
    import json

    script_block = _script_plan_block("scripts/a.py").replace(
        "inputs: []", "inputs: [x]"
    ).replace("outputs: [result]", "outputs: [y]")
    blueprint = _ready_blueprint(_skill_plan_block("\n" + script_block))
    expanded_item = {
        **_function_item("scripts/a.py"),
        "inputs": ["x", "z"],
        "outputs": ["y"],
    }

    async def binding_response(messages, role, fallback_model):
        return json.dumps({"function_items": [expanded_item], "responsibility_edges": []})

    monkeypatch.setattr(api, "complete_creator_role_once", binding_response)
    with pytest.raises(ValueError, match="binding must return only responsibility_edges"):
        await api._bind_executable_responsibility_plan(
            request=_request(),
            current_planner_result=_ready_payload(blueprint),
            planner_model="planner",
            allowed_function_item_targets=["scripts/a.py"],
        )

    async def valid_binding_response(messages, role, fallback_model):
        return json.dumps({"responsibility_edges": []})

    monkeypatch.setattr(api, "complete_creator_role_once", valid_binding_response)
    bound = await api._bind_executable_responsibility_plan(
        request=_request(),
        current_planner_result=_ready_payload(blueprint),
        planner_model="planner",
        allowed_function_item_targets=["scripts/a.py"],
    )
    assert bound["function_items"][0]["inputs"] == ["x"]
    assert bound["function_items"][0]["outputs"] == ["y"]

    async def convergence_response(messages, role, fallback_model):
        return json.dumps({
            **_ready_payload(blueprint),
            "function_items": [expanded_item],
            "responsibility_edges": [],
        })

    monkeypatch.setattr(api, "complete_creator_role_once", convergence_response)
    with pytest.raises(ValueError, match="convergence must return only responsibility_edges"):
        await api._converge_ready_executable_plan(
            request=_request(),
            current_planner_result={
                **_ready_payload(blueprint),
                "function_items": [{
                    **_function_item("scripts/a.py"),
                    "inputs": ["x"],
                    "outputs": ["y"],
                }],
                "responsibility_edges": [],
            },
            planner_model="planner",
            allowed_function_item_targets=["scripts/a.py"],
            frozen_blueprint_text=blueprint,
        )


@pytest.mark.asyncio
async def test_incomplete_binding_target_set_does_not_emit_draft_and_convergence_can_fix(monkeypatch):
    import json

    blueprint = _ready_blueprint(_skill_plan_block("\n" + _script_plan_block("scripts/a.py") + "\n" + _script_plan_block("scripts/b.py")))
    responses = [
        _ready_payload(blueprint),
        {
            "function_items": [_function_item("scripts/a.py")],
            "responsibility_edges": [_output_edge("scripts/a.py")],
        },
        {
            **_ready_payload("ignored convergence blueprint"),
            "function_items": [_function_item("scripts/a.py"), _function_item("scripts/b.py")],
            "responsibility_edges": [_output_edge("scripts/a.py"), _output_edge("scripts/b.py")],
        },
    ]
    events = []

    async def fake_complete(messages, model, **kwargs):
        semantic = _semantic_closure_response(messages)
        return json.dumps(semantic if semantic is not None else responses.pop(0))

    async def emit(event):
        events.append(event)

    _mock_creator_completion(monkeypatch, fake_complete)
    monkeypatch.setattr(api, "_review_responsibility_graph_alignment", lambda **kwargs: _async_result({"passed": True, "issues": []}))
    monkeypatch.setattr(api, "_validate_responsibility_graph_boundary_presence", lambda *args: None)
    result = await api._generate_internal_blueprint_or_questions(_request(), event_emitter=emit)

    assert "planner_draft" not in [event["event"] for event in events]
    assert result["function_items"] == [_function_item("scripts/a.py"), _function_item("scripts/b.py")]
    assert result["internal_blueprint_text"] != "ignored convergence blueprint"


@pytest.mark.asyncio
async def test_incomplete_binding_target_set_does_not_fallback_when_convergence_fails(monkeypatch):
    import json

    blueprint = _ready_blueprint(_skill_plan_block("\n" + _script_plan_block("scripts/a.py") + "\n" + _script_plan_block("scripts/b.py")))
    responses = [
        _ready_payload(blueprint),
        {
            "function_items": [_function_item("scripts/a.py")],
            "responsibility_edges": [_output_edge("scripts/a.py")],
        },
        {
            **_ready_payload("ignored bad convergence"),
            "function_items": [_function_item("scripts/a.py")],
            "responsibility_edges": [_output_edge("scripts/a.py")],
        },
    ]
    events = []

    async def fake_complete(messages, model, **kwargs):
        semantic = _semantic_closure_response(messages)
        return json.dumps(semantic if semantic is not None else responses.pop(0))

    async def emit(event):
        events.append(event)

    _mock_creator_completion(monkeypatch, fake_complete)
    with pytest.raises(api.PreparePlanProtocolError):
        await api._generate_internal_blueprint_or_questions(_request(), event_emitter=emit)

    assert "planner_draft" not in [event["event"] for event in events]


@pytest.mark.asyncio
async def test_unexpected_binding_target_does_not_emit_draft_or_fallback(monkeypatch):
    import json

    blueprint = _ready_blueprint(_skill_plan_block("\n" + _script_plan_block("scripts/a.py")))
    responses = [
        _ready_payload(blueprint),
        {
            "function_items": [_function_item("scripts/a.py"), _function_item("scripts/fake.py")],
            "responsibility_edges": [_output_edge("scripts/a.py"), _output_edge("scripts/fake.py")],
        },
        {
            **_ready_payload("ignored bad convergence"),
            "function_items": [_function_item("scripts/a.py"), _function_item("scripts/fake.py")],
            "responsibility_edges": [_output_edge("scripts/a.py"), _output_edge("scripts/fake.py")],
        },
    ]
    events = []

    async def fake_complete(messages, model, **kwargs):
        semantic = _semantic_closure_response(messages)
        return json.dumps(semantic if semantic is not None else responses.pop(0))

    async def emit(event):
        events.append(event)

    _mock_creator_completion(monkeypatch, fake_complete)
    with pytest.raises(api.PreparePlanProtocolError):
        await api._generate_internal_blueprint_or_questions(_request(), event_emitter=emit)

    assert "planner_draft" not in [event["event"] for event in events]


@pytest.mark.asyncio
async def test_binding_uses_post_repair_file_plan_targets(monkeypatch):
    import json

    pre_repair_blueprint = _ready_blueprint(
        _skill_plan_block(
            "\n"
            "- path: `scripts/pre_repair.py`\n"
            "  role: generic_script\n"
            "  inputs: []\n"
            "  outputs: [result]\n"
            "  dependencies: [references/missing.md]\n"
            "  required_capabilities: []\n"
            "  forbidden_capabilities: []\n"
            "  references: []\n"
            "  constraints: []"
        )
    )
    repaired_blueprint = _ready_blueprint(
        _skill_plan_block("\n" + _script_plan_block("scripts/repaired.py"))
    )
    calls = []
    responses = [
        _ready_payload(pre_repair_blueprint),
        {"internal_blueprint_text": repaired_blueprint},
        {
            "function_items": [_function_item("scripts/repaired.py")],
            "responsibility_edges": [_output_edge("scripts/repaired.py")],
        },
        {
            **_ready_payload("ignored convergence blueprint"),
            "function_items": [_function_item("scripts/repaired.py")],
            "responsibility_edges": [_output_edge("scripts/repaired.py")],
        },
    ]

    async def fake_complete(messages, model, **kwargs):
        calls.append(messages)
        semantic = _semantic_closure_response(messages)
        return json.dumps(semantic if semantic is not None else responses.pop(0))

    _mock_creator_completion(monkeypatch, fake_complete)
    monkeypatch.setattr(api, "_review_responsibility_graph_alignment", lambda **kwargs: _async_result({"passed": True, "issues": []}))
    monkeypatch.setattr(api, "_validate_responsibility_graph_boundary_presence", lambda *args: None)
    result = await api._generate_internal_blueprint_or_questions(_request())

    binding_payload = next(
        json.loads(call[1]["content"])
        for call in calls
        if json.loads(call[1]["content"]).get("task") == "bind_executable_responsibilities"
    )
    assert binding_payload["allowed_function_item_targets"] == ["scripts/repaired.py"]
    assert "scripts/pre_repair.py" not in result["internal_blueprint_text"]
    assert result["function_items"] == [_function_item("scripts/repaired.py")]


@pytest.mark.asyncio
async def test_fileplan_repair_failure_stops_before_freeze_or_binding(monkeypatch):
    import json

    pre_repair_blueprint = _ready_blueprint(
        _skill_plan_block(
            "\n"
            "- path: `scripts/pre_repair.py`\n"
            "  role: generic_script\n"
            "  inputs: []\n"
            "  outputs: [result]\n"
            "  dependencies: [references/missing.md]\n"
            "  required_capabilities: []\n"
            "  forbidden_capabilities: []\n"
            "  references: []\n"
            "  constraints: []"
        )
    )
    calls = []
    events = []

    async def fake_complete(messages, model, **kwargs):
        calls.append(messages)
        return json.dumps(_ready_payload(pre_repair_blueprint))

    async def fail_repair(**kwargs):
        raise RuntimeError("repair unavailable")

    async def emit(event):
        events.append(event)

    _mock_creator_completion(monkeypatch, fake_complete)
    monkeypatch.setattr(api, "_repair_prepare_blueprint_protocol", fail_repair)

    with pytest.raises(api.PreparePlanProtocolError) as exc_info:
        await api._generate_internal_blueprint_or_questions(_request(), event_emitter=emit)

    assert "repair failed before executable target freeze" in str(exc_info.value)
    assert [event["event"] for event in events] == []
    assert len(calls) == 1

@pytest.mark.asyncio
async def test_blueprint_planner_prompt_includes_constraints_serialization_contract(monkeypatch):
    captured = []

    async def fake_complete(messages, model, **kwargs):
        captured.extend(messages)
        return '{"status":"needs_clarification","clarifying_questions":["输入来源？A. 粘贴 B. 上传"],"blockers":[]}'

    _mock_creator_completion(monkeypatch, fake_complete)
    await api._generate_internal_blueprint_or_questions(_request())

    prompt = captured[0]["content"]
    section = prompt[prompt.index("## responsibility constraints"):prompt.index("## 内部处理与脚本拆分")]
    assert "将每个 target-local constraint 写入对应 SkillPlan entry 的 constraints 字段" in section
    assert "constraints 必须是单行合法 JSON array" in section
    assert "不要限制 constraint 类型" in section
    assert "不要广播到所有 scripts" in section


@pytest.mark.asyncio
async def test_blueprint_planner_prompt_preserves_confirmed_decision_contract(monkeypatch):
    captured = []

    async def fake_complete(messages, model, **kwargs):
        captured.extend(messages)
        return '{"status":"needs_clarification","clarifying_questions":["输入来源？A. 粘贴 B. 上传"],"blockers":[]}'

    _mock_creator_completion(monkeypatch, fake_complete)
    await api._generate_internal_blueprint_or_questions(_request())

    prompt = captured[0]["content"]
    assert "confirmed decision" in prompt
    assert "不得返回 status=ready" in prompt and "core action" in prompt
    assert "clarification answer 不是参考意见" in prompt
    assert "Blueprint planning 输入契约" in prompt
    assert "不得通过修改 Blueprint 业务目标来规避工具缺失" in prompt
    assert "已确认的 runtime input、final output / artifact、required business actions" in prompt
    assert "已经问过并得到明确回答的问题不得再次询问" in prompt
    assert "优先写入对应 script 的 default_values" in prompt
    assert "不得自动提升为 runtime input" in prompt
    assert "不得为了让 ResponsibilityGraph provenance 闭合" in prompt


@pytest.mark.asyncio
async def test_blueprint_planner_prompt_requires_explicit_constraints_field(monkeypatch):
    captured = []

    async def fake_complete(messages, model, **kwargs):
        captured.extend(messages)
        return '{"status":"needs_clarification","clarifying_questions":["输入来源？A. 粘贴 B. 上传"],"blockers":[]}'

    _mock_creator_completion(monkeypatch, fake_complete)
    await api._generate_internal_blueprint_or_questions(_request())

    prompt = captured[0]["content"]
    section = prompt[prompt.index("## responsibility constraints"):prompt.index("## 内部处理与脚本拆分")]
    assert "每个 SkillPlan entry 都必须显式输出 constraints 字段" in section
    assert "constraints: []" in section


@pytest.mark.asyncio
async def test_blueprint_planner_prompt_requires_final_delivery_closure(monkeypatch):
    captured = []

    async def fake_complete(messages, model, **kwargs):
        captured.extend(messages)
        return '{"status":"needs_clarification","clarifying_questions":["输入来源？A. 粘贴 B. 上传"],"blockers":[]}'

    _mock_creator_completion(monkeypatch, fake_complete)
    await api._generate_internal_blueprint_or_questions(_request())

    prompt = captured[0]["content"]
    section = prompt[prompt.index("## final delivery closure"):prompt.index("## 返回格式")]
    assert "每一个 required final result" in section
    assert "producer" in section
    assert "生成 status=ready 前" in section
    assert "final delivery closure" in section


@pytest.mark.asyncio
async def test_blueprint_planner_defines_script_only_responsibility_graph(monkeypatch):
    captured = []

    async def fake_complete(messages, model, **kwargs):
        captured.extend(messages)
        return '{"status":"needs_clarification","clarifying_questions":["输入来源？A. 粘贴 B. 上传"],"blockers":[]}'

    _mock_creator_completion(monkeypatch, fake_complete)
    await api._generate_internal_blueprint_or_questions(_request())

    prompt = captured[0]["content"]
    section = prompt[prompt.index("## ResponsibilityGraph and FunctionItem semantics"):prompt.index("## core action fidelity")]
    assert "ResponsibilityGraph" in section
    assert "FunctionItem" in section
    assert "scripts/** only" in section
    assert "One script responsibility equals one FunctionItem" in section
    assert "references/**" in section and "not FunctionItems" in section
    assert "A reference cannot own or execute a core action" in section
    assert "A reference cannot be the producer of a required final result" in section


@pytest.mark.asyncio
async def test_planner_requires_responsibility_edges_and_graph_replay(monkeypatch):
    captured = []

    async def fake_complete(messages, model, **kwargs):
        captured.extend(messages)
        return '{"status":"needs_clarification","clarifying_questions":["输入来源？A. 粘贴 B. 上传"],"blockers":[]}'

    _mock_creator_completion(monkeypatch, fake_complete)
    await api._generate_internal_blueprint_or_questions(_request())
    prompt = captured[0]["content"]

    for term in [
        "FunctionItem",
        "ResponsibilityEdge",
        "complete workflow",
        "graph replay",
        "platform_input_node",
        "platform_output_node",
    ]:
        assert term in prompt


def test_planner_structured_responsibility_edges_are_skillplan_source_of_truth():
    from backend.services.blueprint_parser import build_skill_plan_from_files, FileSpec
    text = 'ResponsibilityEdges: [{"from_node":"scripts/wrong.py","from_output":"x","to_node":"scripts/other.py","to_input":"y","purpose":"wrong","constraints":[]}]'
    edge = {"from_node":"platform_input_node","from_output":"input","to_node":"scripts/a.py","to_input":"source","purpose":"structured","constraints":[]}
    plan = build_skill_plan_from_files(skill_name='demo', files=[FileSpec(path='scripts/a.py', purpose='Process input', required=True)], blueprint_text=text, responsibility_edges=[edge])
    assert plan.responsibility_edges == [edge]


def test_legacy_blueprint_edge_parser_is_fallback_only():
    from backend.services.blueprint_parser import build_skill_plan_from_files, FileSpec
    text = 'ResponsibilityEdges: [{"from_node":"platform_input_node","from_output":"input","to_node":"scripts/a.py","to_input":"source","purpose":"legacy","constraints":[]}]'
    structured = {"from_node":"scripts/a.py","from_output":"result","to_node":"platform_output_node","to_input":"file_outputs","purpose":"structured","constraints":[]}
    plan = build_skill_plan_from_files(skill_name='demo', files=[FileSpec(path='scripts/a.py', purpose='Process input', required=True)], blueprint_text=text, responsibility_edges=[structured])
    assert plan.responsibility_edges == [structured]
    fallback = build_skill_plan_from_files(skill_name='demo', files=[FileSpec(path='scripts/a.py', purpose='Process input', required=True)], blueprint_text=text)
    assert fallback.responsibility_edges and fallback.responsibility_edges[0]['purpose'] == 'legacy'


def test_planner_replay_distinguishes_local_intermediate_from_cross_function_input():
    import inspect
    from backend.services.creator import api
    source = inspect.getsource(api._generate_internal_blueprint_or_questions)
    assert 'local intermediate is not a cross-FunctionItem input' in source
    assert 'FunctionItem' in source and 'ResponsibilityEdge' in source


def test_planner_replay_requires_function_item_and_edge_result_convergence():
    import inspect
    from backend.services.creator import api
    source = inspect.getsource(api._generate_internal_blueprint_or_questions)
    assert 'outgoing ResponsibilityEdge' in source
    assert 'source FunctionItem actually owns and produces' in source
    assert 'revise the FunctionItem or the edge before returning ready' in source


def test_planner_receives_read_only_platform_io_contract_for_graph_edges():
    import inspect
    from backend.services.creator import api
    from backend.services.platform_io_contract import platform_io_contract_prompt_text
    source = inspect.getsource(api._generate_internal_blueprint_or_questions)
    assert 'platform_io_contract_prompt_text()' in source
    assert 'read-only platform boundary contract' in source
    assert platform_io_contract_prompt_text().splitlines()[0]


def test_arbitrary_edge_constraint_survives_structured_planner_transport():
    from backend.services.blueprint_parser import build_skill_plan_from_files, FileSpec
    from backend.services.creator.common import build_default_requirement_graph, function_item_graph_context
    constraint = {"name":"alpha","kind":"completely_custom","value":{"foo":7,"bar":["x","y"]},"comparator":"describes","required":True}
    edge = {"from_node":"scripts/a.py","from_output":"alpha","to_node":"scripts/b.py","to_input":"beta","purpose":"handoff","constraints":[constraint]}
    files=[FileSpec(path='scripts/a.py', purpose='Produce alpha', required=True), FileSpec(path='scripts/b.py', purpose='Consume beta', required=True)]
    plan = build_skill_plan_from_files(skill_name='demo', files=files, responsibility_edges=[edge])
    graph = build_default_requirement_graph(plan.files, responsibility_edges=plan.responsibility_edges)
    ctx = function_item_graph_context(graph, 'scripts/b.py')
    assert ctx['incoming_edges'][0]['constraints'][0]['value'] == {"foo":7,"bar":["x","y"]}


def test_final_tool_selector_receives_responsibility_graph():
    import inspect
    from backend.services.creator import api
    source = inspect.getsource(api._plan_final_tool_pool)
    assert 'normalized_script_contracts' in source
    assert 'responsibility_graph' in source
    assert 'candidate_tool_catalog' in source
    assert 'blueprint_text' not in source


def test_final_tool_selector_replays_selected_tools_against_function_items_and_edges():
    import inspect
    from backend.services.creator import api
    source = inspect.getsource(api._plan_final_tool_pool)
    for text in ['FunctionItem','incoming ResponsibilityEdges','outgoing ResponsibilityEdges','callable means','replay','revise decisions']:
        assert text in source
    for forbidden in ['image','PDF','story']:
        assert forbidden not in source


def test_purpose_short_contract_requires_exact_existing_target_path():
    import inspect
    from backend.services.creator import api
    source = inspect.getsource(api._normalize_script_purpose_short_contracts)
    assert 'target_file must be copied exactly from one provided script path' in source
    assert 'scripts/x.py' not in source


def test_purpose_short_contract_is_semantic_preserving_compression_only():
    import inspect
    from backend.services.creator import api
    source = inspect.getsource(api._normalize_script_purpose_short_contracts)
    assert 'must not remove a core action' in source
    assert 'must not change incoming/outgoing ResponsibilityEdge obligations' in source
    assert 'must not change required capability ownership' in source


def test_structured_edge_empty_endpoint_fails_fast():
    from backend.services.skill_plan import normalize_structured_responsibility_edges
    import pytest
    edge = {"from_node":"platform_input_node","from_output":"","to_node":"scripts/a.py","to_input":"source","purpose":"bad","constraints":[]}
    with pytest.raises(ValueError):
        normalize_structured_responsibility_edges([edge], source='planner')


def test_ready_planner_response_requires_structured_responsibility_edges(monkeypatch):
    import pytest
    async def fake_complete(messages, model, **kwargs):
        return '{"status":"ready","internal_blueprint_text":"## 📋 Skill 架构蓝图","skill_name":"demo-skill","blockers":[]}'
    _mock_creator_completion(monkeypatch, fake_complete)
    with pytest.raises(ValueError, match='responsibility_edges'):
        import asyncio
        asyncio.run(api._generate_internal_blueprint_or_questions(_request()))


def test_purpose_short_contract_uses_canonical_function_item_graph_context():
    import inspect
    source = inspect.getsource(api._normalize_script_purpose_short_contracts)
    assert 'function_item_graph_context' in source
    assert 'build_default_requirement_graph' in source
    assert 'incoming ResponsibilityEdges": [edge for edge' not in source


def test_frontend_persists_and_returns_responsibility_edges():
    from pathlib import Path
    source = Path('frontend/src/views/CreatorView.vue').read_text(encoding='utf-8')
    assert 'const pendingResponsibilityEdges = ref([])' in source
    assert 'responsibility_edges:' in source
    assert 'pendingResponsibilityEdges.value = plan.responsibility_edges' in source
    assert 'creationPlan.value?.responsibility_edges' in source


def test_purpose_short_contract_payload_uses_only_compact_graph_context():
    import inspect
    source = inspect.getsource(api._normalize_script_purpose_short_contracts)
    assert 'compact_context_by_target' in source
    assert 'exact_provided_script_paths' in source
    assert '"blueprint_text:\\n"' not in source
    assert '"workflow_allocation_summary:\\n"' not in source
    assert '"scripts:\\n"' not in source


def test_frontend_clear_chat_clears_pending_responsibility_edges():
    from pathlib import Path
    source = Path('frontend/src/views/CreatorView.vue').read_text(encoding='utf-8')
    clear_source = source[source.index('function clearChat()'):source.index('</script>')]
    assert 'pendingResponsibilityEdges.value = []' in clear_source

@pytest.mark.asyncio
async def test_ready_planner_result_runs_one_convergence_revision(monkeypatch):
    calls = []
    edge = {"from_node":"scripts/a.py","from_output":"alpha","to_node":"platform_output_node","to_input":"file_outputs","purpose":"deliver","constraints":[]}

    async def fake_complete(messages, model, **kwargs):
        calls.append(messages)
        return '{"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"draft","skill_name":"demo","blockers":[],"responsibility_edges":[' + __import__('json').dumps(edge) + ']}'

    _mock_creator_completion(monkeypatch, fake_complete)
    await api._generate_internal_blueprint_or_questions(_request())
    assert len(calls) == 2

    calls.clear()
    async def fake_needs(messages, model):
        calls.append(messages)
        return '{"status":"needs_clarification","clarifying_questions":["q"],"blockers":[],"responsibility_edges":[]}'

    monkeypatch.setattr(api, "complete_chat_once", fake_needs)
    await api._generate_internal_blueprint_or_questions(_request())
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_planner_convergence_replaces_draft_with_complete_revised_plan(monkeypatch):
    import json
    draft_edge = {"from_node":"scripts/a.py","from_output":"result_alpha","to_node":"scripts/b.py","to_input":"result_alpha","purpose":"handoff","constraints":[]}
    revised_edges = [draft_edge, {"from_node":"scripts/a.py","from_output":"result_alpha","to_node":"scripts/c.py","to_input":"result_alpha","purpose":"handoff","constraints":[]}]
    responses = [
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"draft","skill_name":"demo","blockers":[],"responsibility_edges":[draft_edge]},
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"revised","skill_name":"demo","blockers":[],"responsibility_edges":revised_edges},
    ]

    async def fake_complete(messages, model, **kwargs):
        semantic = _semantic_closure_response(messages)
        return json.dumps(semantic if semantic is not None else responses.pop(0))

    _mock_creator_completion(monkeypatch, fake_complete)
    result = await api._generate_internal_blueprint_or_questions(_request())
    assert result["internal_blueprint_text"] == "revised"
    assert result["responsibility_edges"] == revised_edges


@pytest.mark.asyncio
async def test_planner_convergence_uses_structured_edges_as_source_of_truth(monkeypatch):
    import json
    structured = {"from_node":"scripts/a.py","from_output":"structured_alpha","to_node":"scripts/b.py","to_input":"structured_alpha","purpose":"structured","constraints":[]}
    legacy_text = '### ResponsibilityEdges [{"from_node":"scripts/wrong.py","from_output":"wrong","to_node":"scripts/b.py","to_input":"wrong","purpose":"markdown","constraints":[]}]'
    responses = [
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"draft","skill_name":"demo","blockers":[],"responsibility_edges":[structured]},
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":legacy_text,"skill_name":"demo","blockers":[],"responsibility_edges":[structured]},
    ]

    async def fake_complete(messages, model, **kwargs):
        semantic = _semantic_closure_response(messages)
        return json.dumps(semantic if semantic is not None else responses.pop(0))

    _mock_creator_completion(monkeypatch, fake_complete)
    result = await api._generate_internal_blueprint_or_questions(_request())
    assert result["responsibility_edges"] == [structured]


@pytest.mark.asyncio
async def test_creator_does_not_add_missing_responsibility_edges_itself(monkeypatch):
    import json
    edge = {"from_node":"scripts/a.py","from_output":"result_alpha","to_node":"scripts/b.py","to_input":"result_alpha","purpose":"handoff","constraints":[]}
    responses = [
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"draft","skill_name":"demo","blockers":[],"responsibility_edges":[edge]},
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"unchanged","skill_name":"demo","blockers":[],"responsibility_edges":[edge]},
    ]

    async def fake_complete(messages, model, **kwargs):
        semantic = _semantic_closure_response(messages)
        return json.dumps(semantic if semantic is not None else responses.pop(0))

    _mock_creator_completion(monkeypatch, fake_complete)
    result = await api._generate_internal_blueprint_or_questions(_request())
    assert result["responsibility_edges"] == [edge]


def test_planner_and_tool_convergence_do_not_loop():
    import inspect
    planner = inspect.getsource(api._generate_internal_blueprint_or_questions)
    helper = inspect.getsource(api._converge_ready_executable_plan)
    selector = inspect.getsource(api._plan_final_tool_pool)
    assert planner.count("_converge_ready_executable_plan") == 1
    assert "while " not in helper
    assert selector.count("final_tool_selection_convergence") <= 3


@pytest.mark.asyncio
async def test_planner_convergence_missing_responsibility_edges_keeps_draft(monkeypatch):
    import json
    draft_edge = {"from_node":"scripts/a.py","from_output":"result_alpha","to_node":"platform_output_node","to_input":"file_outputs","purpose":"deliver","constraints":[]}
    responses = [
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"draft","skill_name":"demo","blockers":[],"responsibility_edges":[draft_edge]},
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"incomplete","skill_name":"demo","blockers":[]},
    ]

    async def fake_complete(messages, model, **kwargs):
        semantic = _semantic_closure_response(messages)
        return json.dumps(semantic if semantic is not None else responses.pop(0))

    _mock_creator_completion(monkeypatch, fake_complete)
    result = await api._generate_internal_blueprint_or_questions(_request())
    assert result["internal_blueprint_text"] == "draft"
    assert result["responsibility_edges"] == [draft_edge]


@pytest.mark.asyncio
async def test_planner_convergence_incomplete_transport_fields_keeps_draft(monkeypatch):
    import json
    draft_edge = {"from_node":"scripts/a.py","from_output":"result_alpha","to_node":"platform_output_node","to_input":"file_outputs","purpose":"deliver","constraints":[]}
    revised_edge = {"from_node":"scripts/a.py","from_output":"result_beta","to_node":"platform_output_node","to_input":"file_outputs","purpose":"deliver","constraints":[]}
    responses = [
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"draft","skill_name":"demo","blockers":[],"responsibility_edges":[draft_edge]},
        {"status":"ready","clarifying_questions":[],"internal_blueprint_text":"incomplete","skill_name":"demo","blockers":[],"responsibility_edges":[revised_edge]},
    ]

    async def fake_complete(messages, model, **kwargs):
        semantic = _semantic_closure_response(messages)
        return json.dumps(semantic if semantic is not None else responses.pop(0))

    _mock_creator_completion(monkeypatch, fake_complete)
    result = await api._generate_internal_blueprint_or_questions(_request())
    assert result["internal_blueprint_text"] == "draft"
    assert result["responsibility_edges"] == [draft_edge]


@pytest.mark.asyncio
async def test_planner_convergence_null_responsibility_edges_keeps_draft(monkeypatch):
    import json
    draft_edge = {"from_node":"scripts/a.py","from_output":"result_alpha","to_node":"platform_output_node","to_input":"file_outputs","purpose":"deliver","constraints":[]}
    responses = [
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"draft","skill_name":"demo","blockers":[],"responsibility_edges":[draft_edge]},
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"null edges","skill_name":"demo","blockers":[],"responsibility_edges":None},
    ]

    async def fake_complete(messages, model, **kwargs):
        semantic = _semantic_closure_response(messages)
        return json.dumps(semantic if semantic is not None else responses.pop(0))

    _mock_creator_completion(monkeypatch, fake_complete)
    result = await api._generate_internal_blueprint_or_questions(_request())
    assert result["internal_blueprint_text"] == "draft"
    assert result["responsibility_edges"] == [draft_edge]


@pytest.mark.asyncio
async def test_planner_convergence_invalid_transport_shape_keeps_draft(monkeypatch):
    import json
    draft_edge = {"from_node":"scripts/a.py","from_output":"result_alpha","to_node":"platform_output_node","to_input":"file_outputs","purpose":"deliver","constraints":[]}
    responses = [
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"draft","skill_name":"demo","blockers":[],"responsibility_edges":[draft_edge]},
        {"status":"ready","clarifying_questions":"not-a-list","review_summary":[],"internal_blueprint_text":"bad shape","skill_name":"demo","blockers":{},"responsibility_edges":[]},
    ]

    async def fake_complete(messages, model, **kwargs):
        semantic = _semantic_closure_response(messages)
        return json.dumps(semantic if semantic is not None else responses.pop(0))

    _mock_creator_completion(monkeypatch, fake_complete)
    result = await api._generate_internal_blueprint_or_questions(_request())
    assert result["internal_blueprint_text"] == "draft"
    assert result["responsibility_edges"] == [draft_edge]


@pytest.mark.asyncio
async def test_planner_convergence_empty_blueprint_keeps_draft(monkeypatch):
    import json
    draft_edge = {"from_node":"scripts/a.py","from_output":"result_alpha","to_node":"platform_output_node","to_input":"file_outputs","purpose":"deliver","constraints":[]}
    responses = [
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"good draft","skill_name":"demo","blockers":[],"responsibility_edges":[draft_edge]},
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"   ","skill_name":"demo","blockers":[],"responsibility_edges":[]},
    ]

    async def fake_complete(messages, model, **kwargs):
        semantic = _semantic_closure_response(messages)
        return json.dumps(semantic if semantic is not None else responses.pop(0))

    _mock_creator_completion(monkeypatch, fake_complete)
    result = await api._generate_internal_blueprint_or_questions(_request())
    assert result["internal_blueprint_text"] == "good draft"
    assert result["responsibility_edges"] == [draft_edge]

@pytest.mark.asyncio
async def test_invalid_ready_edge_shape_runs_same_planner_convergence(monkeypatch):
    import json
    calls = []
    first_edge = {
        "from": "platform_input_node",
        "from_output": "user_request",
        "to": "scripts/a.py",
        "to_input": "alpha",
        "description": "deliver",
    }
    second_edge = {
        "from_node": "platform_input_node",
        "from_output": "user_request",
        "to_node": "scripts/a.py",
        "to_input": "alpha",
        "purpose": "deliver",
        "constraints": [],
    }
    responses = [
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"draft","skill_name":"demo","blockers":[],"responsibility_edges":[first_edge]},
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"revised","skill_name":"demo","blockers":[],"responsibility_edges":[second_edge]},
    ]

    async def fake_complete(messages, model, **kwargs):
        calls.append(messages)
        semantic = _semantic_closure_response(messages)
        return json.dumps(semantic if semantic is not None else responses.pop(0))

    _mock_creator_completion(monkeypatch, fake_complete)
    result = await api._generate_internal_blueprint_or_questions(_request())
    assert len(calls) == 2
    assert result["internal_blueprint_text"] == "revised"
    assert result["responsibility_edges"] == [second_edge]
    assert set(result["responsibility_edges"][0]) == {
        "from_node", "from_output", "to_node", "to_input", "purpose", "constraints"
    }


def test_creator_does_not_alias_repair_responsibility_edges():
    from backend.services.skill_plan import normalize_structured_responsibility_edges
    edge = {
        "from": "scripts/a.py",
        "to": "scripts/b.py",
        "from_output": "alpha",
        "to_input": "alpha",
        "description": "handoff",
    }
    with pytest.raises(ValueError):
        normalize_structured_responsibility_edges([edge], source="planner")


@pytest.mark.asyncio
async def test_invalid_ready_edge_shape_and_failed_convergence_never_keeps_raw_draft(monkeypatch):
    import json
    bad_edge = {
        "from": "scripts/a.py",
        "to": "scripts/b.py",
        "from_output": "alpha",
        "to_input": "alpha",
        "description": "handoff",
    }
    responses = [
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"draft","skill_name":"demo","blockers":[],"responsibility_edges":[bad_edge]},
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"still bad","skill_name":"demo","blockers":[],"responsibility_edges":[bad_edge]},
    ]

    async def fake_complete(messages, model, **kwargs):
        semantic = _semantic_closure_response(messages)
        return json.dumps(semantic if semantic is not None else responses.pop(0))

    _mock_creator_completion(monkeypatch, fake_complete)
    response = await api.prepare_plan(_request())
    assert response.status == "blocked"
    assert response.prepare_stage == "blueprint_protocol_failed"
    assert response.creation_blockers[0]["field"] == "responsibility_edges"


@pytest.mark.asyncio
async def test_valid_ready_edge_shape_and_failed_convergence_keeps_draft(monkeypatch):
    import json
    draft_edge = {"from_node":"scripts/a.py","from_output":"alpha","to_node":"platform_output_node","to_input":"file_outputs","purpose":"deliver","constraints":[]}
    bad_edge = {"from":"scripts/a.py","from_output":"alpha","to":"platform_output_node","to_input":"file_outputs","description":"deliver"}
    responses = [
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"draft","skill_name":"demo","blockers":[],"responsibility_edges":[draft_edge]},
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"bad","skill_name":"demo","blockers":[],"responsibility_edges":[bad_edge]},
    ]

    async def fake_complete(messages, model, **kwargs):
        semantic = _semantic_closure_response(messages)
        return json.dumps(semantic if semantic is not None else responses.pop(0))

    _mock_creator_completion(monkeypatch, fake_complete)
    result = await api._generate_internal_blueprint_or_questions(_request())
    assert result["internal_blueprint_text"] == "draft"
    assert result["responsibility_edges"] == [draft_edge]


def test_planner_convergence_prompt_declares_exact_edge_transport_schema():
    import inspect
    source = inspect.getsource(api._converge_ready_executable_plan).lower()
    for text in [
        "from_node",
        "from_output",
        "to_node",
        "to_input",
        "purpose",
        "constraints",
        "platform_input_node",
        "platform_output_node",
        "platform_io_contract",
        "do not use aliases",
    ]:
        assert text in source

@pytest.mark.asyncio
async def test_ready_draft_invalid_platform_input_boundary_runs_same_planner_convergence(monkeypatch):
    import json
    calls = []
    first_edge = {"from_node":"platform_input_node","from_output":"semantic_alpha","to_node":"scripts/a.py","to_input":"semantic_alpha","purpose":"deliver","constraints":[]}
    second_edge = {"from_node":"platform_input_node","from_output":"user_request","to_node":"scripts/a.py","to_input":"semantic_alpha","purpose":"deliver","constraints":[]}
    responses = [
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"draft","skill_name":"demo","blockers":[],"responsibility_edges":[first_edge]},
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"revised","skill_name":"demo","blockers":[],"responsibility_edges":[second_edge]},
    ]

    async def fake_complete(messages, model, **kwargs):
        calls.append(messages)
        semantic = _semantic_closure_response(messages)
        return json.dumps(semantic if semantic is not None else responses.pop(0))

    _mock_creator_completion(monkeypatch, fake_complete)
    result = await api._generate_internal_blueprint_or_questions(_request())
    assert len(calls) == 2
    convergence_payload = json.loads(calls[1][1]["content"])
    assert "undefined platform input field" in convergence_payload["draft_transport_error"]
    assert "from_output=semantic_alpha" in convergence_payload["draft_transport_error"]
    assert result["internal_blueprint_text"] == "revised"
    assert result["responsibility_edges"][0]["from_output"] == "user_request"


@pytest.mark.asyncio
async def test_ready_draft_invalid_platform_output_boundary_runs_same_planner_convergence(monkeypatch):
    import json
    calls = []
    first_edge = {"from_node":"scripts/a.py","from_output":"result_alpha","to_node":"platform_output_node","to_input":"final_result_alpha","purpose":"deliver","constraints":[]}
    second_edge = {"from_node":"scripts/a.py","from_output":"result_alpha","to_node":"platform_output_node","to_input":"file_outputs","purpose":"deliver","constraints":[]}
    responses = [
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"draft","skill_name":"demo","blockers":[],"responsibility_edges":[first_edge]},
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"revised","skill_name":"demo","blockers":[],"responsibility_edges":[second_edge]},
    ]

    async def fake_complete(messages, model, **kwargs):
        calls.append(messages)
        semantic = _semantic_closure_response(messages)
        return json.dumps(semantic if semantic is not None else responses.pop(0))

    _mock_creator_completion(monkeypatch, fake_complete)
    result = await api._generate_internal_blueprint_or_questions(_request())
    assert len(calls) == 2
    assert result["internal_blueprint_text"] == "revised"
    assert result["responsibility_edges"][0]["to_input"] == "file_outputs"


@pytest.mark.asyncio
async def test_invalid_platform_boundary_after_convergence_is_protocol_blocked(monkeypatch):
    import json
    bad_edge = {"from_node":"platform_input_node","from_output":"semantic_alpha","to_node":"scripts/a.py","to_input":"semantic_alpha","purpose":"deliver","constraints":[]}
    responses = [
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"draft","skill_name":"demo","blockers":[],"responsibility_edges":[bad_edge]},
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"still bad","skill_name":"demo","blockers":[],"responsibility_edges":[bad_edge]},
    ]

    async def fake_complete(messages, model, **kwargs):
        semantic = _semantic_closure_response(messages)
        return json.dumps(semantic if semantic is not None else responses.pop(0))

    _mock_creator_completion(monkeypatch, fake_complete)
    response = await api.prepare_plan(_request())
    assert response.status == "blocked"
    assert response.prepare_stage == "blueprint_protocol_failed"
    assert response.creation_blockers[0]["field"] == "responsibility_edges"


@pytest.mark.asyncio
async def test_ready_draft_missing_responsibility_edges_runs_same_planner_convergence(monkeypatch):
    import json
    calls = []
    second_edge = {"from_node":"platform_input_node","from_output":"user_request","to_node":"scripts/a.py","to_input":"semantic_alpha","purpose":"deliver","constraints":[]}
    responses = [
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"draft","skill_name":"demo","blockers":[]},
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"revised","skill_name":"demo","blockers":[],"responsibility_edges":[second_edge]},
    ]

    async def fake_complete(messages, model, **kwargs):
        calls.append(messages)
        semantic = _semantic_closure_response(messages)
        return json.dumps(semantic if semantic is not None else responses.pop(0))

    _mock_creator_completion(monkeypatch, fake_complete)
    result = await api._generate_internal_blueprint_or_questions(_request())
    assert len(calls) == 2
    assert result["responsibility_edges"] == [second_edge]


@pytest.mark.asyncio
async def test_ready_draft_null_responsibility_edges_runs_same_planner_convergence(monkeypatch):
    import json
    calls = []
    second_edge = {"from_node":"platform_input_node","from_output":"user_request","to_node":"scripts/a.py","to_input":"semantic_alpha","purpose":"deliver","constraints":[]}
    responses = [
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"draft","skill_name":"demo","blockers":[],"responsibility_edges":None},
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"revised","skill_name":"demo","blockers":[],"responsibility_edges":[second_edge]},
    ]

    async def fake_complete(messages, model, **kwargs):
        calls.append(messages)
        semantic = _semantic_closure_response(messages)
        return json.dumps(semantic if semantic is not None else responses.pop(0))

    _mock_creator_completion(monkeypatch, fake_complete)
    result = await api._generate_internal_blueprint_or_questions(_request())
    assert len(calls) == 2
    assert result["responsibility_edges"] == [second_edge]


def test_validate_structured_responsibility_edge_transport_contract():
    from backend.services.skill_plan import validate_structured_responsibility_edge_transport
    script_edge = {"from_node":"scripts/a.py","from_output":"alpha","to_node":"scripts/b.py","to_input":"beta","purpose":"handoff","constraints":[]}
    platform_input_edge = {"from_node":"platform_input_node","from_output":"user_request","to_node":"scripts/a.py","to_input":"alpha","purpose":"deliver","constraints":[]}
    platform_output_edge = {"from_node":"scripts/a.py","from_output":"alpha","to_node":"platform_output_node","to_input":"file_outputs","purpose":"deliver","constraints":[]}

    assert validate_structured_responsibility_edge_transport([script_edge], source="planner") == [script_edge]
    assert validate_structured_responsibility_edge_transport([platform_input_edge], source="planner") == [platform_input_edge]
    assert validate_structured_responsibility_edge_transport([platform_output_edge], source="planner") == [platform_output_edge]

    invalid_edges = [
        {**platform_input_edge, "from_output": "semantic_alpha"},
        {**platform_output_edge, "to_input": "semantic_alpha"},
        {**script_edge, "from_node": "platform_output_node"},
        {**script_edge, "to_node": "platform_input_node"},
        {"from_node":"platform_input_node","from_output":"user_request","to_node":"platform_output_node","to_input":"file_outputs","purpose":"invalid direct boundary edge","constraints":[]},
        {"from":"scripts/a.py","to":"scripts/b.py","from_output":"alpha","to_input":"beta","description":"handoff"},
        None,
    ]
    for invalid in invalid_edges:
        with pytest.raises(ValueError):
            validate_structured_responsibility_edge_transport(
                None if invalid is None else [invalid],
                source="planner",
            )


def _abstract_function_item(target='scripts/a.py', purpose='produce semantic result'):
    return {
        'target_file': target,
        'role': 'worker',
        'purpose': purpose,
        'inputs': ['semantic_input'],
        'outputs': ['semantic_result'],
        'required_capabilities': ['semantic_capability'],
        'constraints': [{'name': 'constraint', 'kind': 'generic', 'value': 'concrete_result', 'comparator': 'describes', 'required': True}],
    }


def test_structured_function_items_reject_unknown_fields():
    from backend.services.skill_plan import normalize_structured_function_items
    item = _abstract_function_item()
    item['action_type'] = 'forbidden'
    with pytest.raises(ValueError, match='unknown_fields'):
        normalize_structured_function_items([item], source='planner')


def test_structured_function_items_reject_duplicate_targets():
    from backend.services.skill_plan import normalize_structured_function_items
    with pytest.raises(ValueError, match='duplicate'):
        normalize_structured_function_items([_abstract_function_item('scripts/a.py'), _abstract_function_item('scripts/a.py')], source='planner')


def test_structured_function_items_reject_non_script_targets():
    from backend.services.skill_plan import normalize_structured_function_items
    with pytest.raises(ValueError, match='must_start_with_scripts'):
        normalize_structured_function_items([_abstract_function_item('references/a.md')], source='planner')


def test_structured_function_items_require_structured_constraints():
    from backend.services.skill_plan import normalize_structured_function_items
    item = _abstract_function_item()
    item['constraints'] = ['not structured']
    with pytest.raises(ValueError, match='constraints'):
        normalize_structured_function_items([item], source='planner')


@pytest.mark.asyncio
async def test_planner_convergence_revises_function_items_and_edges_together(monkeypatch):
    first_item = _abstract_function_item('scripts/a.py', 'produce semantic result')
    second_items = [
        _abstract_function_item('scripts/a.py', 'produce semantic result'),
        _abstract_function_item('scripts/b.py', 'consume semantic result and produce concrete result'),
    ]
    first_edge = {'from_node':'scripts/a.py','from_output':'semantic_result','to_node':'platform_output_node','to_input':'text','purpose':'deliver semantic result','constraints':[]}
    second_edges = [
        {'from_node':'scripts/a.py','from_output':'semantic_result','to_node':'scripts/b.py','to_input':'semantic_input','purpose':'handoff semantic result','constraints':[]},
        {'from_node':'scripts/b.py','from_output':'semantic_result','to_node':'platform_output_node','to_input':'text','purpose':'deliver concrete result','constraints':[]},
    ]
    responses = iter([
        {'status':'ready','clarifying_questions':[],'review_summary':{},'internal_blueprint_text':'draft','skill_name':'demo','blockers':[],'function_items':[first_item],'responsibility_edges':[first_edge]},
        {'status':'ready','clarifying_questions':[],'review_summary':{},'internal_blueprint_text':'revised','skill_name':'demo','blockers':[],'function_items':second_items,'responsibility_edges':second_edges},
    ])
    async def fake_complete(*args, **kwargs):
        import json
        return json.dumps(next(responses))
    monkeypatch.setattr(api, 'complete_chat_once', fake_complete)
    monkeypatch.setattr(api, 'route_model', lambda *a, **k: type('R', (), {'model':'planner'})())
    result = await api._generate_internal_blueprint_or_questions(_request())
    assert result['function_items'] == second_items
    assert result['responsibility_edges'] == second_edges


@pytest.mark.asyncio
async def test_ready_planner_requires_structured_function_items(monkeypatch):
    edge = {'from_node':'scripts/a.py','from_output':'semantic_result','to_node':'platform_output_node','to_input':'text','purpose':'deliver','constraints':[]}
    revised_item = _abstract_function_item('scripts/a.py')
    responses = iter([
        {'status':'ready','clarifying_questions':[],'review_summary':{},'internal_blueprint_text':'draft','skill_name':'demo','blockers':[],'responsibility_edges':[edge]},
        {'status':'ready','clarifying_questions':[],'review_summary':{},'internal_blueprint_text':'revised','skill_name':'demo','blockers':[],'function_items':[revised_item],'responsibility_edges':[edge]},
    ])
    async def fake_complete(*args, **kwargs):
        import json
        return json.dumps(next(responses))
    monkeypatch.setattr(api, 'complete_chat_once', fake_complete)
    monkeypatch.setattr(api, 'route_model', lambda *a, **k: type('R', (), {'model':'planner'})())
    result = await api._generate_internal_blueprint_or_questions(_request())
    assert result['function_items'] == [revised_item]


@pytest.mark.asyncio
async def test_ready_planner_null_function_items_runs_same_convergence(monkeypatch):
    edge = {'from_node':'scripts/a.py','from_output':'semantic_result','to_node':'platform_output_node','to_input':'text','purpose':'deliver','constraints':[]}
    revised_item = _abstract_function_item('scripts/a.py')
    responses = iter([
        {'status':'ready','clarifying_questions':[],'review_summary':{},'internal_blueprint_text':'draft','skill_name':'demo','blockers':[],'function_items':None,'responsibility_edges':[edge]},
        {'status':'ready','clarifying_questions':[],'review_summary':{},'internal_blueprint_text':'revised','skill_name':'demo','blockers':[],'function_items':[revised_item],'responsibility_edges':[edge]},
    ])
    async def fake_complete(*args, **kwargs):
        import json
        return json.dumps(next(responses))
    monkeypatch.setattr(api, 'complete_chat_once', fake_complete)
    monkeypatch.setattr(api, 'route_model', lambda *a, **k: type('R', (), {'model':'planner'})())
    result = await api._generate_internal_blueprint_or_questions(_request())
    assert result['function_items'] == [revised_item]


def test_render_structured_responsibility_view_replaces_blueprint_sections():
    blueprint = _ready_blueprint("- path: `SKILL.md`\n  role: skill_overview\n- path: `scripts/a.py`\n  role: stale\n  purpose: stale")
    items = [
        _abstract_function_item('scripts/b.py', 'second structured purpose'),
        _abstract_function_item('scripts/a.py', 'structured purpose'),
    ]
    edges = [{'from_node':'scripts/a.py','from_output':'semantic_result','to_node':'scripts/b.py','to_input':'semantic_input','purpose':'handoff','constraints':[]}]
    rendered = api._render_structured_responsibility_view(blueprint, items, edges)
    assert '### Structured FunctionItems View' not in rendered
    assert rendered.count('### 工作流逻辑') == 1
    assert '1. structured purpose' in rendered
    assert rendered.index('1. structured purpose') < rendered.index('2. second structured purpose')
    assert '- path: `scripts/a.py`' in rendered
    assert 'role: worker' in rendered
    assert 'purpose: structured purpose' in rendered
    assert 'purpose: stale' not in rendered


def test_validate_structured_responsibility_edge_transport_rejects_non_function_item_endpoint():
    from backend.services.skill_plan import validate_structured_responsibility_edge_transport

    function_items = [{
        "target_file": "scripts/a.py",
        "role": "script",
        "purpose": "Do A",
        "inputs": [],
        "outputs": ["result"],
        "required_capabilities": [],
        "constraints": [],
    }]
    invalid_edge = {
        "from_node": "references/spec.md",
        "from_output": "rules",
        "to_node": "scripts/a.py",
        "to_input": "rules",
        "purpose": "invalid resource endpoint",
        "constraints": [],
    }

    with pytest.raises(ValueError, match="non-FunctionItem source endpoint"):
        validate_structured_responsibility_edge_transport(
            [invalid_edge],
            function_items=function_items,
            source="planner",
        )


def test_validate_structured_responsibility_edge_transport_accepts_function_item_endpoint():
    from backend.services.skill_plan import validate_structured_responsibility_edge_transport

    function_items = [
        {
            "target_file": "scripts/a.py",
            "role": "script",
            "purpose": "Do A",
            "inputs": [],
            "outputs": ["result"],
            "required_capabilities": [],
            "constraints": [],
        },
        {
            "target_file": "scripts/b.py",
            "role": "script",
            "purpose": "Do B",
            "inputs": ["result"],
            "outputs": [],
            "required_capabilities": [],
            "constraints": [],
        },
    ]
    edge = {
        "from_node": "scripts/a.py",
        "from_output": "result",
        "to_node": "scripts/b.py",
        "to_input": "result",
        "purpose": "valid function-item handoff",
        "constraints": [],
    }

    assert validate_structured_responsibility_edge_transport(
        [edge],
        function_items=function_items,
        source="planner",
    ) == [edge]


@pytest.mark.parametrize(
    ("asset_path", "expected"),
    [
        ("assets/template.pdf", True),
        ("assets/layout.v1", True),
        ("assets/subdir/static-resource", True),
        ("assets/", False),
        ("assets/${x}", False),
        ("assets/{{x}}", False),
        ("assets/<x>", False),
        ("assets/[x]", False),
        ("assets/*", False),
    ],
)
def test_concrete_assets_file_path_rejects_only_dynamic_path_structure(asset_path, expected):
    assert api._is_concrete_assets_file_path(asset_path) is expected


@pytest.mark.parametrize("asset_source", ["", "user_upload", "bundled"])
def test_filter_unconfirmed_asset_plan_file_plan_asset_source_is_not_evidence(asset_source):
    files = [_file("SKILL.md"), _file("assets/template.pdf", asset_source=asset_source)]
    assets = []

    warnings = api._filter_unconfirmed_asset_plan(
        files=files,
        asset_requirements=assets,
        uploaded_files=[],
        review_summary=api.PreparePlanReviewSummary(),
    )

    assert "assets/template.pdf" not in [f.path for f in files]
    assert any(
        w.get("code") == "ungrounded_asset_plan_removed"
        and "assets/template.pdf" in w.get("files", [])
        for w in warnings
    )

def test_filter_unconfirmed_asset_plan_removes_model_asset_without_structured_evidence():
    summary = api.PreparePlanReviewSummary(
        files_to_create_or_update=["SKILL.md", "assets/template.pdf"],
        assets_to_upload=["assets/template.pdf"],
    )
    files = [_file("SKILL.md"), _file("assets/template.pdf", asset_source="user_upload")]
    assets = [AssetRequirementOut(path="assets/template.pdf", source="user_upload", required=True)]

    warnings = api._filter_unconfirmed_asset_plan(files=files, asset_requirements=assets, uploaded_files=[], review_summary=summary)

    assert "assets/template.pdf" not in [f.path for f in files]
    assert "assets/template.pdf" not in [a.path for a in assets]
    assert "assets/template.pdf" not in summary.assets_to_upload
    assert api._required_file_plan_user_upload_asset_paths(files, assets) == []
    assert any(w.get("code") == "ungrounded_asset_plan_removed" and "assets/template.pdf" in w.get("files", []) for w in warnings)


def test_filter_unconfirmed_asset_plan_keeps_confirmed_uploaded_asset():
    summary = api.PreparePlanReviewSummary(files_to_create_or_update=["SKILL.md", "assets/template.pdf"])
    files = [_file("SKILL.md"), _file("assets/template.pdf", asset_source="user_upload")]
    assets = [AssetRequirementOut(path="assets/template.pdf", source="user_upload", required=True)]

    warnings = api._filter_unconfirmed_asset_plan(
        files=files,
        asset_requirements=assets,
        uploaded_files=[{"asset_decision": "include_as_asset", "asset_target_path": "assets/template.pdf"}],
        review_summary=summary,
    )

    asset = next(f for f in files if f.path == "assets/template.pdf")
    assert asset.asset_source == "user_upload"
    assert "assets/template.pdf" in [a.path for a in assets]
    assert not any(w.get("code") == "ungrounded_asset_plan_removed" for w in warnings)


def test_filter_unconfirmed_asset_plan_keeps_user_explicit_asset_and_marks_missing_upload():
    files = [_file("SKILL.md"), _file("assets/template.pdf", asset_source="user_upload")]
    assets = [AssetRequirementOut(path="assets/template.pdf", source="user_explicit", required=True)]

    warnings = api._filter_unconfirmed_asset_plan(files=files, asset_requirements=assets, uploaded_files=[], review_summary=api.PreparePlanReviewSummary())

    assert "assets/template.pdf" in [f.path for f in files]
    assert api._required_file_plan_user_upload_asset_paths(files, assets) == ["assets/template.pdf"]
    assert not warnings


def test_filter_unconfirmed_asset_plan_reference_only_upload_does_not_allow_asset():
    files = [_file("SKILL.md"), _file("assets/template.pdf", asset_source="user_upload")]
    assets = [AssetRequirementOut(path="assets/template.pdf", source="user_upload", required=True)]

    api._filter_unconfirmed_asset_plan(
        files=files,
        asset_requirements=assets,
        uploaded_files=[{"asset_decision": "reference_only", "asset_target_path": "assets/template.pdf"}],
        review_summary=api.PreparePlanReviewSummary(),
    )

    assert "assets/template.pdf" not in [f.path for f in files]
    assert "assets/template.pdf" not in [a.path for a in assets]


@pytest.mark.asyncio
async def test_executable_binding_prompt_explains_real_execution_ownership(monkeypatch):
    captured = []

    async def fake_complete(messages, model, **kwargs):
        captured.extend(messages)
        return '{"function_items":[],"responsibility_edges":[]}'

    _mock_creator_completion(monkeypatch, fake_complete)
    await api._bind_executable_responsibility_plan(
        request=_request(),
        current_planner_result={},
        planner_model="planner",
        allowed_function_item_targets=[],
    )

    prompt = " ".join(captured[0]["content"].lower().split())
    for intent in [
        "functionitems are the executable responsibility owners",
        "cross-responsibility data dependencies and transport",
        "actual host execution model",
        "every required computation must have a real executable owner",
        "script-local intermediate values",
        "platform boundary nodes are immutable transport endpoints, not executable functionitems",
        "platform boundary to functionitem, functionitem to functionitem, or functionitem to platform boundary",
    ]:
        assert intent in prompt


@pytest.mark.asyncio
async def test_convergence_prompt_requires_executable_ownership_closure(monkeypatch):
    captured = []

    async def fake_complete(messages, model, **kwargs):
        captured.extend(messages)
        return (
            '{"status":"ready","clarifying_questions":[],"review_summary":{},'
            '"internal_blueprint_text":"revised","skill_name":"demo","blockers":[],'
            '"function_items":[],"responsibility_edges":[]}'
        )

    _mock_creator_completion(monkeypatch, fake_complete)
    await api._converge_ready_executable_plan(
        request=_request(),
        current_planner_result={},
        planner_model="planner",
        allowed_function_item_targets=[],
    )

    prompt = " ".join(captured[0]["content"].lower().split())
    for intent in [
        "executable ownership closure",
        "replay the complete executable plan against the actual host execution model",
        "every required computation has an executable owner",
        "revise the owning functionitem and affected responsibilityedges together",
        "implicit execution behavior that the host runtime does not provide",
        "platform input boundary, through executable responsibility owners, to platform output boundary",
        "required runtime inputs and required final results are not disconnected",
    ]:
        assert intent in prompt


@pytest.mark.asyncio
async def test_same_planner_convergence_adopts_complete_ownership_revision(monkeypatch):
    import json

    draft_items = [
        {**_function_item("scripts/producer.py"), "outputs": ["units"]},
        {**_function_item("scripts/processor.py"), "inputs": ["unit"], "outputs": ["result"]},
        {**_function_item("scripts/consumer.py"), "inputs": ["results"]},
    ]
    draft_edges = [
        {"from_node": "scripts/producer.py", "from_output": "units", "to_node": "scripts/processor.py", "to_input": "unit", "purpose": "provide a unit", "constraints": []},
        {"from_node": "scripts/processor.py", "from_output": "result", "to_node": "scripts/consumer.py", "to_input": "results", "purpose": "provide results", "constraints": []},
    ]
    revised_items = [
        draft_items[0],
        {**_function_item("scripts/processor.py"), "inputs": ["units"], "outputs": ["results"], "purpose": "process the supplied units"},
        draft_items[2],
    ]
    revised_edges = [
        {"from_node": "scripts/producer.py", "from_output": "units", "to_node": "scripts/processor.py", "to_input": "units", "purpose": "provide units", "constraints": []},
        {"from_node": "scripts/processor.py", "from_output": "results", "to_node": "scripts/consumer.py", "to_input": "results", "purpose": "provide results", "constraints": []},
    ]
    responses = iter([
        {"function_items": draft_items, "responsibility_edges": draft_edges},
        {"status": "ready", "clarifying_questions": [], "review_summary": {}, "internal_blueprint_text": "revised", "skill_name": "demo", "blockers": [], "function_items": revised_items, "responsibility_edges": revised_edges},
    ])

    async def fake_complete(messages, model, **kwargs):
        return json.dumps(next(responses))

    _mock_creator_completion(monkeypatch, fake_complete)
    binding = await api._bind_executable_responsibility_plan(
        request=_request(),
        current_planner_result={},
        planner_model="planner",
        allowed_function_item_targets=[item["target_file"] for item in draft_items],
    )
    result = await api._converge_ready_executable_plan(
        request=_request(),
        current_planner_result=binding,
        planner_model="planner",
        allowed_function_item_targets=[item["target_file"] for item in draft_items],
    )

    assert result["function_items"] == revised_items
    assert result["responsibility_edges"] == revised_edges


def test_executable_ownership_prompts_add_no_backend_semantic_classifier():
    import inspect

    source = "\n".join([
        inspect.getsource(api._bind_executable_responsibility_plan),
        inspect.getsource(api._converge_ready_executable_plan),
    ])
    for classifier_name in [
        "MAP_EACH_STRATEGIES",
        "LOOP_STRATEGIES",
        "CONTROL_FLOW_KEYWORDS",
        "CARDINALITY_RULES",
    ]:
        assert classifier_name not in source


@pytest.mark.asyncio
async def test_responsibility_graph_alignment_review_is_read_only(monkeypatch):
    async def fake_complete(messages, model, **kwargs):
        return '{"passed":false,"issues":[],"function_items":[]}'

    _mock_creator_completion(monkeypatch, fake_complete)
    with pytest.raises(ValueError, match="passed and issues"):
        await api._review_responsibility_graph_alignment(
            request=_request(), frozen_blueprint_text="frozen", allowed_function_item_targets=[],
            function_items=[], responsibility_edges=[], planner_model="planner",
        )


async def _run_ready_graph_alignment_flow(monkeypatch, review, repair=None, initial_edges=None, function_item=None, regenerate=None):
    import json

    blueprint = _ready_blueprint(_skill_plan_block("\n" + _script_plan_block("scripts/a.py")))
    item = function_item or {**_function_item("scripts/a.py"), "inputs": ["source"]}
    edges = [
        {"from_node": "platform_input_node", "from_output": "user_request", "to_node": "scripts/a.py", "to_input": "source", "purpose": "provide input", "constraints": []},
        _output_edge("scripts/a.py"),
    ]
    if initial_edges is not None:
        edges = initial_edges

    async def fake_complete(messages, model, **kwargs):
        return json.dumps(_ready_payload(blueprint))

    async def bind(**kwargs):
        return {"function_items": [item], "responsibility_edges": edges}

    async def converge(**kwargs):
        return {**_ready_payload("converged"), "function_items": [item], "responsibility_edges": edges}

    async def fake_complete_creator_role_once(messages, role, fallback_model):
        semantic = _semantic_closure_response(messages)
        if semantic is not None:
            return json.dumps(semantic)
        return await fake_complete(messages, fallback_model)

    _mock_creator_completion(monkeypatch, fake_complete)
    monkeypatch.setattr(api, "complete_creator_role_once", fake_complete_creator_role_once)
    monkeypatch.setattr(api, "_bind_executable_responsibility_plan", bind)
    monkeypatch.setattr(api, "_converge_ready_executable_plan", converge)
    monkeypatch.setattr(api, "_review_responsibility_graph_alignment", review)
    if repair is not None:
        monkeypatch.setattr(api, "_repair_responsibility_graph_alignment", repair)
    if regenerate is None:
        async def regenerate(**kwargs):
            return {"function_items": kwargs["function_items"], "responsibility_edges": edges}
    monkeypatch.setattr(api, "_regenerate_responsibility_graph", regenerate)
    return await api._generate_internal_blueprint_or_questions(_request()), item, edges


@pytest.mark.asyncio
async def test_alignment_review_pass_does_not_trigger_localized_repair(monkeypatch):
    review_calls = 0
    repair_calls = 0

    async def review(**kwargs):
        nonlocal review_calls
        review_calls += 1
        return {"passed": True, "issues": []}

    async def repair(**kwargs):
        nonlocal repair_calls
        repair_calls += 1
        raise AssertionError("repair must not run after a passing review")

    result, item, edges = await _run_ready_graph_alignment_flow(monkeypatch, review, repair)
    assert result["function_items"] == [item]
    assert result["responsibility_edges"] == edges
    assert review_calls == 1
    assert repair_calls == 0


@pytest.mark.asyncio
async def test_alignment_failure_runs_one_localized_repair_then_final_review(monkeypatch):
    review_calls = 0
    repair_calls = 0

    async def review(**kwargs):
        nonlocal review_calls
        review_calls += 1
        return (
            {"passed": False, "issues": [{"id": "alignment", "target_files": ["scripts/a.py"], "affected_edge_indexes": [], "reason": "missing alignment", "evidence": "current graph", "repair_guidance": "restore ownership"}]}
            if review_calls == 1 else {"passed": True, "issues": []}
        )

    async def repair(**kwargs):
        nonlocal repair_calls
        repair_calls += 1
        item = {**kwargs["function_items"][0]}
        item["purpose"] = "repaired responsibility"
        return {"function_items": [item], "responsibility_edges": [{"from_node": "platform_input_node", "from_output": "user_request", "to_node": "scripts/a.py", "to_input": "source", "purpose": "provide input", "constraints": []}, _output_edge("scripts/a.py")]}

    result, _, edges = await _run_ready_graph_alignment_flow(monkeypatch, review, repair)
    assert result["function_items"][0]["purpose"] == "repaired responsibility"
    assert result["responsibility_edges"] == edges
    assert review_calls == 2
    assert repair_calls == 1


@pytest.mark.asyncio
async def test_alignment_repair_reuses_frozen_target_and_edge_validators(monkeypatch):
    async def review(**kwargs):
        return {"passed": False, "issues": [{"id": "alignment", "target_files": ["scripts/a.py"], "affected_edge_indexes": [], "reason": "x", "evidence": "x", "repair_guidance": "x"}]}

    async def wrong_target_repair(**kwargs):
        return {"function_items": [_function_item("scripts/outside.py")], "responsibility_edges": [_output_edge("scripts/outside.py")]}

    with pytest.raises(api.PreparePlanProtocolError, match="target_file set"):
        await _run_ready_graph_alignment_flow(monkeypatch, review, wrong_target_repair)

    async def invalid_edge_repair(**kwargs):
        return {"function_items": [_function_item("scripts/a.py")], "responsibility_edges": [{"from": "scripts/a.py"}]}

    with pytest.raises(api.PreparePlanProtocolError, match="unknown_fields|missing_fields"):
        await _run_ready_graph_alignment_flow(monkeypatch, review, invalid_edge_repair)


@pytest.mark.asyncio
async def test_alignment_final_review_failure_blocks_after_bounded_calls(monkeypatch):
    review_calls = 0
    repair_calls = 0

    async def review(**kwargs):
        nonlocal review_calls
        review_calls += 1
        return {"passed": False, "issues": [{"id": "alignment", "target_files": ["scripts/a.py"], "affected_edge_indexes": [], "reason": "x", "evidence": "x", "repair_guidance": "x"}]}

    async def repair(**kwargs):
        nonlocal repair_calls
        repair_calls += 1
        return {"function_items": kwargs["function_items"], "responsibility_edges": [{"from_node": "platform_input_node", "from_output": "user_request", "to_node": "scripts/a.py", "to_input": "source", "purpose": "provide input", "constraints": []}, _output_edge("scripts/a.py")]}

    with pytest.raises(api.PreparePlanProtocolError, match="remained unresolved"):
        await _run_ready_graph_alignment_flow(monkeypatch, review, repair)
    assert review_calls == 3
    assert repair_calls == 1


@pytest.mark.asyncio
async def test_failed_localized_repair_runs_one_full_graph_regeneration(monkeypatch):
    review_calls = 0
    repair_calls = 0
    regenerate_calls = 0
    input_edge = {"from_node": "platform_input_node", "from_output": "user_request", "to_node": "scripts/a.py", "to_input": "source", "purpose": "provide input", "constraints": []}
    valid_edges = [input_edge, _output_edge("scripts/a.py")]

    async def review(**kwargs):
        nonlocal review_calls
        review_calls += 1
        if review_calls == 1:
            return {"passed": False, "issues": [{"id": "alignment", "target_files": ["scripts/a.py"], "affected_edge_indexes": [], "reason": "wrong topology", "evidence": "current graph", "repair_guidance": "rebuild edges"}]}
        return {"passed": True, "issues": []}

    async def repair(**kwargs):
        nonlocal repair_calls
        repair_calls += 1
        return {"function_items": kwargs["function_items"], "responsibility_edges": [_output_edge("scripts/a.py")]}

    async def regenerate(**kwargs):
        nonlocal regenerate_calls
        regenerate_calls += 1
        return {"function_items": kwargs["function_items"], "responsibility_edges": valid_edges}

    result, _, _ = await _run_ready_graph_alignment_flow(
        monkeypatch, review, repair, initial_edges=valid_edges, regenerate=regenerate,
    )
    assert result["responsibility_edges"] == valid_edges
    assert (repair_calls, regenerate_calls, review_calls) == (1, 1, 2)


@pytest.mark.asyncio
async def test_invalid_full_graph_regeneration_stops_without_more_retries(monkeypatch):
    repair_calls = 0
    regenerate_calls = 0
    input_edge = {"from_node": "platform_input_node", "from_output": "user_request", "to_node": "scripts/a.py", "to_input": "source", "purpose": "provide input", "constraints": []}
    valid_edges = [input_edge, _output_edge("scripts/a.py")]

    async def review(**kwargs):
        return {"passed": False, "issues": [{"id": "alignment", "target_files": ["scripts/a.py"], "affected_edge_indexes": [], "reason": "wrong topology", "evidence": "current graph", "repair_guidance": "rebuild edges"}]}

    async def repair(**kwargs):
        nonlocal repair_calls
        repair_calls += 1
        return {"function_items": kwargs["function_items"], "responsibility_edges": []}

    async def regenerate(**kwargs):
        nonlocal regenerate_calls
        regenerate_calls += 1
        return {"function_items": kwargs["function_items"], "responsibility_edges": []}

    with pytest.raises(api.PreparePlanProtocolError, match="one localized repair and one full graph regeneration"):
        await _run_ready_graph_alignment_flow(
            monkeypatch, review, repair, initial_edges=valid_edges, regenerate=regenerate,
        )
    assert (repair_calls, regenerate_calls) == (1, 1)


def test_alignment_review_prompt_has_bounded_reviewer_authority():
    import inspect

    review_source = inspect.getsource(api._review_responsibility_graph_alignment)
    repair_source = inspect.getsource(api._repair_responsibility_graph_alignment)

    # Assert durable authority boundaries, rather than the full prompt wording.
    for text in [
        "read-only ResponsibilityGraph Alignment Reviewer",
        "Do not redesign it.",
        "Do not improve it.",
        "CONFIRMED Blueprint",
        "Check ONLY these three principles",
        "Sufficient is PASS.",
        "runtime implementation verification",
        "feedback loops",
        "synchronization mechanisms",
        "file existence",
        "boundary presence, and platform-slot\nlegality are backend-validator facts",
        "Do not propose FilePlan changes.",
    ]:
        assert text in review_source
    assert "the same Blueprint Planner acting only" not in review_source
    assert "actual host execution model" not in review_source
    assert "required platform inputs and final outputs have declared boundary closure" not in review_source
    assert "required platform\nboundaries are connected" not in review_source
    assert "sufficient platform-output closure" not in review_source
    assert "localized" in repair_source


def test_alignment_review_prompt_preserves_semantic_mismatch_regression_boundary():
    import inspect

    review_source = inspect.getsource(api._review_responsibility_graph_alignment)

    # A declared result mismatch remains reviewable; this is not a
    # field-presence-only reviewer.
    assert "producer output and consumer input" in review_source
    assert "story_text into story_segments" in review_source
    assert "affected_edge_indexes must include that edge's index" in review_source


def test_alignment_review_prompt_stops_for_closed_multi_input_and_output_graphs():
    import inspect

    review_source = inspect.getsource(api._review_responsibility_graph_alignment)

    # These boundaries cover the repaired graph: generation based on source
    # content, two inputs consumed by one owner, a terminal artifact edge, and
    # internal-only intermediate outputs.
    for text in [
        "owns generation",
        "separate synchronization, pairing, or mapping responsibility",
        "incoming declared\ndependencies are sufficient",
        "OUTPUT_DIR handling",
        "output MAY be internal-only",
        "post-generation validation",
    ]:
        assert text in review_source


@pytest.mark.asyncio
async def test_alignment_review_payload_contains_only_graph_authorities(monkeypatch):
    import json

    captured_messages = []

    async def fake_complete_creator_role_once(messages, role, fallback_model):
        captured_messages.extend(messages)
        assert role == "reviewer"
        assert fallback_model == "planner"
        return '{"passed": true, "issues": []}'

    monkeypatch.setattr(api, "complete_creator_role_once", fake_complete_creator_role_once)
    request = _request(
        conversation_history=[{"role": "user", "content": "add visual validation"}],
        user_request="an obsolete raw request",
        human_feedback="add a feedback loop",
        previous_blueprint_text="previous blueprint required validation",
    )
    function_items = [{
        **_function_item("scripts/document_builder.py"),
        "outputs": ["pdf_path"],
    }]
    responsibility_edges = [{
        "from_node": "scripts/document_builder.py",
        "from_output": "pdf_path",
        "to_node": "platform_output_node",
        "to_input": "pdf_path",
        "purpose": "deliver the document",
        "constraints": [],
    }]

    review = await api._review_responsibility_graph_alignment(
        request=request,
        frozen_blueprint_text="confirmed blueprint only",
        allowed_function_item_targets=["scripts/document_builder.py"],
        function_items=function_items,
        responsibility_edges=responsibility_edges,
        planner_model="planner",
    )

    assert review == {"passed": True, "issues": []}
    payload = json.loads(captured_messages[1]["content"])
    assert payload == {
        "task": "review_responsibility_graph_alignment",
        "confirmed_blueprint": "confirmed blueprint only",
        "allowed_function_item_targets": ["scripts/document_builder.py"],
        "function_items": function_items,
        "responsibility_edges": responsibility_edges,
        "platform_boundary_contract": {
            "input_fields": api.build_platform_io_contract()["platform_skill_boundary"]["input_envelope_fields"],
            "final_output_fields": api.build_platform_io_contract()["platform_skill_boundary"]["final_output_fields"],
        },
    }

    serialized_payload = json.dumps(payload, ensure_ascii=False)
    for forbidden_text in [
        "OUTPUT_DIR",
        "artifact helper",
        "allowed_artifact_roots",
        "helper_filename_rule",
        "valid_absolute_paths",
        "forbidden_patterns",
        "basename only",
        "file exists",
        "os.path.join(OUTPUT_DIR",
        "stdout JSON transport",
        "argv conventions",
        "conversation_history",
        "previous_blueprint_text",
        "human_feedback",
        "an obsolete raw request",
    ]:
        assert forbidden_text not in serialized_payload
    assert "user_request" not in payload
    assert "user_request" in payload["platform_boundary_contract"]["input_fields"]


def test_responsibility_graph_transport_validator_rejects_missing_declared_io():
    from backend.services.skill_plan import validate_structured_responsibility_edge_transport

    producer = {**_function_item("scripts/producer.py"), "outputs": ["foo"]}
    consumer = {**_function_item("scripts/consumer.py"), "inputs": ["foo"]}
    missing_output = {
        "from_node": "scripts/producer.py", "from_output": "bar",
        "to_node": "scripts/consumer.py", "to_input": "foo",
        "purpose": "invalid source", "constraints": [],
    }
    missing_input = {**missing_output, "from_output": "foo", "to_input": "bar"}

    with pytest.raises(ValueError, match="undefined FunctionItem source output"):
        validate_structured_responsibility_edge_transport(
            [missing_output], function_items=[producer, consumer], source="planner")
    with pytest.raises(ValueError, match="undefined FunctionItem target input"):
        validate_structured_responsibility_edge_transport(
            [missing_input], function_items=[producer, consumer], source="planner")


def test_responsibility_graph_transport_validator_validates_platform_slots_independently():
    from backend.services.skill_plan import validate_structured_responsibility_edge_transport

    source = {**_function_item("scripts/source.py"), "inputs": ["request"], "outputs": ["pdf_path"]}
    valid_edges = [
        {"from_node": "platform_input_node", "from_output": "user_request", "to_node": "scripts/source.py", "to_input": "request", "purpose": "supply request", "constraints": []},
        {"from_node": "scripts/source.py", "from_output": "pdf_path", "to_node": "platform_output_node", "to_input": "pdf_path", "purpose": "deliver artifact", "constraints": []},
    ]
    assert validate_structured_responsibility_edge_transport(valid_edges, function_items=[source], source="planner") == valid_edges
    with pytest.raises(ValueError, match="undefined platform input field"):
        validate_structured_responsibility_edge_transport([{**valid_edges[0], "from_output": "not_a_platform_input"}], function_items=[source], source="planner")
    with pytest.raises(ValueError, match="undefined platform output field"):
        validate_structured_responsibility_edge_transport([{**valid_edges[1], "to_input": "not_a_platform_output"}], function_items=[source], source="planner")


def test_responsibility_graph_terminal_edges_remain_independent():
    from backend.services.skill_plan import validate_structured_responsibility_edge_transport

    builder = {**_function_item("scripts/builder.py"), "outputs": ["pdf_path", "docx_path"]}
    pdf_edge = {"from_node": "scripts/builder.py", "from_output": "pdf_path", "to_node": "platform_output_node", "to_input": "pdf_path", "purpose": "deliver pdf", "constraints": []}
    docx_edge = {**pdf_edge, "from_output": "docx_path", "to_input": "docx_path", "purpose": "deliver docx"}
    assert validate_structured_responsibility_edge_transport([pdf_edge], function_items=[builder], source="planner") == [pdf_edge]
    assert validate_structured_responsibility_edge_transport([pdf_edge, docx_edge], function_items=[builder], source="planner") == [pdf_edge, docx_edge]


@pytest.mark.asyncio
async def test_alignment_repair_payload_contains_only_graph_authorities(monkeypatch):
    import json

    captured_messages = []

    async def fake_complete_creator_role_once(messages, role, fallback_model):
        captured_messages.extend(messages)
        return '{"responsibility_edges": []}'

    monkeypatch.setattr(api, "complete_creator_role_once", fake_complete_creator_role_once)
    await api._repair_responsibility_graph_alignment(
        request=_request(conversation_history=[{"role": "user", "content": "obsolete"}], human_feedback="obsolete", previous_blueprint_text="obsolete"),
        frozen_blueprint_text="confirmed blueprint", allowed_function_item_targets=[],
        function_items=[], responsibility_edges=[], review_issues=[], planner_model="planner",
    )
    serialized_payload = captured_messages[1]["content"]
    for forbidden_text in ["OUTPUT_DIR", "artifact helper", "conversation_history", "previous_blueprint_text", "human_feedback"]:
        assert forbidden_text not in serialized_payload
    assert json.loads(serialized_payload)["platform_boundary_contract"]


@pytest.mark.asyncio
async def test_alignment_review_requires_localizable_failure_issues(monkeypatch):
    responses = iter([
        '{"passed":false,"issues":[]}',
        '{"passed":false,"issues":[{"id":"issue","target_files":[],"affected_edge_indexes":[],"reason":"r","evidence":"e"}]}',
        '{"passed":false,"issues":[{"id":1,"target_files":[],"affected_edge_indexes":[],"reason":"r","evidence":"e","repair_guidance":"g"}]}',
    ])

    async def fake_complete(messages, role, fallback_model):
        return next(responses)

    monkeypatch.setattr(api, "complete_creator_role_once", fake_complete)
    kwargs = {
        "request": _request(), "frozen_blueprint_text": "frozen",
        "allowed_function_item_targets": [], "function_items": [],
        "responsibility_edges": [], "planner_model": "planner",
    }
    with pytest.raises(ValueError, match="must include issues"):
        await api._review_responsibility_graph_alignment(**kwargs)
    with pytest.raises(ValueError, match="invalid structure"):
        await api._review_responsibility_graph_alignment(**kwargs)
    with pytest.raises(ValueError, match="invalid structure"):
        await api._review_responsibility_graph_alignment(**kwargs)


@pytest.mark.asyncio
async def test_alignment_review_and_repair_accept_normal_protocol_outputs(monkeypatch):
    import json

    issue = {"id": "issue", "target_files": [], "affected_edge_indexes": [], "reason": "r", "evidence": "e", "repair_guidance": "g"}
    responses = iter([
        json.dumps({"passed": False, "issues": [issue]}),
        json.dumps({"responsibility_edges": []}),
    ])

    async def fake_complete(messages, role, fallback_model):
        return next(responses)

    monkeypatch.setattr(api, "complete_creator_role_once", fake_complete)
    review = await api._review_responsibility_graph_alignment(
        request=_request(), frozen_blueprint_text="frozen", allowed_function_item_targets=[],
        function_items=[], responsibility_edges=[], planner_model="planner",
    )
    repair = await api._repair_responsibility_graph_alignment(
        request=_request(), frozen_blueprint_text="frozen", allowed_function_item_targets=[],
        function_items=[], responsibility_edges=[], review_issues=review["issues"], planner_model="planner",
    )
    assert review == {"passed": False, "issues": [issue]}
    assert repair == {"function_items": [], "responsibility_edges": []}


@pytest.mark.asyncio
async def test_alignment_repair_rejects_extra_top_level_fields(monkeypatch):
    async def fake_complete(messages, role, fallback_model):
        return '{"responsibility_edges":[],"extra":true}'

    monkeypatch.setattr(api, "complete_creator_role_once", fake_complete)
    with pytest.raises(ValueError, match="only responsibility_edges"):
        await api._repair_responsibility_graph_alignment(
            request=_request(), frozen_blueprint_text="frozen", allowed_function_item_targets=[],
            function_items=[], responsibility_edges=[], review_issues=[], planner_model="planner",
        )


def test_responsibility_graph_boundary_presence_requires_both_platform_ends():
    internal_edge = {"from_node": "scripts/a.py", "from_output": "result", "to_node": "scripts/b.py", "to_input": "source", "purpose": "handoff", "constraints": []}
    input_edge = {"from_node": "platform_input_node", "from_output": "user_request", "to_node": "scripts/a.py", "to_input": "source", "purpose": "provide input", "constraints": []}
    output_edge = _output_edge("scripts/b.py")
    targets = ["scripts/a.py", "scripts/b.py"]

    with pytest.raises(api.GraphValidationError, match="input boundary edge; missing platform output boundary edge") as both_missing:
        api._validate_responsibility_graph_boundary_presence([internal_edge], targets)
    assert both_missing.value.code == "missing_platform_boundary"
    assert both_missing.value.details == {
        "missing_input_boundary": True,
        "missing_output_boundary": True,
    }
    with pytest.raises(api.GraphValidationError, match="output boundary edge") as output_missing:
        api._validate_responsibility_graph_boundary_presence([input_edge], targets)
    assert output_missing.value.details["missing_output_boundary"] is True
    with pytest.raises(ValueError, match="input boundary edge"):
        api._validate_responsibility_graph_boundary_presence([output_edge], targets)
    api._validate_responsibility_graph_boundary_presence([input_edge, internal_edge, output_edge], targets)


@pytest.mark.asyncio
async def test_boundary_presence_failure_uses_one_existing_localized_repair(monkeypatch):
    review_calls = 0
    repair_calls = 0
    input_edge = {"from_node": "platform_input_node", "from_output": "user_request", "to_node": "scripts/a.py", "to_input": "source", "purpose": "provide input", "constraints": []}

    async def review(**kwargs):
        nonlocal review_calls
        review_calls += 1
        return {"passed": True, "issues": []}

    async def repair(**kwargs):
        nonlocal repair_calls
        repair_calls += 1
        return {"function_items": kwargs["function_items"], "responsibility_edges": [input_edge, _output_edge("scripts/a.py")]}

    result, _, _ = await _run_ready_graph_alignment_flow(
        monkeypatch, review, repair, initial_edges=[input_edge]
    )
    assert result["responsibility_edges"] == [input_edge, _output_edge("scripts/a.py")]
    assert review_calls == 1
    assert repair_calls == 1


@pytest.mark.asyncio
async def test_boundary_presence_failure_after_repair_blocks(monkeypatch):
    input_edge = {"from_node": "platform_input_node", "from_output": "user_request", "to_node": "scripts/a.py", "to_input": "source", "purpose": "provide input", "constraints": []}

    async def review(**kwargs):
        return {"passed": True, "issues": []}

    async def repair(**kwargs):
        return {"function_items": kwargs["function_items"], "responsibility_edges": [input_edge]}

    with pytest.raises(api.PreparePlanProtocolError, match="missing platform output boundary edge"):
        await _run_ready_graph_alignment_flow(monkeypatch, review, repair, initial_edges=[input_edge])



@pytest.mark.asyncio
async def test_provenance_repair_cannot_delete_invalid_source_and_leave_input_unresolved(monkeypatch):
    root = api.build_platform_io_contract()["platform_skill_boundary"]["preferred_structured_input_root"]
    item = {**_function_item("scripts/a.py"), "inputs": ["optional_parameter"]}
    invalid_edge = {"from_node": "platform_input_node", "from_output": "invalid_dynamic_slot", "to_node": "scripts/a.py", "to_input": "optional_parameter", "purpose": "invalid dynamic input", "constraints": []}
    repaired_edge = {"from_node": "platform_input_node", "from_output": root, "to_node": "scripts/a.py", "to_input": "optional_parameter", "purpose": "bound dynamic input", "constraints": [{"type": "platform_parameter_binding", "source_key": "dynamic_key", "required": False, "default": None}]}
    repair_issues = []
    review_calls = 0

    async def review(**kwargs):
        nonlocal review_calls
        review_calls += 1
        return {"passed": True, "issues": []}

    async def repair(**kwargs):
        repair_issues.append(kwargs["review_issues"])
        assert review_calls == 0
        if len(repair_issues) == 1:
            # Removing the invalid source alone leaves the declared input dangling.
            return {"function_items": [item], "responsibility_edges": [_output_edge("scripts/a.py")]}
        assert repair_issues[1][0]["id"] == "responsibility_input_provenance"
        return {"function_items": [item], "responsibility_edges": [repaired_edge, _output_edge("scripts/a.py")]}

    result, _, _ = await _run_ready_graph_alignment_flow(
        monkeypatch, review, repair, initial_edges=[invalid_edge, _output_edge("scripts/a.py")], function_item=item,
    )
    assert result["responsibility_edges"] == [repaired_edge, _output_edge("scripts/a.py")]
    assert len(repair_issues) == 2
    assert review_calls == 1


@pytest.mark.asyncio
async def test_provenance_repair_receives_all_current_gaps_together(monkeypatch):
    root = api.build_platform_io_contract()["platform_skill_boundary"]["preferred_structured_input_root"]
    item = {**_function_item("scripts/a.py"), "inputs": ["first_input", "second_input"]}
    repaired_edges = [
        {"from_node": "platform_input_node", "from_output": root, "to_node": "scripts/a.py", "to_input": input_name, "purpose": "bind runtime input", "constraints": [{"type": "platform_parameter_binding", "source_key": source_key, "required": True}]}
        for input_name, source_key in [("first_input", "first_key"), ("second_input", "second_key")]
    ]
    received_issues = []

    async def review(**kwargs):
        return {"passed": True, "issues": []}

    async def repair(**kwargs):
        received_issues.extend(kwargs["review_issues"])
        return {"function_items": [item], "responsibility_edges": [*repaired_edges, _output_edge("scripts/a.py")]}

    await _run_ready_graph_alignment_flow(
        monkeypatch, review, repair, initial_edges=[_output_edge("scripts/a.py")], function_item=item,
    )
    assert [(issue["target_files"], issue["evidence"]) for issue in received_issues] == [
        (["scripts/a.py"], "target_file=scripts/a.py; input=first_input"),
        (["scripts/a.py"], "target_file=scripts/a.py; input=second_input"),
    ]

def test_responsibility_graph_runtime_input_provenance_protocol():
    from backend.services.skill_plan import (
        validate_structured_responsibility_edge_transport,
        validate_structured_responsibility_graph_input_closure,
    )

    root = api.build_platform_io_contract()["platform_skill_boundary"]["preferred_structured_input_root"]
    source = {**_function_item("scripts/a.py"), "inputs": ["primary_input", "optional_parameter"], "outputs": ["result"]}
    top_level = {"from_node": "platform_input_node", "from_output": "user_request", "to_node": "scripts/a.py", "to_input": "primary_input", "purpose": "provide primary input", "constraints": []}
    structured = {"from_node": "platform_input_node", "from_output": root, "to_node": "scripts/a.py", "to_input": "optional_parameter", "purpose": "provide dynamic parameter", "constraints": [{"type": "platform_parameter_binding", "source_key": "custom_parameter", "required": False, "default": None}]}

    valid_edges = [top_level, structured]
    assert validate_structured_responsibility_edge_transport(valid_edges, function_items=[source]) == valid_edges
    validate_structured_responsibility_graph_input_closure([source], valid_edges)

    whole_root_source = {**_function_item("scripts/a.py"), "inputs": ["runtime_payload"]}
    whole_root_edge = {**structured, "to_input": "runtime_payload", "purpose": "provide structured input", "constraints": []}
    assert validate_structured_responsibility_edge_transport([whole_root_edge], function_items=[whole_root_source]) == [whole_root_edge]
    validate_structured_responsibility_graph_input_closure([whole_root_source], [whole_root_edge])

    with pytest.raises(ValueError, match="undefined platform input field"):
        validate_structured_responsibility_edge_transport([{**structured, "from_output": "custom_parameter"}], function_items=[source])
    for invalid_constraints, message in [
        ([{"type": "platform_parameter_binding", "required": False, "default": None}], "source_key"),
        ([{"type": "platform_parameter_binding", "source_key": "custom_parameter", "required": "false", "default": None}], "boolean required"),
        ([{"type": "platform_parameter_binding", "source_key": "custom_parameter", "required": False}], "explicit default"),
        ([
            {"type": "platform_parameter_binding", "source_key": "first_key", "required": True},
            {"type": "platform_parameter_binding", "source_key": "second_key", "required": True},
        ], "multiple platform_parameter_binding"),
    ]:
        with pytest.raises(ValueError, match=message):
            validate_structured_responsibility_edge_transport([{**structured, "constraints": invalid_constraints}], function_items=[source])

    with pytest.raises(ValueError, match=r"target_file=scripts/a.py; input=optional_parameter"):
        validate_structured_responsibility_graph_input_closure([source], [top_level])
    # Deleting an invalid dynamic edge does not resolve the declared input.
    with pytest.raises(ValueError, match="optional_parameter"):
        validate_structured_responsibility_graph_input_closure([source], [top_level])
    with pytest.raises(ValueError, match="runtime_payload"):
        validate_structured_responsibility_graph_input_closure([whole_root_source], [])


def test_responsibility_graph_runtime_input_provenance_accepts_upstream_function_item():
    from backend.services.skill_plan import (
        validate_structured_responsibility_edge_transport,
        validate_structured_responsibility_graph_input_closure,
    )

    producer = {**_function_item("scripts/a.py"), "inputs": [], "outputs": ["result"]}
    consumer = {**_function_item("scripts/b.py"), "inputs": ["input_value"], "outputs": ["final"]}
    edge = {"from_node": "scripts/a.py", "from_output": "result", "to_node": "scripts/b.py", "to_input": "input_value", "purpose": "handoff", "constraints": []}
    assert validate_structured_responsibility_edge_transport([edge], function_items=[producer, consumer]) == [edge]
    validate_structured_responsibility_graph_input_closure([producer, consumer], [edge])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reviewed_id", "expected_passed"),
    [("", True), ("R999", True), ("R2", False)],
)
async def test_requirement_coverage_blocks_only_supplied_ids(
    monkeypatch, reviewed_id, expected_passed,
):
    issue = {
        "issue_type": "requirement_uncovered",
        "requirement_id": reviewed_id,
        "affected_targets": [],
        "reason": "review observation",
        "repair_guidance": "repair",
    }

    async def fake_complete(messages, *_args, **_kwargs):
        return json.dumps({"passed": False, "issues": [issue]})

    monkeypatch.setattr(api, "complete_creator_role_once", fake_complete)
    allocations = [
        {"requirement_id": requirement_id, "requirement": requirement_id,
         "owners": ["scripts/a.py"], "evidence": {}}
        for requirement_id in ("R1", "R2")
    ]
    review = await api._review_blueprint_semantic_closure(
        request=_request(), blueprint_text="blueprint",
        function_items=[_function_item("scripts/a.py")],
        requirement_allocations=allocations, planner_model="test",
    )

    assert review["passed"] is expected_passed
    assert [item["requirement_id"] for item in review["issues"]] == (
        [] if expected_passed else ["R2"]
    )


def test_requirement_allocation_owner_requires_exact_authoritative_path():
    from backend.services.creator.contracts import validate_requirement_allocations

    allocation = {
        "requirement_id": "R1", "requirement": "deliver result",
        "owners": ["scripts/a.py"],
        "evidence": {"responsibility": "deliver", "outputs": [], "capabilities": []},
    }
    assert validate_requirement_allocations(
        [allocation], allowed_owner_targets=["scripts/a.py", "scripts/b.py"]
    )[0]["owners"] == ["scripts/a.py"]
    with pytest.raises(ValueError, match="outside current FunctionItem domain"):
        validate_requirement_allocations(
            [{**allocation, "owners": ["text_generator"]}],
            allowed_owner_targets=["scripts/a.py", "scripts/b.py"],
        )


@pytest.mark.asyncio
async def test_semantic_review_prompt_preserves_planning_phase_boundaries(monkeypatch):
    captured = {}

    async def fake_complete(messages, *_args, **_kwargs):
        captured["prompt"] = messages[0]["content"]
        return json.dumps({"passed": True, "issues": []})

    monkeypatch.setattr(api, "complete_creator_role_once", fake_complete)
    await api._review_blueprint_semantic_closure(
        request=_request(), blueprint_text="references/foo.md",
        function_items=[_function_item("scripts/a.py")],
        requirement_allocations=[{
            "requirement_id": "R1", "requirement": "illustrated output",
            "owners": ["scripts/a.py"], "evidence": {},
        }], planner_model="test",
    )

    prompt = " ".join(captured["prompt"].split())
    assert "file does not exist" in prompt
    assert "Actual resource content quality is reviewed after file generation" in prompt
    assert "text_generation Tool producing image-prompt text is still text_generation" in prompt
