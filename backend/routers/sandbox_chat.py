"""Sandbox-mode chat helpers, planners, and execution routines."""

import asyncio
import base64
import csv
import functools
import hashlib
import io
import json
import logging
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..config import PROJECT_ROOT, settings
from ..services.kernel_loader import (
    load_child_skill_body_prompt,
    load_skill_body_prompt,
    load_skill_metadata_prompt,
    read_skill_resource_text,
)
from ..services.llm_proxy import complete_chat_once, stream_chat
from ..services.model_router import (
    TEXT_TASK,
    infer_sandbox_response_task,
    route_model,
)
from ..services.skill_manager import get_execution_skill_dir
from .chat_utils import (
    _ALLOWED_PLAN_ACTIONS,
    _MAX_DEP_RETRY,
    _NODE_BUILTIN_MODULES,
    _PYTHON_HEREDOC_RE,
    _SCRIPT_INTERPRETERS,
    _allowed_skill_roots,
    _blocks_for_planner,
    _expand_arg_env_vars,
    _extract_all_fenced_blocks,
    _extract_input_session_dir,
    _find_created_skill_roots,
    _friendly_error,
    _get_skill_venv_python,
    _has_creation_confirmation,
    _is_within_sandbox,
    _last_user_text,
    _planner_model_name,
    _request_messages_with_files,
    _rewrite_argv_input_paths,
    _scan_and_install_node_deps,
    _scan_and_install_python_deps,
    _snapshot_dir_files,
    _sse,
    _strip_markdown_json_fence,
    _thought,
    _try_auto_install_interpreter,
    _validate_skill_md,
    _retry_install_node_dep,
    _retry_install_python_dep,
)
from .chat_models import ChatRequest, MarkdownBlock

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

def _skill_root_for_name(skill_name: str) -> Path:
    """Resolve an existing sandbox skill root by skill_name."""
    if not skill_name or "/" in skill_name or "\\" in skill_name or ".." in skill_name:
        raise ValueError(f"非法 skill_name: {skill_name}")
    return get_execution_skill_dir(skill_name, mode="sandbox").resolve()



def _available_scripts_for_root(execution_root: Path | None) -> list[str]:
    """Return real scripts under the current business Skill root only."""
    if execution_root is None:
        return []
    root = execution_root.resolve()
    scripts_dir = root / "scripts"
    if not scripts_dir.is_dir() or not _is_within_sandbox(scripts_dir, root):
        return []
    scripts: list[str] = []
    for entry in sorted(scripts_dir.iterdir()):
        if not entry.is_file():
            continue
        resolved = entry.resolve()
        if _is_within_sandbox(resolved, root):
            scripts.append(f"scripts/{entry.name}")
    return scripts

def _resolve_safe_path(raw_path: str, base_dir: Path | None = None) -> Path:
    """Resolve file paths and ensure they stay within allowed directories.

    确保文件路径是相对于 skill 根目录的，而不是宿主目录。
    """
    path = Path(raw_path).expanduser()

    if path.is_absolute():
        return path

    # 如果是相对路径，基于 execution_root 或者 inferred_skill_root 解析路径
    base_dir = base_dir or Path.cwd()
    return base_dir / path


def _looks_like_skill_resource_dir(path: Path) -> bool:
    return path.name in {"scripts", "references", "assets"}


def _normalize_skill_resource_path(value: str) -> str:
    """Normalize a skill-local resource path for catalog/loaded-path comparison."""
    normalized = str(value or "").strip().replace("\\", "/").lstrip("./")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized

def _infer_skill_root_from_tasks(plan: dict, *, execution_root: Path | None = None) -> Path | None:
    """Infer the active skill root from create_directory/write_file tasks.

    用于 /creator legacy fallback：
    如果模型先创建了 <skill-root>/scripts、references、assets，
    后续相对写入 SKILL.md、scripts/main.py 都应以 <skill-root> 为根。
    """
    candidates: list[Path] = []

    for task in plan.get("tasks", []):
        if not isinstance(task, dict):
            continue

        action = str(task.get("action") or "").strip()
        raw_path = str(task.get("path") or "").strip()
        if not raw_path:
            continue

        try:
            resolved = _resolve_safe_path(raw_path, base_dir=execution_root)
        except Exception:
            continue

        if action == "create_directory":
            if _looks_like_skill_resource_dir(resolved):
                candidates.append(resolved.parent)
            else:
                candidates.append(resolved)

        elif action == "write_file":
            if resolved.name == "SKILL.md":
                candidates.append(resolved.parent)
            elif resolved.parent.name in {"scripts", "references", "assets"}:
                candidates.append(resolved.parent.parent)

    if not candidates:
        return None

    # 优先选择位于 allowed skill roots 下的最深目录
    allowed_roots = _allowed_skill_roots()
    valid: list[Path] = []

    for candidate in candidates:
        for allowed_root in allowed_roots:
            try:
                candidate.resolve().relative_to(allowed_root.resolve())
                valid.append(candidate.resolve())
                break
            except ValueError:
                continue

    if not valid:
        return None

    return sorted(valid, key=lambda p: len(p.parts), reverse=True)[0]

def _resolve_planned_file_path(
    raw_path: str,
    *,
    execution_root: Path | None = None,
    inferred_skill_root: Path | None = None,
) -> Path:
    """Resolve file path for planned write/create actions.

    规则：
    - 绝对路径保持绝对路径；
    - sandbox 有 execution_root 时，相对路径基于 execution_root；
    - creator 推断出 inferred_skill_root 时，Skill 内部相对路径基于 inferred_skill_root；
    - 否则退回原有逻辑。
    """
    path = Path(raw_path).expanduser()

    if path.is_absolute():
        return _resolve_safe_path(raw_path, base_dir=execution_root)

    if inferred_skill_root is not None:
        first = path.parts[0] if path.parts else ""

        # SKILL.md、scripts/main.py、references/xx、assets/xx 都属于当前 skill 根
        if raw_path == "SKILL.md" or first in {"scripts", "references", "assets"}:
            return _resolve_safe_path(raw_path, base_dir=inferred_skill_root)

    return _resolve_safe_path(raw_path, base_dir=execution_root)

def _parse_path_argument(path_expr: str) -> str:
    try:
        parts = shlex.split(path_expr)
    except ValueError as exc:
        raise ValueError(f"路径参数解析失败: {path_expr}") from exc

    if len(parts) != 1:
        raise ValueError(f"只允许一个路径参数: {path_expr}")

    return parts[0]

def _extract_runtime_resource_catalog(body_prompt: str, *, execution_root: "Path | None" = None) -> list[dict]:
    """Extract host-owned resource catalog from Loaded SKILL.md prompt.

    关键原则：
    - 真实 path 只归宿主管理；
    - planner 只能看到 resource_handle；
    - planner 不能自己生成 read_resource.path。

    策略：
    1. 用宽松正则匹配所有 backtick 引用（列表、表格、行内等写法均可识别）。
    2. 若传入 execution_root，从磁盘直接扫 scripts/、references/、assets/ 三个子目录，
       将未被正则发现的文件追加进 catalog（彻底兜底）。
    """
    catalog: list[dict] = []
    seen: set[str] = set()

    # 宽松正则：匹配所有被 backtick 包裹的 references/assets/scripts 路径
    # 覆盖列表（- `scripts/xxx`）、表格单元格、行内引用等写法
    # 可选地捕获紧随其后的「：标题」（兼容旧的列表格式）
    pattern = re.compile(
        r"`(?P<path>(references|assets|scripts)/[^`]+)`(?P<title>：[^\n]+)?",
        re.M,
    )

    def _add_entry(path: str, title: str = "") -> None:
        path = _normalize_skill_resource_path(path)
        if path in seen:
            return
        kind = path.split("/", 1)[0]
        if kind not in {"references", "assets", "scripts"}:
            return
        if execution_root is not None:
            root = execution_root.resolve()
            candidate = (root / path).resolve()
            if not _is_within_sandbox(candidate, root) or not candidate.is_file():
                logger.info("skip missing/non-local skill resource from catalog: root=%s path=%s", root, path)
                return
        seen.add(path)
        if kind == "references":
            allowed_actions = ["read_resource"]
            usage_hint = "参考资料，可在任务需要领域知识、示例、规范时读取。"
        elif kind == "assets":
            allowed_actions = ["read_resource"]
            usage_hint = "模板或配置，可在任务需要固定格式、配置、模板时读取。"
        else:
            allowed_actions = ["run_command"]
            usage_hint = "脚本资源，默认用于执行，不用于读取源码，除非用户明确要求查看脚本内容。"
        catalog.append(
            {
                "resource_handle": f"resource:{len(catalog)}",
                "kind": kind,
                "path": path,
                "title": title,
                "allowed_actions": allowed_actions,
                "usage_hint": usage_hint,
            }
        )

    for match in pattern.finditer(body_prompt):
        title = (match.group("title") or "").lstrip("：").strip()
        _add_entry(match.group("path").strip(), title)

    # 文件系统兜底：扫描磁盘上真实存在的文件，补充正则未捕获的条目
    if execution_root is not None:
        execution_root_resolved = execution_root.resolve()
        # Guard: only scan if execution_root itself is within an allowed root.
        if any(_is_within_sandbox(execution_root_resolved, r.resolve()) for r in _allowed_skill_roots()):
            for subdir in ("scripts", "references", "assets"):
                scan_dir = execution_root_resolved / subdir
                if not scan_dir.is_dir():
                    continue
                for entry in sorted(scan_dir.iterdir()):
                    # Reject symlinks that escape the skill sandbox
                    if not _is_within_sandbox(entry, execution_root_resolved):
                        continue
                    if entry.is_file():
                        _add_entry(f"{subdir}/{entry.name}")

    return catalog

def _resource_catalog_for_planner(catalog: list[dict]) -> list[dict]:
    """Expose resource tree to planner without exposing executable paths for read_resource."""
    return [
        {
            "resource_handle": item["resource_handle"],
            "display_path": item.get("path", ""),
            "kind": item["kind"],
            "title": item.get("title", ""),
            "allowed_actions": item.get("allowed_actions", []),
            "usage_hint": item.get("usage_hint", ""),
        }
        for item in catalog
    ]

def _resource_catalog_by_handle(catalog: list[dict]) -> dict[str, dict]:
    return {str(item["resource_handle"]): item for item in catalog}


def _resolve_resource_handle_alias(value: str, resource_catalog: list[dict]) -> tuple[str | None, dict | None, bool]:
    """Resolve real resource handles and path-like pseudo handles deterministically.

    Returns (canonical_handle, catalog_item, was_alias).  The resolver accepts
    model mistakes such as ``resource:story_templates.md`` only when they map
    uniquely to a catalog path/basename.
    """
    raw = str(value or "").strip()
    if not raw:
        return None, None, False
    by_handle = _resource_catalog_by_handle(resource_catalog)
    if raw in by_handle:
        return raw, by_handle[raw], False

    alias = raw.replace("\\", "/").strip().strip("`'\"").lstrip("./")
    if alias.startswith("resource:"):
        suffix = alias.split(":", 1)[1].strip().lstrip("./")
        # resource:0 is a real handle shape; if it was not found above, do not
        # reinterpret it as a path-like alias.
        if suffix.isdigit():
            return None, None, False
        alias = suffix

    if not alias:
        return None, None, False
    candidates: list[dict] = []
    for item in resource_catalog:
        path = _normalize_skill_resource_path(str(item.get("path") or ""))
        basename = Path(path).name
        if alias == path or alias == basename or alias.endswith("/" + basename):
            candidates.append(item)
    if len(candidates) != 1:
        return None, None, False
    item = candidates[0]
    handle = str(item.get("resource_handle") or "")
    return (handle, item, True) if handle else (None, None, False)

def _compose_resource_selection_prompt() -> str:
    return (
        "你是 Skill 资源按需加载选择器。\n\n"
        "你会看到 Loaded SKILL.md、resource_catalog 和用户请求。"
        "你的任务是判断当前阶段是否需要读取 references/assets/scripts 中的资源正文。\n\n"
        "重要规则：\n"
        "1. 只能从 resource_catalog 中选择真实存在的 resource_handle，例如 resource:0。\n"
        "2. 禁止生成、拼接、改写资源 path；禁止输出 resource:<filename> 或 resource:<path> 这种伪 handle。\n"
        "3. display_path 只帮助你理解 resource:0 对应哪个文件，action 中仍只能输出 resource_handle。\n"
        "4. references 通常用于方法论、规范、示例；scripts 默认用于执行，不要读取源码，除非用户明确要求查看脚本内容。\n"
        "5. assets 在需要模板或配置时读取。\n"
        "6. 如果 SKILL.md body 已经足够完成任务，可以不读取资源。\n"
        "7. 最多选择 5 个资源，避免一次加载过多。\n"
        "8. 只输出严格 JSON，不要 Markdown，不要解释。\n\n"
        "输出格式：\n"
        "{\n"
        "  \"need_resources\": true,\n"
        "  \"resource_handles\": [\"resource:0\", \"resource:1\"],\n"
        "  \"reason\": \"简短原因\"\n"
        "}\n\n"
        "如果不需要资源：\n"
        "{\n"
        "  \"need_resources\": false,\n"
        "  \"resource_handles\": [],\n"
        "  \"reason\": \"简短原因\"\n"
        "}\n"
    )

def _parse_resource_selection_decision(
    text: str,
    *,
    resource_catalog: list[dict],
) -> dict:
    stripped = _strip_markdown_json_fence(text)

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        logger.warning("resource selection decision is not valid JSON: %s", text[:500])
        return {"need_resources": False, "resource_handles": [], "reason": "JSON 解析失败"}

    if not isinstance(data, dict):
        return {"need_resources": False, "resource_handles": [], "reason": "输出不是 JSON object"}

    need_resources = data.get("need_resources", False)
    if isinstance(need_resources, str):
        need_resources = need_resources.strip().lower() in {"true", "1", "yes", "y"}
    else:
        need_resources = bool(need_resources)

    raw_handles = data.get("resource_handles", [])

    if not isinstance(raw_handles, list):
        raw_handles = []

    selected: list[str] = []
    for item in raw_handles:
        handle = str(item or "").strip()
        if not handle:
            continue
        canonical, _resource, _was_alias = _resolve_resource_handle_alias(handle, resource_catalog)
        if not canonical:
            continue
        if canonical not in selected:
            selected.append(canonical)
        if len(selected) >= 5:
            break

    if not need_resources or not selected:
        return {
            "need_resources": False,
            "resource_handles": [],
            "reason": str(data.get("reason") or "").strip(),
        }

    return {
        "need_resources": True,
        "resource_handles": selected,
        "reason": str(data.get("reason") or "").strip(),
    }

async def _run_resource_selection_round(
    *,
    body_prompt: str,
    request: ChatRequest,
    model: str,
    resource_catalog: list[dict],
) -> dict:
    if not resource_catalog:
        return {"need_resources": False, "resource_handles": [], "reason": "无可用资源"}

    messages = [
        {"role": "system", "content": _compose_resource_selection_prompt()},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "loaded_skill_prompt": body_prompt,
                    "resource_catalog": _resource_catalog_for_planner(resource_catalog),
                    "user_messages": _request_messages_with_files(request),
                    "last_user_text": _last_user_text(request),
                },
                ensure_ascii=False,
            ),
        },
    ]

    decision_text = await complete_chat_once(messages, _planner_model_name(model))
    return _parse_resource_selection_decision(
        decision_text,
        resource_catalog=resource_catalog,
    )

def _read_runtime_skill_resource_text(
    *,
    skill_name: str,
    path: str,
    execution_root: Path | None = None,
) -> dict:
    """Read a skill-local resource from the execution root when available."""
    rel_path = _normalize_skill_resource_path(path)
    if execution_root is not None:
        root = execution_root.resolve()
        resource_path = (root / rel_path).resolve()
        if not _is_within_sandbox(resource_path, root) or not resource_path.is_file():
            raise FileNotFoundError(f"resource does not exist in current skill: {rel_path}")
        raw = resource_path.read_text(encoding="utf-8", errors="replace")
        max_chars = settings.skill_resource_max_chars
        return {
            "content": raw[:max_chars],
            "truncated": len(raw) > max_chars,
        }

    if not skill_name:
        raise ValueError("读取 Skill 资源需要 skill_name 或 execution_root")

    return read_skill_resource_text(
        skill_name,
        rel_path,
        max_chars=settings.skill_resource_max_chars,
    )


def _compose_loaded_resources_prompt(
    *,
    skill_name: str,
    resource_catalog: list[dict],
    selected_handles: list[str],
    execution_root: Path | None = None,
) -> dict:
    resource_by_handle = _resource_catalog_by_handle(resource_catalog)
    sections: list[str] = []
    loaded_paths: list[str] = []
    failed_paths: list[dict] = []

    for handle in selected_handles:
        resource = resource_by_handle.get(handle)
        if not resource:
            failed_paths.append({
                "resource_handle": handle,
                "path": "",
                "missing_type": "planner_inconsistent",
                "reason": "selected resource_handle is not in resource_catalog",
            })
            continue

        path = _normalize_skill_resource_path(resource["path"])
        try:
            observation = _read_runtime_skill_resource_text(
                skill_name=skill_name,
                path=path,
                execution_root=execution_root,
            )
        except FileNotFoundError as exc:
            failed_paths.append({
                "resource_handle": handle,
                "path": path,
                "missing_type": "file_missing",
                "reason": str(exc),
            })
            sections.append(
                f"### {handle}\n"
                f"- path: `{path}`\n"
                f"- load_error_type: file_missing\n"
                f"- load_error: {exc}\n"
            )
            continue
        except Exception as exc:
            failed_paths.append({
                "resource_handle": handle,
                "path": path,
                "missing_type": "load_failed",
                "reason": str(exc),
            })
            sections.append(
                f"### {handle}\n"
                f"- path: `{path}`\n"
                f"- load_error_type: load_failed\n"
                f"- load_error: {exc}\n"
            )
            continue

        loaded_paths.append(path)
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

    prompt = ""
    if sections:
        prompt = (
            "\n\n---\n\n"
            "## Loaded On-Demand Resources\n\n"
            "以下资源由宿主根据当前请求按需读取。"
            "这些内容现在可以作为执行当前 Skill 的依据。"
            "如果资源已成功加载，后续规划不得再把它标记为 missing。\n\n"
            + "\n\n".join(sections)
        )

    return {
        "prompt": prompt,
        "loaded_paths": loaded_paths,
        "failed_paths": failed_paths,
    }

def _parse_need_body_decision(text: str) -> bool:
    """Parse first-round metadata decision.

    解析失败时默认进入正文阶段，避免模型格式错误导致 Skill 无法执行。
    """
    stripped = _strip_markdown_json_fence(text)

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        logger.warning("metadata decision is not valid JSON: %s", text[:500])
        return True

    need_body = data.get("need_body", True)

    if isinstance(need_body, bool):
        return need_body

    if isinstance(need_body, str):
        return need_body.strip().lower() in {"true", "1", "yes", "y"}

    return bool(need_body)

def _parse_child_skill_decision(
    text: str,
    *,
    valid_child_refs: set[str] | None = None,
) -> dict:
    """Parse child-skill loading decision.

    关键规则：
    - 只有 child_ref 出现在 Child Skills Manifest 的真实 ref 中，才允许 need_child=true。
    - 模型复制示例 ref 或猜测不存在 ref 时，一律降级为 need_child=false。
    """
    valid_child_refs = valid_child_refs or set()
    stripped = _strip_markdown_json_fence(text)

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        logger.warning("child skill decision is not valid JSON: %s", text[:500])
        return {"need_child": False, "child_ref": "", "reason": "JSON 解析失败"}

    if not isinstance(data, dict):
        return {"need_child": False, "child_ref": "", "reason": "输出不是 JSON object"}

    need_child = data.get("need_child", False)

    if isinstance(need_child, str):
        need_child = need_child.strip().lower() in {"true", "1", "yes", "y"}
    else:
        need_child = bool(need_child)

    child_ref = str(data.get("child_ref") or "").strip()
    reason = str(data.get("reason") or "").strip()

    if not need_child:
        return {"need_child": False, "child_ref": "", "reason": reason}

    if not child_ref:
        return {
            "need_child": False,
            "child_ref": "",
            "reason": "need_child=true 但缺少 child_ref",
        }

    if child_ref not in valid_child_refs:
        return {
            "need_child": False,
            "child_ref": "",
            "reason": (
                "模型返回的 child_ref 不在 Child Skills Manifest 中，已忽略："
                + child_ref
            ),
        }

    return {
        "need_child": True,
        "child_ref": child_ref,
        "reason": reason,
    }

def _extract_child_refs_from_metadata_prompt(metadata_prompt: str) -> set[str]:
    """Extract valid child skill refs from Child Skills Manifest.

    只信任 metadata prompt 中真实出现的：
    - ref: `xxx`
    """
    refs: set[str] = set()

    marker = "## Child Skills Manifest"
    index = metadata_prompt.find(marker)
    if index < 0:
        return refs

    section = metadata_prompt[index:]

    # 截到下一个 markdown 分隔符，避免误扫后面的 resource manifest
    next_sep = section.find("\n---\n")
    if next_sep >= 0:
        section = section[:next_sep]

    for match in re.finditer(r"-\s+ref:\s+`([^`]+)`", section):
        ref = match.group(1).strip()
        if ref and ref != "无":
            refs.add(ref)

    return refs

async def _run_metadata_round(
    *,
    metadata_prompt: str,
    request: ChatRequest,
    model: str,
) -> bool:
    """First internal model round.

    这一轮只给模型 metadata，不给 SKILL.md 正文。
    不向前端流式输出，只用于决定是否进入正文阶段。
    """
    messages = [{"role": "system", "content": metadata_prompt}]
    messages.extend(_request_messages_with_files(request))

    decision_text = await complete_chat_once(messages, model)
    return _parse_need_body_decision(decision_text)

def _compose_child_skill_selection_prompt() -> str:
    return (
        "你是 Skill 分层加载运行时的子 Skill 选择器。\n\n"
        "你会看到父 Skill 的 metadata prompt、valid_child_refs 和用户请求。\n"
        "你的任务是根据用户请求判断是否需要加载某一个子 Skill 的完整 SKILL.md 正文。\n\n"
        "重要规则：\n"
        "1. 只能从 valid_child_refs 中选择 child_ref。\n"
        "2. 如果 valid_child_refs 为空，必须 need_child=false。\n"
        "3. Child Skill 必须是包含 SKILL.md 的子目录，不是普通 references/*.md 文件。\n"
        "4. references/*.md、assets/*、scripts/* 都不是子 Skill，不能作为 child_ref 返回。\n"
        "5. 如果用户请求只需要父 Skill 就能完成，need_child=false。\n"
        "6. 如果用户请求明显匹配某个子 Skill 的 description，need_child=true，并返回 valid_child_refs 中的原样 ref。\n"
        "7. 不要猜测不存在的 ref。\n"
        "8. 不要复制示例占位符。\n"
        "9. 只输出严格 JSON，不要 Markdown，不要解释。\n\n"
        "如果需要子 Skill，输出：\n"
        "{\n"
        "  \"need_child\": true,\n"
        "  \"child_ref\": \"<必须是 valid_child_refs 中的一个值>\",\n"
        "  \"reason\": \"简短原因\"\n"
        "}\n\n"
        "如果不需要子 Skill，输出：\n"
        "{\n"
        "  \"need_child\": false,\n"
        "  \"child_ref\": \"\",\n"
        "  \"reason\": \"简短原因\"\n"
        "}\n"
    )

async def _run_child_skill_selection_round(
    *,
    parent_metadata_prompt: str,
    request: ChatRequest,
    model: str,
) -> dict:
    """Decide whether a child Skill body should be loaded.

    这一轮只使用父 Skill metadata prompt 中的 Child Skills Manifest。
    不读取子 Skill 正文。
    """
    valid_child_refs = _extract_child_refs_from_metadata_prompt(parent_metadata_prompt)

    if not valid_child_refs:
        return {
            "need_child": False,
            "child_ref": "",
            "reason": "Child Skills Manifest 中没有可用子 Skill",
        }

    messages = [
        {"role": "system", "content": _compose_child_skill_selection_prompt()},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "valid_child_refs": sorted(valid_child_refs),
                    "parent_metadata_prompt": parent_metadata_prompt,
                    "user_messages": _request_messages_with_files(request),
                },
                ensure_ascii=False,
            ),
        },
    ]

    decision_text = await complete_chat_once(messages, model)
    return _parse_child_skill_decision(
        decision_text,
        valid_child_refs=valid_child_refs,
    )

_VISION_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _request_messages_with_inline_images(request: ChatRequest, execution_root: Path | None) -> list[dict]:
    """Build OpenAI-compatible multimodal user messages for VL models."""
    messages = _request_messages_with_files(request)
    if execution_root is None or not request.input_files:
        return messages

    image_parts: list[dict] = []
    root = execution_root.resolve()
    for item in request.input_files:
        rel = str(item.get("path") or "")
        path = (root / rel).resolve()
        if path.suffix.lower() not in _VISION_IMAGE_EXTS:
            continue
        if not _is_within_sandbox(path, root) or not path.is_file():
            continue
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        image_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{encoded}"},
        })

    if not image_parts:
        return messages

    for i in reversed(range(len(messages))):
        if messages[i].get("role") == "user":
            text = str(messages[i].get("content") or "")
            messages[i] = {
                "role": "user",
                "content": [{"type": "text", "text": text}, *image_parts],
            }
            break
    return messages


def _strip_runtime_resource_manifest(body_prompt: str) -> str:
    """Remove generated resource manifest section from planner text.

    避免 planner 从 Markdown 资源清单中拼接路径。
    真实资源树通过 resource_catalog 单独传入。
    """
    marker = "## Bundled Resources Manifest"
    index = body_prompt.find(marker)
    if index < 0:
        return body_prompt

    before = body_prompt[:index].rstrip()
    return (
        before
        + "\n\n---\n\n"
        + "## Bundled Resources Manifest\n\n"
        + "资源清单已由宿主以结构化 resource_catalog 单独提供。"
        + "规划 read_resource 时只能使用 resource_handle，不能生成 path。\n"
    )

_COMMAND_BLOCK_LANGS = {"bash", "sh", "shell", "zsh", "console", "terminal"}
_COMMAND_BLOCK_CODE_RE = re.compile(
    r"(?im)(^|\n)\s*(?:python(?:3)?\s+)?scripts/[^\s`]+|"
    r"(^|\n)\s*(?:python|python3|node|npm|npx|bash|sh)\s+[^\n]*scripts/"
)
_HOST_COMMAND_INSTRUCTION_RE = re.compile(
    r"(?i)fenced\s+code\s+block|```|run_command|run command|execute command|"
    r"执行命令|运行命令|执行脚本|运行脚本|调用脚本|scripts/|输出[^\n]{0,30}(?:命令|可执行)"
)

_SKILL_LOCAL_RESOURCE_RE = re.compile(r"(?<![\w./-])(?P<path>(?:scripts|references|assets)/[A-Za-z0-9_./-]+)")
_ACTION_SCHEMA_FIELD_RE = re.compile(r"(?:{field})\s*[：:=]\s*\[?([^\]\n;]+)\]?", re.I)
_ACTION_SCHEMA_ROLE_RE = re.compile(r"(?:role|角色|职责)\s*[：:=]\s*(text_generator|image_generator|composite_generator|pdf_builder|docx_builder|pptx_builder|html_asset_builder|asset_builder|generic_script)", re.I)
_RUNTIME_PLACEHOLDER_RE = re.compile(r"{{\s*([A-Za-z_][\w-]*)\s*}}")
_SCRIPT_ROLES = {"text_generator", "image_generator", "composite_generator", "pdf_builder", "docx_builder", "pptx_builder", "html_asset_builder", "asset_builder", "generic_script"}
_HIGH_IMPACT_CAPABILITIES = {"image_generation", "pdf_generation", "docx_generation", "pptx_generation", "html_generation"}


def _extract_script_path_from_command(command: str) -> str | None:
    """Return the skill-local scripts/... path invoked by a command."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    for part in parts:
        normalized = part.replace("\\", "/").lstrip("./")
        if normalized.startswith("scripts/"):
            return normalized
        idx = normalized.find("/scripts/")
        if idx >= 0:
            return normalized[idx + 1 :]
    return None


def _command_json_argv_keys(command: str, script_path: str | None = None) -> set[str] | None:
    """Return JSON argv keys after the script path, accepting template placeholders."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    for idx, part in enumerate(parts):
        normalized = part.replace("\\", "/").lstrip("./")
        matches = bool(script_path and normalized.endswith(script_path)) or normalized.startswith("scripts/")
        if not matches:
            continue
        if idx + 1 >= len(parts):
            return set()
        candidate = parts[idx + 1]
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return {str(key) for key in payload.keys()}
    return None


def _parse_schema_list_field(text: str, field: str) -> list[str]:
    pattern = _ACTION_SCHEMA_FIELD_RE.pattern.format(field=re.escape(field))
    match = re.search(pattern, text or "", re.I)
    if not match:
        return []
    values = [item.strip().strip("'\"") for item in re.split(r"[,，、]\s*", match.group(1)) if item.strip()]
    cleaned: list[str] = []
    for item in values:
        # Keep only the JSON key.  Runtime contracts may annotate optional
        # inputs as ``custom_character?`` or ``style (optional)``; those
        # annotations must not become part of the argv key.
        item = re.split(r"\s*(?:：|:|=|（|\(|\s)\s*", item, maxsplit=1)[0]
        item = item.rstrip("?")
        item = re.sub(r"[^A-Za-z0-9_./-]", "", item)
        if item:
            cleaned.append(item)
    return cleaned


def _parse_optional_schema_inputs(text: str) -> list[str]:
    """Extract optional JSON argv keys declared near an Action schema block."""
    optional = set(_parse_schema_list_field(text, "optional_inputs"))
    optional.update(_parse_schema_list_field(text, "optional inputs"))
    inputs_match = re.search(_ACTION_SCHEMA_FIELD_RE.pattern.format(field=re.escape("inputs")), text or "", re.I)
    if inputs_match:
        for raw_item in re.split(r"[,，、]\s*", inputs_match.group(1)):
            if re.search(r"(?:\?|optional|可选|选填)", raw_item, re.I):
                key = re.split(r"\s*(?:：|:|=|（|\(|\s)\s*", raw_item.strip().strip("'\""), maxsplit=1)[0].rstrip("?")
                key = re.sub(r"[^A-Za-z0-9_./-]", "", key)
                if key:
                    optional.add(key)
    return sorted(optional)


def _block_context(text: str, block: MarkdownBlock) -> str:
    before = (block.before_context or "")[-800:]
    after = (block.after_context or "")[:800]
    return before + "\n" + after


def _extract_action_schemas_from_text(text: str, *, source_path: str) -> list[dict]:
    """Extract portable Action schema entries from Markdown shell blocks.

    This keeps the fenced-block compatibility path but normalizes it into a
    schema the runtime can validate before execution.
    """
    schemas: list[dict] = []
    for block in _extract_all_fenced_blocks(text or ""):
        lang = (block.lang or "").lower()
        command = (block.code or "").strip()
        if lang not in _COMMAND_BLOCK_LANGS or not command or not _COMMAND_BLOCK_CODE_RE.search(command):
            continue
        script_path = _extract_script_path_from_command(command)
        if not script_path:
            continue
        context = _block_context(text, block)
        role_match = _ACTION_SCHEMA_ROLE_RE.search(context)
        role = role_match.group(1).lower() if role_match else "generic_script"
        inputs = _parse_schema_list_field(context, "inputs")
        outputs = _parse_schema_list_field(context, "outputs")
        optional_inputs = _parse_optional_schema_inputs(context)
        required_capabilities = _parse_schema_list_field(context, "required_capabilities")
        forbidden_capabilities = _parse_schema_list_field(context, "forbidden_capabilities")
        command_keys = _command_json_argv_keys(command, script_path)
        placeholder_keys = set(_RUNTIME_PLACEHOLDER_RE.findall(command))
        schemas.append({
            "script_path": script_path,
            "command": command,
            "source_path": source_path,
            "role": role,
            "inputs": inputs,
            "optional_inputs": optional_inputs,
            "outputs": outputs,
            "required_capabilities": required_capabilities,
            "forbidden_capabilities": forbidden_capabilities,
            "command_keys": sorted(command_keys) if command_keys is not None else None,
            "placeholder_keys": sorted(placeholder_keys),
        })
    return schemas


def _reference_contract_texts(execution_root: Path | None) -> dict[str, str]:
    if execution_root is None:
        return {}
    root = execution_root.resolve()
    references_dir = root / "references"
    if not references_dir.is_dir() or not _is_within_sandbox(references_dir, root):
        return {}
    texts: dict[str, str] = {}
    for path in sorted(references_dir.rglob("*.md")):
        if not path.is_file() or not _is_within_sandbox(path, root):
            continue
        rel = path.relative_to(root).as_posix()
        texts[rel] = path.read_text(encoding="utf-8", errors="replace")[: settings.skill_resource_max_chars]
    return texts


def _validate_action_schema_entries(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (errors, warnings) for runtime action schemas."""
    errors: list[dict] = []
    warnings: list[dict] = []
    by_script: dict[str, list[dict]] = {}
    for entry in entries:
        by_script.setdefault(str(entry.get("script_path") or ""), []).append(entry)
        role = str(entry.get("role") or "generic_script")
        if role not in _SCRIPT_ROLES:
            errors.append({"error": "未知 script role", "entry": entry})
        command_keys = set(entry.get("command_keys") or [])
        inputs = set(entry.get("inputs") or [])
        optional_inputs = set(entry.get("optional_inputs") or [])
        required_inputs = inputs - optional_inputs
        if inputs and not (required_inputs <= command_keys <= inputs):
            errors.append({
                "error": "命令块 JSON keys 与 Action schema inputs 不一致",
                "script_path": entry.get("script_path"),
                "source_path": entry.get("source_path"),
                "inputs": sorted(inputs),
                "optional_inputs": sorted(optional_inputs),
                "required_inputs": sorted(required_inputs),
                "command_keys": sorted(command_keys),
            })
        if role == "generic_script" and set(entry.get("required_capabilities") or []) & _HIGH_IMPACT_CAPABILITIES:
            errors.append({
                "error": "generic_script 不允许声明高风险能力，必须显式声明 image_generator、pdf_builder、docx_builder、pptx_builder、html_asset_builder 或 composite_generator role",
                "script_path": entry.get("script_path"),
                "required_capabilities": entry.get("required_capabilities"),
            })
        if role == "generic_script":
            warnings.append({
                "warning": "低置信度/未显式 role 的 generic_script runtime fallback；不会自动启用图片/PDF/Word/PPT/HTML 等高风险能力",
                "script_path": entry.get("script_path"),
                "source_path": entry.get("source_path"),
            })

    for script_path, script_entries in by_script.items():
        distinct_commands = {str(item.get("command") or "").strip() for item in script_entries}
        if len(distinct_commands) > 1:
            errors.append({
                "error": "同一 script 存在多个不一致执行入口",
                "script_path": script_path,
                "sources": [item.get("source_path") for item in script_entries],
            })
        elif len(script_entries) > 1:
            warnings.append({
                "warning": "同一 script 的执行入口在多个文档中重复声明；runtime 将按唯一命令执行",
                "script_path": script_path,
                "sources": [item.get("source_path") for item in script_entries],
            })
    return errors, warnings


def _build_runtime_action_schema(body_prompt: str, *, execution_root: Path | None = None) -> dict:
    """Build a unified Action schema from SKILL.md and reference command blocks."""
    skill_text = _strip_runtime_resource_manifest(body_prompt)
    reference_texts = _reference_contract_texts(execution_root)
    texts: list[tuple[str, str]] = [("SKILL.md", skill_text), *reference_texts.items()]
    entries: list[dict] = []
    for source_path, text in texts:
        entries.extend(_extract_action_schemas_from_text(text, source_path=source_path))
    errors, warnings = _validate_action_schema_entries(entries)
    errors.extend(_validate_referenced_assets_in_texts(texts, execution_root=execution_root))
    canonical: dict[str, dict] = {}
    for entry in entries:
        script_path = str(entry.get("script_path") or "")
        if script_path and script_path not in canonical:
            canonical[script_path] = entry
    return {
        "version": "skill-action-schema/v1",
        "entries": list(canonical.values()),
        "errors": errors,
        "warnings": warnings,
    }


def _find_runtime_action_entry(action_schema: dict, command: str) -> dict | None:
    script_path = _extract_script_path_from_command(command)
    if not script_path:
        return None
    for entry in action_schema.get("entries") or []:
        if entry.get("script_path") == script_path:
            return entry
    return None


def _validate_runtime_command_against_action_schema(command: str, *, execution_root: Path | None) -> dict | None:
    """Ensure a runtime command block is declared by SKILL.md/reference schema."""
    script_path = _extract_script_path_from_command(command)
    if not script_path:
        return None
    if execution_root is not None:
        root = execution_root.resolve()
        available_scripts = set(_available_scripts_for_root(root))
        script_file = (root / script_path).resolve()
        if script_path not in available_scripts or not _is_within_sandbox(script_file, root) or not script_file.is_file():
            raise ValueError(
                f"命令调用 {script_path}，但该脚本不在当前 Skill available_scripts 中："
                f"available={sorted(available_scripts)}"
            )
    skill_md = ""
    if execution_root is not None and (execution_root / "SKILL.md").is_file():
        skill_md = (execution_root / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    action_schema = _build_runtime_action_schema(skill_md, execution_root=execution_root)
    if action_schema.get("errors"):
        raise ValueError("Skill Action schema 校验失败: " + json.dumps(action_schema["errors"], ensure_ascii=False))
    entry = _find_runtime_action_entry(action_schema, command)
    if entry is None:
        raise ValueError(f"命令调用 {script_path}，但 SKILL.md/references 中没有唯一声明的执行入口")
    expected_keys = set(entry.get("inputs") or entry.get("command_keys") or [])
    optional_keys = set(entry.get("optional_inputs") or [])
    required_keys = expected_keys - optional_keys
    actual_keys = _command_json_argv_keys(command, script_path)
    if actual_keys is None:
        raise ValueError(f"命令 {script_path} 必须使用可解析 JSON argv")
    if expected_keys and not (required_keys <= actual_keys <= expected_keys):
        raise ValueError(
            f"命令 {script_path} JSON keys 与 Action schema inputs 不一致："
            f"expected={sorted(expected_keys)} optional={sorted(optional_keys)} actual={sorted(actual_keys)}"
        )
    return entry



def _payload_has_file_field(payload: dict, *keys: str) -> bool:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, list) and any(isinstance(item, str) and item.strip() for item in value):
            return True
    return False

def _validate_stdout_against_action_entry(stdout: str, entry: dict | None) -> None:
    if not entry:
        _validate_success_stdout_json_if_structured(stdout)
        return
    stripped = (stdout or "").strip()
    if not stripped:
        return
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        if str(entry.get("role")) in {"text_generator", "image_generator", "composite_generator", "pdf_builder", "docx_builder", "pptx_builder", "html_asset_builder", "asset_builder"}:
            raise ValueError("角色脚本 stdout 必须是 JSON object")
        return
    if not isinstance(payload, dict):
        raise ValueError("角色脚本 stdout 必须是 JSON object")
    _validate_structured_stdout_payload(payload)
    role = str(entry.get("role") or "generic_script")
    required_capabilities = set(entry.get("required_capabilities") or [])
    outputs = set(entry.get("outputs") or [])
    if role == "composite_generator":
        if ("text_generation" in required_capabilities or "text" in outputs or "story_text" in outputs) and not str(payload.get("text") or payload.get("story_text") or payload.get("markdown") or "").strip():
            raise ValueError("composite_generator stdout JSON 缺少声明的 text/markdown 输出")
        if ("image_generation" in required_capabilities or {"image_path", "image_paths", "images"} & outputs) and not _payload_image_paths(payload):
            raise ValueError("composite_generator stdout JSON 缺少声明的 image_path/image_paths/images 输出")
        if ("pdf_generation" in required_capabilities or {"pdf_path", "file_paths"} & outputs) and not _payload_has_file_field(payload, "pdf_path", "file_paths"):
            raise ValueError("composite_generator stdout JSON 缺少声明的 pdf_path/file_paths 输出")
        if ("docx_generation" in required_capabilities or {"docx_path"} & outputs) and not _payload_has_file_field(payload, "docx_path", "file_paths"):
            raise ValueError("composite_generator stdout JSON 缺少声明的 docx_path/file_paths 输出")
        if ("pptx_generation" in required_capabilities or {"pptx_path"} & outputs) and not _payload_has_file_field(payload, "pptx_path", "file_paths"):
            raise ValueError("composite_generator stdout JSON 缺少声明的 pptx_path/file_paths 输出")
        if ("html_generation" in required_capabilities or {"html_path", "asset_paths"} & outputs) and not _payload_has_file_field(payload, "html_path", "asset_paths"):
            raise ValueError("composite_generator stdout JSON 缺少声明的 html_path/asset_paths 输出")
    if role == "text_generator" and not str(payload.get("text") or payload.get("story_text") or payload.get("markdown") or "").strip():
        raise ValueError("text_generator stdout JSON 必须包含非空 text/markdown")
    if role == "image_generator" and not _payload_image_paths(payload):
        raise ValueError("image_generator stdout JSON 必须包含 image_path/image_paths/images")
    if role == "pdf_builder" and not _payload_has_file_field(payload, "pdf_path", "file_paths"):
        raise ValueError("pdf_builder stdout JSON 必须包含 pdf_path 或 file_paths")
    if role == "docx_builder" and not _payload_has_file_field(payload, "docx_path", "file_paths"):
        raise ValueError("docx_builder stdout JSON 必须包含 docx_path 或 file_paths")
    if role == "pptx_builder" and not _payload_has_file_field(payload, "pptx_path", "file_paths"):
        raise ValueError("pptx_builder stdout JSON 必须包含 pptx_path 或 file_paths")
    if role in {"html_asset_builder", "asset_builder"}:
        html_path = payload.get("html_path")
        asset_paths = payload.get("asset_paths")
        has_html = isinstance(html_path, str) and html_path.strip()
        has_html = has_html or (isinstance(asset_paths, list) and any(isinstance(p, str) and p.strip() for p in asset_paths))
        if not has_html:
            raise ValueError("html_asset_builder stdout JSON 必须包含 html_path 或 asset_paths")




def _image_dimensions_from_bytes(data: bytes) -> tuple[int, int] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a") and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data.startswith(b"\xff\xd8"):
        idx = 2
        while idx + 9 < len(data):
            if data[idx] != 0xFF:
                idx += 1
                continue
            marker = data[idx + 1]
            idx += 2
            if marker in {0xD8, 0xD9}:
                continue
            if idx + 2 > len(data):
                break
            length = int.from_bytes(data[idx:idx + 2], "big")
            if length < 2:
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and idx + 7 <= len(data):
                return int.from_bytes(data[idx + 5:idx + 7], "big"), int.from_bytes(data[idx + 3:idx + 5], "big")
            idx += length
    return None


def _validate_runtime_asset_contract(path: Path, *, root: Path | None = None) -> None:
    """Type-aware runtime asset validation for referenced/read assets."""
    if not path.is_file():
        raise ValueError(f"asset 不存在或不是文件: {path}")
    if root is not None and not _is_within_sandbox(path, root):
        raise ValueError(f"asset 路径越界: {path}")
    ext = path.suffix.lower()
    data = path.read_bytes()
    if not data:
        raise ValueError(f"asset 不能为空: {path}")
    if ext == ".json":
        json.loads(data.decode("utf-8"))
    elif ext == ".csv":
        rows = list(csv.reader(io.StringIO(data.decode("utf-8"))))
        if len(rows) < 2 or not rows[0] or any(not str(cell).strip() for cell in rows[0]):
            raise ValueError(f"CSV asset 必须包含非空表头和数据行: {path}")
    elif ext in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        magic_ok = data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff") or data.startswith(b"GIF87a") or data.startswith(b"GIF89a") or (data.startswith(b"RIFF") and b"WEBP" in data[:16])
        if not magic_ok:
            raise ValueError(f"image asset 文件头不合法: {path}")
        dims = _image_dimensions_from_bytes(data)
        if dims is not None and (dims[0] < 1 or dims[1] < 1):
            raise ValueError(f"image asset 尺寸不合法: {path}")
    elif ext == ".pdf":
        if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-4096:]:
            raise ValueError(f"PDF asset 文件格式不合法: {path}")
    elif ext in {".md", ".txt", ".yaml", ".yml", ".jinja", ".jinja2", ".template", ".tmpl"}:
        text = data.decode("utf-8", errors="replace").strip()
        if not text:
            raise ValueError(f"Markdown/text asset 不能为空: {path}")


def _validate_referenced_assets_in_texts(texts: list[tuple[str, str]], *, execution_root: Path | None) -> list[dict]:
    if execution_root is None:
        return []
    root = execution_root.resolve()
    errors: list[dict] = []
    seen: set[str] = set()
    for source_path, text in texts:
        for match in _SKILL_LOCAL_RESOURCE_RE.finditer(text or ""):
            rel_path = match.group("path")
            if not rel_path.startswith("assets/") or rel_path in seen:
                continue
            seen.add(rel_path)
            try:
                _validate_runtime_asset_contract((root / rel_path).resolve(), root=root)
            except Exception as exc:
                errors.append({"error": str(exc), "source_path": source_path, "asset_path": rel_path})
    return errors


def _extract_skill_command_contract(body_prompt: str, reference_texts: dict[str, str] | None = None, execution_root: Path | None = None) -> dict:
    """Extract concrete host-executable command examples declared in SKILL.md.

    The sandbox must not ask the final model to invent script invocations from an
    inline `scripts/...` mention.  A skill that wants host execution must include
    a concrete shell fenced block that shows the invocation shape.
    """
    skill_text = _strip_runtime_resource_manifest(body_prompt)
    texts: list[tuple[str, str]] = [("SKILL.md", skill_text)]
    if reference_texts is not None:
        texts.extend((path, text) for path, text in reference_texts.items())
    elif execution_root is not None:
        texts.extend((path, text) for path, text in _reference_contract_texts(execution_root).items())

    command_blocks: list[dict] = []
    action_entries: list[dict] = []
    for source_path, text in texts:
        blocks = _extract_all_fenced_blocks(text)
        for block in blocks:
            lang = (block.lang or "").lower()
            code = (block.code or "").strip()
            if lang not in _COMMAND_BLOCK_LANGS or not code:
                continue
            if not _COMMAND_BLOCK_CODE_RE.search(code):
                continue
            command_blocks.append({
                "block_index": block.index,
                "lang": lang,
                "code": code[:600],
                "before_context": block.before_context[-300:],
                "source_path": source_path,
            })
        action_entries.extend(_extract_action_schemas_from_text(text, source_path=source_path))

    errors, warnings = _validate_action_schema_entries(action_entries)
    errors.extend(_validate_referenced_assets_in_texts(texts, execution_root=execution_root))

    return {
        "has_executable_command_block": bool(command_blocks),
        "command_blocks": command_blocks[:5],
        "action_schema": {
            "version": "skill-action-schema/v1",
            "entries": action_entries[:20],
            "errors": errors,
            "warnings": warnings,
        },
    }


def _final_instruction_requests_host_command(final_instruction: str) -> bool:
    return bool(_HOST_COMMAND_INSTRUCTION_RE.search(final_instruction or ""))


def _extract_executable_command_blocks_from_text(text: str) -> list[str]:
    """Extract host-executable script commands from final_instruction text.

    Prefer shell fenced blocks, but also accept a bare single-line command when
    the planner returned ``final_instruction`` as plain text.  Every returned
    command is still validated later against the current Skill's
    ``available_scripts`` and Action schema before execution.
    """
    commands: list[str] = []
    seen: set[str] = set()

    def add(command: str) -> None:
        command = (command or "").strip()
        if not command or command in seen:
            return
        if not _COMMAND_BLOCK_CODE_RE.search(command):
            return
        seen.add(command)
        commands.append(command)

    for block in _extract_all_fenced_blocks(text or ""):
        lang = (block.lang or "").lower()
        command = (block.code or "").strip()
        if lang not in _COMMAND_BLOCK_LANGS or not command:
            continue
        add(command)

    # Some planners put the SKILL.md command example directly in
    # final_instruction instead of wrapping it in a fenced block.  Only accept
    # self-contained command-looking lines; prose remains ignored.
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        line = re.sub(r"^(?:[-*]\s+|\$\s+)", "", line)
        if not line or line.startswith("```"):
            continue
        if not re.match(r"^(?:python(?:3)?|node|bash|sh)\s+", line):
            continue
        add(line)

    return commands


def _has_successful_run_command_observation(results: list[dict]) -> bool:
    return any(
        isinstance(item, dict)
        and item.get("action") == "run_command"
        and item.get("success", True)
        for item in results
    )


def _execution_requires_run_command_observation(runtime_plan: dict) -> bool:
    final_instruction = str(runtime_plan.get("final_instruction") or "")
    return bool(
        _extract_executable_command_blocks_from_text(final_instruction)
        or _final_instruction_requests_host_command(final_instruction)
    )


def _compose_skill_runtime_planner_prompt() -> str:
    return (
        "你是 Skill Agent 运行时动作意图判断器。\n\n"
        "【重要】你只能输出一个严格的 JSON 对象，绝对不能输出任何自然语言、解释、思考过程或 Markdown 文本。"
        "你的全部输出必须是可直接被 json.loads() 解析的 JSON，不得有任何前缀或后缀。\n\n"
        "你的任务不是回答用户问题，也不是凭空创建命令；你的任务是根据 Loaded SKILL.md、"
        "resource_catalog、available_scripts 和用户请求判断本轮是否需要先让主模型输出显式可执行块。\n\n"
        "核心原则：\n"
        "1. Loaded SKILL.md 是当前 Skill 的执行规范。\n"
        "2. resource_catalog 和 available_scripts 只包含当前业务 Skill 目录内真实存在的 skill-local resources；kernel references 不会暴露给运行时，不能读取或引用。\n"
        "不能用它们推导、补全或发明命令参数。\n"
        "3. 是否执行命令，必须由 SKILL.md/references Action schema 中的显式 shell fenced 命令示例触发；"
        "不要因为磁盘上存在脚本就直接规划 run_command，也不要临时拼接 Skill.md 中没有声明的命令。\n"
        "4. 你可以规划 read_resource，因为读取 reference/asset 是宿主受控动作；"
        "需要执行脚本时，把替换真实参数后的完整命令放入 final_instruction 的 shell fenced block；"
        "不要在 actions 中规划 run_command、write_file 或 create_directory。\n"
        "5. 如果任务需要运行 scripts、生成 PPT/Excel/Word/PDF/图片等文件，或 Loaded SKILL.md 明确要求调用脚本，"
        "只有在 SKILL.md 或其明确引用的 references 中已经包含具体 shell fenced 命令示例、且宿主 Action schema 校验通过时，才可使用 mode=direct_answer 并让主模型按该示例替换真实参数。\n"
        "6. 如果 Skill.md/reference 只写了 `scripts/...` 行内路径、‘调用脚本’等自然语言，但没有具体 fenced 命令示例，"
        "必须使用 mode=ask_user，说明该 Skill 缺少可执行命令 block 示例，不能让主模型临时拼命令。\n"
        "7. 如果 available_scripts 和 resource_catalog 中没有对应脚本，而任务必须依赖脚本，应使用 mode=ask_user 并说明缺少脚本。\n"
        "8. 你不能把函数名、伪代码函数、Python 函数、自然语言动作当成系统命令。\n"
        "9. 如果当前 Skill 是写作、故事生成、公文生成、报告生成、总结、翻译、润色、分析、咨询等语言生成类任务，"
        "且最终产物是纯文本或 Markdown（不是 .pptx/.xlsx/.docx 等格式文件），"
        "应使用 mode=direct_answer，并让主模型按 Loaded SKILL.md 直接回答，不输出可执行块。\n"
        "10. read_resource 只能使用 resource_handle，禁止输出 path。\n"
        "11. resource_handle 必须来自 resource_catalog。\n"
        "12. 如果任务需要 references/assets 的知识、示例、模板或配置，应优先规划 read_resource。\n"
        "13. 不要假装读取、假装执行、假装写入。\n"
        "14. 只输出严格 JSON，不要 Markdown，不要解释。\n\n"
        "允许的 action：\n"
        "- read_resource：读取 resource_catalog 中的资源，只能传 resource_handle。\n"
        "- display / ignore：展示或忽略。\n"
        "禁止的 action：run_command、write_file、create_directory；这些只能由后续主模型显式 fenced block 触发。\n\n"
        "显式可执行 fenced code block 触发规则（给 final_instruction 使用）：\n"
        "- 需要执行命令时，只能要求主模型复用 Action schema 中来自 SKILL.md/references 的具体 shell fenced 命令示例，"
        "替换用户真实参数后输出；禁止从 available_scripts 或脚本文件名临时发明 CLI 参数。\n"
        "- 需要写文件时，要求主模型在代码块前写 `写入文件：<path>` 或 `保存到：<path>`，"
        "文件内容必须放在紧随其后的 fenced code block 内。\n"
        "- 后端只执行 final_instruction 或主模型回复中已经出现、且通过 available_scripts 与 Action schema 校验的命令；资源存在性只做安全校验，不做触发条件。\n\n"
        "mode 选择规则：\n"
        "- direct_answer：主模型继续生成最终回复；如果需要动作，也必须在该回复中输出显式 fenced block 供后端识别执行。\n"
        "- execute：用于 read_resource/display/ignore 这类宿主受控动作；若 final_instruction 含合法命令，宿主会在前置动作后执行该命令。\n"
        "- ask_user：缺少必要输入，或 SKILL.md 要求的脚本/资源不存在，无法安全继续。\n"
        "- not_applicable：用户请求与当前 Skill 明显不匹配。\n\n"
        "输出格式：\n"
        "{\n"
        "  \"mode\": \"execute | direct_answer | ask_user | not_applicable\",\n"
        "  \"actions\": [\n"
        "    {\n"
        "      \"action\": \"read_resource | display | ignore\",\n"
        "      \"resource_handle\": \"resource:0\",\n"
        "      \"reason\": \"为什么需要该动作\"\n"
        "    }\n"
        "  ],\n"
        "  \"missing\": [],\n"
        "  \"errors\": [],\n"
        "  \"final_instruction\": \"需要执行脚本时放入替换真实参数后的 shell fenced 命令；只能引用 SKILL.md/references 中已有命令示例\"\n"
        "}\n"
    )


def _normalize_skill_runtime_plan(
    plan: dict,
    *,
    resource_catalog: list[dict] | None = None,
    execution_root: Path | None = None,
    command_contract: dict | None = None,
    loaded_paths: list[str] | None = None,
    failed_paths: list[dict] | None = None,
    available_scripts: list[str] | None = None,
) -> dict:
    """Normalize planner JSON into executor-compatible plan.

    关键原则：
    - read_resource 的真实 path 不来自模型，而是由宿主根据 resource_handle 映射得到；
    - runtime planner 不直接触发 run_command/write_file/create_directory；
    - 命令和写文件只能由后续主模型回复中的 fenced code block 触发。
    """
    if not isinstance(plan, dict):
        raise ValueError("运行时规划模型输出必须是 JSON object")

    resource_by_handle = _resource_catalog_by_handle(resource_catalog or [])
    loaded_path_set = {_normalize_skill_resource_path(path) for path in (loaded_paths or []) if str(path or "").strip()}
    failed_by_path: dict[str, dict] = {}
    for item in failed_paths or []:
        if not isinstance(item, dict):
            continue
        failed_path = _normalize_skill_resource_path(str(item.get("path") or ""))
        if failed_path:
            failed_by_path[failed_path] = item
    available_script_set = {_normalize_skill_resource_path(path) for path in (available_scripts or [])}
    command_entries = (((command_contract or {}).get("action_schema") or {}).get("entries") or [])
    command_script_set = {_normalize_skill_resource_path(str(entry.get("script_path") or "")) for entry in command_entries if isinstance(entry, dict)}

    mode = str(plan.get("mode") or "").strip()
    if mode not in {"execute", "direct_answer", "ask_user", "not_applicable"}:
        mode = "ask_user"

    actions = plan.get("actions", [])
    errors = plan.get("errors", [])
    missing = plan.get("missing", [])

    if not isinstance(actions, list):
        actions = []

    if not isinstance(errors, list):
        errors = []

    if not isinstance(missing, list):
        missing = []

    planner_inconsistent: list[dict] = []
    normalized_missing: list[dict] = []
    for missing_item in missing:
        if not isinstance(missing_item, dict):
            normalized_missing.append({"missing_type": "planner_inconsistent", "reason": str(missing_item)})
            continue
        normalized_item = dict(missing_item)
        rel_path = _normalize_skill_resource_path(str(normalized_item.get("path") or ""))
        resource_handle = str(normalized_item.get("resource_handle") or "").strip()
        if resource_handle:
            canonical_handle, resolved_resource, was_alias = _resolve_resource_handle_alias(resource_handle, resource_catalog or [])
            if canonical_handle and resolved_resource:
                if was_alias:
                    planner_inconsistent.append({
                        "missing_type": "planner_inconsistent",
                        "resource_handle": resource_handle,
                        "resolved_resource_handle": canonical_handle,
                        "path": _normalize_skill_resource_path(str(resolved_resource.get("path") or "")),
                        "reason": "planner used a path-like pseudo resource_handle; backend resolved it from resource_catalog",
                    })
                resource_handle = canonical_handle
                normalized_item["resource_handle"] = canonical_handle
                if not rel_path:
                    rel_path = _normalize_skill_resource_path(str(resolved_resource.get("path") or ""))
                    normalized_item["path"] = rel_path

        if rel_path in available_script_set:
            planner_inconsistent.append({
                "missing_type": "planner_inconsistent",
                "resource_handle": resource_handle,
                "path": rel_path,
                "reason": "planner reported a script as missing, but backend available_scripts shows it exists",
            })
            continue

        if rel_path in loaded_path_set:
            planner_inconsistent.append({
                "missing_type": "planner_inconsistent",
                "resource_handle": resource_handle,
                "path": rel_path,
                "reason": "planner reported an already loaded resource as missing",
            })
            continue

        if rel_path.startswith("scripts/") and rel_path not in command_script_set and (command_contract or {}).get("has_executable_command_block") is False:
            normalized_item["missing_type"] = "command_block_missing"
            normalized_item["reason"] = normalized_item.get("reason") or "script exists/mentioned but SKILL.md/references has no executable command block"

        if rel_path in failed_by_path:
            failed = failed_by_path[rel_path]
            normalized_item["missing_type"] = failed.get("missing_type") or "load_failed"
            normalized_item["reason"] = failed.get("reason") or normalized_item.get("reason") or "resource load failed"
        else:
            normalized_item["missing_type"] = normalized_item.get("missing_type") or "file_missing"
        normalized_missing.append(normalized_item)
    missing = normalized_missing

    normalized_actions: list[dict] = []

    for action_item in actions:
        if not isinstance(action_item, dict):
            continue

        action = str(action_item.get("action") or "").strip()

        if action not in {"run_command", "write_file", "create_directory", "read_resource", "display", "ignore"}:
            errors.append({"error": f"不支持的 action: {action}", "action_item": action_item})
            continue

        if action in {"run_command", "write_file", "create_directory"}:
            errors.append({
                "error": f"{action} 只能由主模型回复中的显式 fenced code block 触发",
                "action_item": action_item,
                "hint": "runtime planner 只做意图判断和 read_resource；不要直接规划执行命令或写文件。",
            })
            continue

        if action == "read_resource":
            resource_handle = str(action_item.get("resource_handle") or "").strip()
            if not resource_handle:
                errors.append({"error": "read_resource 缺少 resource_handle", "action_item": action_item})
                continue

            canonical_handle, resource, was_alias = _resolve_resource_handle_alias(resource_handle, resource_catalog or [])
            if not resource or not canonical_handle:
                errors.append({
                    "error": "read_resource 使用了不存在的 resource_handle",
                    "resource_handle": resource_handle,
                    "reason": "planner_inconsistent",
                    "available_resource_handles": sorted(resource_by_handle.keys()),
                })
                continue
            if was_alias:
                planner_inconsistent.append({
                    "missing_type": "planner_inconsistent",
                    "resource_handle": resource_handle,
                    "resolved_resource_handle": canonical_handle,
                    "path": _normalize_skill_resource_path(str(resource.get("path") or "")),
                    "reason": "planner used a path-like pseudo resource_handle; backend resolved it from resource_catalog",
                })
                resource_handle = canonical_handle

            rel_path = _normalize_skill_resource_path(str(resource.get("path") or ""))
            if rel_path in failed_by_path:
                failed = failed_by_path[rel_path]
                missing.append({
                    "resource_handle": resource_handle,
                    "path": rel_path,
                    "missing_type": failed.get("missing_type") or "load_failed",
                    "reason": failed.get("reason") or "resource load failed",
                })
                continue

            if execution_root is not None:
                root = execution_root.resolve()
                resource_path = (root / rel_path).resolve()
                if not _is_within_sandbox(resource_path, root) or not resource_path.is_file():
                    missing.append({
                        "resource_handle": resource_handle,
                        "path": rel_path,
                        "missing_type": "file_missing",
                        "reason": "resource_catalog entry no longer exists in current skill",
                    })
                    continue

            allowed_actions = set(resource.get("allowed_actions") or [])
            if "read_resource" not in allowed_actions:
                errors.append({
                    "error": "该资源不允许 read_resource",
                    "resource_handle": resource_handle,
                    "kind": resource.get("kind"),
                    "allowed_actions": sorted(allowed_actions),
                })
                continue

            action_item["resource_handle"] = resource_handle
            action_item["path"] = resource["path"]
            action_item["resource_kind"] = resource["kind"]


        action_item["block_index"] = int(action_item.get("block_index", -1))
        normalized_actions.append(action_item)

    # 如果 planner 要 execute，但所有 action 都被宿主校验拦掉，
    # 不要继续进入 executor，改为 ask_user，让前端看到可解释错误。
    if mode == "execute" and not normalized_actions and errors:
        mode = "ask_user"

    if mode == "ask_user" and planner_inconsistent and not missing and not errors:
        mode = "direct_answer"

    final_instruction = str(plan.get("final_instruction") or "").strip()
    if (
        mode == "direct_answer"
        and _final_instruction_requests_host_command(final_instruction)
        and not (command_contract or {}).get("has_executable_command_block")
    ):
        mode = "ask_user"
        errors.append({
            "error": "Skill.md 缺少可执行命令 fenced block 示例，禁止主模型临时拼接命令",
            "hint": "请在当前 SKILL.md 中用普通 Markdown 写入具体 ```bash 命令示例，并让脚本接口与示例一致。",
        })

    return {
        "mode": mode,
        "tasks": normalized_actions,
        "actions": normalized_actions,
        "missing": missing,
        "errors": errors,
        "planner_inconsistent": planner_inconsistent,
        "final_instruction": final_instruction,
    }

async def _run_skill_runtime_planner_round(
    *,
    body_prompt: str,
    request: ChatRequest,
    model: str,
    execution_root: Path | None = None,
    skill_name: str = "",
    loaded_paths: list[str] | None = None,
    failed_paths: list[dict] | None = None,
) -> dict:
    """Generate an action plan from Loaded SKILL.md and structured host resources.

    对齐反重力式宿主模型：
    - Skill.md 提供流程；
    - resource_catalog 提供资源树；
    - planner 只选择 resource_handle；
    - 真实 path 由宿主解析，不由模型生成。
    """
    resource_catalog = _extract_runtime_resource_catalog(body_prompt, execution_root=execution_root)
    planner_body_prompt = _strip_runtime_resource_manifest(body_prompt)
    command_contract = _extract_skill_command_contract(planner_body_prompt, execution_root=execution_root)

    # Deterministically scan only the current business Skill root. Never scan kernel.
    available_scripts = _available_scripts_for_root(execution_root)
    logger.info(
        "sandbox runtime planner context skill_name=%s execution_root=%s available_scripts=%s",
        skill_name,
        str(execution_root.resolve()) if execution_root else "",
        available_scripts,
    )

    planner_payload = {
        "loaded_skill_prompt": planner_body_prompt,
        "resource_catalog": _resource_catalog_for_planner(resource_catalog),
        "available_scripts": available_scripts,
        "user_messages": _request_messages_with_files(request),
        "last_user_text": _last_user_text(request),
        "execution_root": str(execution_root) if execution_root else "",
        "skill_name": skill_name,
        "loaded_paths": list(loaded_paths or []),
        "failed_paths": list(failed_paths or []),
        "runtime_contract": {
            "skill_md_is_markdown": True,
            "skill_md_code_blocks_have_no_action_tag": True,
            "resource_tree_is_structured": True,
            "planner_must_not_generate_resource_paths": True,
            "read_resource_uses_resource_handle_only": True,
            "resource_path_resolution_is_host_owned": True,
            "execution_requires_main_model_fenced_block": True,
            "action_observation_loop": True,
            "command_generation_requires_skill_md_markdown_example": True,
            "fenced_blocks_are_normalized_to_action_schema": True,
            "reference_command_blocks_are_valid_execution_entries": True,
            "stdout_json_is_observation_for_final_answer": True,
        },
    }

    messages = [
        {"role": "system", "content": _compose_skill_runtime_planner_prompt()},
        {"role": "user", "content": f"## Skill 执行规范\n{planner_body_prompt}"},
        {"role": "user", "content": f"## 可用脚本\n{json.dumps(available_scripts, ensure_ascii=False)}"},
        {"role": "user", "content": f"## SKILL.md / references Action schema\n{json.dumps(command_contract, ensure_ascii=False)}"},
        {"role": "user", "content": f"## 用户请求\n{_last_user_text(request)}"},
        {"role": "user", "content": f"## 执行根目录\n{str(execution_root) if execution_root else ''}"},
        {"role": "user", "content": f"## 技能名称\n{skill_name}"},
        {"role": "user", "content": "## 已加载/加载失败资源\n" + json.dumps({"loaded_paths": list(loaded_paths or []), "failed_paths": list(failed_paths or [])}, ensure_ascii=False)},
        {"role": "user", "content": "请根据以上信息，输出 JSON 格式的执行计划。只输出 JSON，不要任何其他内容。"},
    ]

    planner_model = _planner_model_name(model)
    planner_text = await complete_chat_once(messages, planner_model)

    try:
        stripped = _strip_markdown_json_fence(planner_text)
        raw_plan = json.loads(stripped)
    except json.JSONDecodeError:
        # First attempt failed.  Give the model one more chance with an explicit
        # correction prompt that reinforces the JSON-only requirement.
        logger.warning(
            "Planner returned non-JSON on first attempt, retrying with correction prompt: %s",
            planner_text[:300],
        )
        retry_messages = messages + [
            {"role": "assistant", "content": planner_text},
            {
                "role": "user",
                "content": (
                    "你的上一次回复包含了自然语言或 Markdown，不是合法的 JSON。\n"
                    "请重新输出，只输出一个符合格式要求的 JSON 对象，"
                    "不要任何解释、不要 Markdown、不要代码块标记。\n"
                    "直接输出 { ... }，不要其他内容。"
                ),
            },
        ]
        planner_text = await complete_chat_once(retry_messages, planner_model)
        try:
            stripped = _strip_markdown_json_fence(planner_text)
            raw_plan = json.loads(stripped)
        except json.JSONDecodeError as exc:
            logger.error(
                "Received invalid JSON response from skill runtime planner after retry: %s",
                planner_text,
            )
            raise ValueError(f"运行时规划模型没有返回合法 JSON: {planner_text[:500]}") from exc

    return await asyncio.to_thread(
        functools.partial(
            _normalize_skill_runtime_plan,
            raw_plan,
            resource_catalog=resource_catalog,
            execution_root=execution_root,
            command_contract=command_contract,
            loaded_paths=loaded_paths,
            failed_paths=failed_paths,
            available_scripts=available_scripts,
        )
    )

def _compose_final_answer_prompt() -> str:
    """Generate final answer from action observations."""
    return (
        "你是 Skill Agent 的最终回答生成器。\n\n"
        "你会收到用户请求、Loaded SKILL.md、运行时 action plan、主模型动作前草稿 assistant_draft "
        "以及 executor observation。\n\n"
        "你的任务是基于这些材料生成最终给用户看的结果。\n\n"
        "核心规则：\n"
        "1. 必须遵循 Loaded SKILL.md 的输出格式要求。\n"
        "2. 如果 assistant_draft 中包含有用的正文草稿，可以保留并整理。\n"
        "3. 如果 assistant_draft 中包含用于执行的 fenced command block，最终回答中不要保留这些命令块。\n"
        "4. 如果命令 stdout 是 JSON，应解析其中的 text、markdown、image、image_path、file、path 等字段。\n"
        "5. 如果 observation 中有 output_files，应把对应 url/path 作为 Markdown 链接或图片插入。\n"
        "6. 如果生成的是图片文件，优先用 Markdown 图片语法展示：![说明](路径或URL)。\n"
        "7. 不要输出 base64 data URI，除非 observation 里没有文件路径且 Skill 明确要求 base64。\n"
        "8. 不要输出内部 JSON、plan、完整 SKILL.md 或执行日志。\n"
        "9. 不要假装执行未发生的动作；如果命令失败，简要说明失败原因。\n"
    )

async def _generate_final_answer_from_observation(
    *,
    body_prompt: str,
    request: ChatRequest,
    model: str,
    plan: dict,
    execution_result: dict,
) -> str:
    messages = [
        {"role": "system", "content": _compose_final_answer_prompt()},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "loaded_skill_prompt": body_prompt,
                    "user_messages": _request_messages_with_files(request),
                    "plan": plan,
                    "execution_result": execution_result,
                },
                ensure_ascii=False,
            ),
        },
    ]

    return await complete_chat_once(messages, model)

def _compose_block_planner_prompt() -> str:
    return (
        "你是 Agent 运行时的动作规划器。\n\n"
        "你的唯一输入依据是：主模型已经生成的 assistant_text，以及从 assistant_text 中抽取出的 fenced code block。\n"
        "你不能根据 SKILL.md 模板、系统提示或用户原始意图凭空生成动作。\n\n"
        "核心规则：\n"
        "1. 只能判断 assistant_text 中已经出现的代码块。\n"
        "2. write_file 的文件内容必须来自对应 block 的 code，不能来自其他 block，不能来自解释文字。\n"
        "3. write_file 的 path 必须出现在该代码块紧邻前文中，通常应是代码块前最后 1 到 3 行里的“写入文件：<path>”或“保存到：<path>”。\n"
        "3a. 如果 assistant_text 中已经创建了某个 Skill 根目录，例如 `skills/ai-course-skill/scripts`、"
        "`skills/ai-course-skill/references` 或 `skills/ai-course-skill/assets`，"
        "那么后续写入 `SKILL.md` 必须绑定为 `skills/ai-course-skill/SKILL.md`，"
        "写入 `scripts/main.py` 必须绑定为 `skills/ai-course-skill/scripts/main.py`。\n"
        "3b. 禁止把新 Skill 的 `SKILL.md` 规划为宿主根目录下的 `SKILL.md`。\n"
        "3c. 禁止把新 Skill 的脚本规划为宿主根目录下的 `scripts/main.py`。\n"
        "4. 如果 path 出现在更早的段落、标题、列表或其他代码块附近，不允许把它绑定到当前 block。\n"
        "5. 如果当前 block 前后同时出现多个路径，或者路径与当前 block 内容主题明显不一致，不要猜测，写入 errors。\n"
        "6. 如果当前 block 的前文说写入 SKILL.md，但 block 内容明显是在描述其他文件、步骤、说明文字或另一个文件内容，不允许写入 SKILL.md。\n"
        "7. 如果当前 block 的前文说写入某个文件，但 block 内容明显不是该文件的完整内容，不允许写入该文件。\n"
        "8. 如果代码块表达的是创建目录，不要输出 run_command，必须输出 create_directory。\n"
        "9. 如果一个代码块中创建多个目录，必须拆成多个 create_directory 任务，每个任务一个 path。\n"
        "10. 对于修改宿主状态但宿主没有原生动作支持的操作，应优先 ignore，不要强行归类为 run_command。\n"
        "11. run_command 只用于确实需要运行外部程序、脚本或工具的命令，不要把目录创建、文件写入这类可由宿主原生动作完成的操作归类为 run_command。\n"
        "12. 如果代码块只是示例、说明、模板、教程、展示内容，则 action=display 或 ignore。\n"
        "13. 如果路径、执行意图、命令来源不明确，不要猜测，把问题写入 errors。\n"
        "14. 不允许根据用户希望、SKILL.md 用法、资源清单或常识补全缺失路径。\n"
        "15. 只输出严格 JSON，不要 Markdown，不要解释。\n\n"
        "允许的 action：display、ignore、write_file、run_command、create_directory。\n\n"
        "输出格式：\n"
        "{\n"
        "  \"tasks\": [\n"
        "    {\"block_index\": 0, \"action\": \"create_directory\", \"path\": \"...\", \"reason\": \"...\"},\n"
        "    {\"block_index\": 1, \"action\": \"write_file\", \"path\": \"...\", \"reason\": \"...\"},\n"
        "    {\"block_index\": 2, \"action\": \"run_command\", \"command\": \"...\", \"reason\": \"...\"}\n"
        "  ],\n"
        "  \"errors\": []\n"
        "}\n"
    )

async def _run_block_planner_round(
        *,
        assistant_text: str,
        blocks: list[MarkdownBlock],
        request: ChatRequest,
        model: str,
) -> dict:
    """Run a silent planning round after the main model has produced assistant_text."""
    if not blocks:
        return {"tasks": [], "errors": []}

    planner_payload = {
        "user_messages": _request_messages_with_files(request),
        "assistant_text": assistant_text,
        "blocks": _blocks_for_planner(blocks),
        "runtime_constraints": {
            "block_source": "assistant_text_only",
            "path_source": "assistant_text_near_block_context",
            "content_source": "selected_block_code",
            "command_source": "assistant_text_executable_block_or_near_block_context",
            "directory_creation": {
                "preferred_action": "create_directory",
                "rule": "目录创建应使用 create_directory，不应使用 run_command。",
                "multiple_paths": "如果一次创建多个目录，拆成多个 create_directory 任务。",
            },
            "do_not_use": [
                "SKILL.md code example that was not present in assistant_text",
                "system prompt",
                "resource manifest",
                "implicit intent",
                "guessed path",
                "guessed command",
            ],
        },
    }

    messages = [
        {"role": "system", "content": _compose_block_planner_prompt()},
        {"role": "user", "content": json.dumps(planner_payload, ensure_ascii=False)},
    ]

    planner_text = await complete_chat_once(messages, model)

    try:
        stripped = _strip_markdown_json_fence(planner_text)
        plan = json.loads(stripped)
    except json.JSONDecodeError:
        logger.warning(
            "Block planner returned non-JSON on first attempt, retrying with correction prompt: %s",
            planner_text[:300],
        )
        retry_messages = messages + [
            {"role": "assistant", "content": planner_text},
            {
                "role": "user",
                "content": (
                    "你的上一次回复不是合法 JSON。请把它修正为一个严格 JSON object。\n"
                    "只输出 JSON，不要 Markdown，不要解释，不要代码块标记。\n"
                    "格式必须是：{\"tasks\":[...],\"errors\":[...]}。"
                ),
            },
        ]
        planner_text = await complete_chat_once(retry_messages, model)
        try:
            stripped = _strip_markdown_json_fence(planner_text)
            plan = json.loads(stripped)
        except json.JSONDecodeError as exc:
            logger.error("Received invalid JSON response from planner after retry: %s", planner_text)
            raise ValueError(f"规划模型没有返回合法 JSON: {planner_text[:500]}") from exc

    if not isinstance(plan, dict):
        raise ValueError("规划模型输出必须是 JSON object")

    tasks = plan.get("tasks", [])
    errors = plan.get("errors", [])

    if not isinstance(tasks, list):
        raise ValueError("规划模型输出的 tasks 必须是数组")

    if not isinstance(errors, list):
        errors = []

    normalized_tasks: list[dict] = []

    for task in tasks:
        if not isinstance(task, dict):
            continue

        action = str(task.get("action", "")).strip()

        if action not in _ALLOWED_PLAN_ACTIONS:
            errors.append({"error": f"不支持的 action: {action}", "task": task})
            continue

        try:
            block_index = int(task.get("block_index", -1))
        except (TypeError, ValueError):
            block_index = -1

        if action in {"write_file", "run_command"} and not (0 <= block_index < len(blocks)):
            errors.append({"error": "任务缺少合法 block_index", "task": task})
            continue

        if action in {"write_file", "create_directory"} and not str(task.get("path") or "").strip():
            errors.append({"error": f"{action} 缺少 path", "task": task})
            continue

        if action == "run_command":
            block = blocks[block_index]
            command = str(task.get("command") or block.code or "").strip()
            if not command:
                errors.append({"error": "run_command 缺少 command", "task": task})
                continue
            task["command"] = command

        task["block_index"] = block_index
        normalized_tasks.append(task)

    return {"tasks": normalized_tasks, "errors": errors}

def _runtime_script_dir() -> Path:
    """Directory for executor-generated Python scripts converted from heredoc."""
    roots = _allowed_skill_roots()
    if not roots:
        raise ValueError("没有可用的 Skill 写入根目录")

    directory = roots[0] / ".runtime"
    directory.mkdir(parents=True, exist_ok=True)
    return directory

def _materialize_python_heredoc(command: str) -> list[str] | None:
    """Convert `python - <<'PY' ... PY` into `python <safe-script>.py`.

    目的：兼容模型常输出的多行校验脚本，同时继续使用 shell=False，
    不开放真正 shell 的管道、重定向、变量展开、命令替换等能力。
    """
    match = _PYTHON_HEREDOC_RE.match(command.strip())
    if not match:
        return None

    python_bin = Path(match.group("python")).name
    if python_bin not in {"python", "python3"}:
        raise ValueError(f"只允许运行 python/python3 heredoc 命令: {command}")

    script = match.group("script").rstrip() + "\n"
    digest = hashlib.sha256(script.encode("utf-8")).hexdigest()[:16]
    script_path = _runtime_script_dir() / f"heredoc_{digest}.py"
    script_path.write_text(script, encoding="utf-8")

    resolved = _resolve_safe_path(str(script_path))
    return [python_bin, str(resolved)]

def _extract_skill_local_paths_from_argv(argv: list[str]) -> list[str]:
    """Extract skill-local resource paths mentioned in command argv.

    只识别 scripts/、references/、assets/ 这类 Skill 内资源路径。
    不关心具体语言，不硬编码 python/node/bash。
    """
    result: list[str] = []

    for raw in argv:
        if not raw:
            continue

        candidates = [raw]

        # 支持 --config=assets/config.yaml 这种形式
        if "=" in raw:
            _key, value = raw.split("=", 1)
            if value:
                candidates.append(value)

        for item in candidates:
            item = item.strip()
            if not item or item.startswith("-"):
                continue

            if item.startswith("./"):
                item = item[2:]

            try:
                path = Path(item)
            except Exception:
                continue

            parts = path.parts
            if not parts:
                continue

            if parts[0] in {"scripts", "references", "assets"}:
                normalized = Path(*parts).as_posix()
                if normalized not in result:
                    result.append(normalized)

    return result

def _validate_skill_local_command_paths(
    argv: list[str],
    *,
    base_dir: Path | None,
) -> None:
    """Validate skill-local paths referenced by a command.

    解决：
    - python scripts/main.py 但 scripts/main.py 不存在；
    - bash scripts/run.sh 但脚本不存在；
    - node scripts/index.js 但脚本不存在。

    这是资源存在性校验，不是工具类型白名单。
    """
    if base_dir is None:
        return

    root = base_dir.resolve()

    for rel_path in _extract_skill_local_paths_from_argv(argv):
        rel = Path(rel_path)

        if rel.is_absolute():
            raise ValueError(f"命令引用了非法绝对资源路径: {rel_path}")

        if any(part in {"", ".."} for part in rel.parts):
            raise ValueError(f"命令引用的资源路径越界: {rel_path}")

        resolved = (root / rel).resolve()

        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"命令引用的资源路径越界: {rel_path}") from exc

        if not resolved.exists():
            raise ValueError(f"命令引用的 Skill 资源不存在: {rel_path}")

        if not resolved.is_file():
            raise ValueError(f"命令引用的 Skill 资源不是文件: {rel_path}")

def _prepare_command_argv(
    command: str,
    *,
    base_dir: Path | None = None,
) -> list[str]:
    """Parse and preflight a command before subprocess.run.

    不限制具体执行工具类型；
    只做通用校验：
    - 命令不能为空；
    - 命令必须能被 shlex 解析；
    - argv[0] 必须是 PATH 中的可执行程序，或一个真实存在的路径；
    - command 中引用的 scripts/assets/references 路径必须真实存在。

    额外：对 Python 脚本使用每个 Skill 独立的 venv，执行前静态扫描依赖。
    对 Node.js 脚本执行前扫描并安装缺失的 npm 包。
    """
    argv = _safe_command_argv(command, base_dir=base_dir)

    if not argv:
        raise ValueError("命令为空")

    executable = argv[0]

    # 1. argv[0] 是路径形式：./tool、scripts/run.sh、/usr/bin/env 等
    if "/" in executable or "\\" in executable:
        exe_path = Path(executable).expanduser()

        if not exe_path.is_absolute():
            if base_dir is None:
                exe_path = exe_path.resolve()
            else:
                exe_path = (base_dir / exe_path).resolve()
        else:
            exe_path = exe_path.resolve()

        if not exe_path.exists():
            raise ValueError(f"命令不可执行，文件不存在: {executable}")

        if not exe_path.is_file():
            raise ValueError(f"命令不可执行，目标不是文件: {executable}")

        # 方案 A+B：对已知脚本扩展名（.py/.sh/.js 等）始终注入解释器，
        # 避免 execute bit 判断不一致导致的 PermissionError；
        # 对未知扩展名才依赖 execute bit 直接执行；
        # 若扩展名也无法识别，则给出明确错误提示。
        ext = exe_path.suffix.lower()
        if ext not in _SCRIPT_INTERPRETERS and os.access(exe_path, os.X_OK):
            # 非脚本文件且有执行权限，直接执行
            argv[0] = str(exe_path)
        else:
            interpreter = _SCRIPT_INTERPRETERS.get(ext)
            if interpreter is not None:
                # .ts 特殊处理：直接检查 ts-node 或通过 npx 运行
                if ext == ".ts":
                    if shutil.which("ts-node") is None:
                        _try_auto_install_interpreter("ts-node")
                    if shutil.which("ts-node") is not None:
                        argv = ["ts-node", str(exe_path)] + argv[1:]
                    elif shutil.which("npx") is not None:
                        argv = ["npx", "ts-node", str(exe_path)] + argv[1:]
                    else:
                        raise ValueError(
                            f"无法执行 {executable}：需要 ts-node 或 npx，但它们均不在 PATH 中。"
                        )
                elif ext == ".py" and base_dir is not None:
                    # 使用 Skill 独立 venv 执行 Python 脚本，并预装静态依赖
                    try:
                        venv_python = _get_skill_venv_python(base_dir)
                        _scan_and_install_python_deps(exe_path, venv_python)
                        argv = [str(venv_python), str(exe_path)] + argv[1:]
                    except Exception as venv_exc:
                        logger.warning(
                            "skill-env: venv setup failed, falling back to system python3: %s",
                            venv_exc,
                        )
                        if shutil.which("python3") is None:
                            _try_auto_install_interpreter("python3")
                        if shutil.which("python3") is None:
                            raise ValueError(
                                f"无法执行 {executable}：需要解释器 python3，但它不在 PATH 中。"
                            )
                        argv = ["python3", str(exe_path)] + argv[1:]
                elif ext in {".js", ".mjs", ".cjs"} and base_dir is not None:
                    # 预装 Node.js 依赖到 Skill 独立 node_modules
                    try:
                        _scan_and_install_node_deps(exe_path, base_dir)
                    except Exception as node_exc:
                        logger.warning("skill-env: node dep scan failed: %s", node_exc)
                    if shutil.which("node") is None:
                        _try_auto_install_interpreter("node")
                    if shutil.which("node") is None:
                        raise ValueError(
                            f"无法执行 {executable}：需要解释器 node，但它不在 PATH 中。"
                        )
                    argv = ["node", str(exe_path)] + argv[1:]
                else:
                    if shutil.which(interpreter) is None:
                        # 尝试自动安装后再检查一次
                        _try_auto_install_interpreter(interpreter)
                    if shutil.which(interpreter) is None:
                        raise ValueError(
                            f"无法执行 {executable}：需要解释器 {interpreter}，但它不在 PATH 中。"
                        )
                    argv = [interpreter, str(exe_path)] + argv[1:]
            else:
                raise ValueError(
                    f"命令没有执行权限: {executable}\n"
                    f"文件不可直接执行，且扩展名 '{ext or '(无)'}' 无法自动推断解释器。\n"
                    f"请使用 'node/python3/bash <脚本路径>' 的形式明确指定解释器。"
                )

    # 2. argv[0] 是裸命令：python、node、bash、ffmpeg、convert 等
    # 不做白名单，只检查系统 PATH 中是否存在。
    else:
        exe_name = Path(executable).name
        # 对裸 python/python3 + .py 脚本参数，替换为 Skill 独立 venv python
        if exe_name in {"python", "python3"} and len(argv) >= 2 and base_dir is not None:
            script_arg = argv[1]
            script_path_candidate: Path | None = None
            if not script_arg.startswith("-") and (
                "/" in script_arg or script_arg.endswith(".py")
            ):
                candidate = Path(script_arg)
                if not candidate.is_absolute():
                    candidate = (base_dir / candidate).resolve()
                else:
                    # 处理硬编码的 /app/scripts/ 路径，重写为基于 base_dir 的路径
                    for prefix in ("/app/scripts/", "/app/references/", "/app/assets/"):
                        if str(candidate).startswith(prefix):
                            rel_path = str(candidate)[len("/app/"):]
                            candidate = (base_dir / rel_path).resolve()
                            break
                # Guard: script must reside within the skill directory
                try:
                    candidate.relative_to(base_dir.resolve())
                    if candidate.exists() and candidate.suffix.lower() == ".py":
                        script_path_candidate = candidate
                except ValueError:
                    pass  # path escaped skill dir boundary — skip dep scan
            if script_path_candidate is not None:
                try:
                    venv_python = _get_skill_venv_python(base_dir)
                    _scan_and_install_python_deps(script_path_candidate, venv_python)
                    argv = [str(venv_python)] + argv[1:]
                except Exception as venv_exc:
                    logger.warning(
                        "skill-env: venv setup failed, using system %s: %s",
                        executable,
                        venv_exc,
                    )
                    if shutil.which(executable) is None:
                        _try_auto_install_interpreter(executable)
            else:
                if shutil.which(executable) is None:
                    _try_auto_install_interpreter(executable)
        # 对裸 node/nodejs + .js 脚本参数，预装 Node.js 依赖
        elif exe_name in {"node", "nodejs"} and len(argv) >= 2 and base_dir is not None:
            script_arg = argv[1]
            if not script_arg.startswith("-"):
                candidate = Path(script_arg)
                if not candidate.is_absolute():
                    candidate = (base_dir / candidate).resolve()
                # Guard: script must reside within the skill directory
                try:
                    candidate.relative_to(base_dir.resolve())
                    if candidate.exists() and candidate.suffix.lower() in {".js", ".mjs", ".cjs"}:
                        try:
                            _scan_and_install_node_deps(candidate, base_dir)
                        except Exception as node_exc:
                            logger.warning("skill-env: node dep scan failed: %s", node_exc)
                except ValueError:
                    pass  # path escaped skill dir boundary — skip dep scan
            if shutil.which(executable) is None:
                _try_auto_install_interpreter(executable)
        else:
            if shutil.which(executable) is None:
                # 尝试自动安装后再检查一次
                _try_auto_install_interpreter(executable)

        if shutil.which(executable) is None and not Path(argv[0]).exists():
            raise ValueError(
                f"命令不可执行：{executable} 不在 PATH 中，也不是当前 Skill 内的可执行文件。"
                "如果这是函数名或伪代码，请不要规划 run_command。"
            )

    _validate_skill_local_command_paths(argv, base_dir=base_dir)
    return argv

def _safe_command_argv(command: str, *, base_dir: Path | None = None) -> list[str]:
    """通用命令参数解析器。

    注意：
    - 不限制具体执行工具类型；
    - 不做 python/node/bash 白名单；
    - 真正的可执行性和资源存在性校验由 _prepare_command_argv 完成。
    """
    if not command or not command.strip():
        raise ValueError("命令为空")

    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"命令解析失败: {command}") from exc

    if not argv:
        raise ValueError("命令为空")

    return argv



def _stdout_json_payload(stdout: str) -> dict | None:
    try:
        payload = json.loads((stdout or "").strip())
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _validate_html_asset_outputs_in_generated_dir(stdout: str, *, cwd: Path | None) -> None:
    payload = _stdout_json_payload(stdout)
    if payload is None or cwd is None:
        raise ValueError("html_asset_builder stdout 必须是 JSON object，并包含 html_path 或 asset_paths")
    declared = []
    for key in ("html_path", "asset_path"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            declared.append(value.strip())
    for key in ("asset_paths", "html_paths"):
        value = payload.get(key)
        if isinstance(value, list):
            declared.extend(item.strip() for item in value if isinstance(item, str) and item.strip())
    if not declared:
        raise ValueError("html_asset_builder stdout JSON 必须包含 html_path 或 asset_paths")
    root = cwd.resolve()
    generated_root = (root / "assets" / "generated").resolve()
    for raw in declared:
        candidate = Path(raw)
        normalized_raw = raw.replace("\\", "/")
        if not candidate.is_absolute():
            candidate = (root / candidate).resolve() if normalized_raw.startswith("assets/") else (root / "scripts" / candidate).resolve()
        try:
            candidate.relative_to(generated_root)
        except ValueError as exc:
            raise ValueError("html_asset_builder 输出路径必须位于当前 Skill 的 assets/generated/ 下") from exc
        if not candidate.is_file():
            raise ValueError(f"html_asset_builder 声明的输出文件不存在: {raw}")

def _output_files_from_stdout_json(stdout: str, *, cwd: Path | None, skill_name: str) -> list[dict]:
    """Extract generated artifact paths declared by script stdout JSON."""
    if cwd is None or not skill_name or not (stdout or "").strip():
        return []
    try:
        payload = json.loads(stdout.strip())
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []
    raw_paths: list[str] = []
    for key in ("html_path", "asset_path", "image_path", "image", "pdf_path", "docx_path", "pptx_path"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            raw_paths.append(value.strip())
    for key in ("asset_paths", "html_paths", "image_paths", "file_paths"):
        value = payload.get(key)
        if isinstance(value, list):
            raw_paths.extend(item.strip() for item in value if isinstance(item, str) and item.strip())
    images = payload.get("images")
    if isinstance(images, list):
        for image in images:
            if isinstance(image, dict):
                image_path = image.get("image_path")
                if isinstance(image_path, str) and image_path.strip():
                    raw_paths.append(image_path.strip())
    root = cwd.resolve()
    allowed_roots = [(root / "assets" / "generated").resolve(), (root / "outputs").resolve()]
    output_files: list[dict] = []
    seen: set[str] = set()
    for raw in raw_paths:
        candidate = Path(raw)
        normalized_raw = raw.replace("\\", "/")
        if not candidate.is_absolute():
            if normalized_raw.startswith(("assets/", "outputs/")):
                candidate = (root / candidate).resolve()
            else:
                candidate = (root / "scripts" / candidate).resolve()
        if not any(_is_within_sandbox(candidate, allowed_root) for allowed_root in allowed_roots):
            continue
        rel = candidate.relative_to(root).as_posix()
        if rel in seen or not candidate.is_file():
            continue
        seen.add(rel)
        output_files.append({"path": rel, "url": f"/api/skills/{skill_name}/files/{rel}"})
    return output_files

def _execute_single_task(
    task: dict,
    blocks: "list[MarkdownBlock]",
    request: "ChatRequest",
    *,
    execution_root: "Path | None" = None,
    inferred_skill_root: "Path | None" = None,
    skill_name: str = "",
    session_input_dir: "Path | None" = None,
) -> "tuple[dict, list[Path]]":
    """Execute a single planned action task and return (result_dict, touched_paths).

    This is the per-task workhorse extracted from _execute_planned_actions so
    that callers (including the streaming execute loop in generate()) can run
    tasks one-at-a-time and observe results in real time.

    Returns:
        (result, touched) where *result* is the action result dict and
        *touched* is a (possibly empty) list of Path objects that were
        created or written during this task (used for post-loop validation).
    """
    if not isinstance(task, dict):
        return {}, []

    action = str(task.get("action") or "").strip()
    reason = str(task.get("reason") or "").strip()
    touched: list[Path] = []

    if action in {"display", "ignore"}:
        return {"action": action, "success": True, "reason": reason}, touched

    if action == "read_resource":
        rel_path = str(task.get("path") or "").strip()
        if not rel_path:
            raise ValueError("read_resource 任务缺少 path")
        if not skill_name and execution_root is None:
            raise ValueError("read_resource 任务缺少 skill_name 或 execution_root，无法确定读取哪个 Skill 的资源")
        resource_root = execution_root.resolve() if execution_root is not None else _skill_root_for_name(skill_name)
        resource_path = (resource_root / rel_path).resolve()
        if not _is_within_sandbox(resource_path, resource_root) or not resource_path.is_file():
            raise FileNotFoundError(f"read_resource resource does not exist in current skill: {rel_path}")
        if rel_path.startswith("assets/"):
            _validate_runtime_asset_contract(resource_path, root=resource_root)
        observation = _read_runtime_skill_resource_text(
            skill_name=skill_name,
            path=rel_path,
            execution_root=execution_root,
        )
        return {
            "action": action,
            "path": rel_path,
            "success": True,
            "content": observation.get("content", ""),
            "truncated": observation.get("truncated", False),
            "reason": reason,
        }, touched

    if action == "create_directory":
        raw_path = str(task.get("path") or "").strip()
        if not raw_path:
            raise ValueError("create_directory 任务缺少 path")
        path = _resolve_planned_file_path(
            raw_path,
            execution_root=execution_root,
            inferred_skill_root=inferred_skill_root,
        )
        path.mkdir(parents=True, exist_ok=True)
        touched.append(path)
        return {"action": action, "path": str(path), "success": True, "reason": reason}, touched

    if action == "write_file":
        raw_path = str(task.get("path") or "").strip()
        if not raw_path:
            raise ValueError("write_file 任务缺少 path")
        content = task.get("content", None)
        if content is None:
            block_index = int(task.get("block_index", -1))
            if 0 <= block_index < len(blocks):
                content = blocks[block_index].code
            else:
                raise ValueError("write_file 任务缺少 content，且没有合法 block_index")
        path = _resolve_planned_file_path(
            raw_path,
            execution_root=execution_root,
            inferred_skill_root=inferred_skill_root,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")
        touched.append(path)
        return {
            "action": action,
            "path": str(path),
            "success": True,
            "bytes": len(str(content).encode("utf-8")),
            "reason": reason,
        }, touched

    if action == "run_command":
        command = str(task.get("command") or "").strip()
        if not command:
            raise ValueError("run_command 任务缺少 command")

        stdin_text = task.get("stdin", None)
        if stdin_text is not None:
            stdin_text = str(stdin_text)

        cwd = execution_root or inferred_skill_root
        if cwd is not None and not cwd.exists():
            # Creator bootstrap commands may need to create the inferred Skill root.
            # Run those commands from the nearest existing parent instead of using
            # a not-yet-created cwd, while keeping later commands in the Skill root
            # once it exists.
            fallback_cwd = execution_root or cwd.parent
            if fallback_cwd.exists():
                cwd = fallback_cwd

        # Validate script commands against the unified SKILL.md/reference Action schema
        # before execution. This prevents the main model from inventing a
        # fenced-block command that does not match the SkillPlan inputs/role
        # declared by the Skill package.
        action_entry = _validate_runtime_command_against_action_schema(command, execution_root=cwd)

        # Per-task snapshot taken *before* execution to detect new output files.
        pre_snapshot: set[str] = _snapshot_dir_files(cwd) if cwd else set()

        materialized = _materialize_python_heredoc(command)
        if materialized is not None:
            argv = materialized
            argv = _prepare_command_argv(
                " ".join(shlex.quote(part) for part in argv), base_dir=cwd
            )
        else:
            argv = _prepare_command_argv(command, base_dir=cwd)

        argv = _rewrite_argv_input_paths(
            argv,
            getattr(request, "input_files", []) or [],
            cwd,
            session_input_dir,
        )

        _run_cmd_extra_env: dict[str, str] = {
            "EXECUTION_ROOT": str(execution_root) if execution_root else "",
            "OUTPUT_DIR": str(cwd / "outputs") if cwd else "",
            "INPUT_DIR": str(cwd / "inputs") if cwd else "",
            # Expose configured model endpoints to generated skill scripts so
            # creative/text/image tasks can use the same capability routing as
            # the host instead of hard-coded templates or fake image stubs.
            "LLM_BASE_URL": settings.llm_base_url,
            "DEFAULT_MODEL": settings.default_model,
            "IMAGE_BASE_URL": settings.image_base_url,
            "IMAGE_API_KEY": settings.image_api_key or os.environ.get("IMAGE_API_KEY") or os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or "ollama",
            "IMAGE_SIZE": settings.image_size,
            "TEXT_MODEL": settings.text_model or settings.default_model,
            "CODE_MODEL": settings.code_model or settings.default_model,
            "IMAGE_MODEL": settings.image_model or settings.default_model,
            "VISION_MODEL": settings.vision_model or settings.default_model,
            "PLANNER_MODEL": settings.planner_model or settings.default_model,
            "VALIDATOR_MODEL": settings.validator_model or settings.default_model,
            "PYTHONPATH": os.pathsep.join(
                part for part in [str(PROJECT_ROOT), os.environ.get("PYTHONPATH", "")] if part
            ),
        }
        api_key = (
            settings.llm_api_key
            or settings.openai_api_key
            or os.environ.get("LLM_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or "ollama"
        )
        _run_cmd_extra_env["LLM_API_KEY"] = api_key
        _run_cmd_extra_env["OPENAI_API_KEY"] = settings.openai_api_key or api_key
        if session_input_dir is not None:
            _run_cmd_extra_env["INPUT_SESSION_DIR"] = str(session_input_dir)

        _effective_env = {**os.environ, **_run_cmd_extra_env}
        argv = [_expand_arg_env_vars(arg, _effective_env) for arg in argv]

        # Error-driven retry: up to _MAX_DEP_RETRY times for missing deps.
        completed = None
        for _retry in range(_MAX_DEP_RETRY + 1):
            try:
                completed = subprocess.run(
                    argv,
                    shell=False,
                    input=stdin_text,
                    capture_output=True,
                    text=True,
                    timeout=settings.skill_command_timeout,
                    cwd=str(cwd) if cwd else None,
                    env={**os.environ, **_run_cmd_extra_env},
                )
            except FileNotFoundError as exc:
                raise ValueError(
                    "命令不可执行: " + command + "\n原因: " + str(exc)
                ) from exc
            except PermissionError as exc:
                raise ValueError(
                    "命令没有执行权限: " + command + "\n原因: " + str(exc)
                ) from exc

            if completed.returncode == 0 or _retry == _MAX_DEP_RETRY:
                break

            stderr = completed.stderr or ""
            retried = False

            py_missing = re.search(
                r"ModuleNotFoundError: No module named '([^']+)'", stderr
            )
            if py_missing and cwd is not None:
                module_name = py_missing.group(1).split(".")[0]
                try:
                    venv_python = _get_skill_venv_python(cwd)
                    if _retry_install_python_dep(module_name, venv_python):
                        retried = True
                except Exception as dep_exc:
                    logger.warning(
                        "skill-env: error-driven py dep install failed: %s", dep_exc
                    )

            node_missing = re.search(r"Cannot find module '([^']+)'", stderr)
            if node_missing and cwd is not None:
                raw_mod = node_missing.group(1)
                if raw_mod.startswith("@"):
                    parts = raw_mod.split("/")
                    module_name = "/".join(parts[:2]) if len(parts) >= 2 else parts[0]
                else:
                    module_name = raw_mod.split("/")[0]
                if module_name not in _NODE_BUILTIN_MODULES:
                    if _retry_install_node_dep(module_name, cwd):
                        retried = True

            if not retried and cwd is not None:
                chinese_missing = re.search(
                    r"缺少依赖[:：]\s*([^\n]+)",
                    (completed.stdout or "") + "\n" + stderr,
                )
                if chinese_missing:
                    raw_deps = chinese_missing.group(1)
                    pkg_list = [
                        p.strip()
                        for p in re.split(r"[,，、;；]\s*", raw_deps)
                        if p.strip()
                    ]
                    for dep in pkg_list:
                        if dep in _NODE_BUILTIN_MODULES:
                            continue
                        if (
                            dep.endswith(".js")
                            or (cwd / "node_modules").is_dir()
                            or shutil.which("node")
                        ):
                            if _retry_install_node_dep(dep, cwd):
                                retried = True
                        else:
                            try:
                                venv_python = _get_skill_venv_python(cwd)
                                if _retry_install_python_dep(dep, venv_python):
                                    retried = True
                            except Exception as dep_exc:
                                logger.warning(
                                    "skill-env: chinese dep install failed: %s", dep_exc
                                )

            if not retried:
                break

        assert completed is not None  # noqa: S101 — loop always runs at least once (range >= 1)
        success = completed.returncode == 0
        if success:
            _validate_stdout_against_action_entry(completed.stdout, action_entry)
            if action_entry and str(action_entry.get("role") or "") in {"html_asset_builder", "asset_builder"}:
                _validate_html_asset_outputs_in_generated_dir(completed.stdout, cwd=cwd)

        result: dict = {
            "action": action,
            "command": command,
            "stdin_used": stdin_text is not None,
            "success": success,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "reason": reason,
        }

        # Detect newly created files and attach download metadata.
        effective_skill_name = skill_name or (cwd.name if cwd else "")
        if success and cwd and effective_skill_name:
            post_snapshot = _snapshot_dir_files(cwd)
            new_files = sorted(post_snapshot - pre_snapshot)
            if new_files:
                result["output_files"] = [
                    {
                        "path": f,
                        "url": f"/api/skills/{effective_skill_name}/files/{f}",
                    }
                    for f in new_files
                ]
            declared_output_files = _output_files_from_stdout_json(completed.stdout, cwd=cwd, skill_name=effective_skill_name)
            if declared_output_files:
                by_path = {item["path"]: item for item in result.get("output_files") or []}
                by_path.update({item["path"]: item for item in declared_output_files})
                result["output_files"] = list(by_path.values())

        return result, touched

    raise ValueError(f"不支持的规划动作: {action}")

def _execute_planned_actions(
    plan: dict,
    blocks: list[MarkdownBlock],
    request: ChatRequest,
    *,
    require_confirmation: bool = True,
    execution_root: Path | None = None,
    skill_name: str = "",
) -> dict:
    """执行结构化 action plan，并返回 executor observation。"""
    if require_confirmation and not _has_creation_confirmation(request):
        return {
            "executed": False,
            "reason": "未检测到用户明确确认开始创建，因此不会执行规划任务。",
            "plan": plan,
            "results": [],
            "logs": [],
        }

    inferred_skill_root = _infer_skill_root_from_tasks(
        plan,
        execution_root=execution_root,
    )

    # Pre-compute session input dir once (used for all run_command tasks).
    cwd_for_session = execution_root or inferred_skill_root
    session_input_dir = _extract_input_session_dir(
        getattr(request, "input_files", []) or [], cwd_for_session
    )

    touched: list[Path] = []
    results: list[dict] = []
    logs: list[str] = []

    for task in plan.get("tasks", []):
        if not isinstance(task, dict):
            continue

        action = str(task.get("action") or "").strip()

        result, task_touched = _execute_single_task(
            task,
            blocks,
            request,
            execution_root=execution_root,
            inferred_skill_root=inferred_skill_root,
            skill_name=skill_name,
            session_input_dir=session_input_dir,
        )

        touched.extend(task_touched)
        results.append(result)

        # Build logs from the result dict.
        if action == "read_resource":
            logs.append(f"读取资源成功: {result.get('path')}")
        elif action == "create_directory":
            logs.append(f"创建目录: {result.get('path')}")
        elif action == "write_file":
            logs.append(f"写入文件: {result.get('path')}")
        elif action == "run_command":
            command = str(task.get("command") or "").strip()
            stdin_used = result.get("stdin_used", False)
            if result.get("output_files"):
                logs.append(
                    "新生成文件: " + ", ".join(f["path"] for f in result["output_files"])
                )
            if not result.get("success", True):
                logs.append(
                    f"执行命令失败: {command}\n"
                    f"returncode={result.get('returncode')}\n"
                    f"stdin_used={stdin_used}\n"
                    f"stderr: {(result.get('stderr') or '').strip()}\n"
                    f"stdout: {(result.get('stdout') or '').strip()}"
                )
            else:
                logs.append(
                    f"执行命令成功: {command}\n"
                    f"stdin_used={stdin_used}\n"
                    f"输出: {(result.get('stdout') or '').strip()}"
                )

    validation_logs: list[str] = []

    for root in _find_created_skill_roots(touched):
        skill_md = root / "SKILL.md"
        if skill_md.exists():
            _validate_skill_md(skill_md)
            validation_logs.append(f"校验通过: {skill_md}")

    logs.extend(validation_logs)

    # 汇总所有 run_command 任务产生的新文件
    all_output_files: list[dict] = []
    for r in results:
        all_output_files.extend(r.get("output_files") or [])

    return {
        "executed": bool(results or touched),
        "reason": "已根据结构化 action plan 执行任务。" if (results or touched) else "规划中没有需要执行的任务。",
        "plan": plan,
        "results": results,
        "logs": logs,
        "output_files": all_output_files,
        "touched_paths": [str(path) for path in touched],
    }

# 兼容保留：旧的 bash-block 执行器。不再作为主路径使用。


_MARKDOWN_LINK_RE = re.compile(r"(!?\[[^\]]*\]\()([^()\s]+)(\))")


def _is_external_or_absolute_link(target: str) -> bool:
    lowered = target.strip().lower()
    return bool(
        re.match(r"^[a-z][a-z0-9+.-]*:", lowered)
        or lowered.startswith("//")
        or lowered.startswith("/")
        or lowered.startswith("#")
    )


def _normalize_output_file_ref(value: str) -> str:
    return value.strip().replace("\\", "/").lstrip("./")


def _output_file_lookup(output_files: list[dict] | None) -> dict[str, str]:
    """Build path/basename -> download URL lookup for generated files."""
    lookup: dict[str, str] = {}
    for item in output_files or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        path = _normalize_output_file_ref(str(item.get("path") or ""))
        if not url or not path:
            continue
        lookup[path] = url
        lookup[Path(path).name] = url
    return lookup


def _rewrite_output_file_markdown_links(answer: str, output_files: list[dict] | None) -> str:
    """Rewrite relative Markdown links/images for generated files to served URLs."""
    lookup = _output_file_lookup(output_files)
    if not answer or not lookup:
        return answer

    def replace(match: re.Match) -> str:
        prefix, target, suffix = match.groups()
        if _is_external_or_absolute_link(target):
            return match.group(0)
        normalized = _normalize_output_file_ref(target)
        url = lookup.get(normalized) or lookup.get(Path(normalized).name)
        if not url:
            return match.group(0)
        return f"{prefix}{url}{suffix}"

    return _MARKDOWN_LINK_RE.sub(replace, answer)


def _finalize_answer_output_file_links(answer: str, output_files: list[dict] | None) -> str:
    """Rewrite only file links the final answer already chose to show.

    Do not append generated files automatically: many Skills create auxiliary
    artifacts that should stay available through the structured output_files
    event/download bar without being forced into the final chat answer.
    """
    return _rewrite_output_file_markdown_links(answer, output_files)


def _validate_structured_stdout_payload(payload: dict) -> None:
    """Validate JSON stdout fields consumed by sandbox UI/finalization."""
    if "text" in payload and not isinstance(payload.get("text"), str):
        raise ValueError("stdout JSON 字段 text 必须是字符串")

    if "story_text" in payload and not isinstance(payload.get("story_text"), str):
        raise ValueError("stdout JSON 字段 story_text 必须是字符串")

    if "image_paths" in payload:
        image_paths = payload.get("image_paths")
        if not isinstance(image_paths, list):
            raise ValueError("stdout JSON 字段 image_paths 必须是 list[str]")
        for path in image_paths:
            if not isinstance(path, str):
                raise ValueError("stdout JSON 字段 image_paths 的每一项都必须是字符串")

    if "images" in payload:
        images = payload.get("images")
        if not isinstance(images, list):
            raise ValueError("stdout JSON 字段 images 必须是 list[dict]")
        for image in images:
            if not isinstance(image, dict):
                raise ValueError("stdout JSON 字段 images 的每一项都必须是 object")
            if "image_path" in image and not isinstance(image.get("image_path"), str):
                raise ValueError("stdout JSON 字段 images[].image_path 必须是字符串")

    for file_key in ("html_path", "pdf_path", "docx_path", "pptx_path"):
        if file_key in payload and not isinstance(payload.get(file_key), str):
            raise ValueError(f"stdout JSON 字段 {file_key} 必须是字符串")

    for list_key in ("asset_paths", "file_paths"):
        if list_key in payload:
            paths = payload.get(list_key)
            if not isinstance(paths, list):
                raise ValueError(f"stdout JSON 字段 {list_key} 必须是 list[str]")
            for path in paths:
                if not isinstance(path, str):
                    raise ValueError(f"stdout JSON 字段 {list_key} 的每一项都必须是字符串")


def _validate_success_stdout_json_if_structured(stdout: str) -> None:
    """Validate structured JSON stdout without rejecting legacy plain text."""
    stripped = (stdout or "").strip()
    if not stripped:
        return
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return
    if not isinstance(payload, dict):
        return
    if "error" in payload:
        raise ValueError("stdout JSON 不得包含 error 字段")
    if any(key in payload for key in ("text", "story_text", "image_paths", "images", "pdf_path", "docx_path", "pptx_path", "html_path", "asset_paths", "file_paths")):
        _validate_structured_stdout_payload(payload)


def _payload_image_paths(payload: dict) -> list[str]:
    paths: list[str] = []

    for key in ("image_path", "image"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            paths.append(value.strip())

    image_paths = payload.get("image_paths")
    if isinstance(image_paths, list):
        paths.extend(path.strip() for path in image_paths if isinstance(path, str) and path.strip())

    images = payload.get("images")
    if isinstance(images, list):
        for image in images:
            if isinstance(image, dict):
                path = image.get("image_path")
                if isinstance(path, str) and path.strip():
                    paths.append(path.strip())

    deduped: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        deduped.append(path)
    return deduped


def _render_success_stdout_payload(result: dict) -> str | None:
    """Render structured JSON stdout from a successful command as final user content."""
    for item in result.get("results") or []:
        if not isinstance(item, dict) or not item.get("success"):
            continue
        stdout = str(item.get("stdout") or "").strip()
        if not stdout:
            continue
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        try:
            _validate_structured_stdout_payload(payload)
        except ValueError:
            continue
        text = str(payload.get("text") or payload.get("story_text") or payload.get("markdown") or "").strip()
        image_paths = _payload_image_paths(payload)
        parts: list[str] = []
        if text:
            parts.append(text)
        for image_path in image_paths:
            if image_path not in text:
                parts.append(f"![插图]({image_path})")
        if parts:
            return "\n\n".join(parts)
    return None


def _format_execution_report(result: dict) -> str:
    if not result.get("executed"):
        reason = result.get("reason", "未知原因")
        errors = result.get("plan", {}).get("errors", []) if isinstance(result.get("plan"), dict) else []
        if errors:
            rendered_errors = "\n".join(f"- {json.dumps(item, ensure_ascii=False)}" for item in errors)
            return f"\n\n⚠️ 后台未执行规划任务：{reason}\n规划提示：\n{rendered_errors}"
        return f"\n\n⚠️ 后台未执行规划任务：{reason}"

    logs = result.get("logs") or []

    if not logs:
        for item in result.get("results", []):
            action = item.get("action")
            if action == "read_resource":
                logs.append(f"读取资源: {item.get('path')}")
            elif action == "write_file":
                logs.append(f"写入文件: {item.get('path')}")
            elif action == "run_command":
                logs.append(f"执行命令成功: {item.get('command')}")
            elif action == "create_directory":
                logs.append(f"创建目录: {item.get('path')}")

    if not logs:
        return "\n\n✅ 后台已执行规划任务。"

    rendered = "\n".join(f"- {line}" for line in logs)
    return f"\n\n✅ 后台已执行规划任务：\n{rendered}"

async def _plan_and_execute_generated_output(
    *,
    assistant_text: str,
    request: ChatRequest,
    model: str,
    require_confirmation: bool = True,
    execution_root: Path | None = None,
    skill_name: str = "",
) -> dict:
    """Legacy fallback: plan and execute actions from main model Markdown output.

    新主路径不再依赖这个函数。
    仅当 runtime planner 判断 direct_answer，或者旧 Skill 仍要求通过主模型 Markdown 输出动作时，才作为兜底。
    """
    blocks = _extract_all_fenced_blocks(assistant_text)

    if not blocks:
        return {
            "executed": False,
            "reason": "主模型回复中未检测到 fenced code block。",
            "plan": {"tasks": [], "errors": []},
            "results": [],
        }

    planner_model = _planner_model_name(model)

    plan = await _run_block_planner_round(
        assistant_text=assistant_text,
        blocks=blocks,
        request=request,
        model=planner_model,
    )

    if plan.get("errors") and not plan.get("tasks"):
        return {
            "executed": False,
            "reason": "规划模型未生成可执行任务。",
            "plan": plan,
            "results": [],
        }

    return await asyncio.to_thread(
        functools.partial(
            _execute_planned_actions,
            plan,
            blocks,
            request,
            require_confirmation=require_confirmation,
            execution_root=execution_root,
            skill_name=skill_name,
        )
    )

def _make_stream(skill_context: dict, request: ChatRequest):
    """Staged Skill execution with shared runtime planning and action execution."""
    requested_model = request.model or settings.default_model
    model = route_model(TEXT_TASK, requested_model=requested_model, reason="sandbox default response").model
    _MAX_CMD_DISPLAY_LENGTH = 60
    force_body = bool(skill_context.get("force_body", False))
    enable_action_execution = bool(skill_context.get("enable_action_execution", False))
    require_action_confirmation = bool(skill_context.get("require_action_confirmation", True))
    strict_skill_execution = bool(skill_context.get("strict_skill_execution", False))
    execution_root = skill_context.get("execution_root")
    child_body_loader = skill_context.get("child_body_loader")
    parent_skill_name = skill_context.get("skill_name", "")
    enable_resource_preload = bool(skill_context.get("enable_resource_preload", False))

    if execution_root is not None:
        execution_root = Path(execution_root).resolve()
        # Verify the resolved path is within an allowed skill root so that
        # a crafted skill_context cannot steer execution outside the sandbox.
        allowed_roots = _allowed_skill_roots()
        if not any(_is_within_sandbox(execution_root, r.resolve()) for r in allowed_roots):
            raise ValueError(
                f"execution_root '{execution_root}' is outside all allowed skill roots."
            )

    async def generate():
        try:
            if force_body:
                need_body = True
                logger.debug("force_body=True, skip metadata decision and load SKILL.md body directly")
            else:
                yield _sse({"status": {"phase": "analyzing", "message": "分析请求匹配度…"}})
                need_body = await _run_metadata_round(
                    metadata_prompt=skill_context["metadata_prompt"],
                    request=request,
                    model=model,
                )
                yield _thought(
                    "metadata_decision",
                    "分析匹配度",
                    f"{'需要加载正文' if need_body else '请求与 Skill 不匹配，跳过正文'}",
                    {
                        "need_body": need_body,
                        "metadata_chars": len(skill_context.get("metadata_prompt", "")),
                    },
                )

            if not need_body:
                yield _sse({"status": None})
                fallback_messages = [
                    {
                        "role": "system",
                        "content": (
                            "当前用户请求与已选 Skill 及其子 Skill 的 metadata 不匹配。"
                            "请简短说明该 Skill 不适用，并提示用户重新描述需求。"
                        ),
                    }
                ]
                fallback_messages.extend(_request_messages_with_files(request))

                async for chunk in stream_chat(fallback_messages, model):
                    yield _sse({"content": chunk})

                yield "data: [DONE]\n\n"
                return

            yield _sse({"status": {"phase": "loading", "message": "加载 Skill 正文…"}})
            body_prompt = skill_context["body_loader"]()
            yield _thought(
                "body_loaded",
                "加载 SKILL.md",
                f"正文已加载，共 {len(body_prompt)} 字符",
                {
                    "body_chars": len(body_prompt),
                    "skill_name": parent_skill_name,
                },
            )

            if child_body_loader:
                yield _sse({"status": {"phase": "loading_child", "message": "检查子 Skill…"}})
                child_decision = await _run_child_skill_selection_round(
                    parent_metadata_prompt=skill_context["metadata_prompt"],
                    request=request,
                    model=model,
                )
                yield _thought(
                    "child_decision",
                    "子 Skill 检查",
                    (
                        f"加载子 Skill：{child_decision.get('child_ref')}"
                        if child_decision.get("need_child")
                        else f"无需子 Skill：{child_decision.get('reason', '')}"
                    ),
                    {
                        "need_child": child_decision.get("need_child"),
                        "child_ref": child_decision.get("child_ref", ""),
                        "reason": child_decision.get("reason", ""),
                    },
                )

                if child_decision.get("need_child"):
                    child_ref = child_decision.get("child_ref", "")
                    yield _sse({"status": {"phase": "loading_child", "message": f"加载子 Skill：{child_ref}…"}})
                    try:
                        child_body_prompt = child_body_loader(child_ref)
                        body_prompt = (
                            f"{body_prompt}\n\n"
                            "---\n\n"
                            "## Loaded Child Skill Body\n\n"
                            f"父 Skill 已根据用户请求按需加载子 Skill：`{child_ref}`。\n"
                            "下面是该子 Skill 的完整执行正文。\n\n"
                            f"{child_body_prompt}"
                        )
                    except Exception as exc:
                        logger.warning(
                            "failed to load child skill body parent=%s child_ref=%s error=%s",
                            parent_skill_name,
                            child_ref,
                            exc,
                        )
                        body_prompt = (
                            f"{body_prompt}\n\n"
                            "---\n\n"
                            "## Child Skill Load Warning\n\n"
                            f"运行时尝试加载子 Skill `{child_ref}`，但加载失败：{exc}\n"
                            "请不要假装已经读取该子 Skill 正文。"
                        )

            loaded_resource_paths: list[str] = []
            failed_resource_paths: list[dict] = []

            if enable_resource_preload:
                resource_catalog = _extract_runtime_resource_catalog(body_prompt, execution_root=execution_root)
                if resource_catalog:
                    yield _sse({"status": {"phase": "loading_resources", "message": "按需加载资源…"}})
                resource_decision = await _run_resource_selection_round(
                    body_prompt=body_prompt,
                    request=request,
                    model=model,
                    resource_catalog=resource_catalog,
                )
                yield _thought(
                    "resource_selection",
                    "资源选择",
                    (
                        f"加载 {len(resource_decision.get('resource_handles', []))} 个资源：{', '.join(resource_decision.get('resource_handles', []))}"
                        if resource_decision.get("need_resources")
                        else f"无需加载额外资源：{resource_decision.get('reason', '')}"
                    ),
                    {
                        "need_resources": resource_decision.get("need_resources"),
                        "resource_handles": resource_decision.get("resource_handles", []),
                        "catalog_size": len(resource_catalog),
                        "reason": resource_decision.get("reason", ""),
                    },
                )

                if resource_decision.get("need_resources"):
                    selected = resource_decision.get("resource_handles") or []
                    yield _sse({"status": {"phase": "loading_resources", "message": f"加载 {len(selected)} 个资源…"}})
                    loaded_resources = _compose_loaded_resources_prompt(
                        skill_name=parent_skill_name,
                        resource_catalog=resource_catalog,
                        selected_handles=selected,
                        execution_root=execution_root,
                    )
                    loaded_resource_paths = loaded_resources.get("loaded_paths", [])
                    failed_resource_paths = loaded_resources.get("failed_paths", [])
                    loaded_resources_prompt = str(loaded_resources.get("prompt") or "")

                    if loaded_resources_prompt:
                        body_prompt = body_prompt + loaded_resources_prompt

            # Append uploaded input-file context to the body prompt so the LLM
            # knows which files are available. For small text files the content is
            # embedded directly so the LLM can reason about the data without running
            # a script first. Binary or large files are described by path only.
            if getattr(request, "input_files", None):
                _TEXT_CONTENT_SUFFIXES = frozenset({
                    ".txt", ".md", ".csv", ".tsv", ".json", ".jsonl",
                    ".yaml", ".yml", ".xml", ".html", ".htm", ".log",
                })
                _MAX_INLINE_BYTES = 100 * 1024  # 100 KB

                file_sections: list[str] = []
                for f in request.input_files:
                    rel_path = f.get("path", "")
                    filename = f.get("filename", rel_path.split("/")[-1] if rel_path else "")
                    suffix = Path(filename).suffix.lower() if filename else ""

                    # Try to read text content for embedding
                    content_block = ""
                    if rel_path and parent_skill_name and suffix in _TEXT_CONTENT_SUFFIXES:
                        try:
                            abs_path = (settings.skills_path / parent_skill_name / rel_path).resolve()
                            # Ensure path stays inside the skill directory
                            skill_dir_check = (settings.skills_path / parent_skill_name).resolve()
                            abs_path.relative_to(skill_dir_check)
                            if abs_path.is_file():
                                raw = abs_path.read_bytes()
                                if len(raw) <= _MAX_INLINE_BYTES:
                                    text = raw.decode("utf-8", errors="replace")
                                    # Choose a fence that doesn't appear in the content.
                                    # Prefer ``` but fall back to a tilde fence when the
                                    # file itself contains triple-backtick sequences.
                                    if "```" not in text:
                                        fence, content_text = "```", text
                                    else:
                                        fence = "~~~~"
                                        content_text = text.replace("~~~~", "~ ~ ~ ~")
                                    content_block = (
                                        f"\n\n  文件内容如下：\n\n  {fence}\n{content_text}\n  {fence}"
                                    )
                        except Exception:
                            pass  # fall back to path-only if read fails

                    if content_block:
                        file_sections.append(
                            f"- `{rel_path}`（文件名：`{filename}`）{content_block}"
                        )
                    else:
                        # Strip the leading "inputs/" component so the script only needs
                        # os.path.join(INPUT_DIR, remaining) — INPUT_DIR points to inputs/.
                        try:
                            _rel_path_obj = Path(rel_path)
                            # Use parts[0] to avoid Windows backslash ambiguity.
                            if _rel_path_obj.parts and _rel_path_obj.parts[0] == "inputs":
                                rel_to_input_dir = Path(*_rel_path_obj.parts[1:]).as_posix()
                            else:
                                rel_to_input_dir = rel_path
                        except (ValueError, IndexError):
                            rel_to_input_dir = rel_path
                        file_sections.append(
                            f"- `{rel_path}`（文件名：`{filename}`，"
                            f"脚本可通过 `os.path.join(os.environ['INPUT_DIR'], '{rel_to_input_dir}')` 读取；"
                            "或直接用 `os.environ['INPUT_SESSION_DIR']` 目录（该目录下包含本次会话所有上传文件）"
                            "）"
                        )

                if file_sections:
                    sections_text = "\n".join(file_sections)
                    body_prompt = (
                        body_prompt
                        + "\n\n---\n\n"
                        "## 当前对话已上传文件\n\n"
                        "用户在本次对话中上传了以下文件，你必须以这些文件为输入进行分析或处理。\n"
                        "- 对于文本/数据文件，内容已直接展示在下方，请直接阅读并回答。\n"
                        "- 需要执行计算、统计、转换等操作时，可生成 Python 脚本并运行，"
                        "脚本中使用 `os.environ['INPUT_SESSION_DIR']` 获取上传文件目录，"
                        "使用 `os.environ['OUTPUT_DIR']` 输出结果文件。\n\n"
                        f"{sections_text}\n"
                    )

            if enable_action_execution:
                try:
                    yield _sse({"status": {"phase": "planning", "message": "规划执行方案…"}})
                    runtime_plan = await _run_skill_runtime_planner_round(
                        body_prompt=body_prompt,
                        request=request,
                        model=model,
                        execution_root=execution_root,
                        skill_name=parent_skill_name,
                        loaded_paths=loaded_resource_paths,
                        failed_paths=failed_resource_paths,
                    )

                    response_route = route_model(
                        infer_sandbox_response_task(
                            body_prompt=body_prompt,
                            user_text=_last_user_text(request),
                            plan=runtime_plan,
                        ),
                        requested_model=requested_model,
                        reason="sandbox runtime plan classification",
                    )
                    response_model = response_route.model
                    yield _sse({"model_ack": response_route.ack()})

                    mode = runtime_plan.get("mode")
                    tasks = runtime_plan.get("tasks") or []

                    # Emit planner_output thought with safe task summaries (no SKILL.md content).
                    yield _thought(
                        "planner_output",
                        "规划结果",
                        f"模式：{mode}，共 {len(tasks)} 个动作",
                        {
                            "mode": mode,
                            "task_count": len(tasks),
                            "tasks": [
                                {
                                    "action": t.get("action"),
                                    "command": (str(t.get("command") or ""))[:120] or None,
                                    "path": t.get("path") or t.get("resource_handle") or None,
                                    "reason": str(t.get("reason") or "")[:200],
                                }
                                for t in tasks
                            ],
                            "errors": runtime_plan.get("errors") or [],
                            "missing": runtime_plan.get("missing") or [],
                        },
                    )

                    _planned_followup_commands = _extract_executable_command_blocks_from_text(
                        str(runtime_plan.get("final_instruction") or "")
                    )

                    if mode == "execute" and (tasks or _planned_followup_commands):
                        # Set up shared execution context for the per-task loop.
                        _exec_inferred_root = _infer_skill_root_from_tasks(
                            runtime_plan, execution_root=execution_root
                        )
                        _exec_cwd = execution_root or _exec_inferred_root
                        _exec_session_dir = _extract_input_session_dir(
                            getattr(request, "input_files", []) or [], _exec_cwd
                        )

                        _exec_all_results: list[dict] = []
                        _exec_all_touched: list[Path] = []

                        # Execute tasks one at a time so the frontend receives
                        # real-time thought events after each task completes.
                        for task in tasks:
                            task_action = str(task.get("action") or "").strip()

                            # Announce what is about to happen.
                            if task_action == "run_command":
                                cmd = str(task.get("command") or "")
                                short_cmd = cmd[:_MAX_CMD_DISPLAY_LENGTH] + (
                                    "…" if len(cmd) > _MAX_CMD_DISPLAY_LENGTH else ""
                                )
                                yield _sse({"status": {"phase": "executing", "message": f"执行命令：{short_cmd}"}})
                                yield _thought(
                                    "action_start",
                                    "执行命令",
                                    short_cmd,
                                    {"action": "run_command", "command": cmd[:200]},
                                )
                            elif task_action == "read_resource":
                                res_path = str(task.get("path") or task.get("resource_handle") or "")
                                yield _sse({"status": {"phase": "reading", "message": f"读取资源：{res_path}"}})
                                yield _thought(
                                    "action_start",
                                    "读取资源",
                                    res_path,
                                    {"action": "read_resource", "path": res_path},
                                )
                            elif task_action == "write_file":
                                wf_path = str(task.get("path") or "")
                                yield _sse({"status": {"phase": "writing", "message": f"写入文件：{wf_path}"}})
                                yield _thought(
                                    "action_start",
                                    "写入文件",
                                    wf_path,
                                    {"action": "write_file", "path": wf_path},
                                )
                            elif task_action == "create_directory":
                                cd_path = str(task.get("path") or "")
                                yield _sse({"status": {"phase": "creating", "message": f"创建目录：{cd_path}"}})
                                yield _thought(
                                    "action_start",
                                    "创建目录",
                                    cd_path,
                                    {"action": "create_directory", "path": cd_path},
                                )
                            else:
                                yield _thought(
                                    "action_start",
                                    "执行动作",
                                    task_action,
                                    {"action": task_action},
                                )

                            # Run the task in a thread and capture the result.
                            task_result, task_touched = await asyncio.to_thread(
                                functools.partial(
                                    _execute_single_task,
                                    task,
                                    [],
                                    request,
                                    execution_root=execution_root,
                                    inferred_skill_root=_exec_inferred_root,
                                    skill_name=parent_skill_name,
                                    session_input_dir=_exec_session_dir,
                                )
                            )
                            _exec_all_results.append(task_result)
                            _exec_all_touched.extend(task_touched)

                            # Build safe result data for the thought (truncate stdout/stderr).
                            _safe_result = {
                                k: (v[:1000] if isinstance(v, str) else v)
                                for k, v in task_result.items()
                                if k not in {"content"}  # omit large resource content
                            }

                            success_flag = task_result.get("success", True)
                            if task_action == "run_command":
                                rc = task_result.get("returncode", 0)
                                yield _thought(
                                    "action_result",
                                    "执行结果",
                                    f"{'成功' if success_flag else '失败'} exit={rc}",
                                    _safe_result,
                                )
                            elif task_action == "read_resource":
                                yield _thought(
                                    "action_result",
                                    "读取结果",
                                    f"{'成功' if success_flag else '失败'}，"
                                    f"{len(task_result.get('content', ''))} 字符",
                                    _safe_result,
                                )
                            else:
                                yield _thought(
                                    "action_result",
                                    "操作结果",
                                    f"{'成功' if success_flag else '失败'}",
                                    _safe_result,
                                )

                        # read_resource actions are only pre-actions in execute mode.
                        # If the planner's final_instruction already contains a
                        # concrete SKILL.md command block, execute it now and use
                        # its real stdout JSON as the final observation.
                        _followup_commands = list(_planned_followup_commands)
                        for command in _followup_commands:
                            script_path = _extract_script_path_from_command(command) or ""
                            available_scripts = set(_available_scripts_for_root(execution_root))
                            if not script_path or script_path not in available_scripts:
                                raise ValueError(
                                    "final_instruction 命令未通过 available_scripts 校验："
                                    f"script_path={script_path!r} available={sorted(available_scripts)}"
                                )

                            short_cmd = command[:_MAX_CMD_DISPLAY_LENGTH] + (
                                "…" if len(command) > _MAX_CMD_DISPLAY_LENGTH else ""
                            )
                            yield _sse({"status": {"phase": "executing", "message": f"执行命令：{short_cmd}"}})
                            yield _thought(
                                "action_start",
                                "执行 final_instruction 命令",
                                short_cmd,
                                {
                                    "action": "run_command",
                                    "command": command[:200],
                                    "script_path": script_path,
                                    "source": "runtime_plan.final_instruction",
                                },
                            )
                            task_result, task_touched = await asyncio.to_thread(
                                functools.partial(
                                    _execute_single_task,
                                    {
                                        "action": "run_command",
                                        "command": command,
                                        "reason": "runtime_plan.final_instruction 中声明的脚本命令",
                                    },
                                    [],
                                    request,
                                    execution_root=execution_root,
                                    inferred_skill_root=_exec_inferred_root,
                                    skill_name=parent_skill_name,
                                    session_input_dir=_exec_session_dir,
                                )
                            )
                            _exec_all_results.append(task_result)
                            _exec_all_touched.extend(task_touched)
                            _safe_result = {
                                k: (v[:1000] if isinstance(v, str) else v)
                                for k, v in task_result.items()
                            }
                            yield _thought(
                                "action_result",
                                "执行结果",
                                f"{'成功' if task_result.get('success', True) else '失败'} exit={task_result.get('returncode', 0)}",
                                _safe_result,
                            )

                        if (
                            _execution_requires_run_command_observation(runtime_plan)
                            and not _has_successful_run_command_observation(_exec_all_results)
                        ):
                            yield _sse({"status": None})
                            yield _sse({
                                "content": (
                                    "已完成前置资源读取，但本轮没有获得成功的 run_command observation；"
                                    "因此不能声称脚本已执行、故事已生成或图片已生成。"
                                )
                            })
                            yield "data: [DONE]\n\n"
                            return

                        # Post-loop: validate any newly created Skill roots.
                        for root in _find_created_skill_roots(_exec_all_touched):
                            skill_md = root / "SKILL.md"
                            if skill_md.exists():
                                _validate_skill_md(skill_md)

                        # Assemble exec_result compatible with _generate_final_answer_from_observation.
                        _exec_all_output_files: list[dict] = []
                        for r in _exec_all_results:
                            _exec_all_output_files.extend(r.get("output_files") or [])

                        exec_result = {
                            "executed": True,
                            "reason": "已根据结构化 action plan 逐任务执行。",
                            "plan": runtime_plan,
                            "results": _exec_all_results,
                            "logs": [],
                            "output_files": _exec_all_output_files,
                        }

                        yield _sse({"status": {"phase": "generating", "message": "生成最终回答…"}})
                        final_answer = _render_success_stdout_payload(exec_result)
                        if final_answer is None:
                            final_answer = await _generate_final_answer_from_observation(
                                body_prompt=body_prompt,
                                request=request,
                                model=response_model,
                                plan=runtime_plan,
                                execution_result=exec_result,
                            )
                        final_answer = _finalize_answer_output_file_links(final_answer, _exec_all_output_files)
                        yield _thought(
                            "final_answer",
                            "生成回答",
                            f"共 {len(final_answer)} 字符，包含 {len(_exec_all_output_files)} 个输出文件",
                            {
                                "answer_chars": len(final_answer),
                                "has_output_files": bool(_exec_all_output_files),
                                "output_file_count": len(_exec_all_output_files),
                            },
                        )

                        yield _sse({"status": None})

                        # Emit structured output_files event so the frontend can
                        # render download links without relying on LLM text parsing.
                        if _exec_all_output_files:
                            yield _sse({
                                "action_result": {
                                    "action": "output_files",
                                    "name": parent_skill_name,
                                    "success": True,
                                    "message": f"生成了 {len(_exec_all_output_files)} 个文件",
                                    "output_files": _exec_all_output_files,
                                }
                            })

                        yield _sse({"content": final_answer})
                        yield "data: [DONE]\n\n"
                        return

                    if mode == "ask_user":
                        yield _sse({"status": None})
                        missing = runtime_plan.get("missing") or []
                        errors = runtime_plan.get("errors") or []

                        if missing:
                            text = "缺少必要信息，无法执行 Skill：\n" + "\n".join(
                                f"- {item}" for item in missing
                            )
                        elif errors:
                            text = "运行时规划失败：\n" + "\n".join(
                                f"- {json.dumps(item, ensure_ascii=False)}" for item in errors
                            )
                        else:
                            text = "缺少必要信息，无法执行当前 Skill。"

                        yield _sse({"content": text})
                        yield "data: [DONE]\n\n"
                        return

                    if mode == "not_applicable":
                        yield _sse({"status": None})
                        yield _sse({"content": "当前用户请求与该 Skill 不匹配，请重新选择 Skill 或重新描述需求。"})
                        yield "data: [DONE]\n\n"
                        return

                    # mode == direct_answer 时继续走普通主模型回复。
                    yield _sse({"status": None})

                except Exception as exc:
                    logger.exception("runtime skill action planning/execution failed")
                    yield _sse({"status": None})
                    yield _sse({"error": "错误：运行时规划或执行失败"})
                    yield "data: [DONE]\n\n"
                    return

            response_route = route_model(
                infer_sandbox_response_task(
                    body_prompt=body_prompt,
                    user_text=_last_user_text(request),
                    plan=locals().get("runtime_plan") if isinstance(locals().get("runtime_plan"), dict) else None,
                    input_files=request.input_files,
                ),
                requested_model=requested_model,
                reason="sandbox final response classification",
            )
            response_model = response_route.model
            yield _sse({"model_ack": response_route.ack()})

            final_messages: list[dict] = []
            final_messages.append(
                {
                    "role": "system",
                    "content": body_prompt,
                }
            )

            _runtime_plan_for_final = locals().get("runtime_plan")
            if isinstance(_runtime_plan_for_final, dict):
                _final_instruction = str(_runtime_plan_for_final.get("final_instruction") or "").strip()
                if _final_instruction:
                    final_messages.append(
                        {
                            "role": "system",
                            "content": (
                                "运行时动作意图判断器给出的本轮执行提示：\n"
                                f"{_final_instruction}\n\n"
                                "如果该提示要求输出可执行动作，必须把真实命令或文件内容放入 fenced code block；"
                                "后端只会执行本轮回复中已经出现的 fenced code block。"
                            ),
                        }
                    )

            if strict_skill_execution:
                final_messages.append(
                    {
                        "role": "system",
                        "content": (
                            "当前处于沙盒 Skill 严格执行模式。\n\n"
                            "你必须严格遵循已经加载的 Loaded SKILL.md，禁止把它当作普通参考资料。\n"
                            "你不得绕过 Loaded SKILL.md 自由回答用户请求。\n"
                            "你不得自行编造业务结果、执行结果、计划内容、文件内容或命令输出。\n\n"
                            "如果 Loaded SKILL.md 语义上要求通过某种动作完成任务，"
                            "例如运行程序、调用脚本、执行命令、写入文件、读取资源、生成配置、运行测试或调用工具，"
                            "你必须先按照 Loaded SKILL.md 的原始要求输出该动作的实际形式。\n"
                            "动作表达形式由 Loaded SKILL.md 决定，不能固定假设某种章节、某种语言、某种命令或某种格式。\n\n"
                            "如果动作中包含示例输入、占位输入、演示参数或模板参数，"
                            "只要语义上对应当前用户输入，就必须替换为当前用户的真实输入。\n"
                            "不能在应该替换时原样保留示例值或占位值。\n\n"
                            "如果缺少必要参数，必须明确指出缺少哪些信息；"
                            "不得猜测，不得保留占位符继续输出，不得直接编造最终结果。\n\n"
                            "只有当 Loaded SKILL.md 明确要求直接生成文本结果，"
                            "或者不存在任何外部动作要求时，才可以直接生成文本结果。\n"
                        ),
                    }
                )

            if response_route.task == "vision":
                final_messages.extend(_request_messages_with_inline_images(request, execution_root))
            else:
                final_messages.extend(_request_messages_with_files(request))

            assistant_chunks: list[str] = []
            ack_payload = {}

            def _capture_final_ack(payload: dict) -> None:
                ack_payload.update(payload)

            async for chunk in stream_chat(final_messages, response_model, model_ack_callback=_capture_final_ack):
                if ack_payload:
                    yield _sse({"model_ack": {**response_route.ack(actual_model=ack_payload.get("actual_model")), "provider": ack_payload}})
                    ack_payload.clear()
                assistant_chunks.append(chunk)
                if not enable_action_execution:
                    yield _sse({"content": chunk})

            assistant_text = "".join(assistant_chunks)

            if enable_action_execution:
                try:
                    exec_result = await _plan_and_execute_generated_output(
                        assistant_text=assistant_text,
                        request=request,
                        model=model,
                        require_confirmation=require_action_confirmation,
                        execution_root=execution_root,
                        skill_name=parent_skill_name,
                    )

                    if exec_result.get("executed"):
                        exec_result["assistant_draft"] = assistant_text

                        yield _sse({"status": {"phase": "generating", "message": "整合执行结果…"}})

                        final_answer = await _generate_final_answer_from_observation(
                            body_prompt=body_prompt,
                            request=request,
                            model=route_model(
                                TEXT_TASK,
                                requested_model=requested_model,
                                reason="sandbox finalization after actions",
                            ).model,
                            plan=locals().get("runtime_plan")
                            if isinstance(locals().get("runtime_plan"), dict)
                            else exec_result.get("plan", {}),
                            execution_result=exec_result,
                        )

                        output_files = exec_result.get("output_files") or []
                        final_answer = _finalize_answer_output_file_links(final_answer, output_files)
                        yield _sse({"content": final_answer})

                        if output_files:
                            yield _sse({
                                "action_result": {
                                    "action": "output_files",
                                    "name": parent_skill_name,
                                    "success": True,
                                    "message": f"生成了 {len(output_files)} 个文件",
                                    "output_files": output_files,
                                }
                            })

                        # Emit structured output_files event for the fallback path too.
                        output_files = exec_result.get("output_files") or []
                        if output_files:
                            yield _sse({
                                "action_result": {
                                    "action": "output_files",
                                    "name": parent_skill_name,
                                    "success": True,
                                    "message": f"生成了 {len(output_files)} 个文件",
                                    "output_files": output_files,
                                }
                            })

                except Exception as exc:
                    logger.exception("legacy markdown action fallback failed")
                    yield _sse({"status": None})
                    yield _sse({"error": "错误：后台规划或执行文件操作失败"})
                    yield "data: [DONE]\n\n"
                    return
            if enable_action_execution and assistant_text and not locals().get("exec_result", {}).get("executed"):
                yield _sse({"content": assistant_text})
            yield _sse({"status": None})
            yield "data: [DONE]\n\n"

        except Exception as exc:
            logger.exception("LLM stream error")
            yield _sse({"status": None})
            yield _sse({"error": _friendly_error(exc)})
            yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
def build_skill_context(skill_name: str) -> dict:
    """Build sandbox skill context for an existing skill."""
    skill_root = _skill_root_for_name(skill_name)
    skill_metadata_prompt = load_skill_metadata_prompt(skill_name)

    return {
        "skill_name": skill_name,
        "metadata_prompt": skill_metadata_prompt,
        "body_loader": lambda: load_skill_body_prompt(skill_name),
        "child_body_loader": lambda child_ref: load_child_skill_body_prompt(skill_name, child_ref),
        "force_body": False,
        "enable_action_execution": True,
        "require_action_confirmation": False,
        "execution_root": skill_root,
        "strict_skill_execution": True,
        "enable_resource_preload": True,
    }


@router.post("/sandbox/{skill_name}")
async def chat_in_sandbox(skill_name: str, request: ChatRequest):
    """Multi-turn chat with a specific skill loaded in sandbox mode."""
    try:
        skill_context = build_skill_context(skill_name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    return _make_stream(skill_context, request)
