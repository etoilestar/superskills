import pytest
from fastapi import HTTPException

from backend.services.creator import api
from backend.services.creator.common import AnalyzeBlueprintResponse, FileSpecOut


def _request(**kwargs):
    data = {"user_request": "做一个工具", "human_feedback": "", "model": None}
    data.update(kwargs)
    return api.PreparePlanRequest(**data)


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


def test_preflight_rejects_runtime_input_assets_and_missing_skillplan_path():
    text = _ready_blueprint("- path: `SKILL.md`\n  role: skill_overview") + "\n运行时每次上传的用户输入文件 assets/input.pdf\n"
    codes = {i["code"] for i in api._preflight_prepare_blueprint_text(text)}
    assert "directory_or_text_path_missing_from_skill_plan" in codes


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
    resp = await api.prepare_plan(_request(human_feedback=feedback, prepare_action="request_supplement"))
    assert resp.status == "needs_clarification"
    assert "请补充你的其他要求" in resp.clarifying_questions[0]
    assert resp.files == []


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
