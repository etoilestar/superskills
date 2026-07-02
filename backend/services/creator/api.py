"""Creator FastAPI endpoint handlers and response assembly."""

import hashlib
from typing import Literal

from .common import *  # noqa: F403
from .contracts import *  # noqa: F403
from .e2e import *  # noqa: F403
from .repair import *  # noqa: F403
from .generation import *  # noqa: F403
from ..kernel_loader import load_kernel_creator_for_phase


class PreparePlanRequest(BaseModel):
    mode: Literal["create", "revise"] = "create"
    skill_name: str | None = None
    user_request: str = ""
    conversation_history: list[dict[str, Any]] = []
    uploaded_files: list[dict[str, Any]] = []
    previous_blueprint_text: str = ""
    human_feedback: str = ""
    prepare_action: Literal["none", "confirm", "request_supplement", "submit_supplement"] = "none"
    model: str | None = None


class PreparePlanReviewSummary(BaseModel):
    goal: str = ""
    input: str = ""
    output: str = ""
    workflow: list[str] = []
    files_to_create_or_update: list[str] = []
    assets_to_upload: list[str] = []
    risks: list[str] = []
    changes: list[str] = []


class PreparePlanResponse(BaseModel):
    status: Literal["ready", "needs_clarification", "blocked"]
    prepare_stage: Literal["business_clarification", "creation_points_confirmation", "supplement_confirmation", "ready"] = "business_clarification"
    clarifying_questions: list[str] = Field(default_factory=list)
    review_summary: PreparePlanReviewSummary = Field(default_factory=PreparePlanReviewSummary)
    blueprint_text: str = ""
    skill_name: str = ""
    files: list[FileSpecOut] = Field(default_factory=list)
    warnings: list[Any] = Field(default_factory=list)
    asset_requirements: list[AssetRequirementOut] = Field(default_factory=list)
    final_outputs: list[Any] = Field(default_factory=list)
    available_tools: list[Any] = Field(default_factory=list)
    missing_tool_configs: list[Any] = Field(default_factory=list)
    tool_requirements: list[Any] = Field(default_factory=list)
    creation_blockers: list[Any] = Field(default_factory=list)
    requirement_graph: Any = Field(default_factory=dict)
    workflow_allocation_summary: str = ""


def _read_prepare_existing_skill_context(skill_name: str | None) -> dict[str, Any]:
    if not skill_name:
        return {}
    safe_name = _validate_skill_name(skill_name)
    root = settings.skills_path / safe_name
    if not root.is_dir():
        return {"skill_name": safe_name, "missing": True}

    def read_text(rel: str) -> str:
        path = root / rel
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""

    def list_dir(rel: str) -> list[str]:
        base = root / rel
        if not base.is_dir():
            return []
        return sorted(p.relative_to(root).as_posix() for p in base.rglob("*") if p.is_file())

    return {
        "skill_name": safe_name,
        "skill_md": read_text("SKILL.md")[:20000],
        "scripts": list_dir("scripts"),
        "references": list_dir("references"),
        "assets": list_dir("assets"),
        "requirement_graph": read_text(".creator/requirement_graph.json")[:20000],
        "workflow_allocation_summary": read_text(".creator/workflow_allocation_summary.txt")[:12000],
    }


def _parse_prepare_plan_json(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise ValueError("prepare-plan model did not return JSON")
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("prepare-plan JSON must be an object")
    return data


def _strip_prepare_summary_risks(summary: PreparePlanReviewSummary) -> PreparePlanReviewSummary:
    summary.risks = []
    return summary


def _coerce_prepare_summary(data: Any) -> PreparePlanReviewSummary:
    if not isinstance(data, dict):
        return PreparePlanReviewSummary()
    return PreparePlanReviewSummary(
        goal=str(data.get("goal") or ""),
        input=str(data.get("input") or ""),
        output=str(data.get("output") or ""),
        workflow=[str(x) for x in (data.get("workflow") or []) if str(x).strip()],
        files_to_create_or_update=[str(x) for x in (data.get("files_to_create_or_update") or []) if str(x).strip()],
        assets_to_upload=[str(x) for x in (data.get("assets_to_upload") or []) if str(x).strip()],
        risks=[str(x) for x in (data.get("risks") or []) if str(x).strip()],
        changes=[str(x) for x in (data.get("changes") or []) if str(x).strip()],
    )


MAX_PREPARE_BUSINESS_CLARIFICATION_ROUNDS = 2
MAX_PREPARE_SUPPLEMENT_ROUNDS = 1

_PREPARE_SUPPLEMENT_QUESTION = "以上创建要点是否还需要补充？A. 没有，按这些要点继续 B. 有，我补充说明"


def _prepare_history_text(request: PreparePlanRequest) -> str:
    parts = [str(request.user_request or ""), str(request.human_feedback or "")]
    for item in request.conversation_history or []:
        if isinstance(item, dict):
            parts.append(str(item.get("content") or ""))
    return "\n".join(parts)


def _count_prepare_business_clarification_rounds(request: PreparePlanRequest) -> int:
    count = 0
    for item in request.conversation_history or []:
        if not isinstance(item, dict) or item.get("role") != "assistant":
            continue
        text = str(item.get("content") or "")
        if any(token in text for token in ("以上创建要点是否还需要补充", "是否还需要继续补充", "还有其他需要补充")):
            continue
        if "我还需要确认一个必要信息" in text or "needs_clarification" in text or _prepare_questions_have_options([text]):
            count += 1
    return count


def _prepare_business_clarification_limit_reached(request: PreparePlanRequest) -> bool:
    return _count_prepare_business_clarification_rounds(request) >= MAX_PREPARE_BUSINESS_CLARIFICATION_ROUNDS


def _prepare_supplement_check_seen(request: PreparePlanRequest) -> bool:
    return any(token in _prepare_history_text(request) for token in ("以上创建要点是否还需要补充", "是否还需要继续补充", "还有其他需要补充"))


def _prepare_feedback_choice_text(request: PreparePlanRequest) -> str:
    text = str(request.human_feedback or "").strip()
    matches = re.findall(r"(?im)^\s*选择\s*[:：]\s*(.+?)\s*$", text)
    return matches[-1].strip() if matches else text


def _prepare_has_explicit_action(request: PreparePlanRequest) -> bool:
    return str(getattr(request, "prepare_action", "none") or "none") != "none"


def _prepare_user_confirmed_no_more_supplement(request: PreparePlanRequest) -> bool:
    if _prepare_has_explicit_action(request):
        return request.prepare_action == "confirm"
    text = _prepare_feedback_choice_text(request)
    return bool(re.search(r"(没有|无|暫時沒有|暂时没有).{0,12}补充|按(这些|上面|已有)信息继续|按这些要点继续|按上面的选择继续", text))


def _prepare_user_wants_to_add_supplement(request: PreparePlanRequest) -> bool:
    if _prepare_has_explicit_action(request):
        return request.prepare_action == "request_supplement"
    text = _prepare_feedback_choice_text(request)
    if not text or _prepare_user_confirmed_no_more_supplement(request):
        return False
    return any(marker in text for marker in ("有，我补充说明", "我补充", "有补充", "继续补充"))


def _prepare_user_has_provided_supplement_content(request: PreparePlanRequest) -> bool:
    if _prepare_has_explicit_action(request):
        return request.prepare_action == "submit_supplement"
    full_text = str(request.human_feedback or "").strip()
    text = _prepare_feedback_choice_text(request)
    if not full_text or _prepare_user_confirmed_no_more_supplement(request):
        return False
    return "补充：" in full_text or (_prepare_user_wants_to_add_supplement(request) and len(full_text) > 80)


def _count_prepare_supplement_rounds(request: PreparePlanRequest) -> int:
    text = _prepare_history_text(request)
    return len(re.findall(r"补充：", text))

def _prepare_questions_have_options(questions: list[str]) -> bool:
    option_marker = re.compile(r"(?<![A-Za-z0-9])(?:[A-D][\.、)]|[①②③④]|\([A-D]\))")
    for question in questions:
        text = str(question or "")
        if "选项" in text:
            continue
        markers = option_marker.findall(text)
        if not (("A." in text or "A、" in text or "A)" in text) and ("B." in text or "B、" in text or "B)" in text)) and len(markers) < 2:
            return False
    return bool(questions)


def _prepare_questions_include_supplement_check(questions: list[str]) -> bool:
    if not questions:
        return False
    last = str(questions[-1] or "")
    return any(token in last for token in ("补充", "其他要求", "其他内容", "还有", "需要补充"))


def _normalize_prepare_clarifying_questions(raw_questions: Any) -> list[str]:
    questions = [str(q).strip() for q in (raw_questions or []) if str(q).strip()]
    if not questions:
        questions = ["请补充当前最阻塞创建计划的信息。A. 我现在补充 B. 暂时没有补充，按已有信息继续"]
    question = questions[0]
    if not _prepare_questions_have_options([question]):
        question = f"{question} A. 按推荐方式继续 B. 我补充说明"
    return [question]


def _prepare_feedback_wants_supplement(request: PreparePlanRequest) -> bool:
    if _prepare_has_explicit_action(request):
        return request.prepare_action == "request_supplement"
    feedback = _prepare_feedback_choice_text(request)
    if not feedback:
        return False
    if re.search(r"(没有|暫時沒有|暂时没有).{0,8}补充", feedback):
        return False
    supplement_markers = ("有，我补充说明", "我补充", "有补充")
    if any(marker in feedback for marker in supplement_markers):
        # A frontend quick action sends only the choice. Once the user types real
        # supplementary content, the feedback should contain more than just that
        # short choice/context wrapper and may proceed through normal model logic.
        compact = re.sub(r"\s+", "", feedback)
        choice_only_patterns = (
            "B.有,我补充说明", "B.有，我补充说明", "选择:B.有,我补充说明",
            "选择：B.有，我补充说明", "我补充", "有补充",
        )
        return len(feedback) <= 80 or any(pattern.replace(" ", "") in compact for pattern in choice_only_patterns)
    return False


def _prepare_protocol_issue(code: str, message: str, *, path: str = "", field: str = "") -> dict[str, Any]:
    return {"code": code, "path": path, "field": field, "message": message, "severity": "error"}


def _extract_prepare_skill_plan_paths(blueprint_text: str) -> list[str]:
    paths: list[str] = []
    for match in re.finditer(r"(?im)^\s*-\s*path\s*:\s*`?([^`\n]+?)`?\s*$", blueprint_text or ""):
        path = _normalize_skill_path(match.group(1).strip().strip("'\""))
        if path and path not in paths:
            paths.append(path)
    return paths


def _preflight_prepare_blueprint_text(blueprint_text: str) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    text = str(blueprint_text or "")
    plan_paths = _extract_prepare_skill_plan_paths(text)
    plan_path_set = set(plan_paths)
    dynamic_re = re.compile(r"[<>{}\*]|\$\{|\[[^\]]*(?:name|path|file|ext|文件|名称)[^\]]*\]", re.I)
    runtime_dir_re = re.compile(r"^(?:outputs?|OUTPUT_DIR|generated|build|dist|tmp)(?:/|$)", re.I)

    for path in plan_paths:
        normalized = _normalize_skill_path(path)
        if normalized in {"assets", "assets/"}:
            issues.append(_prepare_protocol_issue("invalid_asset_directory_path", "assets 不能声明为目录路径。", path=path))
        if normalized.startswith("assets/") and dynamic_re.search(normalized):
            issues.append(_prepare_protocol_issue("invalid_asset_placeholder_path", "assets path 不能包含占位符或通配符。", path=path))
        if not normalized or normalized.endswith("/") or _is_directory_like_skill_path(normalized) or dynamic_re.search(normalized):
            issues.append(_prepare_protocol_issue("invalid_dynamic_or_directory_path", "SkillPlan path 必须是具体文件路径。", path=path))
        if runtime_dir_re.search(normalized):
            issues.append(_prepare_protocol_issue("runtime_artifact_path_in_skill_plan", "运行时产物目录不能出现在 SkillPlan path。", path=path))
        if normalized.startswith("assets/"):
            block_match = re.search(rf"(?ims)^\s*-\s*path\s*:\s*`?{re.escape(path)}`?\s*$([\s\S]*?)(?=^\s*-\s*path\s*:|\Z)", text)
            block = block_match.group(1) if block_match else ""
            if not re.search(r"(?im)^\s*source\s*:\s*(user_upload|bundled)\s*$", block):
                issues.append(_prepare_protocol_issue("asset_missing_source", "assets path 必须声明 source=user_upload 或 source=bundled。", path=path, field="source"))
            if re.search(r"运行时|每次上传|用户输入|runtime\s+input|粘贴|待用户上传", block, re.I):
                issues.append(_prepare_protocol_issue("runtime_input_described_as_asset", "运行时用户输入文件不能描述为 Creator assets。", path=path))

    for dep_match in re.finditer(r"(?im)^\s*dependencies\s*:\s*\[?([^\]\n]*)\]?", text):
        deps = dep_match.group(1)
        if re.search(r"outputs?/|OUTPUT_DIR|generated/|build/|dist/|tmp/|[<>{}\*]", deps, re.I):
            issues.append(_prepare_protocol_issue("invalid_runtime_dependency", "dependencies 只能写运行前静态依赖，不能包含运行时产物、动态文件名或输出目录。", field="dependencies"))

    declared_paths = set(_extract_declared_skill_paths(text))
    concrete_declared = {
        p for p in declared_paths
        if p.startswith(("scripts/", "references/", "assets/")) and _has_file_extension(p)
    }
    for path in sorted(concrete_declared - plan_path_set):
        issues.append(_prepare_protocol_issue("directory_or_text_path_missing_from_skill_plan", "蓝图中出现的具体文件必须在 SkillPlan path 中声明。", path=path))
    return issues


async def _repair_prepare_blueprint_protocol(
    *,
    request: PreparePlanRequest,
    blueprint_text: str,
    protocol_errors: list[dict[str, Any]],
) -> str:
    repaired = str(blueprint_text or "")
    seen = {repaired}
    for _ in range(2):
        prompt = load_kernel_creator_for_phase("prepare_plan") + """
你只修复 internal_blueprint_text 的 Creator 硬协议问题。只输出修复后的蓝图正文，不要 JSON，不要 Markdown 解释。
修复要求：
- 不要把运行时用户输入文件写入 assets；
- 不要输出 assets/、assets/<name.ext>、assets/* 或动态 assets path；
- 如果不需要静态素材，删除 assets 文件计划；
- 目录结构不要列具体文件名；
- 目录结构与 SkillPlan path 必须一致；
- dependencies 只能写运行前静态依赖；
- 运行时产物只能出现在脚本 outputs/stdout JSON/file_outputs。
"""
        route = route_model("creator_prepare_plan", requested_model=request.model, reason="creator prepare blueprint protocol repair")
        text = await complete_chat_once([
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps({"blueprint_text": repaired, "protocol_errors": protocol_errors}, ensure_ascii=False, default=str)},
        ], route.model)
        candidate = str(text or "").strip()
        if candidate.startswith("```"):
            candidate = re.sub(r"^```(?:markdown|md)?\s*", "", candidate, flags=re.IGNORECASE).strip()
            candidate = re.sub(r"\s*```$", "", candidate).strip()
        if not candidate or candidate in seen:
            break
        repaired = candidate
        seen.add(repaired)
        protocol_errors = _preflight_prepare_blueprint_text(repaired)
        if not protocol_errors:
            break
    return repaired


def _blocked_prepare_response(request: PreparePlanRequest, summary: PreparePlanReviewSummary, *, skill_name: str, issues: list[dict[str, Any]]) -> PreparePlanResponse:
    return PreparePlanResponse(
        status="blocked",
        prepare_stage="creation_points_confirmation",
        review_summary=summary,
        skill_name=skill_name or request.skill_name or "",
        creation_blockers=["创建计划暂时无法通过平台协议预检，请补充更明确的输入、输出、资源边界或文件计划后重试。"],
        warnings=[{"severity": "user_warning", "code": "prepare_plan_protocol_blocked", "source": "prepare_plan", "path": "", "field": "", "message": "Creator plan preparation is blocked by protocol validation.", "issues": issues[:5]}],
    )


async def _generate_internal_blueprint_or_questions(request: PreparePlanRequest) -> dict[str, Any]:
    existing_context = _read_prepare_existing_skill_context(request.skill_name) if request.mode == "revise" else {}
    system_prompt = load_kernel_creator_for_phase("prepare_plan") + """

你现在服务 /api/creator/prepare-plan。只输出严格 JSON object，不要 Markdown，不要解释文本。

返回格式：
{
  "status": "ready" | "needs_clarification" | "blocked",
  "clarifying_questions": ["只包含一个真正必要且带选项的问题"],
  "review_summary": {
    "goal": "",
    "input": "",
    "output": "",
    "workflow": [],
    "files_to_create_or_update": [],
    "assets_to_upload": [],
    "risks": [],
    "changes": []
  },
  "internal_blueprint_text": "当 status=ready 时填写完整 Skill 架构蓝图",
  "skill_name": "可选",
  "blockers": []
}

约束：
- status=ready 之前必须先判断需求成熟度；不要直接把粗需求扩写成 ready 蓝图。
- 信息足够时 status=ready，并生成完整 internal_blueprint_text。
- 信息不足且 clarification_rounds 未达到上限时 status=needs_clarification，clarifying_questions 必须只包含 1 个问题，且只能问当前最阻塞生成/E2E 的问题；问题必须带 2-4 个选项。
- 每轮 needs_clarification 只能问一个问题；下一个问题必须基于 conversation_history 和 human_feedback 中上一轮的回答继续判断。
- 如果 clarification_rounds 达到上限，不得继续返回业务澄清问题；达到上限后必须归纳创建要点，并询问用户是否补充。
- 创建要点必须体现蓝图和责任图谱合同需要落实的功能，不要输出风险项。
- 用户确认无补充后，不得继续 needs_clarification，必须生成 internal_blueprint_text。
- 用户补充后，重新归纳要点；达到补充上限后必须生成 internal_blueprint_text。
- “是否还有其他补充内容”必须作为所有必要问题解决后的单独一轮问题；不要和业务问题放在同一轮。
- 如果用户选择“有，我补充说明”，不得 ready，应等待用户补充；如果用户选择“没有，按上面的选择继续”，且其他阻塞点已解决，才可以 ready。
- 无法继续且用户必须先提供外部素材/权限/上下文时 status=blocked，并说明 blockers。
- 不要询问使用平台、使用频率、质量/速度优先级、是否拆模块等非阻塞问题。
- assets/** 只能声明 user_upload 或 bundled；不要把运行时用户输入文件或运行时产物放入 assets。
- 不要生成 assets/、assets/<name.ext>、assets/* 或动态 assets path；目录结构不要列具体文件名，具体文件只在 SkillPlan 中声明。
"""
    payload = {
        "mode": request.mode,
        "skill_name": request.skill_name,
        "user_request": request.user_request,
        "conversation_history": request.conversation_history,
        "uploaded_files": request.uploaded_files,
        "previous_blueprint_text": request.previous_blueprint_text,
        "human_feedback": request.human_feedback,
        "existing_skill_context": existing_context,
        "clarification_rounds": _count_prepare_business_clarification_rounds(request),
        "max_clarification_rounds": MAX_PREPARE_BUSINESS_CLARIFICATION_ROUNDS,
        "clarification_limit_reached": _prepare_business_clarification_limit_reached(request),
        "supplement_rounds": _count_prepare_supplement_rounds(request),
        "max_supplement_rounds": MAX_PREPARE_SUPPLEMENT_ROUNDS,
        "user_confirmed_no_more_supplement": _prepare_user_confirmed_no_more_supplement(request),
    }
    route = route_model("creator_prepare_plan", requested_model=request.model, reason="creator prepare plan")
    text = await complete_chat_once([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
    ], route.model)
    return _parse_prepare_plan_json(text)


async def _prepare_summarize_confirmed_requirements(
    *,
    request: PreparePlanRequest,
    prepared: dict[str, Any] | None = None,
) -> PreparePlanReviewSummary:
    prepared = prepared or {}
    base = _coerce_prepare_summary(prepared.get("review_summary"))
    prompt = load_kernel_creator_for_phase("prepare_plan") + """
你只归纳 Creator 创建要点，不生成蓝图，不提风险。只输出严格 JSON object，字段为 goal/input/output/workflow/files_to_create_or_update/assets_to_upload/risks/changes。
创建要点必须体现后续 internal_blueprint_text、SkillPlan 和 requirement graph 需要落实的功能合同：目标功能、运行时输入、运行时输出、处理流程、文件职责、脚本 inputs/outputs/stdout JSON 字段、资源边界、默认决策。
risks 必须输出空数组。assets_to_upload 只包含 Creator 静态 assets；运行时输入文件不得放入。未明确但必须落地的部分使用默认推荐项：最小可用、可执行可验证、JSON + 可读 Markdown、最小文件集、不确定不创建 assets path。
"""
    payload = {
        "user_request": request.user_request,
        "conversation_history": request.conversation_history,
        "human_feedback": request.human_feedback,
        "uploaded_files": request.uploaded_files,
        "model_summary": base.model_dump(mode="json"),
    }
    try:
        route = route_model("creator_prepare_plan", requested_model=request.model, reason="creator prepare requirements summary")
        text = await complete_chat_once([
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
        ], route.model)
        summary = _coerce_prepare_summary(_parse_prepare_plan_json(text))
    except Exception:
        summary = base
    if not summary.goal:
        summary.goal = str(request.user_request or "创建一个可执行 Skill").strip()[:500]
    if not summary.input:
        summary.input = "运行时由用户提供文本、文件、参数或素材；未明确的文件默认作为运行时输入。"
    if not summary.output:
        summary.output = "默认返回结构化 JSON + 可读 Markdown；如生成文件则返回 OUTPUT_DIR 文件路径。"
    if not summary.workflow:
        summary.workflow = ["读取运行时输入", "按 Skill 目标处理并验证关键字段", "返回 stdout JSON 和可读结果"]
    if not summary.files_to_create_or_update:
        summary.files_to_create_or_update = ["SKILL.md"]
    summary.risks = []
    if not summary.changes:
        summary.changes = ["默认决策：优先最小可用、可执行可验证；不确定的素材按运行时输入处理，不创建 assets path。"]
    summary.assets_to_upload = [p for p in summary.assets_to_upload if str(p).strip().startswith("assets/")]
    return summary


async def _generate_internal_blueprint_from_confirmed_summary(
    *,
    request: PreparePlanRequest,
    summary: PreparePlanReviewSummary,
) -> dict[str, Any]:
    prompt = load_kernel_creator_for_phase("prepare_plan") + """
基于已确认的创建要点、conversation_history、human_feedback 生成 internal_blueprint_text。只输出严格 JSON object：{"status":"ready","internal_blueprint_text":"...","review_summary":{...},"skill_name":"..."}。
不得继续返回 needs_clarification，不得询问问题。未明确的非关键偏好使用默认推荐项。运行时输入默认不作为 Creator assets。输出必须满足 analyze_blueprint(strict=True) 可解析，包含基本信息、I/O 契约、目录结构、工作流逻辑、SkillPlan / 文件职责计划、宿主执行方式、资源清单。
"""
    payload = {
        "confirmed_summary": summary.model_dump(mode="json"),
        "user_request": request.user_request,
        "conversation_history": request.conversation_history,
        "human_feedback": request.human_feedback,
        "uploaded_files": request.uploaded_files,
    }
    route = route_model("creator_prepare_plan", requested_model=request.model, reason="creator confirmed summary to blueprint")
    text = await complete_chat_once([
        {"role": "system", "content": prompt},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
    ], route.model)
    data = _parse_prepare_plan_json(text)
    data["status"] = "ready"
    data.setdefault("review_summary", summary.model_dump(mode="json"))
    return data

def _tool_names_from_entry_contract(entry: Any) -> list[str]:
    data = entry if isinstance(entry, dict) else getattr(entry, "__dict__", {})
    names: list[str] = []
    # required_capabilities are capability hints from the skill plan, not proof
    # that a concrete registered tool exists or is authorized. Only explicit
    # selected_tools / required_tool_slots may become hard tool requirements.
    raw_selected = data.get("selected_tools") if isinstance(data, dict) else None
    if isinstance(raw_selected, list):
        names.extend(str(item).strip() for item in raw_selected if str(item).strip())
    for slot in (data.get("required_tool_slots") if isinstance(data, dict) else []) or []:
        slot_data = slot if isinstance(slot, dict) else getattr(slot, "__dict__", {})
        for key in ("tool_id", "capability", "slot_id"):
            value = str(slot_data.get(key) or "").strip() if isinstance(slot_data, dict) else ""
            if value:
                names.append(value)
    return list(dict.fromkeys(names))


def _creator_tool_readiness_blockers(entry: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    requirements: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    for capability_name in _tool_names_from_entry_contract(entry):
        cap = get_tool_capability(capability_name)
        if not cap:
            blocker = {
                "type": "tool_not_ready",
                "capability": capability_name,
                "tool_name": capability_name,
                "status": "missing_tool",
                "blocking": True,
                "message": f"工具能力 {capability_name} 未注册，Creator 不能编造工具调用。",
                "details": {},
            }
            blockers.append(blocker)
            requirements.append(blocker)
            continue
        status = tool_status(cap)
        missing_runtime_helpers = status.get("missing_runtime_helpers") or []
        missing_dependencies = status.get("missing_dependencies") or []
        status_code = "ready"
        if not status.get("enabled"):
            status_code = "disabled"
        elif not cap.allow_creator_use:
            status_code = "forbidden_for_creator"
        elif not status.get("configured"):
            status_code = "not_authorized"
        elif missing_runtime_helpers or missing_dependencies:
            status_code = "not_validated"
        requirement = {
            "type": "tool_requirement",
            "capability": capability_name,
            "tool_name": cap.name,
            "status": status_code,
            "blocking": status_code != "ready",
            "message": f"工具能力 {capability_name} 已就绪。" if status_code == "ready" else f"工具能力 {capability_name} 未就绪：{status_code}。",
            "details": status,
        }
        requirements.append(requirement)
        if status_code != "ready":
            blockers.append({**requirement, "type": "tool_not_ready", "blocking": True})
    return requirements, blockers


def _extract_script_tool_calls(content: str) -> list[str]:
    text = str(content or "")
    names: list[str] = []
    call_pattern = r"\b(?:run_registered_tool|run_tool|call_tool)\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"
    names.extend(re.findall(call_pattern, text))
    import_pattern = r"^\s*from\s+backend\.services\.runtime_tools\.custom_tools\.([A-Za-z_][A-Za-z0-9_]*)\s+import\b"
    names.extend(re.findall(import_pattern, text, flags=re.MULTILINE))
    return list(dict.fromkeys(names))


def _script_tool_boundary_violations(content: str, allowed_tools: list[str]) -> list[dict[str, Any]]:
    allowed = {str(name) for name in allowed_tools or []}
    unexpected = [name for name in _extract_script_tool_calls(content) if name not in allowed]
    if not unexpected:
        return []
    return [{
        "id": "script.hallucinated_tool_call",
        "layer": "script_tool_boundary",
        "message": "脚本调用了未注册/未允许的工具：" + ", ".join(unexpected),
        "expected": "只能调用 selected_tools/allowed_tools 中的工具；缺工具时返回 creation_blocker，不能编造工具。",
    }]


async def _extract_requirement_graph_with_validator(
    *,
    blueprint_text: str,
    files_out: list[FileSpecOut],
    requested_model: str | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> RequirementGraph:
    """Build deterministic responsibility graph and optionally apply compact model patches."""
    graph = validate_requirement_graph_schema(build_default_requirement_graph(files_out), files_out)
    route = route_model(VALIDATOR_TASK, requested_model=requested_model, reason="creator responsibility graph patch")
    file_payload = [file_spec.model_dump(mode="json", exclude={"requirements"}) for file_spec in files_out]
    messages = [
        {
            "role": "system",
            "content": (
                "你是 Creator requirement graph patcher，只输出严格 JSON object。\n"
                "后端已经根据 file_plan/contracts 生成 deterministic requirement graph；你只能返回 compact patches。\n"
                "patch 只能补充 must_do、must_not_do、depends_on；不得输出 purpose、constraints、evidence_policy、graph_quality、non_requirements、expected、minimal_edit。\n"
                "platform_io_contract 是 deterministic and immutable，只读参考；不得输出、修改或 patch platform_io_contract。\n"
                "requirement graph patcher 不负责字段级强对齐；inputs/outputs 是共同推荐字段；platform_io_contract 只读。不得把平台字段词表复制成内部业务字段，不得输出字段映射硬规则。\n"
                "不得 patch platform_input_node/platform_output_node/platform_io_contract，不得输出 dataflow_edges 作为 hard contract。\n"
                "purpose 已由 workflow_allocation 或原始文件计划确定；requirement_graph 阶段不得修改 purpose，不得重新划分脚本职责，不得改写 final inputs / final outputs。\n"
                "must_do 只补关键职责缺口，保持短句、少量条目。\n"
                "返回格式：{\"patches\":[{\"target_file\":\"scripts/x.py\",\"must_do\":[],\"must_not_do\":[],\"depends_on\":[]}]}。"
                "不得 patch platform_input_node 或 platform_output_node；平台边界节点由系统确定性注入并覆盖模型输出。"
            ),
        },
        {
            "role": "user",
            "content": (
                "blueprint_text:\n" + (blueprint_text or "")[:12000] + "\n\n"
                "file_plan_and_contracts:\n" + json.dumps(file_payload, ensure_ascii=False, default=str)[:20000] + "\n\n"
                "platform_io_contract (read-only, deterministic, immutable):\n" + platform_io_contract_prompt_text() + "\n\n"
                "deterministic_responsibility_graph:\n" + graph.model_dump_json()[:12000]
            ),
        },
    ]
    try:
        text = await complete_chat_once(messages, route.model)
        data = parse_requirement_graph_result(text)
        if isinstance(data, dict) and "patches" not in data and "requirements" in data:
            raise RequirementGraphValidationError(
                "Requirement graph patch JSON must use patches format.",
                code="validator_incomplete",
            )
        patches = data.get("patches", []) if isinstance(data, dict) else []
        if not isinstance(patches, list):
            raise RequirementGraphValidationError("Responsibility graph patch JSON must contain patches list.", code="validator_incomplete")
        by_file = {item.target_file: item for item in graph.requirements}
        allowed = {"target_file", "must_do", "must_not_do", "depends_on"}
        ignored_purpose_targets: list[str] = []
        requirement_targets: list[str] = []
        for idx, patch in enumerate(patches):
            if not isinstance(patch, dict):
                raise RequirementGraphValidationError("Responsibility graph patch item must be object.", code="validator_incomplete", details={"index": idx})
            unknown_fields = set(patch) - allowed
            if "purpose" in unknown_fields:
                target_for_log = str(patch.get("target_file") or "").strip()
                if target_for_log:
                    ignored_purpose_targets.append(target_for_log)
                unknown_fields.remove("purpose")
            if unknown_fields:
                raise RequirementGraphValidationError("Requirement graph patch contains unsupported fields.", code="validator_incomplete", details={"index": idx, "fields": sorted(unknown_fields)})
            target = str(patch.get("target_file") or "").strip()
            item = by_file.get(target)
            if not item:
                continue
            patched_requirement = False
            for field_name in ("must_do", "must_not_do", "depends_on"):
                values = RequirementItem._coerce_string_list(patch.get(field_name))
                if values:
                    existing = list(getattr(item, field_name))
                    for value in values:
                        if value not in existing:
                            existing.append(value)
                            patched_requirement = True
                    setattr(item, field_name, existing)
            if patched_requirement:
                requirement_targets.append(target)
        graph.requirement_graph_source = "deterministic+patch"
        logger.info("[Creator][requirement_graph_patch][result] %s", json.dumps({
            "event": "requirement_graph_patch_result",
            "patch_count": len(patches),
            "requirement_targets": sorted(set(requirement_targets)),
            "ignored_purpose_targets": sorted(set(ignored_purpose_targets)),
        }, ensure_ascii=False, default=str))
        return graph
    except Exception as exc:
        if warnings is not None:
            code = getattr(exc, "code", "validator_error")
            warnings.append({
                "severity": "validator_warning",
                "code": str(code),
                "source": "responsibility_graph",
                "path": "",
                "field": "requirement_graph",
                "message": f"Responsibility graph patch model failed; using deterministic graph: {exc}",
            })
        logger.info("[Creator][requirement_graph_patch][failed] %s", json.dumps({
            "event": "requirement_graph_patch_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False, default=str))
        return graph


def _persist_requirement_graph(skill_name: str, graph: RequirementGraph) -> None:
    metadata_dir = settings.skills_path / _validate_skill_name(skill_name) / ".creator"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    (metadata_dir / "requirement_graph.json").write_text(
        graph.model_dump_json(indent=2),
        encoding="utf-8",
    )


def _persist_workflow_allocation_summary(skill_name: str, summary: str) -> None:
    metadata_dir = settings.skills_path / _validate_skill_name(skill_name) / ".creator"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    (metadata_dir / "workflow_allocation_summary.txt").write_text(str(summary or "").strip(), encoding="utf-8")


def _load_workflow_allocation_summary(skill_name: str) -> str:
    path = settings.skills_path / _validate_skill_name(skill_name) / ".creator" / "workflow_allocation_summary.txt"
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _load_persisted_requirement_graph(skill_name: str) -> RequirementGraph | None:
    path = settings.skills_path / _validate_skill_name(skill_name) / ".creator" / "requirement_graph.json"
    if not path.is_file():
        return None
    return normalize_requirement_graph(parse_requirement_graph_result(path.read_text(encoding="utf-8")))

async def _allocate_workflow_script_responsibilities(
    *,
    blueprint_text: str,
    files_out: list[FileSpecOut],
    requested_model: str | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> tuple[str, set[str], bool]:
    """Return (summary, applied patch targets, allocation resolved flag) after patching script contracts."""
    targets = [
        file_spec
        for file_spec in files_out
        if file_spec.path.startswith("scripts/") and file_spec.required
    ]
    if not targets:
        return "", set(), True
    route = route_model(VALIDATOR_TASK, requested_model=requested_model, reason="creator workflow responsibility allocation")
    all_nodes = [
        {
            "path": item.path,
            "purpose": item.purpose,
            "role": item.role,
            "required": item.required,
            "inputs": item.inputs,
            "outputs": item.outputs,
            "dependencies": item.dependencies,
        }
        for item in files_out
        if item.path == "SKILL.md" or item.path.startswith(("scripts/", "references/", "assets/"))
    ]
    payload = [
        {
            "path": item.path,
            "purpose": item.purpose,
            "role": item.role,
            "inputs": item.inputs,
            "outputs": item.outputs,
            "dependencies": item.dependencies,
        }
        for item in targets
    ]
    messages = [
        {"role": "system", "content": (
            "你是 Creator workflow executable responsibility allocator，只输出严格 JSON object。\n"
            "先在内部区分 executable workflow graph 与 reference/context graph（不要输出复杂结构）。\n"
            "executable workflow graph 只能包含：platform guaranteed input envelope、required scripts/*.py、scripts stdout、scripts artifacts、final artifact；只有这些节点/边可以承担运行时 dataflow。\n"
            "输出的 inputs/outputs 是 recommended shared vocabulary，用于 SKILL.md 和 script 生成时尽量采用一致字段；它们不是 hard validation schema，不要求与平台 IO 字段名相等，也不要求第一轮图谱阶段完全闭合。平台 IO 只作为来源/出口层说明。\n"
            "SKILL.md、references/*.md、assets/** 只能作为 reference/context graph 中的说明、规范或资源上下文，不能作为可执行 dataflow 节点：SKILL.md 不承担字段转换、循环、聚合、排序、映射或产物生成；references/*.md 不产生 stdout 字段，不补齐 producer，不补齐集合结果；assets/** 只是上传或静态资源输入，不主动生成中间结果。\n"
            "逐边判断：平台输入如何进入第一步；每个 required script 消费什么上游 stdout/artifact 或 platform runtime 输入；当前脚本完整交付什么；下游真正需要什么；是否存在局部自洽但全局断链；是否存在隐式循环、隐式聚合、集合到单项再到集合的问题；是否需要最小联动调整相邻上下游脚本的 purpose/inputs/outputs。\n"
            "责任分配必须先从全局可执行合同推导，再落到单个脚本。全局合同由最终产物目标、下游消费者 inputs、上游已能交付的 outputs、平台执行能力边界、可执行脚本链路闭环共同决定；原始 SkillPlanEntry inputs/outputs 只作为局部能力提示，不能覆盖全局闭环。\n"
            "责任优先级从高到低：全局可执行闭环 > 最终产物目标 > 下游消费者真实输入需求 > 平台执行能力边界 > scripts stdout/artifact 串接 > allocation 后的 final inputs/final outputs > 当前脚本 purpose > 原始 blueprint/原始单脚本计划/references/SKILL.md 描述。低优先级信息与高优先级合同冲突时，必须以高优先级合同为准。\n"
            "职责分配禁止依据 role 名称、文件名或固定业务词表；必须依据当前脚本的上游输入、下游消费者、声明能力与禁止能力、可观察信息、实际可交付输出、全局最终产物需要的中间结果。\n"
            "任何运行链路闭环、字段转换、子字段提取、集合遍历、聚合交付、顺序映射，都必须落到 scripts/*.py 或平台真实 runtime 能力中；不得用 SKILL.md 的自然语言、reference 的规则说明、assets 的存在来解释缺失的 producer、loop、aggregation 或 field mapping。\n"
            "当前平台没有显式可执行 loop/map/foreach 节点。如果蓝图语义需要逐项处理、批量处理、一一对应、多输入单元生成多输出单元、聚合交付或顺序映射，必须把该执行责任落到某个脚本内部；不得只在 purpose 或 SKILL.md 中写逐项调用、每个生成一个、依次处理、保持对应，却没有任何脚本承担真实循环/聚合。\n"
            "如果原始单脚本合同表达‘单项输入 -> 单项输出’，但下游需要集合/聚合结果且平台没有 loop/map/foreach，则该脚本最终责任必须提升为‘集合/整体输入 -> 集合/聚合输出’；单项处理只能作为脚本内部循环体，不能作为 workflow 级 final inputs/final outputs。\n"
            "如果下游脚本需要消费上游集合元素中的子字段（例如从某个 structured collection item 中读取 description/text/scene/metadata），这不是自动存在的 workflow 顶层变量。除非上游脚本明确把该字段作为 stdout 顶层输出，否则下游不能直接把它作为 input；若平台没有显式 loop/map/foreach 节点，遍历集合并提取子字段的责任必须落到某个脚本内部。\n"
            "workflow_allocation_summary、patch purpose、patch inputs、patch outputs 必须描述同一个全局责任合同；summary 不得继续描述被替换掉的旧脚本级 inputs/outputs。purpose 的来源必须与 patch inputs 字段名和粒度一致，purpose 的交付必须与 patch outputs 字段名和粒度一致，不得出现 outputs 与 purpose 中单复数/类型/字段名模糊或冲突。\n"
            "patch 默认只改当前脚本 purpose/final inputs/final outputs；如果当前职责调整影响直接上游或直接下游，可以同步 patch 相邻 required scripts 的 purpose/inputs/outputs，做最小联动。不要新增文件，不硬编码业务字段，不按字段名、文件名、role、单复数机械判断。\n"
            "workflow allocation patch 中的 inputs/outputs 表示 final inputs/final outputs；如果 patch 提供 inputs/outputs，默认替换原始 inputs/outputs，不再默认 append。只有明确设置 replace_inputs=false 或 replace_outputs=false 时才按 legacy append 兼容。需要把旧单项接口升级为整体/集合接口时，应提供 inputs/outputs 并保持默认替换，避免错误旧字段残留。\n"
            "只输出 compact patches；不要新增复杂结构。purpose 格式：来源：... | 动作：... | 交付：... | 约束：...\\n说明：...\n"
            "返回：{\"workflow_allocation_summary\":\"...\",\"patches\":[{\"target_file\":\"scripts/x.py\",\"purpose\":\"...\",\"inputs\":[],\"outputs\":[],\"replace_inputs\":true,\"replace_outputs\":true}]}"
        )},
        {"role": "user", "content": (
            "blueprint_text:\n" + (blueprint_text or "")[:14000] + "\n\n"
            "graph_nodes_from_file_plan:\n" + json.dumps(all_nodes, ensure_ascii=False, default=str)[:20000] + "\n\n"
            "required_scripts_to_patch:\n" + json.dumps(payload, ensure_ascii=False, default=str)[:16000]
        )},
    ]
    logger.info("[Creator][workflow_allocation][start] %s", json.dumps({
        "event": "workflow_allocation_start",
        "script_count": len(targets),
        "scripts": [item.path for item in targets],
    }, ensure_ascii=False, default=str))
    try:
        conflict_feedback = ""
        data: dict[str, Any] = {}
        patches: list[Any] = []
        fatal_conflicts: list[dict[str, Any]] = []
        soft_conflicts: list[dict[str, Any]] = []
        summary = ""
        for attempt in range(2):
            attempt_messages = list(messages)
            if conflict_feedback:
                attempt_messages.append({
                    "role": "user",
                    "content": (
                        "上一次 workflow allocation JSON 存在 final contract 一致性冲突；"
                        "请只修正 JSON，不新增复杂结构。冲突如下：\n"
                        f"{conflict_feedback[:6000]}\n\n"
                        "修正要求：workflow_allocation_summary、patch purpose、patch inputs、patch outputs "
                        "必须描述同一个全局责任合同；inputs/outputs 是 final inputs/final outputs。"
                    ),
                })
            data = _parse_validator_json_object(await complete_chat_once(attempt_messages, route.model))
            patches_candidate = data.get("patches") if isinstance(data, dict) else None
            summary = str(data.get("workflow_allocation_summary") or "").strip() if isinstance(data, dict) else ""
            if not isinstance(patches_candidate, list):
                raise ValueError("missing patches list")
            conflict_report = _workflow_allocation_patch_conflicts(patches_candidate)
            fatal_conflicts = list(conflict_report.get("fatal", []))
            soft_conflicts = list(conflict_report.get("soft", []))
            if not fatal_conflicts:
                patches = patches_candidate
                break
            conflict_feedback = json.dumps(fatal_conflicts, ensure_ascii=False, default=str)
            logger.info("[Creator][workflow_allocation][contract_conflict] %s", json.dumps({
                "event": "workflow_allocation_contract_conflict",
                "attempt": attempt + 1,
                "fatal_conflicts": fatal_conflicts,
                "soft_conflicts": soft_conflicts,
            }, ensure_ascii=False, default=str))
        else:
            patches = [
                patch for patch in (patches_candidate if isinstance(patches_candidate, list) else [])
                if isinstance(patch, dict)
                and str(patch.get("target_file") or "").strip()
                not in {str(item.get("target_file") or "").strip() for item in fatal_conflicts if isinstance(item, dict)}
            ]
        allocation_resolved = not fatal_conflicts
        if soft_conflicts:
            logger.info("[Creator][workflow_allocation][soft_conflict] %s", json.dumps({
                "event": "workflow_allocation_soft_conflict",
                "soft_conflicts": soft_conflicts,
                "allocation_resolved": allocation_resolved,
            }, ensure_ascii=False, default=str))
        if soft_conflicts and warnings is not None:
            warnings.append({
                "severity": "validator_warning",
                "code": "workflow_allocation_soft_conflict",
                "source": "analyze_blueprint",
                "path": "",
                "field": "inputs",
                "message": (
                    "Workflow allocation had soft input wording conflicts; "
                    "safe patches were kept because final outputs were consistent."
                ),
                "details": soft_conflicts,
            })
        if fatal_conflicts and warnings is not None:
            warnings.append({
                "severity": "validator_warning",
                "code": "workflow_allocation_unresolved",
                "source": "analyze_blueprint",
                "path": "",
                "field": "outputs",
                "message": (
                    "Workflow allocation still has fatal final-output contract conflicts after retry; "
                    "only patches without fatal conflicts were applied and global contract is unresolved."
                ),
                "details": fatal_conflicts,
            })
        if not isinstance(patches, list):
            raise ValueError("missing patches list")
        by_path = {item.path: item for item in targets}
        applied: list[str] = []
        for patch in patches:
            if not isinstance(patch, dict):
                continue
            target = str(patch.get("target_file") or "").strip()
            purpose = str(patch.get("purpose") or "").strip()
            if target in by_path and purpose:
                script = by_path[target]
                script.purpose = purpose
                for field_name, replace_flag in (("inputs", "replace_inputs"), ("outputs", "replace_outputs")):
                    cleaned = _clean_allocation_io_values(patch.get(field_name))
                    if cleaned is not None:
                        if patch.get(replace_flag) is not False:
                            setattr(script, field_name, cleaned)
                        else:
                            existing = list(getattr(script, field_name) or [])
                            for value in cleaned:
                                if value not in existing:
                                    existing.append(value)
                            setattr(script, field_name, existing)
                applied.append(target)
        logger.info("[Creator][workflow_allocation][result] %s", json.dumps({
            "event": "workflow_allocation_result",
            "applied_targets": applied,
            "summary": summary,
        }, ensure_ascii=False, default=str))
        return summary, set(applied), allocation_resolved
    except Exception as exc:
        logger.info("[Creator][workflow_allocation][failed] %s", json.dumps({
            "event": "workflow_allocation_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False, default=str))
        if warnings is not None:
            warnings.append({
                "severity": "validator_warning",
                "code": "workflow_allocation_failed",
                "source": "analyze_blueprint",
                "path": "",
                "field": "purpose",
                "message": f"Workflow responsibility allocation failed; keeping parsed purposes: {exc}",
            })
        return "", set(), False

def _looks_like_semantic_short_contract(text: str) -> bool:
    value = str(text or "")
    return all(marker in value for marker in ("来源：", "动作：", "交付：", "约束：", "说明："))


def _clean_allocation_io_values(values: Any) -> list[str] | None:
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        return None
    cleaned: list[str] = []
    for value in values:
        item = value.strip()
        if item and item not in cleaned:
            cleaned.append(item)
    return cleaned


def _workflow_allocation_patch_conflicts(patches: list[Any]) -> dict[str, list[dict[str, Any]]]:
    """Lightweight final-contract consistency check for allocation patches.

    This deliberately stays local to the returned compact patches: it does not
    build or validate a graph.  It only verifies that final inputs/outputs and
    purpose text name the same contract before purpose normalization can run.
    """
    fatal: list[dict[str, Any]] = []
    soft: list[dict[str, Any]] = []
    for patch in patches:
        if not isinstance(patch, dict):
            continue
        target = str(patch.get("target_file") or "").strip()
        purpose = str(patch.get("purpose") or "").strip()
        outputs = _clean_allocation_io_values(patch.get("outputs"))
        if not target or not purpose:
            if target and outputs:
                fatal.append({
                    "target_file": target,
                    "field": "purpose",
                    "message": "allocation patch with final outputs must include purpose for the same contract",
                })
            continue
        purpose_lower = purpose.lower()
        inputs = _clean_allocation_io_values(patch.get("inputs"))
        if outputs is not None:
            if not outputs:
                fatal.append({
                    "target_file": target,
                    "field": "outputs",
                    "message": "allocation patch outputs is present but empty; final outputs cannot be determined",
                })
            missing_outputs = [value for value in outputs if value.lower() not in purpose_lower]
            if missing_outputs:
                fatal.append({
                    "target_file": target,
                    "field": "outputs",
                    "missing_from_purpose": missing_outputs,
                    "message": "final outputs must appear in purpose delivery text",
                })
        if inputs:
            missing_inputs = [value for value in inputs if value.lower() not in purpose_lower]
            if missing_inputs:
                soft.append({
                    "target_file": target,
                    "field": "inputs",
                    "missing_from_purpose": missing_inputs,
                    "message": (
                        "final inputs are not spelled out in purpose; this is advisory "
                        "because input sources may be described naturally"
                    ),
                })
    return {"fatal": fatal, "soft": soft}

async def _normalize_script_purpose_short_contracts(
    *,
    blueprint_text: str,
    files_out: list[FileSpecOut],
    workflow_allocation_summary: str = "",
    skip_targets: set[str] | None = None,
    requested_model: str | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> None:
    skip_targets = set(skip_targets or set())
    targets = [
        file_spec
        for file_spec in files_out
        if (
            file_spec.path.startswith("scripts/")
            and file_spec.required
            and file_spec.path not in skip_targets
            and not _looks_like_semantic_short_contract(file_spec.purpose)
        )
    ]
    if not targets:
        return
    logger.info("[Creator][purpose_short_contract][start] %s", json.dumps({
        "event": "purpose_short_contract_start",
        "targets": [item.path for item in targets],
    }, ensure_ascii=False, default=str))
    route = route_model(VALIDATOR_TASK, requested_model=requested_model, reason="creator script purpose short-contract normalization")
    messages = [
        {"role": "system", "content": (
            "你是 Creator 脚本职责短合同压缩器，只输出严格 JSON object。\n"
            "为每个 required script 生成简短 purpose；不要新增结构字段，不复制蓝图长文。\n"
            "格式必须是：来源：... | 动作：... | 交付：... | 约束：...\\n说明：...\n"
            "来源/动作/交付/约束要来自蓝图语义；inputs/outputs 只是接口提示。\n"
            "必须同时读取 workflow_allocation_summary：如果其中已表达完整交付、批量处理、聚合交付、顺序映射、结构映射或集合边界要求，normalizer 不得把 purpose 重新压窄为单项局部职责。"
        )},
        {"role": "user", "content": (
            "blueprint_text:\n" + (blueprint_text or "")[:12000] + "\n\n"
            "workflow_allocation_summary:\n" + (workflow_allocation_summary or "")[:6000] + "\n\n"
            "scripts:\n" + json.dumps([
                {
                    "path": item.path,
                    "purpose": item.purpose,
                    "role": item.role,
                    "inputs": item.inputs,
                    "outputs": item.outputs,
                    "dependencies": item.dependencies,
                }
                for item in targets
            ], ensure_ascii=False, default=str)[:16000] + "\n\n"
            "返回：{\"patches\":[{\"target_file\":\"scripts/x.py\",\"purpose\":\"来源：... | 动作：... | 交付：... | 约束：...\\n说明：...\"}]}"
        )},
    ]
    try:
        data = _parse_validator_json_object(await complete_chat_once(messages, route.model))
        patches = data.get("patches") if isinstance(data, dict) else None
        if not isinstance(patches, list):
            raise ValueError("missing patches list")
        by_path = {item.path: item for item in targets}
        for patch in patches:
            if not isinstance(patch, dict):
                continue
            target = str(patch.get("target_file") or "").strip()
            purpose = str(patch.get("purpose") or "").strip()
            if target in by_path and _looks_like_semantic_short_contract(purpose):
                by_path[target].purpose = purpose
        logger.info("[Creator][purpose_short_contract][result] %s", json.dumps({
            "event": "purpose_short_contract_result",
            "patched_targets": [
                item.path for item in targets if _looks_like_semantic_short_contract(item.purpose)
            ],
        }, ensure_ascii=False, default=str))
    except Exception as exc:
        logger.info("[Creator][purpose_short_contract][failed] %s", json.dumps({
            "event": "purpose_short_contract_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False, default=str))
        if warnings is not None:
            warnings.append({
                "severity": "validator_warning",
                "code": "purpose_short_contract_failed",
                "source": "analyze_blueprint",
                "path": "",
                "field": "purpose",
                "message": f"Script purpose short-contract normalization failed; keeping parsed purposes: {exc}",
            })


@router.post("/prepare-plan", response_model=PreparePlanResponse)
async def prepare_plan(request: PreparePlanRequest):
    prepare_action = str(request.prepare_action or "none")

    if prepare_action == "request_supplement":
        return PreparePlanResponse(
            status="needs_clarification",
            prepare_stage="creation_points_confirmation",
            clarifying_questions=["好的，请补充你的其他要求。"],
            review_summary=PreparePlanReviewSummary(),
            skill_name=request.skill_name or "",
        )

    if prepare_action == "submit_supplement":
        summary = await _prepare_summarize_confirmed_requirements(request=request, prepared={})
        return PreparePlanResponse(
            status="needs_clarification",
            prepare_stage="supplement_confirmation",
            clarifying_questions=["已根据补充内容更新创建要点。是否按这些要点继续？A. 没有其他补充，按这些要点继续 B. 继续补充说明"],
            review_summary=_strip_prepare_summary_risks(summary),
            skill_name=request.skill_name or "",
        )

    try:
        prepared = await _generate_internal_blueprint_or_questions(request)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"prepare-plan 生成失败：{exc}") from exc

    raw_status = str(prepared.get("status") or "").strip()
    status = raw_status if raw_status in {"ready", "needs_clarification", "blocked"} else "needs_clarification"
    summary = _coerce_prepare_summary(prepared.get("review_summary"))
    skill_name = str(prepared.get("skill_name") or request.skill_name or "")

    async def summarize_and_confirm(question: str) -> PreparePlanResponse:
        confirmed = await _prepare_summarize_confirmed_requirements(request=request, prepared=prepared)
        return PreparePlanResponse(status="needs_clarification", prepare_stage="creation_points_confirmation", clarifying_questions=[question], review_summary=_strip_prepare_summary_risks(confirmed), skill_name=skill_name)

    if prepare_action == "confirm" and status != "ready":
        summary = await _prepare_summarize_confirmed_requirements(request=request, prepared=prepared)
        prepared = await _generate_internal_blueprint_from_confirmed_summary(request=request, summary=summary)
        status = "ready"
        skill_name = str(prepared.get("skill_name") or skill_name)
    elif prepare_action == "none" and _prepare_user_confirmed_no_more_supplement(request) and status != "ready":
        summary = await _prepare_summarize_confirmed_requirements(request=request, prepared=prepared)
        prepared = await _generate_internal_blueprint_from_confirmed_summary(request=request, summary=summary)
        status = "ready"
        skill_name = str(prepared.get("skill_name") or skill_name)
    elif prepare_action == "none" and _prepare_user_has_provided_supplement_content(request):
        summary = await _prepare_summarize_confirmed_requirements(request=request, prepared=prepared)
        current_feedback_has_supplement = "补充：" in str(request.human_feedback or "")
        prior_supplement_rounds = max(0, _count_prepare_supplement_rounds(request) - (1 if current_feedback_has_supplement else 0))
        if prior_supplement_rounds >= MAX_PREPARE_SUPPLEMENT_ROUNDS:
            prepared = await _generate_internal_blueprint_from_confirmed_summary(request=request, summary=summary)
            status = "ready"
            skill_name = str(prepared.get("skill_name") or skill_name)
        else:
            return PreparePlanResponse(
                status="needs_clarification",
                prepare_stage="supplement_confirmation",
                clarifying_questions=["已根据补充内容更新创建要点。是否按这些要点继续？A. 没有其他补充，按这些要点继续 B. 继续补充说明"],
                review_summary=_strip_prepare_summary_risks(summary),
                skill_name=skill_name,
            )
    elif prepare_action == "none" and _prepare_feedback_wants_supplement(request):
        return PreparePlanResponse(
            status="needs_clarification",
            prepare_stage="creation_points_confirmation",
            clarifying_questions=["好的，请补充你的其他要求。"],
            review_summary=PreparePlanReviewSummary(),
            skill_name=skill_name,
        )

    if status == "needs_clarification":
        if not _prepare_business_clarification_limit_reached(request):
            return PreparePlanResponse(
                status="needs_clarification",
                prepare_stage="business_clarification",
                clarifying_questions=_normalize_prepare_clarifying_questions(prepared.get("clarifying_questions")),
                review_summary=PreparePlanReviewSummary(),
                skill_name=skill_name,
            )
        return await summarize_and_confirm(_PREPARE_SUPPLEMENT_QUESTION)

    if status == "blocked":
        return await summarize_and_confirm("系统已整理出创建要点，但还需要你确认是否按这些要点继续。A. 按这些要点继续 B. 我补充说明")

    if status == "ready" and prepare_action != "confirm" and not _prepare_user_confirmed_no_more_supplement(request):
        summary = await _prepare_summarize_confirmed_requirements(request=request, prepared=prepared)
        return PreparePlanResponse(
            status="needs_clarification",
            prepare_stage="creation_points_confirmation",
            clarifying_questions=[_PREPARE_SUPPLEMENT_QUESTION],
            review_summary=_strip_prepare_summary_risks(summary),
            skill_name=skill_name,
        )

    blueprint_text = str(prepared.get("internal_blueprint_text") or prepared.get("blueprint_text") or "").strip()
    if not blueprint_text:
        return await summarize_and_confirm("系统已整理出创建要点，但还需要你确认是否按这些要点继续。A. 按这些要点继续 B. 我补充说明")

    protocol_errors = _preflight_prepare_blueprint_text(blueprint_text)
    if protocol_errors:
        try:
            blueprint_text = await _repair_prepare_blueprint_protocol(request=request, blueprint_text=blueprint_text, protocol_errors=protocol_errors)
            protocol_errors = _preflight_prepare_blueprint_text(blueprint_text)
        except Exception:
            pass
    if protocol_errors:
        summary = await _prepare_summarize_confirmed_requirements(request=request, prepared=prepared)
        try:
            prepared = await _generate_internal_blueprint_from_confirmed_summary(request=request, summary=summary)
            blueprint_text = str(prepared.get("internal_blueprint_text") or prepared.get("blueprint_text") or "").strip()
            protocol_errors = _preflight_prepare_blueprint_text(blueprint_text)
        except Exception:
            pass
    if protocol_errors:
        return PreparePlanResponse(status="needs_clarification", prepare_stage="creation_points_confirmation", clarifying_questions=["系统已整理出创建要点，但还需要你确认是否按这些要点继续。A. 按这些要点继续 B. 我补充说明"], review_summary=_strip_prepare_summary_risks(summary), skill_name=skill_name)

    plan = None
    analyze_errors: list[dict[str, Any]] = []
    for attempt in range(3):
        try:
            plan = await analyze_blueprint(AnalyzeBlueprintRequest(
                messages=[{"role": "assistant", "content": blueprint_text}],
                model=request.model,
                strict=True,
                refine_contract=True,
                refine_rounds=3,
            ))
            break
        except HTTPException as exc:
            analyze_errors = [_prepare_protocol_issue("strict_analyze_failed", "内部蓝图未通过 strict analyze。", field="analyze_blueprint")]
            if attempt >= 2:
                break
            try:
                blueprint_text = await _repair_prepare_blueprint_protocol(request=request, blueprint_text=blueprint_text, protocol_errors=[{**analyze_errors[0], "detail": str(exc.detail)}])
            except Exception:
                break
            protocol_errors = _preflight_prepare_blueprint_text(blueprint_text)
            if protocol_errors:
                analyze_errors = protocol_errors
                break
    if plan is None:
        return PreparePlanResponse(status="needs_clarification", prepare_stage="creation_points_confirmation", clarifying_questions=["系统已整理出创建要点，但还需要你确认是否按这些要点继续。A. 按这些要点继续 B. 我补充说明"], review_summary=_strip_prepare_summary_risks(summary), skill_name=skill_name)

    summary.files_to_create_or_update = [
        file_spec.path
        for file_spec in (plan.files or [])
        if getattr(file_spec, "path", "")
        and not _is_directory_like_skill_path(getattr(file_spec, "path", ""))
        and not re.search(r"[<>{}\*]", getattr(file_spec, "path", ""))
    ]
    summary.assets_to_upload = [
        str(getattr(asset, "path", "") or "").strip()
        for asset in (plan.asset_requirements or [])
        if str(getattr(asset, "path", "") or "").strip()
        and str(getattr(asset, "source", "") or "").strip() in {"user_upload", "bundled"}
        and not re.search(r"运行时|每次上传|用户输入|runtime", str(getattr(asset, "description", "") or ""), re.I)
    ]
    graph_payload = plan.requirement_graph.model_dump(mode="json") if hasattr(plan.requirement_graph, "model_dump") else dict(plan.requirement_graph or {})
    return PreparePlanResponse(
        status="ready",
        prepare_stage="ready",
        review_summary=summary,
        blueprint_text=plan.blueprint_text or blueprint_text,
        skill_name=plan.skill_name,
        files=plan.files,
        warnings=plan.warnings,
        asset_requirements=plan.asset_requirements,
        final_outputs=plan.final_outputs,
        available_tools=plan.available_tools,
        missing_tool_configs=plan.missing_tool_configs,
        tool_requirements=plan.tool_requirements,
        creation_blockers=plan.creation_blockers,
        requirement_graph=graph_payload,
        workflow_allocation_summary=_load_workflow_allocation_summary(plan.skill_name),
    )


@router.post("/analyze-blueprint", response_model=AnalyzeBlueprintResponse)
async def analyze_blueprint(request: AnalyzeBlueprintRequest):
    try:
        plan: BlueprintPlan = parse_blueprint(request.messages, strict=request.strict)
    except BlueprintShapeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    effective_messages = request.messages
    blueprint_contract_warnings: list[dict[str, Any]] = []
    entries_by_path = {
        entry.path: entry
        for entry in (plan.skill_plan.files if plan.skill_plan else [])
        if not _is_directory_like_skill_path(entry.path)
    }

    blueprint_text = "\n\n".join(
        str(message.get("content") or "")
        for message in effective_messages
        if isinstance(message, dict)
    )

    def is_directory_placeholder(path: str) -> bool:
        normalized = _normalize_skill_path(path)
        return not normalized or normalized in {"assets", "assets/"} or normalized.endswith("/") or (
            normalized.startswith(("assets/", "references/", "scripts/")) and not _has_file_extension(normalized)
        )

    base_paths = {f.path for f in plan.files if not is_directory_placeholder(f.path)}

    candidate_paths: set[str] = {path for path in _extract_declared_skill_paths(blueprint_text) if not is_directory_placeholder(path)}
    candidate_paths.update(entries_by_path.keys())

    extra_paths = []
    extra_path_warnings: list[str] = []
    for path in sorted(candidate_paths):
        if path in base_paths or not path.startswith("references/"):
            continue
        if is_runtime_artifact_semantic(path, _local_blueprint_text_for_path(path, blueprint_text)):
            extra_path_warnings.append(
                f"已忽略运行时产物文件计划项 {path}；运行时生成文件只能通过脚本 outputs/stdout metadata 表示。"
            )
            continue
        extra_paths.append(path)

    def fallback_role(path: str) -> str | None:
        if path == "SKILL.md":
            return "skill_overview"
        if path.startswith("scripts/"):
            return "generic_script"
        if path.startswith("references/"):
            return "reference"
        if path.startswith("assets/"):
            return "asset"
        return None

    def fallback_file_type(path: str) -> str | None:
        if path == "SKILL.md":
            return "skill"
        if path.startswith("scripts/"):
            return "script"
        if path.startswith("references/"):
            return "reference"
        if path.startswith("assets/"):
            return "asset"
        return None

    def serialize_plan_items(items: Any) -> list[dict[str, Any]]:
        return [
            dict(getattr(item, "__dict__", item))
            for item in (items or [])
            if isinstance(getattr(item, "__dict__", item), dict)
        ]

    def selected_tools_for_entry(entry: SkillPlanEntry | None) -> list[str]:
        if not entry:
            return []
        return list(resolve_tools_for_skill_plan_entry(entry).allowed_tools or [])

    files_out: list[FileSpecOut] = []

    directory_asset_requirements: list[AssetRequirementOut] = []
    for f in plan.files:
        if is_directory_placeholder(f.path):
            local_context = _local_blueprint_text_for_path(f.path, blueprint_text) or f.purpose or blueprint_text
            # TODO: move directory-level upload needs into the normalized plan so
            # asset_requirements are explicit and no longer inferred from text.
            if _normalize_skill_path(f.path).startswith("assets") and re.search(r"上传|user[_ -]?upload|素材|图片|image|asset", local_context, re.IGNORECASE) and not re.search(r"无需|不需要|不用|无需创建|不生成", local_context):
                directory_asset_requirements.append(AssetRequirementOut(
                    path="assets/",
                    generation_order=_generation_order_for_file("assets/", "user_upload"),
                    source="user_upload",
                    required=getattr(f, "required", True),
                    description=f.purpose or "需要用户上传素材",
                ))
            continue
        entry = entries_by_path.get(f.path)
        role = entry.role if entry else fallback_role(f.path)
        file_type = entry.file_type if entry else fallback_file_type(f.path)
        language = entry.language if entry else language_for_path(f.path)
        runtime = entry.runtime if entry else runtime_for_language(language, file_type or "")

        files_out.append(
            FileSpecOut(
                path=f.path,
                generation_order=_generation_order_for_file(f.path, f.asset_source if f.path.startswith("assets/") else ""),
                purpose=f.purpose,
                required=f.required,
                can_skip=f.can_skip,
                file_type=file_type,
                file_kind=entry.file_kind if entry else file_kind_for_path(f.path),
                role=role,
                component_hint=entry.component_hint if entry else (role or ""),
                inputs=entry.inputs if entry else [],
                outputs=entry.outputs if entry else [],
                dependencies=entry.dependencies if entry else [],
                side_effects=entry.side_effects if entry else [],
                required_tool_slots=serialize_plan_items(entry.required_tool_slots) if entry else [],
                implementation_strategy=serialize_plan_items(entry.implementation_strategy) if entry else [],
                selected_tools=selected_tools_for_entry(entry),
                runtime_contract=entry.runtime_contract if entry else {},
                artifact_contract=entry.artifact_contract if entry else {},
                required_capabilities=entry.required_capabilities if entry else [],
                raw_capability_hints=entry.raw_capability_hints if entry else [],
                forbidden_capabilities=entry.forbidden_capabilities if entry else [],
                reference_files=entry.reference_files if entry else [],
                skill_local_references=entry.skill_local_references if entry else [],
                creator_internal_references=entry.creator_internal_references if entry else [],
                language=language,
                runtime=runtime,
                entrypoint=entry.entrypoint if entry else "",
                command_template=entry.command_template if entry else "",
                references=entry.reference_files if entry else [],
                low_confidence=(entry.confidence < 0.7) if entry else False,
                confidence=entry.confidence if entry else 1.0,
                reason=entry.reason if entry else "fallback path classification",
                heuristic_signals=entry.heuristic_signals if entry else [],
                asset_source=f.asset_source if f.path.startswith("assets/") else "",
            )
        )

    for path in extra_paths:
        role = fallback_role(path)
        file_type = fallback_file_type(path)
        language = language_for_path(path)
        runtime = runtime_for_language(language, file_type or "")
        is_asset = path.startswith("assets/")

        required_capabilities, forbidden_capabilities = [], []
        inputs, outputs = default_io_for_file_kind(file_kind_for_path(path))

        files_out.append(
            FileSpecOut(
                path=path,
                generation_order=_generation_order_for_file(path, ""),
                purpose=(
                    f"用户上传的静态素材：{path}"
                    if is_asset
                    else f"参考说明文件：{path}"
                ),
                required=True,
                can_skip=False,
                file_type=file_type,
                file_kind=file_kind_for_path(path),
                role=role,
                component_hint=role or "",
                inputs=list(inputs or []),
                outputs=list(outputs or []),
                dependencies=[],
                side_effects=[],
                required_tool_slots=[],
                implementation_strategy=[],
                selected_tools=[],
                runtime_contract={},
                artifact_contract={},
                required_capabilities=list(required_capabilities or []),
                raw_capability_hints=[],
                forbidden_capabilities=[
                    cap for cap in list(forbidden_capabilities or [])
                    if cap not in set(required_capabilities or [])
                ],
                reference_files=[],
                skill_local_references=[],
                creator_internal_references=[],
                language=language,
                runtime=runtime,
                entrypoint=path if path.startswith("scripts/") else "",
                command_template="",
                references=[],
                low_confidence=False,
                confidence=1.0,
                reason="fallback path classification from declared blueprint path",
                heuristic_signals=["declared_skill_path"],
                asset_source="",
            )
        )

    warnings: list[dict[str, Any]] = []
    workflow_allocation_summary, allocation_patched_targets, workflow_allocation_resolved = await _allocate_workflow_script_responsibilities(
        blueprint_text=blueprint_text,
        files_out=files_out,
        requested_model=request.model,
        warnings=warnings,
    )
    await _normalize_script_purpose_short_contracts(
        blueprint_text=blueprint_text,
        files_out=files_out,
        workflow_allocation_summary=workflow_allocation_summary,
        skip_targets=allocation_patched_targets,
        requested_model=request.model,
        warnings=warnings,
    )
    resolved_contracts = [
        {
            "path": file_spec.path,
            "final_inputs": list(file_spec.inputs or []),
            "final_outputs": list(file_spec.outputs or []),
            "purpose_digest": hashlib.sha256(str(file_spec.purpose or "").encode("utf-8")).hexdigest()[:12],
        }
        for file_spec in files_out
        if file_spec.path.startswith("scripts/") and file_spec.required
    ]
    logger.info(
        "[Creator][global_contract][%s] %s",
        "resolved" if workflow_allocation_resolved else "unresolved",
        json.dumps({
            "event": (
                "creator_global_contract_resolved"
                if workflow_allocation_resolved
                else "creator_global_contract_unresolved"
            ),
            "contracts": resolved_contracts,
            "allocation_patched_targets": sorted(allocation_patched_targets),
        }, ensure_ascii=False, default=str),
    )
    warnings.append({
        "severity": "info" if workflow_allocation_resolved else "validator_warning",
        "code": (
            "workflow_allocation_resolved"
            if workflow_allocation_resolved
            else "workflow_allocation_unresolved_using_safe_partial_or_fallback"
        ),
        "source": "analyze_blueprint",
        "path": "",
        "field": "workflow_allocation",
        "message": (
            "Using resolved executable workflow final contracts."
            if workflow_allocation_resolved
            else (
                "Executable workflow final contract is unresolved; safe allocation patches "
                "were applied where possible, otherwise original plan remains as fallback."
            )
        ),
    })
    fallback_requirement_graph = build_default_requirement_graph(files_out)
    try:
        requirement_graph = await _extract_requirement_graph_with_validator(
            blueprint_text=blueprint_text,
            files_out=files_out,
            requested_model=request.model,
            warnings=warnings,
        )
    except RequirementGraphValidationError as exc:
        requirement_graph = validate_requirement_graph_schema(fallback_requirement_graph, files_out)
        warnings.append({
            "severity": "validator_warning",
            "code": exc.code,
            "source": "responsibility_graph",
            "path": str((exc.details or {}).get("path") or ""),
            "field": "requirement_graph",
            "message": f"Responsibility graph patch failed; using deterministic graph: {exc}",
        })
    requirements_by_file: dict[str, list[RequirementItem]] = {}
    for req in requirement_graph.requirements:
        requirements_by_file.setdefault(req.target_file, []).append(req)
    for file_spec in files_out:
        file_spec.requirements = list(requirements_by_file.get(file_spec.path, []))
    _persist_requirement_graph(plan.skill_name, requirement_graph)
    _persist_workflow_allocation_summary(plan.skill_name, workflow_allocation_summary)

    asset_requirements = [
        AssetRequirementOut(
            path=file_spec.path,
            generation_order=file_spec.generation_order,
            source=file_spec.asset_source,
            required=file_spec.required,
            description=file_spec.purpose,
        )
        for file_spec in files_out
        if file_spec.path.startswith("assets/") and file_spec.asset_source == "user_upload"
    ] + directory_asset_requirements

    available_tools = [tool_status(cap) for cap in list_tool_capabilities()]
    tool_requirements: list[dict[str, Any]] = []
    creation_blockers: list[dict[str, Any]] = []
    for file_spec in files_out:
        entry_requirements, entry_blockers = _creator_tool_readiness_blockers(file_spec.model_dump(mode="json"))
        tool_requirements.extend(entry_requirements)
        creation_blockers.extend(entry_blockers)
    required_tool_names = {
        requirement.get("capability")
        for requirement in tool_requirements
        if requirement.get("capability")
    }
    missing_tool_configs = []
    def normalize_warning(item: Any) -> dict[str, Any] | None:
        if isinstance(item, dict):
            return {
                "severity": str(item.get("severity") or "normalization_note"),
                "code": str(item.get("code") or "normalization_note"),
                "source": str(item.get("source") or "skill_plan"),
                "path": str(item.get("path") or ""),
                "field": str(item.get("field") or ""),
                "message": str(item.get("message") or ""),
            }
        text = str(item or "").strip()
        if not text:
            return None
        return {
            "severity": "normalization_note",
            "code": "normalization_note",
            "source": "skill_plan",
            "path": "",
            "field": "",
            "message": text,
        }

    seen_warning_keys: set[str] = set()
    for raw_warning in [*list(plan.warnings), *extra_path_warnings, *blueprint_contract_warnings]:
        warning = normalize_warning(raw_warning)
        if not warning:
            continue
        key = ":".join(str(warning.get(part) or "") for part in ("source", "path", "field", "code"))
        if key in seen_warning_keys:
            continue
        seen_warning_keys.add(key)
        warnings.append(warning)
    for capability_name in sorted(required_tool_names):
        cap = get_tool_capability(capability_name)
        if not cap or cap.category == "resource":
            continue
        status = tool_status(cap)
        missing_runtime_helpers = status.get("missing_runtime_helpers") or []
        missing_dependencies = status.get("missing_dependencies") or []
        if not status["creator_available"]:
            warnings.append({"severity": "user_warning", "code": "tool_unavailable", "source": "generator", "path": "", "field": "required_capabilities", "message": f"工具能力 {capability_name} 已被禁用或不允许 Creator 使用，相关脚本不会默认获得该能力。"})
        if missing_runtime_helpers:
            warnings.append({"severity": "user_warning", "code": "tool_runtime_helper_missing", "source": "generator", "path": "", "field": "required_capabilities", "message": f"工具能力 {capability_name} 缺少 runtime helper: {', '.join(missing_runtime_helpers)}。"})
        if missing_dependencies:
            warnings.append({"severity": "user_warning", "code": "tool_runtime_dependency_missing", "source": "generator", "path": "", "field": "required_capabilities", "message": f"工具能力 {capability_name} 缺少 runtime dependency: {', '.join(missing_dependencies)}。"})
        if not status["configured"] or missing_runtime_helpers or missing_dependencies or not status["creator_available"]:
            missing_tool_configs.append(status)

    return AnalyzeBlueprintResponse(
        skill_name=plan.skill_name,
        files=files_out,
        warnings=warnings,
        asset_requirements=asset_requirements,
        final_outputs=_final_outputs_from_plan_entries(list(entries_by_path.values())),
        available_tools=available_tools,
        missing_tool_configs=missing_tool_configs,
        tool_requirements=tool_requirements,
        creation_blockers=creation_blockers,
        requirement_graph=requirement_graph,
        blueprint_text=blueprint_text,
        blueprint_refined=False,
    )


@router.post("/init-skill", response_model=InitSkillResponse)
async def init_skill(request: InitSkillRequest):
    """Initialise a new Skill directory structure."""
    skill_name = _validate_skill_name(request.skill_name)
    result = run_action({"action": "init", "name": skill_name})
    return InitSkillResponse(
        success=result["success"],
        path=result.get("path"),
        message=result["message"],
    )

@router.post("/upload-asset", response_model=UploadAssetResponse)
async def upload_asset(
    skill_name: str = Form(...),
    file_path: str = Form(...),
    file: UploadFile = File(...),
):
    skill_name = _validate_skill_name(skill_name)
    target_rel_path = _validate_asset_upload_path(file_path)

    skill_dir = settings.skills_path / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)

    target_path = skill_dir / target_rel_path
    target_path.parent.mkdir(parents=True, exist_ok=True)

    total = 0
    try:
        with target_path.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break

                total += len(chunk)
                if total > _MAX_ASSET_UPLOAD_BYTES:
                    try:
                        target_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    raise HTTPException(
                        status_code=413,
                        detail=f"素材文件超过大小限制：{_MAX_ASSET_UPLOAD_BYTES // 1024 // 1024}MB",
                    )

                out.write(chunk)
    finally:
        await file.close()

    return UploadAssetResponse(
        success=True,
        path=target_rel_path,
        size=total,
        message=f"素材已上传：{target_rel_path}",
    )


def _contract_failure_layer(results: list[ContractCheckResult]) -> str:
    failed = [result for result in results if not result.passed]
    if not failed:
        return "content_review"

    first = failed[0]
    if getattr(first, "layer", None):
        return str(first.layer)

    if getattr(first, "id", None):
        return str(first.id)

    return "content_review"


def _stage_error_from_exception(source: str, exc: Exception, *, default_layer: str) -> FileGenerationStageError:
    if isinstance(exc, FileGenerationStageError):
        return exc

    if isinstance(exc, ContractValidationError):
        layer = _contract_failure_layer(exc.results) or default_layer

        # 让 SKILL.md 蓝图一致性失败进入专门的返修 source。
        # 不是新增责任审查，只是把已有审查结果正确分类。
        if layer == "skill_md_blueprint_alignment" or any(
            getattr(result, "layer", None) == "skill_md_blueprint_alignment"
            for result in exc.results
            if not result.passed
        ):
            return FileGenerationStageError(
                source="skill_md_blueprint_alignment",
                layer="skill_md_blueprint_alignment",
                detail=str(exc),
                original=exc,
            )

        return FileGenerationStageError(
            source=source,
            layer=layer,
            detail=str(exc),
            original=exc,
        )

    return FileGenerationStageError(
        source=source,
        layer=default_layer,
        detail=str(exc),
        original=exc,
    )


def _first_round_repair_limit(source: str) -> int:
    return {
        "hard_format": 3,
        "markdown_format": 3,
        "content_review": 4,
        "skill_md_blueprint_alignment": 6,
        "script_responsibility": 5,
        "script_functional": 5,
        "model_empty_content": len(_EMPTY_GENERATION_PROMPT_VARIANTS),
    }.get(source, 4)


def _prompt_chars(messages: list[dict]) -> int:
    return sum(len(str(message.get("content") or "")) for message in messages if isinstance(message, dict))


async def _complete_creator_file_generation(
    *,
    messages: list[dict],
    model: str,
    skill_name: str,
    file_path: str,
    prompt_variant: str,
    retry_index: int,
) -> str:
    """Call the file-generation model with minimum diagnostic logging."""
    messages = _ensure_user_visible_task_message(messages)
    prompt_text = "\n".join(str(message.get("content") or "") for message in messages if isinstance(message, dict))
    logger.info(
        "[Creator][generate_file][llm_request] skill=%s file_path=%s model=%s prompt_variant=%s retry_index=%d prompt_chars=%d message_roles=%s system_chars=%d user_chars=%d uses_full_blueprint=%s uses_platform_protocol_text=%s",
        skill_name,
        file_path,
        model,
        prompt_variant,
        retry_index,
        _prompt_chars(messages),
        json.dumps(_message_role_counts(messages), ensure_ascii=False, sort_keys=True),
        _message_role_chars(messages, "system"),
        _message_role_chars(messages, "user"),
        "已确认的蓝图" in prompt_text or "Skill 架构蓝图" in prompt_text,
        any(
            marker in prompt_text
            for marker in ("宿主 Markdown 执行说明", "SKILL.md workflow", "第二轮 E2E", "平台执行协议")
        ),
    )
    try:
        content = await complete_chat_once(messages, model)
    except Exception as exc:
        logger.exception(
            "[Creator][generate_file][llm_response] skill=%s file_path=%s model=%s prompt_variant=%s retry_index=%d message_roles=%s system_chars=%d user_chars=%d error_type=%s",
            skill_name,
            file_path,
            model,
            prompt_variant,
            retry_index,
            json.dumps(_message_role_counts(messages), ensure_ascii=False, sort_keys=True),
            _message_role_chars(messages, "system"),
            _message_role_chars(messages, "user"),
            type(exc).__name__,
        )
        raise
    logger.info(
        "[Creator][generate_file][llm_response] skill=%s file_path=%s model=%s prompt_variant=%s retry_index=%d message_roles=%s system_chars=%d user_chars=%d finish_reason=%s content_len=%d error_type=%s",
        skill_name,
        file_path,
        model,
        prompt_variant,
        retry_index,
        json.dumps(_message_role_counts(messages), ensure_ascii=False, sort_keys=True),
        _message_role_chars(messages, "system"),
        _message_role_chars(messages, "user"),
        "unknown",
        len(content or ""),
        "",
    )
    return content

def _strict_contract_rewrite_allowed(source: str) -> bool:
    # First-round repairs must stay localized: the validator identifies the
    # failed function/region, and the repair model edits only that region while
    # preserving entrypoints, JSON argv parsing, stdout fields, and artifact
    # protocol.  Do not escalate script failures into full rewrites.
    return source in {"content_review"}


def _repair_mode_for_first_round(*, source: str, file_path: str, attempt: int) -> str:
    """Choose repair mode for first-round generation failures.

    第一轮 scripts/** 只剩：
    - content_review：裸源码、语法、安全；
    - script_functional / script_responsibility：职责未完成。

    不再存在 script_smoke。
    """
    if source in {"script_functional", "script_responsibility", "script_requirement_failed"} and file_path.startswith("scripts/"):
        return "localized_patch"

    if attempt >= 2 and file_path.startswith("scripts/") and _strict_contract_rewrite_allowed(source):
        return "strict_contract_rewrite"

    return "minimal_edit"


def _contract_result_to_failure(result: ContractCheckResult) -> dict[str, Any]:
    issue = result.details.get("issue") if isinstance(result.details, dict) else None
    issue = issue if isinstance(issue, dict) else {}
    return {
        "id": result.id,
        "target": result.target,
        "message": result.message,
        "expected": result.expected,
        "minimal_edit": result.minimal_edit,
        "details": result.details,
        "layer": result.layer or _contract_layer_for_check_id(result.id),
        "resource_role": issue.get("resource_role"),
        "claim_type": issue.get("claim_type"),
        "repair_ops": issue.get("repair_ops") if isinstance(issue.get("repair_ops"), list) else [],
    }


def _exception_to_skill_md_failures(exc: Exception, *, source: str = "skill_md") -> list[dict[str, Any]]:
    if isinstance(exc, CreatorValidatorReviewError):
        return [{
            "id": f"{source}.validator_error",
            "target": "SKILL.md",
            "message": str(exc),
            "expected": "重试蓝图一致性 reviewer，获得有效 JSON 后再决定是否需要修 SKILL.md。",
            "minimal_edit": "不要修改 SKILL.md；这是审查器输出格式问题。",
            "details": {"raw_excerpt": exc.raw_excerpt},
            "layer": "validator_error",
            "severity": "advisory",
            "advisory": True,
        }]
    if isinstance(exc, ContractValidationError):
        return [_contract_result_to_failure(result) for result in exc.results if not result.passed]
    return [{
        "id": f"{source}.validation_error",
        "target": "SKILL.md",
        "message": str(exc),
        "expected": "SKILL.md 第一轮只修格式、蓝图一致性、脚本说明、资源说明、最终产物说明和平台/内部字段泄露。",
        "minimal_edit": "只修改 SKILL.md；不要修改 scripts、assets、SkillPlan 或 runtime specs。",
        "details": {},
        "layer": source,
    }]


def classify_skill_md_failure_severity(failure: dict[str, Any]) -> str:
    if str(failure.get("severity") or "").lower() in {"advisory", "note", "warning"}:
        return "advisory"
    if str(failure.get("layer") or "").lower() in {"advisory", "advisory_notes", "validator_error"}:
        return "advisory"
    if bool(failure.get("advisory")):
        return "advisory"
    return "hard"

def _failure_signature(failure: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(failure.get("target") or failure.get("target_file") or ""),
        str(failure.get("layer") or failure.get("source_layer") or ""),
        str(failure.get("id") or failure.get("check_id") or ""),
        str(failure.get("evidence") or (failure.get("details") or {}).get("evidence") or ""),
    )


def merge_duplicate_failures(failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for failure in failures:
        if not isinstance(failure, dict):
            continue
        key = _failure_signature(failure)
        if key not in merged:
            merged[key] = dict(failure)
        else:
            messages = [str(merged[key].get("message") or ""), str(failure.get("message") or "")]
            merged[key]["message"] = " / ".join(dict.fromkeys(m for m in messages if m))
            ops = []
            for source in (merged[key].get("repair_ops"), failure.get("repair_ops")):
                if isinstance(source, list):
                    ops.extend(op for op in source if isinstance(op, dict))
            if ops:
                merged[key]["repair_ops"] = ops
    return list(merged.values())


def resolve_resource_role_conflicts(failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize reviewer resource-role conflicts from structured fields only."""
    normalized: list[dict[str, Any]] = []
    for failure in failures:
        item = dict(failure)
        role = str(item.get("resource_role") or item.get("role") or "").lower()
        claim = str(item.get("claim_type") or item.get("resource_claim") or "").lower()
        target = str(item.get("target_file") or item.get("target") or "")
        if role == "reference" or target.startswith("references/"):
            if claim in {"forbid_read", "no_runtime_read", "must_not_read"}:
                item["severity"] = "advisory"
                item["message"] = (
                    str(item.get("message") or "")
                    + "（已按资源角色规则降级：reference 可按需只读加载，但不能执行、修改、生成或作为产物/素材。）"
                )
            elif claim in {"execution_step", "artifact", "asset_material", "model_generated", "modifiable"}:
                item["expected"] = (
                    "references/*.md 是只读、按需加载的参考资料；非执行步骤、非产物、非生成素材，且不得被修改。"
                )
        if role == "asset" or target.startswith("assets/"):
            if claim in {"model_generated", "modifiable", "write_asset"}:
                item["expected"] = "assets/** 只能是用户上传或结构预留的静态素材，不能由模型生成或写入。"
        normalized.append(item)
    return normalized


def _normalize_repair_ops(failure: dict[str, Any]) -> list[dict[str, Any]]:
    """Pass through structured repair ops; do not parse natural language."""
    allowed = {"replace", "delete", "append_after", "append_before"}
    source = failure.get("repair_ops")
    ops: list[dict[str, Any]] = []
    if isinstance(source, list):
        candidates = source
    elif isinstance(source, dict):
        candidates = [source]
    else:
        candidates = []
    for op in candidates:
        if not isinstance(op, dict):
            continue
        op_name = str(op.get("op") or "").lower()
        anchor = op.get("anchor") or op.get("evidence") or failure.get("evidence") or (failure.get("details") or {}).get("evidence")
        if op_name not in allowed or not isinstance(anchor, str) or not anchor:
            continue
        normalized = {"op": op_name, "anchor": anchor}
        if isinstance(op.get("text"), str):
            normalized["text"] = op["text"]
        if isinstance(op.get("replacement"), str):
            normalized["replacement"] = op["replacement"]
        if isinstance(op.get("new"), str):
            normalized["replacement"] = op["new"]
        ops.append(normalized)
    return ops


def normalize_skill_md_failures(failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    resolved = resolve_resource_role_conflicts(merge_duplicate_failures(failures))
    for failure in resolved:
        ops = _normalize_repair_ops(failure)
        if ops:
            failure["repair_ops"] = ops
    return [failure for failure in resolved if classify_skill_md_failure_severity(failure) == "hard"]




def _structured_failure_signature(stage_error: FileGenerationStageError, deterministic_error: str) -> str:
    """Stable signature for repeated first-round failures, independent of candidate text."""
    original = getattr(stage_error, "original", None)
    records: list[dict[str, Any]] = []
    if isinstance(original, ContractValidationError):
        for result in original.results:
            if getattr(result, "passed", False):
                continue
            details = getattr(result, "details", {}) or {}
            records.append({
                "check_id": getattr(result, "id", ""),
                "layer": getattr(result, "layer", "") or getattr(stage_error, "layer", ""),
                "target": getattr(result, "target", ""),
                "missing_evidence": details.get("missing_evidence") or details.get("missing_requirement_ids") or [],
                "matched": details.get("matches") or details.get("matched_paths") or getattr(result, "matched_paths", []),
                "code_region": details.get("code_region") or details.get("line_region") or "",
                "evidence": details.get("evidence") or getattr(result, "message", ""),
            })
    elif isinstance(original, ScriptFunctionalValidationError):
        for issue in getattr(original, "issues", []) or []:
            if not isinstance(issue, dict):
                continue
            records.append({
                "check_id": issue.get("id") or issue.get("issue_type") or "script_functional",
                "layer": getattr(original, "layer", "") or getattr(stage_error, "layer", ""),
                "target": issue.get("failed_file") or issue.get("target_file") or "",
                "requirement_id": issue.get("requirement_id") or "",
                "missing_evidence": issue.get("missing_evidence") or [],
                "matched": issue.get("matched_ranges") or issue.get("matches") or [],
                "code_region": issue.get("code_region") or issue.get("line_region") or "",
                "evidence": issue.get("evidence") or issue.get("reason") or "",
            })
    if not records:
        records.append({
            "check_id": getattr(stage_error, "source", ""),
            "layer": getattr(stage_error, "layer", ""),
            "evidence": str(deterministic_error or "")[:1000],
        })
    payload = {"source": stage_error.source, "layer": stage_error.layer, "records": records}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()

def _canonicalize_generated_candidate(
    *,
    file_path: str,
    content: str,
    role: str | None = None,
    skill_plan_entry: dict[str, Any] | None = None,
    skill_name: str = "",
    purpose: str = "",
) -> str:
    """Return the canonical single-file candidate for validation/repair/SSE."""
    canonical = _sanitize_generated_file_content(
        file_path,
        content,
        role=role,
        skill_plan_entry=skill_plan_entry,
    )
    if file_path != "SKILL.md":
        canonical, _metadata_patched = _canonicalize_markdown_frontmatter_for_file(
            file_path=file_path,
            content=canonical,
            skill_name=skill_name,
            purpose=purpose,
        )
    if file_path.startswith("references/") and Path(file_path).suffix.lower() == ".md":
        canonical = _ensure_reference_metadata_frontmatter(
            file_path=file_path,
            content=canonical,
            purpose=purpose,
            skill_plan_entry=skill_plan_entry,
        )
    return canonical

_SCRIPT_RAW_SOURCE_FORMAT_ERROR_IDS = {
    "script.raw_source.single_file",
    "script.raw_source.ambiguous_multi_code_blocks",
    "script.raw_source.multi_file_bundle",
    "script.raw_source.ambiguous_script_candidate",
}

_MARKDOWN_FORMAT_ERROR_ID_PREFIXES = (
    "skill_md.frontmatter",
    "skill_md.markdown_body_structure",
    "skill_md.command_block",
    "skill_md.script_command",
    "command_block",
)

_MARKDOWN_FORMAT_ERROR_TEXT_MARKERS = (
    "hard_format",
    "markdown_format",
    "frontmatter",
    "metadata_region",
    "body_region",
    "fenced_block",
    "fence",
    "code fence",
    "command_block",
    "json_argv",
    "shell_command",
    "markdown_body_structure",
    "yaml",
)

_SKILL_MD_BODY_FORMAT_REQUIREMENTS = """SKILL.md body_region 格式硬要求：
1. 只输出 Markdown 正文，不输出 YAML frontmatter。
2. 所有 fenced code block 必须完整闭合。
3. 每个真实 scripts/*.py 必须有一个独立、无缩进的 ```bash fenced code block。
4. 每个 ```bash block 内只能包含一条真实 shell 命令。
5. ```bash block 内禁止出现多条命令、说明文字、列表、注释、JSON 配置对象或伪命令对象。
6. 命令必须直接调用真实 scripts/*.py 路径。
7. 默认命令格式是：python scripts/<file>.py '<JSON object argv>'。
8. JSON argv 必须是 shell-quoted 的 JSON object 字符串。
9. 动态 placeholder 必须作为 JSON 字符串值出现。
10. 禁止在 ```bash block 内放 runtime/entrypoint/argv JSON 对象。
11. 禁止在 ```bash block 内放 runner/script/argv JSON 对象。
12. 禁止使用 --argv，除非当前脚本源码明确实现了 --argv。
13. Creator 默认脚本协议是 sys.argv[1] JSON object。
14. compact_requirement_graph 只是职责上下文，不是命令块格式。
15. 不得把 compact_requirement_graph 条目复制成 JSON block。
16. 不得把 runtime、target_file、inputs、outputs 这些图谱字段原样写成 bash block 内容。"""

_REFERENCE_MD_BODY_FORMAT_REQUIREMENTS = """references/*.md body_region 格式硬要求：
references/*.md 是参考资料正文，不是执行步骤。
不得输出调用 scripts/*.py 的 executable ```bash block。
不得重新定义 scripts 的 final inputs/outputs。
不得把 reference 写成 workflow 执行入口。
如需展示命令形态，只能使用 ```text 或普通说明。
所有 fenced block 必须完整闭合。"""

_SKILL_MD_COMMAND_TEMPLATE_SEMANTIC_RULES = """SKILL.md bash command block 语义规则：
1. bash command block 是运行模板，不是示例调用。
2. requirement_graph / workflow_allocation 的 inputs/outputs 是强语义参考，不是字段名硬合同；argv key 可以与图谱字段不逐字一致。
3. 生成 JSON argv 时必须先语义理解图谱中的输入、输出和依赖关系。
4. argv key/value 必须语义上可追踪到用户输入、上游脚本 stdout、当前脚本配置或蓝图明确常量。
5. 不得为了让命令看起来完整而编造无来源字面值或占位参数。
6. 不得把示例调用、示例值或说明性样例写进 bash command block。
7. 不得把下游脚本输入写成无来源字面值；应语义上来自上游 stdout，字段名可由第二轮 E2E 对齐。
8. 不得把用户输入写成字面值；应引用平台输入 placeholder 或传入通用 payload。
9. 如果不确定具体字段名，优先使用通用 user_request/input/payload，由脚本解析。
10. 第一轮只判断是否语义可追踪、是否明显示例调用、是否明显无来源占位、是否完全脱离图谱 IO 语义。
11. 第一轮不得要求 argv key 必须逐字等于 graph.inputs，也不得要求 placeholder 必须逐字等于 graph.outputs。
12. compact_requirement_graph 只是职责上下文，不是命令块 JSON schema；不得把条目机械复制成 JSON block。"""


def _is_markdown_creator_file(file_path: str) -> bool:
    return (
        file_path == "SKILL.md"
        or file_path.startswith("references/")
        or Path(file_path).suffix.lower() in {".md", ".markdown"}
    )


def _markdown_format_requirements_for_prompt(file_path: str, region: str) -> str:
    if region != "body_region":
        return (
            "metadata_region 格式硬要求：只输出闭合 YAML frontmatter；"
            "不得输出正文；不得输出未闭合 fence；不得写 workflow/inputs/outputs/runtime_contract 等内部合同字段。"
        )
    if file_path == "SKILL.md":
        return f"{_SKILL_MD_BODY_FORMAT_REQUIREMENTS}\n\n{_SKILL_MD_COMMAND_TEMPLATE_SEMANTIC_RULES}"
    if file_path.startswith("references/"):
        return _REFERENCE_MD_BODY_FORMAT_REQUIREMENTS
    return (
        "Markdown body_region 格式硬要求：只输出正文，不输出 YAML frontmatter；"
        "所有 fenced code block 必须完整闭合。"
    )


def _contract_result_id(result: Any) -> str:
    if isinstance(result, ContractCheckResult):
        return str(result.id or "")
    if isinstance(result, dict):
        return str(result.get("id") or result.get("check_id") or "")
    return ""


def _contract_result_layer(result: Any) -> str:
    if isinstance(result, ContractCheckResult):
        return str(result.layer or "")
    if isinstance(result, dict):
        return str(result.get("layer") or "")
    return ""


def _is_markdown_format_result(result: Any) -> bool:
    result_id = _contract_result_id(result)
    layer = _contract_result_layer(result)
    combined = f"{result_id}\n{layer}".lower()
    return (
        any(result_id.startswith(prefix) for prefix in _MARKDOWN_FORMAT_ERROR_ID_PREFIXES)
        or any(marker in combined for marker in _MARKDOWN_FORMAT_ERROR_TEXT_MARKERS)
    )


def is_script_raw_source_format_error(stage_error: FileGenerationStageError) -> bool:
    """Route scripts/* raw-source structure failures to regeneration only."""
    if getattr(stage_error, "source", "") not in {"content_review", "script_raw_source", "format_stage"}:
        return False
    original = getattr(stage_error, "original", None)
    if isinstance(original, ContractValidationError):
        return any((not result.passed) and result.id in _SCRIPT_RAW_SOURCE_FORMAT_ERROR_IDS for result in original.results)
    detail = str(getattr(stage_error, "detail", "") or "")
    return any(error_id in detail for error_id in _SCRIPT_RAW_SOURCE_FORMAT_ERROR_IDS)


def is_generation_format_error(stage_error: FileGenerationStageError) -> bool:
    return is_script_raw_source_format_error(stage_error) or (
        getattr(stage_error, "source", "") == "format_stage"
        and _stage_error_has_full_format_rewrite_contract(stage_error)
    )


def _result_requires_full_format_rewrite(result: Any) -> bool:
    """Detect structured first-step format failures without matching prose/id text."""
    if isinstance(result, ContractCheckResult):
        if result.passed:
            return False
        details = result.details if isinstance(result.details, dict) else {}
        severity = str(details.get("severity") or "").strip()
        repair_strategy = str(details.get("repair_strategy") or "").strip()
        model_patch_allowed = details.get("model_patch_allowed")
        return (
            _is_markdown_format_result(result)
            or result.layer == "hard_format"
            or severity == "hard_format"
            or repair_strategy == "full_rewrite"
            or model_patch_allowed is False
        )

    if isinstance(result, dict):
        if result.get("passed") is True:
            return False
        return (
            _is_markdown_format_result(result)
            or str(result.get("layer") or "").strip() == "hard_format"
            or str(result.get("severity") or "").strip() == "hard_format"
            or str(result.get("repair_strategy") or "").strip() == "full_rewrite"
            or result.get("model_patch_allowed") is False
        )

    return False


def _stage_error_has_full_format_rewrite_contract(stage_error: FileGenerationStageError) -> bool:
    original = getattr(stage_error, "original", None)
    if isinstance(original, ContractValidationError):
        return any(_result_requires_full_format_rewrite(result) for result in original.results)

    detail = str(getattr(stage_error, "detail", "") or "").strip()
    if not detail:
        return False
    try:
        parsed = json.loads(detail)
    except Exception:
        return False
    items = parsed if isinstance(parsed, list) else [parsed]
    return any(_result_requires_full_format_rewrite(item) for item in items)


def is_markdown_hard_format_error(stage_error: FileGenerationStageError) -> bool:
    if getattr(stage_error, "source", "") == "hard_format" or getattr(stage_error, "layer", "") == "hard_format":
        return True
    if str(getattr(stage_error, "layer", "") or "") == "markdown_format":
        return True
    return _stage_error_has_full_format_rewrite_contract(stage_error)


def _first_round_format_stage_error(
    *,
    file_path: str,
    content: str,
    role: str | None = None,
    skill_plan_entry: dict[str, Any] | None = None,
) -> FileGenerationStageError | None:
    """FORMAT_STAGE: decide only whether candidate is legal content for file_path.

    This stage runs before any responsibility review.  Failures returned here
    must be handled by full current-file regeneration, not patch repair.
    """
    if not str(content or "").strip():
        return FileGenerationStageError(
            source="format_stage",
            layer="format_stage",
            detail="FORMAT_STAGE failed: generated candidate is empty.",
        )

    if (
        file_path != "SKILL.md"
        and (
            file_path.startswith("references/")
            or Path(file_path).suffix.lower() in {".md", ".markdown"}
        )
    ):
        format_failures = detect_markdown_hard_format_failures(
            file_path,
            content,
            require_frontmatter=(file_path == "SKILL.md"),
        )
        if format_failures:
            return FileGenerationStageError(
                source="hard_format",
                layer="hard_format",
                detail=json.dumps(format_failures, ensure_ascii=False, indent=2, default=str),
            )

    if file_path.startswith("scripts/"):
        raw_source_error_id = script_raw_source_candidate_error_id(content)
        if raw_source_error_id:
            return FileGenerationStageError(
                source="format_stage",
                layer="script_raw_source",
                detail=raw_source_error_id,
                original=ContractValidationError("FORMAT_STAGE script source structure failed.", [
                    ContractCheckResult(
                        id=raw_source_error_id,
                        passed=False,
                        target=file_path,
                        message="FORMAT_STAGE failed: candidate is not a single raw script source file.",
                        expected="Candidate must be one complete source file for the requested path.",
                        minimal_edit="Regenerate the complete current file; do not patch.",
                        details={"repair_strategy": "full_rewrite", "model_patch_allowed": False},
                        layer="format_stage",
                    )
                ]),
            )

    return None


def _build_markdown_format_full_rewrite_prompt(
    *,
    file_path: str,
    skill_name: str,
    blueprint_text: str,
    deterministic_error: str,
    current_content: str,
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "你是 Markdown 文件 hard format 全量重写器。"
                "你必须只输出完整目标 Markdown 文件内容。"
                "不要 JSON patch；不要 old_lines/new_lines；不要 diff；不要解释；不要日志；"
                "不要把 repair proposal JSON 嵌进 Markdown。"
                "frontmatter 必须完整闭合；所有 fenced block 必须成对闭合。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"文件路径：{file_path}\n"
                f"Skill 名称：{skill_name}\n\n"
                "后台 Markdown hard format 校验失败项如下；这是格式重试，不是业务语义 repair：\n"
                f"{deterministic_error}\n\n"
                "硬性要求：\n"
                "1. 只输出完整目标 Markdown 文件内容。\n"
                "2. 不要 JSON patch。\n"
                "3. 不要 old_lines/new_lines。\n"
                "4. 不要解释、日志或分析。\n"
                "5. frontmatter 必须完整闭合。\n"
                "6. 所有 fenced block 必须成对闭合。\n"
                "7. 不要把 repair proposal JSON 嵌进 Markdown。\n"
                "8. 不要混入 command argv / 字段对齐 / workflow dataflow 的局部修复；格式合法后由后续校验处理。\n\n"
                "蓝图上下文：\n"
                f"{(blueprint_text or '')[:8000]}\n\n"
                "当前文件内容：\n"
                "<<<CURRENT_FILE\n"
                f"{current_content or ''}\n"
                "CURRENT_FILE\n"
            ),
        },
    ]


def _build_markdown_region_rewrite_prompt(
    *,
    file_path: str,
    skill_name: str,
    blueprint_text: str,
    deterministic_error: str,
    current_content: str,
    region: str,
) -> list[dict[str, str]]:
    regions = split_markdown_regions(current_content or "")
    metadata_summary = regions.metadata_region[:1200] if regions.metadata_region else "（无 metadata_region）"
    if region == "metadata_region":
        system = (
            "你是 Markdown metadata_region 格式修复器。"
            "只输出修复后的 metadata_region；不要输出正文 body；不要解释。"
            "metadata_region 必须是闭合、可解析的 YAML frontmatter。"
        )
        user = (
            f"文件路径：{file_path}\nSkill 名称：{skill_name}\n\n"
            "失败项：\n"
            f"{deterministic_error}\n\n"
            f"{_markdown_format_requirements_for_prompt(file_path, 'metadata_region')}\n\n"
            "只修 metadata_region。禁止输出 body_region，禁止修改正文语义。\n"
            "输出必须以 --- 开始，并以单独一行 --- 闭合。\n"
            "metadata 只描述当前文件自身，不要写其它 scripts 的 capability/runtime/tool 边界。\n\n"
            "当前 metadata_region：\n<<<METADATA_REGION\n"
            f"{regions.metadata_region or ''}\n"
            "METADATA_REGION\n\n"
            "当前 body_region 摘要（仅供理解，禁止输出）：\n<<<BODY_SUMMARY\n"
            f"{(regions.body_region or '')[:3000]}\n"
            "BODY_SUMMARY\n\n"
            "蓝图上下文：\n"
            f"{(blueprint_text or '')[:6000]}"
        )
    else:
        body_format_requirements = _markdown_format_requirements_for_prompt(file_path, "body_region")
        system = (
            "你是 Markdown body_region 格式修复器。"
            "只输出修复后的 body_region；不要输出 YAML frontmatter；不要解释。"
            "body_region 可包含普通 Markdown、bash/json/code fence，但 fence 必须闭合。"
        )
        user = (
            f"文件路径：{file_path}\nSkill 名称：{skill_name}\n\n"
            "失败项：\n"
            f"{deterministic_error}\n\n"
            f"{body_format_requirements}\n\n"
            "只修 body_region。禁止重新生成 metadata/frontmatter，禁止输出文件开头 ---。\n"
            "如果 command/bash block 格式错误，只修正文中的 block。"
            "reference 正文不得重新定义 scripts/*.py 的 capability/runtime/tool 边界。\n\n"
            "已校验 metadata_region 摘要（只供遵循，禁止输出）：\n<<<METADATA_REGION\n"
            f"{metadata_summary}\n"
            "METADATA_REGION\n\n"
            "当前 body_region：\n<<<BODY_REGION\n"
            f"{regions.body_region or current_content or ''}\n"
            "BODY_REGION\n\n"
            "蓝图上下文：\n"
            f"{(blueprint_text or '')[:6000]}"
        )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _merge_markdown_region_rewrite(current_content: str, rewritten_region: str, region: str) -> str:
    regions = split_markdown_regions(current_content or "")
    if region == "metadata_region":
        return merge_markdown_regions(rewritten_region, regions.body_region)
    body = rewritten_region
    # Guard against model accidentally returning a second frontmatter while body
    # repair is requested; preserve already-validated metadata.
    accidental = split_markdown_regions(body)
    if accidental.metadata_region and accidental.metadata_closed:
        body = accidental.body_region
    return merge_markdown_regions(regions.metadata_region, body)


def _compact_requirement_graph_for_prompt(raw_graph: Any) -> dict[str, Any]:
    """Return a small responsibility ledger for Markdown prompts.

    This intentionally preserves only contract-level context and never expands
    into generated source/reference content or a second responsibility author.
    """

    if raw_graph is None:
        return {"requirements": []}
    if hasattr(raw_graph, "model_dump"):
        raw_graph = raw_graph.model_dump(mode="json")
    if not isinstance(raw_graph, dict):
        return {"requirements": []}

    def trunc(value: Any, limit: int = 500) -> str:
        text = str(value or "").strip()
        return text[:limit]

    def string_list(value: Any) -> list[str]:
        if isinstance(value, list):
            items = value
        elif isinstance(value, tuple):
            items = list(value)
        elif value in (None, ""):
            items = []
        else:
            items = [value]
        out: list[str] = []
        for item in items:
            text = trunc(item)
            if text:
                out.append(text)
        return out[:50]

    requirements: list[dict[str, Any]] = []
    raw_requirements = raw_graph.get("requirements")
    if not isinstance(raw_requirements, list):
        raw_requirements = []
    for item in raw_requirements[:50]:
        if hasattr(item, "model_dump"):
            item = item.model_dump(mode="json")
        if not isinstance(item, dict):
            continue
        compact = {
            "target_file": trunc(item.get("target_file")),
            "role": trunc(item.get("role")),
            "runtime": trunc(item.get("runtime")),
            "purpose": trunc(item.get("purpose")),
            "inputs": string_list(item.get("inputs")),
            "outputs": string_list(item.get("outputs")),
            "depends_on": string_list(item.get("depends_on")),
            "must_do": string_list(item.get("must_do")),
            "must_not_do": string_list(item.get("must_not_do")),
        }
        if compact["target_file"]:
            requirements.append(compact)

    compact_graph = {"requirements": requirements}
    serialized = json.dumps(compact_graph, ensure_ascii=False, default=str)
    if len(serialized) <= 14000:
        return compact_graph
    trimmed: list[dict[str, Any]] = []
    for item in requirements:
        trimmed.append(item)
        if len(json.dumps({"requirements": trimmed}, ensure_ascii=False, default=str)) > 14000:
            trimmed.pop()
            break
    return {"requirements": trimmed}


def _build_markdown_initial_region_prompt(
    *,
    file_path: str,
    skill_name: str,
    purpose: str,
    blueprint_text: str,
    region: str,
    metadata_region: str = "",
    requirement_graph: dict[str, Any] | None = None,
    workflow_allocation_summary: str = "",
    final_outputs: list[Any] | None = None,
) -> list[dict[str, str]]:
    if region == "metadata_region":
        if file_path == "SKILL.md":
            allowed = (
                "SKILL.md metadata 只生成闭合 YAML frontmatter；默认只允许 name 和 description。"
                "只有蓝图明确要求时才允许 license 或 allowed-tools。"
            )
        elif file_path.startswith("references/"):
            allowed = (
                "references/*.md metadata 只生成闭合 YAML frontmatter；默认只允许 title 和 description。"
                "只有蓝图明确要求时才允许 source 或 license。"
            )
        else:
            allowed = "Markdown metadata 只描述当前文件自身，保持最小可解析 YAML frontmatter。"
        return [
            {"role": "system", "content": "你是 Markdown metadata_region 生成器。只输出闭合 YAML frontmatter。"},
            {"role": "user", "content": (
                f"为 {file_path} 生成 metadata_region。\n"
                f"Skill 名称：{skill_name}\n职责：{purpose}\n\n"
                f"{allowed}\n"
                "不要主动生成 metadata.creator；不要输出 body。\n"
                "所有 Markdown metadata 禁止生成这些键：workflow, inputs, outputs, dependencies, "
                "required_capabilities, business_forbidden_capabilities, references, assets, role, path, type, "
                "scope, script_order, resource_references, runtime_contract, artifact_contract, "
                "implementation_strategy, file_plan, capabilities。\n"
                "metadata 只描述当前文件自身；必须可解析、闭合；不要写正文长段落；"
                "不要定义其它 scripts/*.py 的 capability/runtime/tool 边界。\n"
                + "只输出 metadata_region，不输出 body。\n\n蓝图：\n"
                f"{(blueprint_text or '')[:8000]}"
            )},
        ]
    compact_graph = _compact_requirement_graph_for_prompt(requirement_graph)
    contract_context = (
        "compact_requirement_graph:\n"
        f"{json.dumps(compact_graph, ensure_ascii=False, default=str)[:14000]}\n\n"
        "workflow_allocation_summary:\n"
        f"{(workflow_allocation_summary or '')[:6000]}\n\n"
        "final_outputs:\n"
        f"{json.dumps(final_outputs or [], ensure_ascii=False, default=str)[:4000]}\n\n"
    )
    if file_path == "SKILL.md":
        body_rules = (
            f"{_SKILL_MD_BODY_FORMAT_REQUIREMENTS}\n\n"
            f"{_SKILL_MD_COMMAND_TEMPLATE_SEMANTIC_RULES}\n\n"
            "SKILL.md body 必须基于 blueprint_text、compact requirement_graph、workflow_allocation_summary、"
            "final_outputs 以及 references/assets 路径写最终用户说明。\n"
            "应包含：Skill 用途；用户需要提供什么；高层执行流程；每个真实脚本的自然语言职责说明；"
            "每个真实脚本的 bash 命令块；references 的只读参考角色；assets 的上传/静态素材角色；"
            "最终产物；注意事项。\n"
            "不要写 Creator 创建流程、点击开始创建、已通过 E2E、系统将自动生成文件、Runtime Contract JSON、"
            "ToolSlot/implementation_strategy/capability cards、reference 正文全文、脚本源码解释、validator/repair 日志。"
        )
    elif file_path.startswith("references/"):
        body_rules = (
            f"{_REFERENCE_MD_BODY_FORMAT_REQUIREMENTS}\n\n"
            "references/*.md body 只写参考资料正文；不作为执行步骤；不要写可执行 bash/sh/shell block 调用 scripts/*.py；"
            "不重新定义 scripts 的 final inputs/outputs；可以包含普通说明、模板、示例、格式规则；"
            "命令示例必须是非执行性质，优先用 text code block。"
        )
    else:
        body_rules = (
            "body 可包含普通说明、工作流、bash/json/markdown code block、示例和注意事项；"
            "所有 code fence 必须闭合。reference body 不要重新定义 scripts/*.py 的 capability/runtime/tool 边界。"
        )
    return [
        {"role": "system", "content": "你是 Markdown body_region 生成器。只输出正文，不输出 YAML frontmatter。"},
        {"role": "user", "content": (
            f"为 {file_path} 生成 body_region。\n"
            f"Skill 名称：{skill_name}\n职责：{purpose}\n\n"
            "已校验 metadata_region：\n<<<METADATA_REGION\n"
            f"{metadata_region}\n"
            "METADATA_REGION\n\n"
            f"{body_rules}\n"
            "只输出 body_region，不输出 frontmatter。\n\n蓝图：\n"
            f"{(blueprint_text or '')[:8000]}\n\n"
            f"{contract_context}"
        )},
    ]


async def _generate_markdown_initial_regions(
    *,
    file_path: str,
    skill_name: str,
    purpose: str,
    blueprint_text: str,
    model: str,
    requirement_graph: dict[str, Any] | None = None,
    workflow_allocation_summary: str = "",
    final_outputs: list[Any] | None = None,
) -> str:
    """Generate .md files as metadata/body regions with metadata rewrite retry."""

    metadata_region = ""
    metadata_failures: list[dict[str, Any]] = []
    retry_limit = _first_round_repair_limit("hard_format")
    for retry_index in range(0, retry_limit + 1):
        if retry_index == 0:
            messages = _build_markdown_initial_region_prompt(
                file_path=file_path,
                skill_name=skill_name,
                purpose=purpose,
                blueprint_text=blueprint_text,
                region="metadata_region",
                requirement_graph=requirement_graph,
                workflow_allocation_summary=workflow_allocation_summary,
                final_outputs=final_outputs,
            )
            prompt_variant = "generate_markdown_metadata_region"
        else:
            messages = _build_markdown_region_rewrite_prompt(
                file_path=file_path,
                skill_name=skill_name,
                blueprint_text=blueprint_text,
                deterministic_error=json.dumps(metadata_failures, ensure_ascii=False, indent=2, default=str),
                current_content=merge_markdown_regions(metadata_region, ""),
                region="metadata_region",
            )
            prompt_variant = "rewrite_markdown_metadata_region"

        metadata_region = await _complete_creator_file_generation(
            messages=messages,
            model=model,
            skill_name=skill_name,
            file_path=file_path,
            prompt_variant=prompt_variant,
            retry_index=retry_index,
        )
        metadata_failures = [
            failure for failure in detect_markdown_hard_format_failures(
                file_path,
                merge_markdown_regions(metadata_region, "# temporary body\n"),
                require_frontmatter=(file_path == "SKILL.md"),
            )
            if markdown_failure_region(failure) == "metadata_region"
        ]
        if not metadata_failures:
            break
    else:
        raise FileGenerationStageError(
            source="hard_format",
            layer="hard_format",
            detail=json.dumps(metadata_failures, ensure_ascii=False, default=str),
        )

    body_region = await _complete_creator_file_generation(
        messages=_build_markdown_initial_region_prompt(
            file_path=file_path,
            skill_name=skill_name,
            purpose=purpose,
            blueprint_text=blueprint_text,
            region="body_region",
            metadata_region=metadata_region,
            requirement_graph=requirement_graph,
            workflow_allocation_summary=workflow_allocation_summary,
            final_outputs=final_outputs,
        ),
        model=model,
        skill_name=skill_name,
        file_path=file_path,
        prompt_variant="generate_markdown_body_region",
        retry_index=0,
    )
    return merge_markdown_regions(metadata_region, body_region)


def _markdown_warning_error_type(file_path: str, default: str = "md_format_warning") -> str:
    if file_path.startswith("references/"):
        return "reference_content_warning"
    return default


def _build_strict_script_source_only_regeneration_prompt(
    *,
    file_path: str,
    skill_name: str,
    purpose: str,
    blueprint_text: str,
    role: str | None,
    skill_plan_entry: dict[str, Any] | None,
    deterministic_error: str,
    previous_content: str,
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "你是 scripts/* 单文件源码生成器。必须直接重新生成完整单文件脚本源码。"
                "只输出目标脚本源码；不要 Markdown；不要 ``` fence；不要解释；不要多个版本；"
                "不要 Wait / Actually / Let me correct 自我修正；不要文件路径标题；不要多文件包；"
                "不要把旧内容做 patch；不要输出 diff/JSON；不要输出 old_lines/new_lines。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"文件路径：{file_path}\n"
                f"Skill 名称：{skill_name}\n"
                f"角色：{role or ''}\n"
                f"用途：{purpose or ''}\n\n"
                "上一次生成被判定为 scripts/* raw source 格式失败；这不是业务语义 repair，必须重新生成。\n"
                "失败信息：\n"
                f"{deterministic_error}\n\n"
                "硬性输出要求：\n"
                "- 只输出目标脚本源码。\n"
                "- 不要 Markdown。\n"
                "- 不要 ``` fence。\n"
                "- 不要解释。\n"
                "- 不要多个版本。\n"
                "- 不要 Wait / Actually / Let me correct 自我修正。\n"
                "- 不要文件路径标题。\n"
                "- 不要多文件包。\n"
                "- 不要把旧内容做 patch。\n"
                "- 直接重新生成完整单文件脚本源码。\n\n"
                "SkillPlanEntry：\n"
                f"{json.dumps(skill_plan_entry or {}, ensure_ascii=False, default=str)[:8000]}\n\n"
                "蓝图上下文：\n"
                f"{(blueprint_text or '')[:12000]}\n\n"
                "上一轮错误内容仅供避免重复格式错误，不要 patch：\n"
                "<<<PREVIOUS_CONTENT\n"
                f"{(previous_content or '')[:12000]}\n"
                "PREVIOUS_CONTENT\n"
            ),
        },
    ]


@router.post("/generate-file")
async def generate_file(request: GenerateFileRequest):
    """Generate one Creator file and stream it back as SSE.

    Important:
    - This endpoint must not write files to disk.
    - The frontend expects streamed content and then calls /write-file.
    - assets/** are upload-only and must never be generated by model.
    - SKILL.md is returned as runtime instructions; post-write E2E validates command extraction, script execution, stdout, and artifacts.
    - references/*.md may omit YAML frontmatter; if present it must use ordinary document metadata only.
    """
    skill_name = _validate_skill_name(request.skill_name)
    _validate_file_path(request.file_path)

    if request.file_path.startswith("assets/") and (request.skill_plan_entry or {}).get("asset_source") != "bundled":
        raise HTTPException(
            status_code=400,
            detail=f"{request.file_path} 属于 assets 静态素材目录；只有 source=bundled 的预置静态资源可由 Creator 生成，source=user_upload 必须上传。",
        )

    # request.skill_plan_entry 来自前端请求态，只能作为 hint，不能作为唯一可信合同。
    # 这里不再因为 outputs/artifact_contract/file_kind 不完整而在生成循环外 422；
    # 真正的单文件合同会在 event_stream 内由后端基于 file_path、role、purpose、SKILL.md/blueprint 重新归一化。
    # 只有明确的 path 冲突才属于不可恢复请求错误。
    raw_skill_plan_entry = request.skill_plan_entry if isinstance(request.skill_plan_entry, dict) else {}
    raw_entry_path = str(raw_skill_plan_entry.get("path") or "").strip()
    if request.file_path.startswith("scripts/") and raw_entry_path and raw_entry_path != request.file_path:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "contract_path_mismatch",
                "severity": "user_warning",
                "source": "generator",
                "path": request.file_path,
                "field": "skill_plan_entry.path",
                "message": f"skill_plan_entry.path={raw_entry_path} 与当前 file_path={request.file_path} 不一致。",
            },
        )

    async def event_stream():
        try:
            route = route_creator_file_model(
                file_path=request.file_path,
                purpose=request.purpose,
                requested_model=request.model,
            )
            _log_creator_model_usage(
                phase="generate.route",
                skill_name=skill_name,
                file_path=request.file_path,
                route=route,
            )

            effective_skill_plan_entry = request.skill_plan_entry if isinstance(request.skill_plan_entry, dict) else None
            if request.file_path.startswith("scripts/"):
                # 后端生成阶段重新构建 canonical entry，避免依赖前端传来的不完整 entry。
                # 这仍然是单文件合同，不做跨文件 E2E 判断。
                skill_md_for_entry = (
                    (settings.skills_path / skill_name / "SKILL.md").read_text(encoding="utf-8")
                    if (settings.skills_path / skill_name / "SKILL.md").is_file()
                    else request.blueprint_text
                )
                entry_obj = _skill_plan_entry_for_file(
                    file_path=request.file_path,
                    purpose=request.purpose,
                    blueprint_text=skill_md_for_entry or request.blueprint_text,
                    role=request.role,
                    skill_plan_entry=effective_skill_plan_entry,
                )
                effective_skill_plan_entry = dict(getattr(entry_obj, "__dict__", {}) or {})
                effective_skill_plan_entry.setdefault("path", request.file_path)
                effective_skill_plan_entry.setdefault("purpose", request.purpose)
                _, tool_blockers = _creator_tool_readiness_blockers(effective_skill_plan_entry)
                if tool_blockers:
                    yield _file_done_error_sse(
                        file_path=request.file_path,
                        role=request.role,
                        error="required tools are not ready; Creator cannot hallucinate tools",
                        error_type="tool_not_ready",
                    )
                    return

            prompt_messages = _build_generate_file_prompt(
                request.file_path,
                skill_name,
                request.purpose,
                request.blueprint_text,
                request.conversation_history,
                role=request.role,
                skill_plan_entry=effective_skill_plan_entry,
            )
            prompt_variant = "standard"
        except Exception as exc:
            logger.exception("Creator generate_file prepare failed: %s", exc)
            yield _file_done_error_sse(
                file_path=request.file_path,
                role=request.role,
                error=f"生成前准备失败：{exc}",
                error_type="prepare_failed",
            )
            return

        candidate = ""
        repair_counts_by_layer: dict[str, int] = {}
        format_retry_count = 0
        markdown_format_retry_count = 0
        business_repair_count = 0
        repair_failure_signatures: dict[str, tuple[int, str]] = {}

        try:
            if request.file_path.startswith("references/") or (Path(request.file_path).suffix.lower() in {".md", ".markdown"} and request.file_path != "SKILL.md"):
                candidate = await _generate_markdown_initial_regions(
                    file_path=request.file_path,
                    skill_name=skill_name,
                    purpose=request.purpose,
                    blueprint_text=request.blueprint_text,
                    model=route.model,
                    requirement_graph=request.requirement_graph,
                    workflow_allocation_summary=request.workflow_allocation_summary,
                    final_outputs=request.final_outputs,
                )
            else:
                candidate = await _complete_creator_file_generation(
                    messages=prompt_messages,
                    model=route.model,
                    skill_name=skill_name,
                    file_path=request.file_path,
                    prompt_variant=prompt_variant,
                    retry_index=0,
                )
        except Exception as exc:
            logger.exception("Creator generate_file initial model call failed: %s", exc)
            yield _file_done_error_sse(
                file_path=request.file_path,
                role=request.role,
                error=f"模型调用失败：{exc}",
                error_type="model_call_failed",
            )
            return

        for attempt in range(1, _MAX_FILE_REPAIR_ATTEMPTS + 1):
            try:
                if len(candidate or "") == 0:
                    raise FileGenerationStageError(
                        source="model_empty_content",
                        layer="file_generation",
                        detail="model_empty_content: 模型生成结果 content_chars=0，跳过 validator/repair；进入 prompt 降级重试。",
                    )

                candidate = _canonicalize_generated_candidate(
                    file_path=request.file_path,
                    content=candidate,
                    role=request.role,
                    skill_plan_entry=effective_skill_plan_entry,
                    skill_name=skill_name,
                    purpose=request.purpose,
                )

                content = candidate

                if request.file_path.startswith("scripts/"):
                    allowed_tools = list(resolve_tools_for_skill_plan_entry(effective_skill_plan_entry or {}).allowed_tools or [])
                    boundary_violations = _script_tool_boundary_violations(content, allowed_tools)
                    if boundary_violations:
                        first_violation = boundary_violations[0]
                        raise FileGenerationStageError(
                            source=first_violation["id"],
                            layer=first_violation["layer"],
                            detail=first_violation["message"],
                        )

                if request.file_path == "SKILL.md":
                    logger.info(
                        "[Creator][generate_file] skip SKILL.md static format/contract repair; E2E validates runtime commands file=%s content_chars=%d",
                        request.file_path,
                        len(content),
                    )
                    yield _sse({
                        "type": "file_content",
                        "status": "success",
                        "success": True,
                        "file_path": request.file_path,
                        "role": request.role,
                        "content": content,
                        "editable": True,
                        "disabled": False,
                    })
                    yield _sse({
                        "type": "file_done",
                        "status": "success",
                        "success": True,
                        "file_path": request.file_path,
                        "role": request.role,
                        "done": True,
                        "editable": True,
                        "disabled": False,
                    })
                    return

                format_stage_error = _first_round_format_stage_error(
                    file_path=request.file_path,
                    content=content,
                    role=request.role,
                    skill_plan_entry=effective_skill_plan_entry,
                )
                if format_stage_error is not None:
                    raise format_stage_error

                try:

                    if request.file_path == "SKILL.md":
                        _raise_file_contract_failures(validate_file_contract(
                            file_path=request.file_path,
                            content=content,
                            blueprint_text=request.blueprint_text,
                            skill_plan_entry=effective_skill_plan_entry,
                        ))

                        _validate_skill_md_against_existing_files(
                            skill_name,
                            content,
                            blueprint_text=request.blueprint_text,
                            require_existing=False,
                        )

                        await _validate_skill_md_blueprint_alignment(
                            skill_name=skill_name,
                            content=content,
                            blueprint_text=request.blueprint_text,
                            skill_plan_entry=effective_skill_plan_entry,
                            requirement_graph=request.requirement_graph,
                            model=request.model or route.model,
                        )

                    elif request.file_path.startswith("references/"):
                        reference_entry = {
                            **(effective_skill_plan_entry or {}),
                            "purpose": request.purpose or request.blueprint_text,
                        }
                        reference_results = validate_file_contract(
                            file_path=request.file_path,
                            content=content,
                            blueprint_text=request.blueprint_text,
                            skill_plan_entry=reference_entry,
                        )
                        if any((not result.passed and result.id == "reference.no_placeholder_phrases") for result in reference_results):
                            patched_reference = _sanitize_reference_placeholders(content)
                            if patched_reference != content:
                                patched_results = validate_file_contract(
                                    file_path=request.file_path,
                                    content=patched_reference,
                                    blueprint_text=request.blueprint_text,
                                    skill_plan_entry=reference_entry,
                                )
                                if not any(not result.passed for result in patched_results):
                                    content = patched_reference
                                    reference_results = patched_results
                        _raise_file_contract_failures(reference_results)

                    elif request.file_path.startswith("scripts/"):
                        pass

                except Exception as exc:
                    raise _stage_error_from_exception("responsibility_stage", exc, default_layer="responsibility_stage") from exc

                if request.file_path.startswith("scripts/"):
                    try:
                        skill_md = (
                            (settings.skills_path / skill_name / "SKILL.md").read_text(encoding="utf-8")
                            if (settings.skills_path / skill_name / "SKILL.md").is_file()
                            else ""
                        )

                        entry = _skill_plan_entry_for_file(
                            file_path=request.file_path,
                            blueprint_text=skill_md,
                            role=request.role,
                            skill_plan_entry=effective_skill_plan_entry,
                        )

                        entry_requirements = []
                        persisted_graph = _load_persisted_requirement_graph(skill_name)
                        if persisted_graph is not None:
                            entry_requirements = [req for req in persisted_graph.requirements if req.target_file == request.file_path]
                        if not entry_requirements and isinstance(effective_skill_plan_entry, dict):
                            entry_requirements = effective_skill_plan_entry.get("requirements") or []
                        workflow_allocation_summary = _load_workflow_allocation_summary(skill_name)
                        responsibility_review = await _run_script_responsibility_review(
                            file_path=request.file_path,
                            script_content=content,
                            skill_plan_entry=entry,
                            requirements=entry_requirements,
                            deterministic_issues=[],
                            requested_model=request.model or route.model,
                            review_context={
                                "phase": "RESPONSIBILITY_STAGE",
                                "policy": "只判断当前文件职责是否完成。",
                                "blueprint_text": request.blueprint_text,
                                "purpose_short_contract": getattr(entry, "purpose", request.purpose),
                                "workflow_allocation_summary": workflow_allocation_summary,
                                "trial_stdout": "第一轮责任审查在局部 patch 前可能尚未执行试运行；如为空，不得把缺 stdout 当接口失败。",
                                "artifact_info": "第一轮责任审查只用 artifact 信息辅助判断语义交付；真实存在性由运行/E2E 检查。",
                            },
                        )

                        if not responsibility_review.get("passed"):
                            failure_type = str(responsibility_review.get("failure_type") or "script_requirement_failed")
                            if failure_type in {"script_requirement_validator_error", "script_requirement_validator_incomplete"}:
                                raise FileGenerationStageError(
                                    source=failure_type,
                                    layer=failure_type,
                                    detail=json.dumps(responsibility_review, ensure_ascii=False, default=str),
                                )
                            issues = (
                                responsibility_review.get("issues")
                                if isinstance(responsibility_review.get("issues"), list)
                                else []
                            )
                            raise ScriptFunctionalValidationError(
                                issues or [{
                                    "id": "script_responsibility.failed",
                                    "failed_file": request.file_path,
                                    "failed_function": "current script",
                                    "code_region": "current file responsibility logic",
                                    "reason": "职责审查模型判定当前脚本没有完成自身职责。",
                                    "minimal_edit": str(
                                        responsibility_review.get("repair_instructions")
                                        or "只修改当前脚本中未完成职责的业务逻辑。"
                                    ),
                                    "allowed_scope": "只允许修改当前脚本职责实现区域。",
                                    "repair_boundary": "当前文件职责实现区域。",
                                    "details": {"review": responsibility_review},
                                }],
                                layer="responsibility",
                            )

                    except ScriptFunctionalValidationError as exc:
                        raise _stage_error_for_script_functional(exc) from exc
                    except Exception as exc:
                        raise _stage_error_from_exception(
                            "script_responsibility",
                            exc,
                            default_layer="responsibility",
                        ) from exc

                logger.info(
                    "[Creator][generate_file] validation passed file=%s role=%s content_chars=%d",
                    request.file_path,
                    request.role or "",
                    len(content),
                )

                yield _sse({
                    "type": "file_content",
                    "status": "success",
                    "success": True,
                    "file_path": request.file_path,
                    "role": request.role,
                    "content": content,
                    "editable": True,
                    "disabled": False,
                })

                yield _sse({
                    "type": "file_done",
                    "status": "success",
                    "success": True,
                    "file_path": request.file_path,
                    "role": request.role,
                    "done": True,
                    "editable": True,
                    "disabled": False,
                })

                return

            except Exception as exc:
                stage_error = (
                    exc
                    if isinstance(exc, FileGenerationStageError)
                    else _stage_error_from_exception("content_review", exc, default_layer="content_review")
                )
                deterministic_error = str(stage_error)
                if request.file_path == "SKILL.md":
                    yield _file_done_error_sse(
                        file_path=request.file_path,
                        role=request.role,
                        error=(
                            "SKILL.md 生成失败；Creator 不再对 SKILL.md 做 frontmatter/Markdown 静态格式 hard gate 或 LLM repair。"
                            f"原始错误：{deterministic_error}"
                        ),
                        error_type="skill_md_generation_failed",
                        content=candidate or "",
                        recoverable=True,
                    )
                    return
                error_source = stage_error.source
                error_layer = f"{stage_error.source}:{stage_error.layer}"
                if is_generation_format_error(stage_error) and request.file_path.startswith("scripts/"):
                    format_retry_count += 1
                elif is_markdown_hard_format_error(stage_error) and _is_markdown_creator_file(request.file_path):
                    markdown_format_retry_count += 1
                else:
                    business_repair_count += 1
                    repair_counts_by_layer[error_layer] = repair_counts_by_layer.get(error_layer, 0) + 1
                failure_signature = _structured_failure_signature(stage_error, deterministic_error)
                candidate_digest = hashlib.sha256((candidate or "").encode("utf-8")).hexdigest()
                signature_key = f"{error_layer}:{failure_signature}"
                previous_repeat_count, previous_digest = repair_failure_signatures.get(signature_key, (0, ""))
                repeated_same_failure = previous_repeat_count >= 1
                repeated_same_candidate = previous_digest == candidate_digest
                repair_failure_signatures[signature_key] = (previous_repeat_count + 1, candidate_digest)

                if is_script_raw_source_format_error(stage_error) and request.file_path.startswith("scripts/"):
                    layer_limit = _first_round_repair_limit("content_review")
                    if format_retry_count > layer_limit:
                        yield _file_done_error_sse(
                            file_path=request.file_path,
                            role=request.role,
                            error=(
                                f"脚本源码格式重新生成失败：已重新生成 {layer_limit} 次仍未通过。"
                                f"最后错误：{deterministic_error}"
                            ),
                            error_type="script_source_format_regenerate_failed",
                            content=candidate or "",
                            recoverable=True,
                        )
                        return

                    yield _sse({
                        "type": "validation",
                        "status": "regenerating",
                        "success": False,
                        "file_path": request.file_path,
                        "role": request.role,
                        "validation": {
                            "status": "regenerating",
                            "attempt": format_retry_count,
                            "format_retry_count": format_retry_count,
                            "business_repair_count": business_repair_count,
                            "source": error_source,
                            "layer": stage_error.layer,
                            "error": deterministic_error,
                        },
                    })

                    next_messages = _build_strict_script_source_only_regeneration_prompt(
                        file_path=request.file_path,
                        skill_name=skill_name,
                        purpose=request.purpose,
                        blueprint_text=request.blueprint_text,
                        role=request.role,
                        skill_plan_entry=effective_skill_plan_entry,
                        deterministic_error=deterministic_error,
                        previous_content=candidate or "",
                    )
                    candidate = await _complete_creator_file_generation(
                        messages=next_messages,
                        model=route.model,
                        skill_name=skill_name,
                        file_path=request.file_path,
                        prompt_variant="strict_source_only_regeneration",
                        retry_index=format_retry_count - 1,
                    )
                    prompt_messages = next_messages
                    prompt_variant = "strict_source_only_regeneration"
                    continue

                if error_source == "model_empty_content":
                    empty_retry_index = repair_counts_by_layer[error_layer]
                    if empty_retry_index >= len(_EMPTY_GENERATION_PROMPT_VARIANTS):
                        prompt_messages = _ensure_user_visible_task_message(prompt_messages)
                        logger.warning(
                            "[Creator][generate_file][model_empty_content] skill=%s file_path=%s model=%s prompt_variant=%s retry_index=%d prompt_chars=%d message_roles=%s system_chars=%d user_chars=%d content_len=%d finish_reason=%s variants=%s error_type=%s",
                            skill_name,
                            request.file_path,
                            route.model,
                            prompt_variant,
                            empty_retry_index,
                            _prompt_chars(prompt_messages),
                            json.dumps(_message_role_counts(prompt_messages), ensure_ascii=False, sort_keys=True),
                            _message_role_chars(prompt_messages, "system"),
                            _message_role_chars(prompt_messages, "user"),
                            len(candidate or ""),
                            "unknown",
                            "->".join(_EMPTY_GENERATION_PROMPT_VARIANTS),
                            "model_empty_content",
                        )
                        yield _file_done_error_sse(
                            file_path=request.file_path,
                            role=request.role,
                            error="文件内容生成失败：same-model prompt degradation 已尝试 standard -> simplified -> minimal 后仍为空。",
                            error_type="model_empty_content",
                            content=candidate or "",
                            recoverable=True,
                        )
                        return

                    next_variant = _EMPTY_GENERATION_PROMPT_VARIANTS[empty_retry_index]
                    next_messages = (
                        _build_script_generate_file_prompt_variant(
                            file_path=request.file_path,
                            skill_name=skill_name,
                            purpose=request.purpose,
                            blueprint_text=request.blueprint_text,
                            role=request.role,
                            skill_plan_entry=effective_skill_plan_entry,
                            variant=next_variant,
                        )
                        if request.file_path.startswith("scripts/")
                        else prompt_messages
                    )

                    yield _sse({
                        "type": "validation",
                        "status": "regenerating",
                        "success": False,
                        "file_path": request.file_path,
                        "role": request.role,
                        "validation": {
                            "status": "regenerating",
                            "attempt": attempt,
                            "source": error_source,
                            "layer": stage_error.layer,
                            "error": deterministic_error,
                        },
                    })

                    candidate = await _complete_creator_file_generation(
                        messages=next_messages,
                        model=route.model,
                        skill_name=skill_name,
                        file_path=request.file_path,
                        prompt_variant=next_variant,
                        retry_index=empty_retry_index,
                    )
                    prompt_messages = next_messages
                    prompt_variant = next_variant
                    continue
                if error_source in {"script_requirement_validator_error", "script_requirement_validator_incomplete"}:
                    static_blockers: list[dict[str, Any]] = []
                    if request.file_path.startswith("scripts/"):
                        try:
                            static_skill_md = (
                                (settings.skills_path / skill_name / "SKILL.md").read_text(encoding="utf-8")
                                if (settings.skills_path / skill_name / "SKILL.md").is_file()
                                else ""
                            )
                            static_entry = _skill_plan_entry_for_file(
                                file_path=request.file_path,
                                blueprint_text=static_skill_md,
                                role=request.role,
                                skill_plan_entry=effective_skill_plan_entry,
                            )
                            static_requirements: list[Any] = []
                            static_graph = _load_persisted_requirement_graph(skill_name)
                            if static_graph is not None:
                                static_requirements = [req for req in static_graph.requirements if req.target_file == request.file_path]
                            if not static_requirements and isinstance(effective_skill_plan_entry, dict):
                                static_requirements = effective_skill_plan_entry.get("requirements") or []
                            static_blockers = _runtime_tool_contract_static_blockers(
                                candidate or "",
                                static_entry,
                                static_requirements,
                            )
                            static_blockers += _detect_script_responsibility_static_blockers(
                                candidate or "",
                                static_entry,
                                static_requirements,
                            )
                        except Exception:
                            static_blockers = []
                    if not static_blockers:
                        # Single-file production validation failures must still enter
                        # the patch repair loop.  A validator error/incomplete review
                        # is not a reason to return a terminal error to the frontend;
                        # repair should make the current script's responsibility path
                        # explicit enough for the next validation round.
                        static_blockers = [{
                            "id": error_source,
                            "failed_file": request.file_path,
                            "failed_function": "single_file_production_validation",
                            "code_region": "current file responsibility implementation",
                            "reason": (
                                "Single-file production validator failed or returned incomplete checks; "
                                "auto-repair the current file instead of returning directly to the frontend."
                            ),
                            "missing_evidence": [
                                "validator-readable current-file responsibility evidence",
                                "input/tool result participates in constructed output",
                            ],
                            "minimal_edit": (
                                "只修改当前文件职责实现区域，让职责证据更明确。"
                            ),
                            "allowed_scope": "current file responsibility implementation",
                            "details": {"validator_error": deterministic_error},
                        }]
                    stage_error = FileGenerationStageError(
                        source="script_requirement_failed",
                        layer="responsibility",
                        detail=json.dumps({"issues": static_blockers}, ensure_ascii=False, default=str),
                        original=ScriptFunctionalValidationError(static_blockers, layer="responsibility"),
                    )
                    deterministic_error = str(stage_error)
                    error_source = stage_error.source
                    error_layer = f"{stage_error.source}:{stage_error.layer}"
                    repair_counts_by_layer[error_layer] = repair_counts_by_layer.get(error_layer, 0) + 1

                if is_markdown_hard_format_error(stage_error) and _is_markdown_creator_file(request.file_path):
                    layer_limit = _first_round_repair_limit(error_source)

                    if markdown_format_retry_count > layer_limit:
                        yield _file_done_error_sse(
                            file_path=request.file_path,
                            role=request.role,
                            error=(
                                f"Markdown 格式修复失败：已区域重写 {layer_limit} 轮仍未通过。"
                                f"最后错误：{deterministic_error}"
                            ),
                            error_type=_markdown_warning_error_type(request.file_path),
                            content=candidate or "",
                            recoverable=True,
                        )
                        return

                    yield _sse({
                        "type": "validation",
                        "status": "format_region_rewrite",
                        "success": False,
                        "file_path": request.file_path,
                        "role": request.role,
                        "editable": True,
                        "disabled": False,
                        "validation": {
                            "status": "format_region_rewrite",
                            "attempt": markdown_format_retry_count,
                            "markdown_format_retry_count": markdown_format_retry_count,
                            "business_repair_count": business_repair_count,
                            "source": error_source,
                            "layer": stage_error.layer,
                            "error": deterministic_error,
                        },
                    })

                    failed_region = markdown_failure_region(deterministic_error)
                    rewrite_messages = _build_markdown_region_rewrite_prompt(
                        file_path=request.file_path,
                        skill_name=skill_name,
                        blueprint_text=request.blueprint_text,
                        deterministic_error=deterministic_error,
                        current_content=candidate or "",
                        region=failed_region,
                    )

                    rewritten_region = await _complete_creator_file_generation(
                        messages=rewrite_messages,
                        model=route.model,
                        skill_name=skill_name,
                        file_path=request.file_path,
                        prompt_variant=f"rewrite_markdown_{failed_region}",
                        retry_index=markdown_format_retry_count - 1,
                    )
                    candidate = _merge_markdown_region_rewrite(candidate or "", rewritten_region, failed_region)
                    continue
                layer_limit = _first_round_repair_limit(error_source)
                if repair_counts_by_layer[error_layer] > layer_limit:
                    yield _file_done_error_sse(
                        file_path=request.file_path,
                        role=request.role,
                        error=(
                            f"文件内容生成失败：同一阶段/层 {error_layer} "
                            f"已修复 {layer_limit} 次仍未通过。最后错误：{deterministic_error}"
                        ),
                        error_type=(
                            _markdown_warning_error_type(request.file_path, default="md_content_repair_warning")
                            if _is_markdown_creator_file(request.file_path)
                            else "repair_layer_limit_exceeded"
                        ),
                        content=candidate or "",
                        recoverable=True,
                    )
                    return

                targeted_repair = _targeted_generated_file_repair_instructions(
                    file_path=request.file_path,
                    deterministic_error=deterministic_error,
                )

                if request.file_path == "SKILL.md":
                    targeted_repair += (
                        "\n\n额外修复目标：SKILL.md 必须与蓝图意图一致。"
                        "不得新增蓝图外能力、脚本、reference 或 asset；"
                        "必须覆盖 required_capabilities；不得包含 forbidden_capabilities；"
                        "assets/** 只能描述为上传/静态素材。"
                    )

                if request.file_path.startswith("references/"):
                    targeted_repair += (
                        "\n\n额外修复目标：references/*.md 必须是一份正式 Markdown 参考资料文档。"
                        "必须包含 YAML frontmatter，且 title/description 非空；"
                        "frontmatter 顶层只允许 title、description、source、license、metadata。"
                        "frontmatter 后必须有 Markdown 正文，正文必须包含标题，并提供可复用参考内容。"
                        "不要输出聊天式澄清问题、确认选项、状态说明或计划询问。"
                    )

                contract_text = _build_generated_file_contract_text(
                    request.file_path,
                    request.blueprint_text,
                    request.purpose,
                    role=request.role,
                    skill_plan_entry=effective_skill_plan_entry,
                )

                passed_checks_text = ""
                failed_checks_text = ""
                original_exc = stage_error.original
                if isinstance(original_exc, ContractValidationError):
                    passed_checks_text = _format_contract_checks(original_exc.results, passed=True)
                    failed_checks_text = _format_contract_checks(original_exc.results, passed=False)

                if attempt >= _MAX_FILE_REPAIR_ATTEMPTS:
                    error_message = (
                        f"文件内容生成失败：已自动修复 {attempt - 1} 次仍未通过。"
                        f"最后错误：{deterministic_error}"
                    )

                    logger.info(
                        "[Creator][generate_file] validation failed finally file=%s role=%s attempts=%d error=%s",
                        request.file_path,
                        request.role or "",
                        attempt,
                        deterministic_error,
                    )

                    yield _file_done_error_sse(
                        file_path=request.file_path,
                        role=request.role,
                        error=error_message,
                        error_type="repair_limit_exceeded",
                        content=candidate or "",
                        recoverable=True,
                    )
                    return

                yield _sse({
                    "type": "validation",
                    "status": "repairing",
                    "success": False,
                    "file_path": request.file_path,
                    "role": request.role,
                    "validation": {
                        "status": "repairing",
                        "attempt": attempt,
                        "source": error_source,
                        "layer": stage_error.layer,
                        "error": deterministic_error,
                    },
                })

                try:
                    repair_mode = "strict_patch" if repeated_same_failure else _repair_mode_for_first_round(
                        source=error_source,
                        file_path=request.file_path,
                        attempt=attempt,
                    )

                    if (
                        error_source in {"script_requirement_failed", "script_functional", "script_responsibility"}
                        and request.file_path.startswith("scripts/")
                    ):
                        responsibility_issues = []
                        original_exc = stage_error.original
                        if isinstance(original_exc, ScriptFunctionalValidationError):
                            responsibility_issues = original_exc.issues
                        feedback = (
                            "RESPONSIBILITY_PATCH_STAGE\n"
                            "只根据 RESPONSIBILITY_STAGE 明确给出的当前文件职责缺失做最小修改。\n\n"
                            "当前文件职责缺失说明：\n"
                            f"{json.dumps(responsibility_issues, ensure_ascii=False, indent=2, default=str)}"
                        )
                        passed_checks_text = ""
                        failed_checks_text = ""
                        contract_text = ""
                        targeted_repair = "只在当前文件内做满足职责缺失的最小功能实现修改。"
                    else:
                        validator_report = await _run_generated_file_validator_round(
                            file_path=request.file_path,
                            content=candidate,
                            deterministic_error=deterministic_error,
                            requested_model=route.model,
                            targeted_repair=targeted_repair,
                            contract_text=contract_text,
                            passed_checks_text=passed_checks_text,
                            failed_checks_text=failed_checks_text,
                            repair_mode=repair_mode,
                        )

                        feedback = _format_file_validator_feedback(
                            deterministic_error,
                            validator_report,
                            targeted_repair=targeted_repair,
                            file_path=request.file_path,
                        )

                    repaired_candidate = await _repair_generated_file_with_feedback(
                        prompt_messages=prompt_messages,
                        model=route.model,
                        file_path=request.file_path,
                        previous_content=candidate,
                        validation_error=feedback,
                        targeted_repair=targeted_repair,
                        contract_text=contract_text,
                        passed_checks_text=passed_checks_text,
                        failed_checks_text=failed_checks_text,
                        repair_mode=repair_mode,
                        skill_plan_entry=effective_skill_plan_entry,
                    )
                    repaired_candidate = _canonicalize_generated_candidate(
                        file_path=request.file_path,
                        content=repaired_candidate,
                        role=request.role,
                        skill_plan_entry=effective_skill_plan_entry,
                        skill_name=skill_name,
                        purpose=request.purpose,
                    )

                except Exception as repair_exc:
                    if (
                        _is_markdown_creator_file(request.file_path)
                    ) and (
                        "hard_format_regression" in str(repair_exc)
                        or "hard_format failure must not enter localized patch repair" in str(repair_exc)
                        or "model_patch_allowed" in str(repair_exc)
                    ):
                        stage_error = FileGenerationStageError(
                            source="hard_format",
                            layer="hard_format",
                            detail=str(repair_exc),
                        )
                        deterministic_error = str(stage_error)
                        markdown_format_retry_count += 1
                        layer_limit = _first_round_repair_limit("hard_format")
                        if markdown_format_retry_count > layer_limit:
                            yield _file_done_error_sse(
                                file_path=request.file_path,
                                role=request.role,
                                error=(
                                    f"Markdown 格式修复失败：已区域重写 {layer_limit} 轮仍未通过。"
                                    f"最后错误：{deterministic_error}"
                                ),
                                error_type=_markdown_warning_error_type(request.file_path),
                                content=candidate or "",
                                recoverable=True,
                            )
                            return
                        yield _sse({
                            "type": "validation",
                            "status": "format_region_rewrite",
                            "success": False,
                            "file_path": request.file_path,
                            "role": request.role,
                            "editable": True,
                            "disabled": False,
                            "validation": {
                                "status": "format_region_rewrite",
                                "attempt": markdown_format_retry_count,
                                "markdown_format_retry_count": markdown_format_retry_count,
                                "business_repair_count": business_repair_count,
                                "source": "hard_format",
                                "layer": "hard_format",
                                "error": deterministic_error,
                            },
                        })
                        failed_region = markdown_failure_region(deterministic_error)
                        rewrite_messages = _build_markdown_region_rewrite_prompt(
                            file_path=request.file_path,
                            skill_name=skill_name,
                            blueprint_text=request.blueprint_text,
                            deterministic_error=deterministic_error,
                            current_content=candidate or "",
                            region=failed_region,
                        )
                        rewritten_region = await _complete_creator_file_generation(
                            messages=rewrite_messages,
                            model=route.model,
                            skill_name=skill_name,
                            file_path=request.file_path,
                            prompt_variant=f"rewrite_markdown_{failed_region}",
                            retry_index=markdown_format_retry_count - 1,
                        )
                        candidate = _merge_markdown_region_rewrite(candidate or "", rewritten_region, failed_region)
                        continue
                    logger.exception(
                        "[Creator][generate_file][repair_failed] file=%s source=%s layer=%s attempt=%d",
                        request.file_path,
                        error_source,
                        stage_error.layer,
                        attempt,
                    )
                    yield _file_done_error_sse(
                        file_path=request.file_path,
                        role=request.role,
                        error=f"文件内容修复阶段异常：{type(repair_exc).__name__}: {repair_exc}",
                        error_type=(
                            _markdown_warning_error_type(request.file_path, default="md_format_warning")
                            if request.file_path.startswith("references/")
                            else "repair_failed"
                        ),
                        content=candidate or "",
                        recoverable=True,
                    )
                    return
                if request.file_path.startswith("scripts/") and repaired_candidate.strip() == (candidate or "").strip():
                    logger.warning(
                        "[Creator][generate_file][repair_noop] file=%s source=%s layer=%s attempt=%d repair_mode=%s",
                        request.file_path,
                        error_source,
                        stage_error.layer,
                        attempt,
                        repair_mode,
                    )

                    if repair_mode == "strict_patch":
                        if error_source in {"script_requirement_validator_error", "script_requirement_validator_incomplete"}:
                            candidate = repaired_candidate
                            continue
                        yield _file_done_error_sse(
                            file_path=request.file_path,
                            role=request.role,
                            error=(
                                "repair_noop_with_same_failure_signature: strict_patch 返回 no-op；"
                                f" failure_signature={failure_signature} error={deterministic_error}"
                            ),
                            error_type="repair_noop_with_same_failure_signature",
                            content=candidate or "",
                            recoverable=True,
                        )
                        return

                    repaired_candidate = await _repair_generated_file_with_feedback(
                        prompt_messages=prompt_messages,
                        model=route.model,
                        file_path=request.file_path,
                        previous_content=candidate,
                        validation_error=(
                            feedback
                            + "\n\n上一轮 repair 没有改变文件内容，这是无效修复。"
                            + "现在必须执行 strict_patch：只修改 deterministic_error / localization 指出的失败行附近代码，"
                            + "必须落实 localization.minimal_edit，不得保留 traceback 指出的错误表达式。"
                        ),
                        targeted_repair=targeted_repair,
                        contract_text=contract_text,
                        passed_checks_text=passed_checks_text,
                        failed_checks_text=failed_checks_text,
                        repair_mode="strict_patch",
                        skill_plan_entry=effective_skill_plan_entry,
                    )
                    repaired_candidate = _canonicalize_generated_candidate(
                        file_path=request.file_path,
                        content=repaired_candidate,
                        role=request.role,
                        skill_plan_entry=effective_skill_plan_entry,
                        skill_name=skill_name,
                        purpose=request.purpose,
                    )

                candidate = repaired_candidate

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )

@router.post("/write-file", response_model=WriteFileResponse)
async def write_file(request: WriteFileRequest):
    """Write generated content to disk.

    /write-file 只落盘：
    - 不做 content contract 校验；
    - 不做 script responsibility review；
    - 不做 script smoke trial run；
    - 不做 SKILL.md 蓝图一致性或跨文件检查；
    - 不重新 canonicalize，避免前端展示内容与落盘内容不一致。

    SKILL.md 写入后只作为运行说明来源；后续 validate-skill/E2E 只验证 command block 提取、脚本执行、stdout/artifact 平台协议。
    """
    skill_name = _validate_skill_name(request.skill_name)
    _validate_file_path(request.file_path)

    if request.file_path.startswith("assets/") and (request.skill_plan_entry or {}).get("asset_source") != "bundled":
        raise HTTPException(
            status_code=400,
            detail=f"{request.file_path} 属于 assets 静态素材目录；只有 source=bundled 的预置静态资源可写入，source=user_upload 必须通过 /api/creator/upload-asset 上传。",
        )

    skill_dir = settings.skills_path / skill_name
    if not skill_dir.exists():
        raise HTTPException(status_code=404, detail=f"Skill 不存在：{skill_name}")

    content = request.content or ""

    target_path = skill_dir / request.file_path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(content, encoding="utf-8")

    return WriteFileResponse(
        success=True,
        path=str(target_path),
        bytes=len(content.encode("utf-8")),
        message=f"已写入：{request.file_path}",
    )

def _validate_skill_package_smoke(
    skill_name: str,
    *,
    mode: str = "trial",
    external_context: dict[str, Any] | None = None,
    requested_model: str | None = None,
) -> list[str]:
    """Strict end-to-end workflow validation."""
    return validate_workflow_e2e(
        skill_name,
        external_context=external_context,
        requested_model=requested_model,
    )

def _external_context_from_skill_action_request(request: SkillActionRequest) -> dict[str, Any]:
    """Build external context for Creator E2E.

    validate-skill / package-skill 经常不是从真实用户运行入口触发，
    request.messages 可能为空。如果不补一个非空通用输入，
    SKILL.md 中 {{user_request}} 会被渲染成空字符串，进而让
    argument-effect review 误判脚本没有消费参数。

    这里补的是平台外部输入 envelope，不是业务字段名：
    - user_request
    - input
    - text
    - payload

    不补 theme/topic/story_text 这类业务字段。
    """
    context = build_creator_external_input_context(
        messages=request.messages,
        input_files=request.input_files,
        fields=request.fields,
        options=request.options,
    )

    if not isinstance(context, dict):
        context = {}

    # 优先从真实 request.messages 取最后一条用户文本。
    user_text = ""
    for message in reversed(request.messages or []):
        if not isinstance(message, dict):
            continue
        if message.get("role") != "user":
            continue
        value = str(message.get("content") or "").strip()
        if value:
            user_text = value
            break

    # 如果没有真实用户文本，使用通用 E2E 测试输入。
    # 注意：这是平台外部 envelope 的测试值，不是业务字段名硬编码。
    fallback_text = (
        user_text
        or "Creator E2E 验证输入：请根据这个请求完成当前 Skill 的主要任务，"
           "内容包含中文、English words 和标点，用于验证参数传递、脚本消费和输出闭环。"
    )

    # 如果 build_creator_external_input_context 已经给了非空值，则保留。
    for key in ("user_request", "input", "text", "payload"):
        if not _json_value_non_empty(context.get(key)):
            context[key] = fallback_text

    if not isinstance(context.get("fields"), dict):
        context["fields"] = dict(request.fields or {})

    if not isinstance(context.get("options"), dict):
        context["options"] = dict(request.options or {})

    if not isinstance(context.get("input_files"), list):
        context["input_files"] = list(request.input_files or [])

    if not _json_value_non_empty(context.get("files")):
        context["files"] = list(context.get("input_files") or [])

    return context


@router.post("/validate-skill", response_model=SkillActionResponse)
async def validate_skill(request: SkillActionRequest):
    """Validate and strictly E2E-run a Skill package.

    Flow:
    1. strict SKILL.md workflow E2E execution
    2. if failed, route feedback to MD/code model and rewrite the failing file
    3. retry until success or max attempts exhausted
    """
    skill_name = _validate_skill_name(request.skill_name)

    skill_dir = settings.skills_path / skill_name
    skill_md_path = skill_dir / "SKILL.md"
    if not skill_dir.is_dir():
        result = {"success": False, "path": str(skill_dir), "message": f"Skill directory does not exist: {skill_dir}"}
    elif not skill_md_path.is_file():
        result = {"success": False, "path": str(skill_dir), "message": f"SKILL.md does not exist: {skill_md_path}"}
    else:
        result = {"success": True, "path": str(skill_dir), "message": "Skill files exist; running E2E."}

    if not result["success"]:
        return SkillActionResponse(
            success=False,
            path=result.get("path"),
            message=result["message"],
        )

    max_attempts = max(0, min(int(request.max_e2e_repair_attempts or 0), 10))
    attempt = 0
    attempts_by_target: dict[str, int] = {}
    completed_targets: set[str] = set()
    repair_logs: list[str] = []
    repair_events: list[dict[str, Any]] = []
    e2e_session = _create_e2e_session(skill_name, source_skill_dir=settings.skills_path / skill_name)

    while True:
        external_context = _external_context_from_skill_action_request(request)
        try:
            e2e_errors = validate_workflow_e2e(
                skill_name,
                external_context=external_context,
                requested_model=request.model,
                e2e_session=e2e_session,
            )
        except Exception as exc:
            logger.exception("validate-skill e2e validator crashed skill=%s", skill_name)
            e2e_errors = [
                _e2e_error(
                    target="SKILL.md",
                    layer="e2e_internal_exception",
                    message=(
                        "严格端到端工作流校验内部异常，已按校验失败返回而不是 HTTP 500。\n"
                        f"exception_type={type(exc).__name__}\n"
                        f"exception={exc}"
                    ),
                )
            ]
        if not e2e_errors:
            suffix = ""
            if repair_logs:
                suffix = "\n\n端到端自动修复记录：\n" + "\n".join(repair_logs)
            return SkillActionResponse(
                success=True,
                path=result.get("path"),
                message=result["message"] + "\n严格端到端工作流校验通过：SKILL.md 命令已按顺序真实执行，中间 JSON 边界已流转，最终 stdout 已对齐 sandbox 平台输出协议。" + suffix,
                repair_events=repair_events or e2e_session.events,
            )

        if not request.auto_repair:
            return SkillActionResponse(
                success=False,
                path=None,
                message=(
                    "严格端到端工作流校验失败：\n"
                    + "\n\n".join(e2e_errors)
                    + (
                        "\n\n端到端自动修复记录：\n" + "\n".join(repair_logs)
                        if repair_logs else ""
                    )
                ),
                repair_events=repair_events or e2e_session.events,
            )

        if any(re.search(r"^E2E_LAYER=e2e_requirement_validator_(?:error|incomplete)", err, re.M) for err in e2e_errors):
            return SkillActionResponse(
                success=False,
                path=None,
                message=(
                    "严格端到端 requirement validator 失败；这不是业务文件修复目标，请重试 validator 或切换 validator 模型：\n"
                    + "\n\n".join(e2e_errors)
                ),
                repair_events=repair_events or e2e_session.events,
            )

        target_path = _e2e_repair_target_from_errors(e2e_errors)
        if target_path in completed_targets:
            return SkillActionResponse(
                success=False,
                path=None,
                message=(
                    "严格端到端工作流校验失败：已修复目标出现同目标回归，停止重复修复：\n"
                    + "\n\n".join(e2e_errors)
                    + f"\n\n回归目标：{target_path}"
                    + (
                        "\n\n端到端自动修复记录：\n" + "\n".join(repair_logs)
                        if repair_logs else ""
                    )
                ),
                repair_events=repair_events or e2e_session.events,
            )
        if attempts_by_target.get(target_path, 0) >= max_attempts:
            return SkillActionResponse(
                success=False,
                path=None,
                message=(
                    "严格端到端工作流校验失败，且自动修复达到当前目标最大次数：\n"
                    + "\n\n".join(e2e_errors)
                    + f"\n\n自动修复目标：{target_path}"
                    + f"\n当前目标尝试次数：{attempts_by_target.get(target_path, 0)}/{max_attempts}"
                    + (
                        "\n\n端到端自动修复记录：\n" + "\n".join(repair_logs)
                        if repair_logs else ""
                    )
                ),
                repair_events=repair_events or e2e_session.events,
            )
        try:
            attempts_by_target[target_path] = attempts_by_target.get(target_path, 0) + 1
            repair_result = await _repair_existing_file_for_e2e_failure(
                skill_name=skill_name,
                target_path=target_path,
                e2e_errors=e2e_errors,
                requested_model=request.model,
                external_context=external_context,
                repair_events=repair_events,
                e2e_session=e2e_session,
            )
            attempt += 1
            status = repair_result.get("status")
            repaired_target = repair_result.get("repaired_target") or target_path
            if status == "target_changed":
                completed_targets.add(repaired_target)
                next_target = repair_result.get("next_target")
                repair_logs.append(
                    f"第 {attempt} 轮：{repaired_target} 当前目标错误已消失，失败转移到 {next_target}，继续修复下一个目标"
                )
                continue
            if status == "repaired":
                completed_targets.add(repaired_target)
                repair_logs.append(
                    f"第 {attempt} 轮：根据端到端失败反馈修复 {repaired_target}"
                )
                continue
            if status == "still_failed_same_target":
                repair_logs.append(
                    f"第 {attempt} 轮：{repaired_target} 仍报同目标错误，未完成修复"
                )
                return SkillActionResponse(
                    success=False,
                    path=None,
                    message=(
                        "严格端到端工作流校验失败，且内容补丁修复未完成；文件保持可编辑草稿：\n"
                        + "\n\n".join(e2e_errors)
                        + f"\n\n自动修复目标：{target_path}"
                        + f"\n自动修复反馈：{repair_result.get('last_failure') or 'still_failed_same_target'}"
                        + (
                            "\n\n端到端自动修复记录：\n" + "\n".join(repair_logs)
                            if repair_logs else ""
                        )
                    ),
                    repair_events=repair_events or e2e_session.events,
                    validation_status="needs_repair",
                    error_type="e2e_content_repair_warning",
                    editable=True,
                    disabled=False,
                    recoverable=True,
                )
            raise ValueError(json.dumps(repair_result, ensure_ascii=False, default=str))
        except Exception as exc:
            logger.exception(
                "validate-skill e2e auto repair failed skill=%s target=%s",
                skill_name,
                target_path,
            )
            return SkillActionResponse(
                success=False,
                path=None,
                message=(
                    "严格端到端工作流校验失败，且自动修复未完成：\n"
                    + "\n\n".join(e2e_errors)
                    + f"\n\n自动修复目标：{target_path}"
                    + f"\n自动修复异常：{exc}"
                    + (
                        "\n\n端到端自动修复记录：\n" + "\n".join(repair_logs)
                        if repair_logs else ""
                    )
                ),
                repair_events=repair_events or e2e_session.events,
            )


@router.post("/package-skill", response_model=SkillActionResponse)
async def package_skill(request: PackageSkillRequest):
    """Package a Skill directory into a distributable .skill archive.

    Packaging is intentionally gated by strict E2E validation so the frontend
    or any direct API caller cannot download a package that failed the real
    workflow trial run.

    Final local-resource existence check is performed only at package time:
    - During SKILL.md generation, scripts/references may not exist yet.
    - During packaging, all SKILL.md referenced scripts/references/assets
      must already exist on disk or the package is invalid.
    """
    skill_name = _validate_skill_name(request.skill_name)

    if request.validate_before_package:
        external_context = _external_context_from_skill_action_request(request)
        e2e_errors = _validate_skill_package_smoke(
            skill_name,
            mode="trial",
            external_context=external_context,
            requested_model=request.model,
        )
        if e2e_errors:
            return SkillActionResponse(
                success=False,
                path=None,
                message=(
                    "打包已中止：严格端到端工作流校验未通过。\n"
                    "请先调用 /api/creator/validate-skill 完成自动修复，"
                    "或根据以下错误手动修改后重试：\n"
                    + "\n\n".join(e2e_errors)
                ),
            )

    try:
        _validate_skill_md_final_resource_existence(skill_name)
    except Exception as exc:
        return SkillActionResponse(
            success=False,
            path=None,
            message=(
                "打包已中止：最终资源存在性校验失败。\n"
                "原因：SKILL.md 引用了尚未生成、尚未上传或不存在的本地资源。\n"
                "请确认 scripts/**、references/** 已生成，assets/** 已上传。\n\n"
                f"{exc}"
            ),
        )

    result = run_action({"action": "package", "name": skill_name})
    if not result["success"]:
        return SkillActionResponse(
            success=False,
            path=result.get("path"),
            message=result["message"],
        )

    return SkillActionResponse(
        success=True,
        path=result.get("path"),
        message=result["message"],
    )

@router.post("/init-from-blueprint", response_model=InitFromBlueprintResponse)
async def init_from_blueprint(request: InitFromBlueprintRequest):
    """Initialize Skill directory structure from blueprint file list.

    只创建 Skill 根目录和必要子目录，不再 touch 空文件。

    原因：
    - 文件内容必须由 /generate-file 成功生成后，再由 /write-file 写入；
    - 如果这里预先 touch 文件，前端会看到 0 B 文件，并可能误显示为“已写入”；
    - 这会掩盖模型生成失败或空内容问题。
    """
    skill_name = _validate_skill_name(request.skill_name)
    skill_root = settings.skill_public_dir / skill_name

    try:
        skill_root.mkdir(parents=True, exist_ok=True)

        dirs_created = 0
        seen_dirs: set[Path] = set()

        for file_spec in request.files:
            rel_path = _normalize_skill_path(file_spec.path)
            if not rel_path:
                continue

            target_path = skill_root / rel_path

            if _is_directory_like_skill_path(rel_path):
                dir_path = target_path
            else:
                dir_path = target_path.parent

            if dir_path in seen_dirs:
                continue

            existed = dir_path.exists()
            dir_path.mkdir(parents=True, exist_ok=True)
            seen_dirs.add(dir_path)

            if not existed:
                dirs_created += 1

        return InitFromBlueprintResponse(
            success=True,
            path=str(skill_root),
            files_created=0,
            message=(
                f"已初始化 Skill 目录结构，创建目录 {dirs_created} 个。"
                "文件将在 generate-file 成功返回非空内容后写入，不再预创建 0 B 空文件。"
            ),
        )

    except Exception as exc:
        logger.exception("init-from-blueprint error")
        return InitFromBlueprintResponse(
            success=False,
            path=None,
            files_created=0,
            message=f"初始化失败：{exc}",
        )

@router.post("/list-files", response_model=ListFilesResponse)
async def list_files(request: ListFilesRequest):
    """List all files in a Skill directory.
    
    Returns the actual file structure on disk, useful for displaying
    to the user after initializing the Skill directory structure.
    """
    skill_name = _validate_skill_name(request.skill_name)
    
    skill_root = settings.skill_public_dir / skill_name
    if not skill_root.exists():
        return ListFilesResponse(
            success=False,
            files=[],
            message=f"Skill '{skill_name}' 不存在",
        )
    
    files: list[FileInfo] = []
    
    def scan_dir(base: Path, rel_path: Path = Path("")):
        for entry in sorted(base.iterdir()):
            entry_rel = rel_path / entry.name
            if entry.is_dir():
                files.append(FileInfo(
                    path=str(entry_rel),
                    is_directory=True,
                ))
                scan_dir(entry, entry_rel)
            else:
                files.append(FileInfo(
                    path=str(entry_rel),
                    is_directory=False,
                    size=entry.stat().st_size,
                ))
    
    scan_dir(skill_root)
    
    return ListFilesResponse(
        success=True,
        files=files,
        message=f"已列出 {len(files)} 个文件",
    )

__all__ = [name for name in globals() if not name.startswith("__")]
