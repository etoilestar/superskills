import re

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

@pytest.mark.asyncio
async def test_blueprint_planner_prompt_includes_constraints_serialization_contract(monkeypatch):
    captured = []

    async def fake_complete(messages, model):
        captured.extend(messages)
        return '{"status":"needs_clarification","clarifying_questions":["输入来源？A. 粘贴 B. 上传"],"blockers":[]}'

    monkeypatch.setattr(api, "complete_chat_once", fake_complete)
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

    async def fake_complete(messages, model):
        captured.extend(messages)
        return '{"status":"needs_clarification","clarifying_questions":["输入来源？A. 粘贴 B. 上传"],"blockers":[]}'

    monkeypatch.setattr(api, "complete_chat_once", fake_complete)
    await api._generate_internal_blueprint_or_questions(_request())

    prompt = captured[0]["content"]
    assert "confirmed decision" in prompt
    assert "不得返回 status=ready" in prompt and "core action" in prompt
    assert "clarification answer 不是参考意见" in prompt
    assert "Blueprint planning 输入契约" in prompt
    assert "不得通过修改 Blueprint 业务目标来规避工具缺失" in prompt


@pytest.mark.asyncio
async def test_blueprint_planner_prompt_requires_explicit_constraints_field(monkeypatch):
    captured = []

    async def fake_complete(messages, model):
        captured.extend(messages)
        return '{"status":"needs_clarification","clarifying_questions":["输入来源？A. 粘贴 B. 上传"],"blockers":[]}'

    monkeypatch.setattr(api, "complete_chat_once", fake_complete)
    await api._generate_internal_blueprint_or_questions(_request())

    prompt = captured[0]["content"]
    section = prompt[prompt.index("## responsibility constraints"):prompt.index("## 内部处理与脚本拆分")]
    assert "每个 SkillPlan entry 都必须显式输出 constraints 字段" in section
    assert "constraints: []" in section


@pytest.mark.asyncio
async def test_blueprint_planner_prompt_requires_final_delivery_closure(monkeypatch):
    captured = []

    async def fake_complete(messages, model):
        captured.extend(messages)
        return '{"status":"needs_clarification","clarifying_questions":["输入来源？A. 粘贴 B. 上传"],"blockers":[]}'

    monkeypatch.setattr(api, "complete_chat_once", fake_complete)
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

    async def fake_complete(messages, model):
        captured.extend(messages)
        return '{"status":"needs_clarification","clarifying_questions":["输入来源？A. 粘贴 B. 上传"],"blockers":[]}'

    monkeypatch.setattr(api, "complete_chat_once", fake_complete)
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

    async def fake_complete(messages, model):
        captured.extend(messages)
        return '{"status":"needs_clarification","clarifying_questions":["输入来源？A. 粘贴 B. 上传"],"blockers":[]}'

    monkeypatch.setattr(api, "complete_chat_once", fake_complete)
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
    structured = {"from_node":"scripts/a.py","from_output":"result","to_node":"platform_output_node","to_input":"final_output","purpose":"structured","constraints":[]}
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
    async def fake_complete(messages, model):
        return '{"status":"ready","internal_blueprint_text":"## 📋 Skill 架构蓝图","skill_name":"demo-skill","blockers":[]}'
    monkeypatch.setattr(api, "complete_chat_once", fake_complete)
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
    edge = {"from_node":"scripts/a.py","from_output":"alpha","to_node":"platform_output_node","to_input":"final_output","purpose":"deliver","constraints":[]}

    async def fake_complete(messages, model):
        calls.append(messages)
        return '{"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"draft","skill_name":"demo","blockers":[],"responsibility_edges":[' + __import__('json').dumps(edge) + ']}'

    monkeypatch.setattr(api, "complete_chat_once", fake_complete)
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

    async def fake_complete(messages, model):
        return json.dumps(responses.pop(0))

    monkeypatch.setattr(api, "complete_chat_once", fake_complete)
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

    async def fake_complete(messages, model):
        return json.dumps(responses.pop(0))

    monkeypatch.setattr(api, "complete_chat_once", fake_complete)
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

    async def fake_complete(messages, model):
        return json.dumps(responses.pop(0))

    monkeypatch.setattr(api, "complete_chat_once", fake_complete)
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
    draft_edge = {"from_node":"scripts/a.py","from_output":"result_alpha","to_node":"platform_output_node","to_input":"final_output","purpose":"deliver","constraints":[]}
    responses = [
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"draft","skill_name":"demo","blockers":[],"responsibility_edges":[draft_edge]},
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"incomplete","skill_name":"demo","blockers":[]},
    ]

    async def fake_complete(messages, model):
        return json.dumps(responses.pop(0))

    monkeypatch.setattr(api, "complete_chat_once", fake_complete)
    result = await api._generate_internal_blueprint_or_questions(_request())
    assert result["internal_blueprint_text"] == "draft"
    assert result["responsibility_edges"] == [draft_edge]


@pytest.mark.asyncio
async def test_planner_convergence_incomplete_transport_fields_keeps_draft(monkeypatch):
    import json
    draft_edge = {"from_node":"scripts/a.py","from_output":"result_alpha","to_node":"platform_output_node","to_input":"final_output","purpose":"deliver","constraints":[]}
    revised_edge = {"from_node":"scripts/a.py","from_output":"result_beta","to_node":"platform_output_node","to_input":"final_output","purpose":"deliver","constraints":[]}
    responses = [
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"draft","skill_name":"demo","blockers":[],"responsibility_edges":[draft_edge]},
        {"status":"ready","clarifying_questions":[],"internal_blueprint_text":"incomplete","skill_name":"demo","blockers":[],"responsibility_edges":[revised_edge]},
    ]

    async def fake_complete(messages, model):
        return json.dumps(responses.pop(0))

    monkeypatch.setattr(api, "complete_chat_once", fake_complete)
    result = await api._generate_internal_blueprint_or_questions(_request())
    assert result["internal_blueprint_text"] == "draft"
    assert result["responsibility_edges"] == [draft_edge]


@pytest.mark.asyncio
async def test_planner_convergence_null_responsibility_edges_keeps_draft(monkeypatch):
    import json
    draft_edge = {"from_node":"scripts/a.py","from_output":"result_alpha","to_node":"platform_output_node","to_input":"final_output","purpose":"deliver","constraints":[]}
    responses = [
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"draft","skill_name":"demo","blockers":[],"responsibility_edges":[draft_edge]},
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"null edges","skill_name":"demo","blockers":[],"responsibility_edges":None},
    ]

    async def fake_complete(messages, model):
        return json.dumps(responses.pop(0))

    monkeypatch.setattr(api, "complete_chat_once", fake_complete)
    result = await api._generate_internal_blueprint_or_questions(_request())
    assert result["internal_blueprint_text"] == "draft"
    assert result["responsibility_edges"] == [draft_edge]


@pytest.mark.asyncio
async def test_planner_convergence_invalid_transport_shape_keeps_draft(monkeypatch):
    import json
    draft_edge = {"from_node":"scripts/a.py","from_output":"result_alpha","to_node":"platform_output_node","to_input":"final_output","purpose":"deliver","constraints":[]}
    responses = [
        {"status":"ready","clarifying_questions":[],"review_summary":{},"internal_blueprint_text":"draft","skill_name":"demo","blockers":[],"responsibility_edges":[draft_edge]},
        {"status":"ready","clarifying_questions":"not-a-list","review_summary":[],"internal_blueprint_text":"bad shape","skill_name":"demo","blockers":{},"responsibility_edges":[]},
    ]

    async def fake_complete(messages, model):
        return json.dumps(responses.pop(0))

    monkeypatch.setattr(api, "complete_chat_once", fake_complete)
    result = await api._generate_internal_blueprint_or_questions(_request())
    assert result["internal_blueprint_text"] == "draft"
    assert result["responsibility_edges"] == [draft_edge]
