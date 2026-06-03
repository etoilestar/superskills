"""Creator-mode chat helpers and routes."""

import asyncio
import functools
import json
import logging
import re
from pathlib import Path
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..config import settings
from ..services.kernel_loader import load_kernel_creator_body_prompt, load_kernel_creator_metadata_prompt, load_kernel_creator_for_phase, read_skill_resource_text
from ..services.llm_proxy import complete_chat_once, stream_chat
from ..services.model_router import CODE_TASK, TEXT_TASK, VALIDATOR_TASK, route_model
from .chat_models import ChatRequest
from .chat_utils import (
    _last_user_text,
    _allowed_skill_roots,
    _extract_input_session_dir,
    _find_created_skill_roots,
    _friendly_error,
    _is_within_sandbox,
    _planner_model_name,
    _request_messages_with_files,
    _sse,
    _quick_actions,
    _strip_markdown_json_fence,
    _thought,
    _validate_skill_md,
)
from .sandbox_chat import (
    _execute_single_task,
    _format_execution_report,
    _infer_skill_root_from_tasks,
    _plan_and_execute_generated_output,
    _run_skill_runtime_planner_round,
)

logger = logging.getLogger(__name__)


def _extract_skill_name_from_messages(messages: list) -> str:
    """从对话历史中提取技能名称（从蓝图消息中）"""
    for msg in reversed(messages):
        # Message 是 Pydantic BaseModel，使用属性访问
        content = getattr(msg, "content", "")
        if "Skill 架构蓝图" in content or "Skill 名称" in content:
            match = re.search(r"Skill\s*名称\s*[：:]\s*([a-z0-9-]+)", content)
            if match:
                return match.group(1)
    return ""


def _safe_async_generator(generator_func):
    """
    安全的异步生成器包装器，正确处理 GeneratorExit 和 asyncio.CancelledError.
    
    解决问题："Attempted to exit cancel scope in a different task than it was entered in"
    """
    @functools.wraps(generator_func)
    async def wrapper(*args, **kwargs):
        try:
            async for item in generator_func(*args, **kwargs):
                try:
                    yield item
                except (GeneratorExit, asyncio.CancelledError):
                    # Client disconnected - clean exit
                    return
        except (GeneratorExit, asyncio.CancelledError):
            # Clean exit
            return
    return wrapper


router = APIRouter(prefix="/api/chat", tags=["chat"])

_CREATOR_PHASE3_MARKER = '{"creator_phase":"phase3_start"}'


def _message_role_content(msg) -> tuple[str, str]:
    role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", "")
    content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
    return str(role or ""), str(content or "")


def _last_user_message_content(messages: list) -> str:
    for msg in reversed(messages):
        role, content = _message_role_content(msg)
        if role == "user":
            return content
    return ""


def _latest_blueprint_text(messages: list) -> str:
    for msg in reversed(messages):
        role, content = _message_role_content(msg)
        if role == "assistant" and "📋 Skill 架构蓝图" in content:
            return content[-5000:]
    return ""


_REVISION_INTENT_HINT_RE = re.compile(
    r"(不需要|无需|不用|不要用|去掉|移除|删除|改成|换成|使用内置|内置.*模型|"
    r"多模态模型|不需要\s*api|不需要.*密钥|不需要.*数据库|api\s*密钥|关键词.*数据库)",
    re.IGNORECASE,
)
_PURE_CONFIRM_RE = re.compile(r"^\s*(确认|确认，继续构建|确认继续|继续构建|没问题|对，就这样|开始制作|开始做吧)[。.!！\s]*$")


def _latest_user_sounds_like_revision(text: str) -> bool:
    return bool(_REVISION_INTENT_HINT_RE.search(text or ""))


def _latest_user_is_pure_confirmation(text: str) -> bool:
    return bool(_PURE_CONFIRM_RE.match(text or ""))


def _parse_phase_refinement(text: str) -> dict:
    try:
        data = json.loads(_strip_markdown_json_fence(text))
    except json.JSONDecodeError:
        return {"phase": "", "reason": "invalid_json"}
    if not isinstance(data, dict):
        return {"phase": "", "reason": "not_object"}
    phase = str(data.get("phase") or "").strip()
    if phase not in {"phase2", "phase3+"}:
        phase = ""
    return {"phase": phase, "reason": str(data.get("reason") or "")[:300]}


async def _refine_creator_phase_with_model(messages: list, current_phase: str, model: str) -> dict:
    """Use a lightweight classifier to avoid treating blueprint edits as build confirmation."""
    last_user = _last_user_message_content(messages)
    if current_phase != "phase3+" or not last_user or _latest_user_is_pure_confirmation(last_user):
        return {"phase": current_phase, "reason": "heuristic_unambiguous", "used_model": False}

    # Fast local guard for common revision wording; model handles the long tail.
    if _latest_user_sounds_like_revision(last_user):
        return {"phase": "phase2", "reason": "latest_user_revision_hint", "used_model": False}

    classifier_model = route_model(
        VALIDATOR_TASK,
        requested_model=model,
        reason="creator phase refinement",
    ).model
    messages_for_classifier = [
        {
            "role": "system",
            "content": (
                "你是 Creator 阶段分类器，只输出 JSON。\n"
                "如果最后一条用户消息是在修改/否定/替换已确认蓝图中的需求、依赖、资源、模型、API、数据库或执行方式，phase=phase2。\n"
                "只有最后一条用户消息是明确确认开始构建且没有新增修改要求时，phase=phase3+。\n"
                "输出格式：{\"phase\":\"phase2|phase3+\",\"reason\":\"...\"}"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "heuristic_phase": current_phase,
                    "last_user_message": last_user,
                    "latest_blueprint_excerpt": _latest_blueprint_text(messages),
                },
                ensure_ascii=False,
            ),
        },
    ]
    try:
        decision_text = await complete_chat_once(messages_for_classifier, classifier_model)
        decision = _parse_phase_refinement(decision_text)
    except Exception as exc:
        logger.warning("creator phase refinement failed: %s", exc)
        return {"phase": current_phase, "reason": "classifier_failed", "used_model": True}

    return {
        "phase": decision.get("phase") or current_phase,
        "reason": decision.get("reason") or "classifier_empty",
        "used_model": True,
        "model": classifier_model,
    }


def _guess_current_phase(messages: list) -> str:
    """
    根据对话历史智能猜测当前所处的阶段。

    关键逻辑：
    - 如果 Skill 已创建完成 → 重置到 phase1
    - 如果有 phase3 marker → phase3+
    - 如果有用户确认（"对，开始做吧"等）+ 有蓝图 → phase3+
    - 如果有蓝图但无确认 → phase2
    - 如果 Phase 1 完成 → phase2
    - 默认 → phase1
    """

    # 0. 检查是否有 Skill 创建完成的标记（在检测 phase3 marker 之前）
    completion_keywords = [
        "Skill 创建成功",
        "技能创建成功",
        "创建完成",
        "创建成功"
    ]
    for msg in messages:
        content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
        if any(keyword in content for keyword in completion_keywords):
            return "phase1"  # 技能已创建完成，后续对话重新开始

    # 1. 检查是否有 phase3 marker
    for msg in messages:
        role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", "")
        content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
        if role == "assistant" and content and '{"creator_phase":"phase3_start"}' in content:
            return "phase3+"

    # 2. 检查是否有用户确认消息
    user_confirm_keywords = [
        "对，开始做吧",
        "开始制作",
        "开始干吧",
        "确认",
        "确认，继续构建",
        "继续构建",
        "确认继续",
        "没问题",
        "对，就这样"
    ]
    has_user_confirm = False

    for msg in reversed(messages):
        role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", "")
        content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")

        if role == "user":
            if any(keyword in content for keyword in user_confirm_keywords):
                has_user_confirm = True
                break

    # 3. 检查是否有蓝图
    has_blueprint = False
    for msg in messages:
        role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", "")
        content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
        if role == "assistant" and "📋 Skill 架构蓝图" in content:
            has_blueprint = True
            break

    # 4. 如果有确认 + 有蓝图 → phase3+（但需要检查确认后是否有修改请求）
    if has_user_confirm and has_blueprint:
        # 检查确认之后是否有用户修改请求
        modification_keywords = [
            "修改",
            "调整",
            "变更",
            "改一下",
            "重新",
            "换一个",
            "不要",
            "不对",
            "重新来",
            "我想改"
        ]
        
        last_confirm_index = -1
        for i, msg in enumerate(messages):
            role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", "")
            content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
            if role == "user":
                if any(keyword in content for keyword in user_confirm_keywords):
                    last_confirm_index = i
        
        if last_confirm_index != -1:
            for msg in messages[last_confirm_index+1:]:
                role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", "")
                content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
                if role == "user":
                    if any(keyword in content for keyword in modification_keywords):
                        return "phase2"
        
        return "phase3+"

    # 5. 如果只有蓝图 → phase2
    if has_blueprint:
        return "phase2"

    # 6. 检查 Phase 1 是否完成
    phase1_complete = False
    phase1_final_step_keywords = [
        "这是一个简单的单步操作",
        "这涉及多个步骤",
        "这需要一些参考资料",
        "根据你的需求，我觉得这个 Skill",
        "架构解耦评估"
    ]

    for msg in reversed(messages):
        role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", "")
        content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")

        if role == "assistant":
            if any(keyword in content for keyword in phase1_final_step_keywords):
                phase1_complete = True
                break

    # 7. 如果 Phase 1 完成，检查是否有 Phase 2 关键词
    if phase1_complete:
        for msg in reversed(messages):
            content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
            if not content:
                continue
            phase2_keywords = ["蓝图", "架构", "I/O", "目录结构", "工作流", "确认"]
            if any(keyword in content for keyword in phase2_keywords):
                return "phase2"

    # 8. 默认在 phase1
    return "phase1"


def _parse_ask_user_question(text: str) -> tuple[str, list[dict] | None]:
    """
    Parse AskUserQuestion format to extract question and options, returns (clean_text, actions_list).
    
    Args:
        text: Raw text
        
    Returns:
        (clean_text, actions_list): actions_list is list of quick_actions buttons, or None if not found
    """
    # First try full ```text ... ``` format
    ask_user_pattern = re.compile(r'```text\s*问题[：:]\s*.*?```', re.DOTALL)
    ask_user_blocks = ask_user_pattern.findall(text)
    
    # If not found, try simpler format without ```text wrapper
    if not ask_user_blocks:
        simple_pattern = re.compile(r'(?:^|\n)(问题[：:].*?)(?=\n\n|\n\s*[A-Z][a-z]+[：:]|$)', re.DOTALL)
        ask_user_blocks = simple_pattern.findall(text)
    
    # If still not found, try looser detection
    if not ask_user_blocks:
        confirm_patterns = [
            r'(?:你对以上内容满意|准备进入下一步|确认|对吗|好吗)[？?]',
            r'(?:开始做|开始创建|继续实现)',
        ]
        has_confirm = any(re.search(p, text, re.IGNORECASE) for p in confirm_patterns)
        
        if has_confirm:
            actions = [
                {"text": "对，开始做吧", "value": "对，开始做吧", "style": "primary"},
                {"text": "大体对，但有些地方要改", "value": "大体对，但有些地方要改", "style": "default"},
                {"text": "不对，我重新说一下", "value": "不对，我重新说一下", "style": "default"},
            ]
            return text, actions
    
    if not ask_user_blocks:
        return text, None
    
    first_block = ask_user_blocks[0].strip()
    
    # Parse question - more flexible regex
    question_match = re.search(r'问题[：:]\s*["\']?(.*?)["\']?\s*(?:\n|$)', first_block, re.DOTALL)
    if not question_match:
        question_match = re.search(r'问题[：:]\s*(.*?)(?:\n|$)', first_block)
    
    # Extract options - support multiple formats
    options_matches = []
    
    options_with_quotes = re.findall(r'-\s*["\']?(.*?)["\']?\s*(?:\n|$)', first_block)
    if options_with_quotes:
        options_matches = options_with_quotes
    else:
        options_without_quotes = re.findall(r'-\s*(.+?)(?:\n|$)', first_block)
        if options_without_quotes:
            options_matches = options_without_quotes
    
    # If no options found but we have a confirm question, use default options
    if not options_matches and question_match:
        options_matches = ["对，开始做吧", "大体对，但有些地方要改", "不对，我重新说一下"]
    
    if not question_match:
        return text, None
    
    question = question_match.group(1).strip()
    actions = []
    
    for opt in options_matches:
        opt_text = opt.strip()
        if opt_text and not opt_text.lower().startswith(("选项", "options", "问题")):
            actions.append({
                "text": opt_text,
                "value": opt_text,
                "style": "default"
            })
    
    for action in actions:
        if any(keyword in action["text"] for keyword in ["开始做", "确认", "开始创建", "继续", "没问题", "对，"]):
            action["style"] = "primary"
    
    first_pos = text.find(first_block)
    if first_pos != -1:
        end_pos = first_pos + len(first_block)
        clean_text = text[:end_pos].rstrip()
    else:
        clean_text = text
    
    return clean_text, actions


def _ensure_single_question(text: str) -> tuple[str, list[dict] | None]:
    """Ensure we only show one set of quick actions at a time."""
    return _parse_ask_user_question(text)


def _strip_phase3_marker_from_visible_text(text: str) -> str:
    """Remove accidental phase3 marker lines from user-visible Creator text."""
    return re.sub(
        r"(?m)^\s*\{\s*\"creator_phase\"\s*:\s*\"phase3_start\"\s*\}\s*\n?",
        "",
        text,
    )


@_safe_async_generator
async def _execute_conversation_mode(
    final_messages: list[dict],
    model: str,
    current_phase: str,
    request: ChatRequest,
    execution_root: Path,
    parent_skill_name: str,
) -> AsyncGenerator[str, None]:
    """Phase 1-2 conversation mode: stream directly to user."""
    yield _sse({"status": None})

    # Collect first, then stream sanitized user-visible text.
    # Phase 1-2 must never show an accidental phase3 marker to the user.
    assistant_chunks: list[str] = []
    
    try:
        yield _sse({"model_ack": {"task": "text", "model": model, "reason": f"creator {current_phase} conversation"}})
        async for chunk in stream_chat(final_messages, model):
            assistant_chunks.append(chunk)
    except Exception:
        logger.exception("Error during LLM streaming")
    
    if not assistant_chunks:
        return
    
    assistant_text = "".join(assistant_chunks)
    visible_text = _strip_phase3_marker_from_visible_text(assistant_text)
    if visible_text:
        yield _sse({"content": visible_text})
    
    # Check for quick actions after streaming completes
    if current_phase in ["phase1", "phase2", "unknown"]:
        _, actions = _ensure_single_question(assistant_text)
        if actions:
            yield _quick_actions(actions)
    
    yield "data: [DONE]\n\n"


@_safe_async_generator
async def _execute_phase3_mode(
        final_messages: list[dict],
        model: str,
        request: ChatRequest,
        execution_root: Path,
        parent_skill_name: str,
) -> AsyncGenerator[str, None]:
    """Phase 3+ execution mode: collect model output and execute."""

    yield _sse({"type": "phase3_start", "message": "开始执行 Skill 创建流程..."})
    yield _sse({"status": {"phase": "phase3", "message": "正在生成实现方案…"}})

    # 1. 收集模型完整输出（不流式展示给用户）
    assistant_chunks: list[str] = []
    try:
        yield _sse({"model_ack": {"task": "code", "model": model, "reason": "creator phase3 implementation"}})
        async for chunk in stream_chat(final_messages, model):
            assistant_chunks.append(chunk)
    except Exception as e:
        logger.exception("Error during Phase 3 streaming")
        yield _sse({"error": f"模型输出错误: {str(e)}"})
        yield "data: [DONE]\n\n"
        return

    if not assistant_chunks:
        yield _sse({"error": "模型没有输出内容"})
        yield "data: [DONE]\n\n"
        return

    assistant_text = "".join(assistant_chunks)
    yield _sse({"status": {"phase": "planning", "message": "正在解析实现输出并规划执行…"}})

    # 2. 调用执行函数
    try:
        yield _sse({"status": {"phase": "executing", "message": "正在执行生成的文件操作…"}})
        exec_result = await _plan_and_execute_generated_output(
            assistant_text=assistant_text,
            request=request,
            model=model,
            require_confirmation=False,
            execution_root=execution_root,
            skill_name=parent_skill_name,
        )

        # 3. 返回结果
        yield _sse({"type": "progress", "step": "执行完成", "step_num": 3, "total_steps": 3})

        if exec_result.get("executed"):
            output_files = exec_result.get("output_files", [])

            # 查找 skill 名称和路径
            skill_name = None
            skill_path = None
            for file_info in output_files:
                file_path = file_info.get("path", "")
                if "skills/" in file_path and "/SKILL.md" in file_path:
                    parts = file_path.split("skills/", 1)[1].split("/", 1)
                    if parts:
                        skill_name = parts[0]
                        skill_path = str(Path(file_path).parent)
                    break

            yield _sse({
                "type": "completed",
                "success": True,
                "skill_name": skill_name,
                "skill_path": skill_path,
                "created_files": output_files,
                "message": "Skill 创建成功！" if skill_name else "文件创建完成！"
            })

    except Exception as e:
        logger.exception("Phase 3 execution failed")
        yield _sse({"type": "error", "message": f"执行失败: {_friendly_error(e)}"})

    yield "data: [DONE]\n\n"

def _extract_creator_resource_catalog(body_prompt: str) -> list[dict]:
    """Extract creator references/assets/scripts from a loaded prompt."""
    catalog: list[dict] = []
    seen: set[str] = set()
    pattern = re.compile(r"`(?P<path>(references|assets|scripts)/[^`]+)`(?P<title>：[^\n]+)?", re.M)

    for match in pattern.finditer(body_prompt):
        path = match.group("path").strip()
        if path in seen:
            continue
        seen.add(path)
        kind = path.split("/", 1)[0]
        catalog.append({
            "resource_handle": f"resource:{len(catalog)}",
            "kind": kind,
            "path": path,
            "title": (match.group("title") or "").lstrip("：").strip(),
            "allowed_actions": ["read_resource"],
        })

    return catalog


def _parse_creator_resource_selection_decision(text: str, *, resource_catalog: list[dict]) -> dict:
    """Parse creator resource selection from JSON or plain-text handles."""
    valid_handles = {str(item.get("resource_handle")) for item in resource_catalog}
    stripped = _strip_markdown_json_fence(text)
    need_resources = False
    raw_handles: list[str] = []
    reason = ""

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        raw_handles = re.findall(r"resource:\d+", text)
        need_resources = bool(raw_handles)
        reason = "从普通文本中识别 resource handle" if raw_handles else "JSON 解析失败"
    else:
        if not isinstance(data, dict):
            return {"need_resources": False, "resource_handles": [], "reason": "输出不是 JSON object"}
        raw = data.get("resource_handles", [])
        raw_handles = raw if isinstance(raw, list) else []
        flag = data.get("need_resources", bool(raw_handles))
        need_resources = flag.strip().lower() in {"true", "1", "yes", "y"} if isinstance(flag, str) else bool(flag)
        reason = str(data.get("reason") or "").strip()

    selected: list[str] = []
    for item in raw_handles:
        handle = str(item or "").strip()
        if handle in valid_handles and handle not in selected:
            selected.append(handle)
        if len(selected) >= 5:
            break

    return {
        "need_resources": bool(need_resources and selected),
        "resource_handles": selected if need_resources else [],
        "reason": reason,
    }


def _build_creator_resource_catalog():
    """Build a catalog of available resources in the kernel prompt."""
    try:
        return _extract_creator_resource_catalog(load_kernel_creator_body_prompt())
    except Exception:
        logger.exception("failed to build creator resource catalog")
        return []


async def _run_creator_resource_selection_round(
    body_prompt: str,
    request: ChatRequest,
    model: str,
    resource_catalog: list,
    current_phase_hint: str = "unknown"
):
    """Run a planner round to choose creator resources when useful."""
    if not resource_catalog:
        return {"is_structured": True, "need_resources": False, "resource_handles": [], "reason": "无可用资源"}

    messages = [
        {"role": "system", "content": "只从给定 resource_catalog 选择最多 5 个 resource_handle。只输出 JSON。"},
        {
            "role": "user",
            "content": json.dumps({
                "current_phase_hint": current_phase_hint,
                "loaded_skill_prompt": body_prompt,
                "resource_catalog": resource_catalog,
                "user_messages": _request_messages_with_files(request),
            }, ensure_ascii=False),
        },
    ]
    decision_text = await complete_chat_once(messages, _planner_model_name(model))
    decision = _parse_creator_resource_selection_decision(decision_text, resource_catalog=resource_catalog)
    decision["is_structured"] = True
    return decision


def _compose_creator_loaded_resources_prompt(
    skill_name: str,
    resource_catalog: list,
    selected_handles: list
):
    """Compose loaded creator resources into a prompt for the LLM."""
    resource_by_handle = {str(item.get("resource_handle")): item for item in resource_catalog}
    sections: list[str] = []

    for handle in selected_handles:
        resource = resource_by_handle.get(str(handle))
        if not resource:
            continue

        path = str(resource.get("path") or "")
        try:
            observation = read_skill_resource_text(
                skill_name,
                path,
                max_chars=settings.skill_resource_max_chars,
            )
        except Exception as exc:
            sections.append(
                f"### {handle}\n"
                f"- path: `{path}`\n"
                f"- load_error: {exc}\n"
            )
            continue

        content = observation.get("content", "")
        truncated = observation.get("truncated", False)
        sections.append(
            f"### {handle}\n"
            f"- kind: {resource.get('kind')}\n"
            f"- path: `{path}`\n"
            f"- truncated: {truncated}\n\n"
            "```text\n"
            f"{content}\n"
            "```"
        )

    if not sections:
        return ""

    return (
        "\n\n---\n\n"
        "## Loaded Creator Resources\n\n"
        "下面是宿主按需加载的 Creator 资源正文；只能依据已加载内容使用这些资源。\n\n"
        + "\n\n".join(sections)
    )


def _compose_creator_runtime_contract_prompt() -> str:
    """Inject non-negotiable platform runtime contracts for generated Skills."""
    image_model = settings.image_model or settings.default_model
    text_model = settings.text_model or settings.default_model
    vision_model = settings.vision_model or settings.default_model
    return f"""

---

## Platform Runtime Contract for Generated Skills

生成 Skill 时必须遵守以下平台运行时契约：

1. 模型来源必须分离：
   - 文本、翻译、语义改写：`LLM_BASE_URL` + `TEXT_MODEL`。
   - 看图理解、OCR、多模态问答：`LLM_BASE_URL` + `VISION_MODEL`。
   - 生成图片：Stable Diffusion 图片运行时，使用 `IMAGE_BASE_URL` + `IMAGE_MODEL` 与 `IMAGE_SIZE`；不得使用 `VISION_MODEL` 生成图片。
2. 创建出来的 `SKILL.md` 不要写中文 topic 翻译、TEXT_MODEL 翻译调用、图片接口字段解析等平台细节；只写“使用平台 Stable Diffusion 图片生成能力”。
3. 生成的 `scripts/*.py` 如果需要图片生成，必须调用：
   `from backend.services.skill_runtime import generate_stable_diffusion_image, print_json`
   并把用户 topic 原文传给 `generate_stable_diffusion_image(...)`。平台 helper 会静默完成中文 topic 到英文 Stable Diffusion prompt 的转换、`b64_json` 解析、图片写入 `OUTPUT_DIR`。
4. 图片脚本 stdout 必须输出 JSON，包含 `image_path`；禁止输出 base64 data URI，禁止假设图片接口只返回 `url`。
5. 模型与认证相关参数由平台运行时注入；生成脚本可按需读取 `IMAGE_MODEL`、`IMAGE_BASE_URL`、`IMAGE_SIZE`、`IMAGE_API_KEY` / `LLM_API_KEY` / `OPENAI_API_KEY` 等环境变量，但不要硬编码这些值，也不需要额外校验它们是否存在。
"""


@_safe_async_generator
async def _make_stream_creator_generator(
    skill_context: dict, 
    request: ChatRequest,
    model: str,
    execution_root: Path | None,
    parent_skill_name: str,
    enable_resource_preload: bool,
    _MAX_CMD_DISPLAY_LENGTH: int
) -> AsyncGenerator[str, None]:
    """Core generator function that produces SSE stream."""
    
    messages_list = getattr(request, "messages", [])
    heuristic_phase = _guess_current_phase(messages_list)
    phase_refinement = await _refine_creator_phase_with_model(messages_list, heuristic_phase, model)
    current_phase = str(phase_refinement.get("phase") or heuristic_phase)

    phase_description = {
        "phase1": "Phase 1（深度需求挖掘）",
        "phase2": "Phase 2（技能架构蓝图）",
        "phase3+": "Phase 3+（工程化实现）",
        "unknown": "阶段未知"
    }.get(current_phase, "阶段未知")

    yield _thought(
        "phase_detection",
        "阶段检测",
        f"当前处于 {phase_description}",
        {
            "current_phase": current_phase,
            "heuristic_phase": heuristic_phase,
            "phase_refinement": phase_refinement,
        },
    )

    is_first_time = len(messages_list) == 0
    
    if is_first_time:
        loading_phase = "first_time"
        prompt_desc = "初始指导（块 0+1）"
    elif current_phase == "phase1":
        loading_phase = "phase1"
        prompt_desc = "Phase1 指导（块 0+1）"
    elif current_phase == "phase2":
        loading_phase = "phase2"
        prompt_desc = "Phase2 指导（块 0+2）"
    elif current_phase == "phase3+":
        loading_phase = "phase3+"
        prompt_desc = "Phase3+ 指导（块 0+4）"
    else:
        loading_phase = "phase1"
        prompt_desc = "Phase1 指导（块 0+1）"
    
    yield _sse({"status": {"phase": "loading", "message": f"加载 {prompt_desc}…"}})
    body_prompt = load_kernel_creator_for_phase(loading_phase)
    prompt_type = f"渐进式披露 {prompt_desc}"

    yield _thought(
        "body_loaded",
        "加载 SKILL.md",
        f"{prompt_type}已加载，共 {len(body_prompt)} 字符",
        {"body_chars": len(body_prompt), "skill_name": parent_skill_name, "prompt_type": prompt_type},
    )

    loaded_resources_prompt = ""
    if enable_resource_preload and current_phase in ["phase1", "phase2", "unknown"]:
        resource_catalog = _build_creator_resource_catalog()
        if resource_catalog:
            yield _sse({"status": {"phase": "loading_resources", "message": "按需加载资源…"}})
            
            resource_decision = await _run_creator_resource_selection_round(
                body_prompt=body_prompt,
                request=request,
                model=model,
                resource_catalog=resource_catalog,
                current_phase_hint=current_phase,
            )
            
            is_structured = resource_decision.get("is_structured", False)

            if is_structured:
                if resource_decision.get("need_resources"):
                    yield _thought(
                        "resource_selection",
                        "资源选择",
                        (
                            f"加载 {len(resource_decision.get('resource_handles', []))} 个资源"
                            if resource_decision.get("need_resources")
                            else f"无需加载额外资源：{resource_decision.get('reason', '')}"
                        ),
                        {
                            "need_resources": resource_decision.get("need_resources"),
                            "resource_handles": resource_decision.get("resource_handles", []),
                            "catalog_size": len(resource_catalog),
                            "current_phase": current_phase,
                        },
                    )

                    selected = resource_decision.get("resource_handles") or []
                    if selected:
                        loaded_resources_prompt = _compose_creator_loaded_resources_prompt(
                            skill_name=parent_skill_name,
                            resource_catalog=resource_catalog,
                            selected_handles=selected,
                        )

    body_prompt = body_prompt + _compose_creator_runtime_contract_prompt()

    if loaded_resources_prompt:
        body_prompt = body_prompt + loaded_resources_prompt

    if current_phase in ["phase1", "phase2", "unknown"]:
        single_step_instruction = """

---
## 交互指导

请严格按照 SKILL.md 中的流程执行:
- Phase 1: 通过多轮对话充分收集用户需求
- Phase 2: 生成完整蓝图并让用户确认
- Phase 3: 执行实现（需要先输出 phase3 marker）

一次只问一个问题，等待用户回复。
"""
        body_prompt = body_prompt + single_step_instruction

    final_messages: list[dict] = [{"role": "system", "content": body_prompt}]
    final_messages.extend(_request_messages_with_files(request))

    # if current_phase == "phase3+":
    #     yield _sse({"status": {"phase": "planning", "message": "规划执行方案…"}})
    #     try:
    #         # 加载 Phase 3+ 的 SKILL.md
    #         body_prompt = load_kernel_creator_for_phase("phase3+")
    #         # 从对话历史中提取技能名称
    #         skill_name = _extract_skill_name_from_messages(request.messages)
    #         runtime_plan = await _run_skill_runtime_planner_round(
    #             body_prompt=body_prompt,
    #             request=request,
    #             model=model,
    #             execution_root=execution_root,
    #             skill_name=skill_name,
    #         )
    #
    #         mode = runtime_plan.get("mode")
    #         tasks = runtime_plan.get("tasks") or []
    #
    #         yield _thought(
    #             "planner_output",
    #             "规划结果",
    #             f"模式：{mode}，共 {len(tasks)} 个动作",
    #             {
    #                 "mode": mode,
    #                 "task_count": len(tasks),
    #                 "tasks": [
    #                     {
    #                         "action": t.get("action"),
    #                         "command": (str(t.get("command") or ""))[:120] or None,
    #                         "path": t.get("path") or t.get("resource_handle") or None,
    #                         "reason": str(t.get("reason") or "")[:200],
    #                     }
    #                     for t in tasks
    #                 ],
    #             },
    #         )
    #
    #         if mode == "execute" and tasks:
    #             _exec_inferred_root = _infer_skill_root_from_tasks(
    #                 runtime_plan, execution_root=execution_root
    #             )
    #             _exec_cwd = execution_root or _exec_inferred_root
    #             _exec_session_dir = _extract_input_session_dir(
    #                 getattr(request, "input_files", []) or [], _exec_cwd
    #             )
    #
    #             _exec_all_results: list[dict] = []
    #             _exec_all_touched: list[Path] = []
    #
    #             for task in tasks:
    #                 task_action = str(task.get("action") or "").strip()
    #
    #                 if task_action == "run_command":
    #                     cmd = str(task.get("command") or "")
    #                     short_cmd = cmd[:_MAX_CMD_DISPLAY_LENGTH] + (
    #                         "…" if len(cmd) > _MAX_CMD_DISPLAY_LENGTH else ""
    #                     )
    #                     yield _sse({"status": {"phase": "executing", "message": f"执行命令：{short_cmd}"}})
    #                     yield _thought("action_start", "执行命令", short_cmd,
    #                                    {"action": "run_command", "command": cmd[:200]})
    #                 elif task_action == "write_file":
    #                     wf_path = str(task.get("path") or "")
    #                     yield _sse({"status": {"phase": "writing", "message": f"写入文件：{wf_path}"}})
    #                     yield _thought("action_start", "写入文件", wf_path,
    #                                    {"action": "write_file", "path": wf_path})
    #                 elif task_action == "create_directory":
    #                     cd_path = str(task.get("path") or "")
    #                     yield _sse({"status": {"phase": "creating", "message": f"创建目录：{cd_path}"}})
    #                     yield _thought("action_start", "创建目录", cd_path,
    #                                    {"action": "create_directory", "path": cd_path})
    #                 elif task_action == "read_resource":
    #                     res_path = str(task.get("path") or task.get("resource_handle") or "")
    #                     yield _sse({"status": {"phase": "reading", "message": f"读取资源：{res_path}"}})
    #                     yield _thought("action_start", "读取资源", res_path,
    #                                    {"action": "read_resource", "path": res_path})
    #                 else:
    #                     yield _thought("action_start", "执行动作", task_action, {"action": task_action})
    #
    #                 task_result, task_touched = await asyncio.to_thread(
    #                     functools.partial(
    #                         _execute_single_task,
    #                         task,
    #                         [],
    #                         request,
    #                         execution_root=execution_root,
    #                         inferred_skill_root=_exec_inferred_root,
    #                         skill_name=parent_skill_name,
    #                         session_input_dir=_exec_session_dir,
    #                     )
    #                 )
    #                 _exec_all_results.append(task_result)
    #                 _exec_all_touched.extend(task_touched)
    #
    #                 _safe_result = {
    #                     k: (v[:1000] if isinstance(v, str) else v)
    #                     for k, v in task_result.items()
    #                     if k not in {"content"}
    #                 }
    #                 success_flag = task_result.get("success", True)
    #                 yield _thought(
    #                     "action_result",
    #                     "操作结果",
    #                     f"{'成功' if success_flag else '失败'}",
    #                     _safe_result,
    #                 )
    #
    #             for root in _find_created_skill_roots(_exec_all_touched):
    #                 skill_md = root / "SKILL.md"
    #                 if skill_md.exists():
    #                     _validate_skill_md(skill_md)
    #
    #             _exec_all_output_files: list[dict] = []
    #             for r in _exec_all_results:
    #                 _exec_all_output_files.extend(r.get("output_files") or [])
    #
    #             exec_result = {
    #                 "executed": True,
    #                 "reason": "已根据结构化 action plan 逐任务执行。",
    #                 "plan": runtime_plan,
    #                 "results": _exec_all_results,
    #                 "logs": [],
    #                 "output_files": _exec_all_output_files,
    #             }
    #
    #             yield _sse({"status": {"phase": "generating", "message": "生成最终回答…"}})
    #             from .sandbox_chat import _compose_final_answer_prompt
    #
    #             _final_messages = [
    #                 {"role": "system", "content": _compose_final_answer_prompt()},
    #                 {
    #                     "role": "user",
    #                     "content": json.dumps(
    #                         {
    #                             "loaded_skill_prompt": body_prompt,
    #                             "user_messages": _request_messages_with_files(request),
    #                             "plan": runtime_plan,
    #                             "execution_result": exec_result,
    #                         },
    #                         ensure_ascii=False,
    #                     ),
    #                 },
    #             ]
    #
    #             yield _sse({"status": None})
    #
    #             if _exec_all_output_files:
    #                 yield _sse({
    #                     "action_result": {
    #                         "action": "output_files",
    #                         "name": parent_skill_name,
    #                         "success": True,
    #                         "message": f"生成了 {len(_exec_all_output_files)} 个文件",
    #                         "output_files": _exec_all_output_files,
    #                     }
    #                 })
    #
    #             async for chunk in stream_chat(_final_messages, model):
    #                 yield _sse({"content": chunk})
    #
    #             yield "data: [DONE]\n\n"
    #             return
    #
    #         if mode == "ask_user":
    #             yield _sse({"status": None})
    #             missing = runtime_plan.get("missing") or []
    #             text = "缺少必要信息，无法执行：\n" + "\n".join(
    #                 f"- {item}" for item in missing) if missing else "缺少必要信息。"
    #             yield _sse({"content": text})
    #             yield "data: [DONE]\n\n"
    #             return
    #
    #         yield _sse({"status": None})
    #
    #     except Exception as exc:
    #         logger.exception("creator runtime planning/execution failed")
    #         yield _sse({"status": None})
    #         yield _sse({"error": "错误：运行时规划或执行失败"})
    #         yield "data: [DONE]\n\n"
    #         return

    def _conversation_has_phase3(messages: list) -> bool:
        for msg in messages:
            role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", "")
            content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
            if role == "assistant" and content and _CREATOR_PHASE3_MARKER in content:
                return True
        return False

    if current_phase in ["phase3+", "phase3", "phase4", "phase5"]: # or _conversation_has_phase3(request.messages):
        phase3_route = route_model(
            CODE_TASK,
            requested_model=getattr(request, "model", None) or settings.default_model,
            reason="creator phase3 implementation",
        )
        async for sse in _execute_phase3_mode(
            final_messages=final_messages,
            model=phase3_route.model,
            request=request,
            execution_root=execution_root,
            parent_skill_name=parent_skill_name,
        ):
            yield sse
    else:
        async for sse in _execute_conversation_mode(
            final_messages=final_messages,
            model=model,
            current_phase=current_phase,
            request=request,
            execution_root=execution_root,
            parent_skill_name=parent_skill_name,
        ):
            yield sse


def _make_stream_creator(skill_context: dict, request: ChatRequest):
    """Creator-specific streaming with phase-aware behaviour.

    Unlike _make_stream which always runs the runtime planner, this function
    detects whether the conversation is still in Phase 1-2 (pure conversation)
    or has advanced to Phase 3+ (action execution).

    Phase detection:
    - If the LLM's response in the current turn contains the phase-3 marker,
      run the planner/executor on the generated output.
    - Otherwise, stream the LLM response directly without planning.

    This avoids the overhead and potential mis-planning of running the runtime
    planner on conversational Phase 1-2 turns that are purely asking questions.
    """
    requested_model = request.model or settings.default_model
    model = route_model(TEXT_TASK, requested_model=requested_model, reason="creator conversation").model
    _MAX_CMD_DISPLAY_LENGTH = 60
    execution_root = skill_context.get("execution_root")
    parent_skill_name = skill_context.get("skill_name", "")
    enable_resource_preload = bool(skill_context.get("enable_resource_preload", False))

    if execution_root is not None:
        execution_root = Path(execution_root).resolve()
        allowed_roots = _allowed_skill_roots()
        if not any(_is_within_sandbox(execution_root, r.resolve()) for r in allowed_roots):
            raise ValueError(
                f"execution_root '{execution_root}' is outside all allowed skill roots."
            )

    return StreamingResponse(
        _make_stream_creator_generator(
            skill_context=skill_context,
            request=request,
            model=model,
            execution_root=execution_root,
            parent_skill_name=parent_skill_name,
            enable_resource_preload=enable_resource_preload,
            _MAX_CMD_DISPLAY_LENGTH=_MAX_CMD_DISPLAY_LENGTH
        ),
        media_type="text/event-stream"
    )


def build_kernel_skill_context():
    """Build the context for the kernel skill mode."""
    return {
        "skill_name": "",
        "execution_root": None,
        "enable_resource_preload": False
    }


@router.post("/creator")
async def chat_with_creator(request: ChatRequest):
    """Create Skill Creator endpoint."""
    try:
        skill_context = build_kernel_skill_context()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return _make_stream_creator(skill_context, request)