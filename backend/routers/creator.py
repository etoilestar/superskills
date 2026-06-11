"""Creator router — file-by-file Skill generation endpoints.

These endpoints decouple the file-creation phase from the main
/api/chat/creator conversation endpoint:

- POST /api/creator/analyze-blueprint  — extract file list from blueprint (no LLM)
- POST /api/creator/init-skill          — create Skill directory structure
- POST /api/creator/generate-file       — SSE: stream single-file content from LLM
- POST /api/creator/write-file          — write generated content to disk
- POST /api/creator/validate-skill      — validate SKILL.md format
- POST /api/creator/package-skill       — package Skill directory into .skill archive
"""

import ast
import base64
import csv
import io
import json
from dataclasses import MISSING, dataclass, field, fields as dataclass_fields, replace
import logging
import re
import shlex
import subprocess
import tempfile
import yaml
from pathlib import Path
from typing import Any, Optional
import shutil

from fastapi import APIRouter, HTTPException, File, Form, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..config import settings
from ..services.blueprint_parser import BlueprintPlan, parse_blueprint
from ..services.skill_plan import SkillPlanEntry, build_skill_plan_entry, capabilities_for_role, command_template_for_entry, default_io_for_role, file_role_classifier, file_type_for_path, language_for_path, runtime_for_language, normalize_required_capabilities, is_runtime_artifact_semantic, command_payload_placeholders, render_script_command_from_skill_plan
from ..services.creator_tool_registry import get_tool_capability, list_tool_capabilities, tool_status
from ..services.llm_proxy import complete_chat_once, stream_chat
from ..services.model_router import VALIDATOR_TASK, route_creator_file_model, route_model
from ..services.skill_executor import _build_script_runtime_env, run_action
from ..services.artifact_validator import validate_stdout_file_outputs, FileOutputValidationError
from .chat_utils import _get_skill_venv_python, _scan_and_install_python_deps

logger = logging.getLogger(__name__)


def _log_creator_model_usage(
    *,
    phase: str,
    file_path: str,
    route=None,
    model: str | None = None,
    skill_name: str = "",
    attempt: int | None = None,
    actual_model: str | None = None,
    provider: dict | None = None,
    extra: str = "",
) -> None:
    """Emit compact model routing/ack logs for docker logs debugging."""
    expected_model = model or (getattr(route, "model", "") if route is not None else "")
    task = getattr(route, "task", "") if route is not None else ""
    requested_model = getattr(route, "requested_model", None) if route is not None else None
    reason = getattr(route, "reason", "") if route is not None else ""
    matched = None
    if route is not None and actual_model:
        try:
            matched = route.ack(actual_model=actual_model).get("matched")
        except Exception:  # pragma: no cover - defensive logging only
            matched = None
    logger.info(
        "[Creator][model] phase=%s skill=%s file=%s attempt=%s task=%s model=%s requested_model=%s actual_model=%s matched=%s reason=%s provider=%s%s",
        phase,
        skill_name,
        file_path,
        "" if attempt is None else attempt,
        task,
        expected_model,
        requested_model or "",
        actual_model or "",
        "" if matched is None else matched,
        reason,
        provider or {},
        f" extra={extra}" if extra else "",
    )


_SKILL_MD_MARKDOWN_EXECUTION_GUIDE = """

宿主 Markdown 执行说明（写入生成的 SKILL.md 正文时必须保持常见 Markdown 形态）：
- SKILL.md 是普通 Markdown 说明书，只描述做什么、何时使用资源，以及 assistant 在运行时应如何表达动作；不要引入自定义协议章节（例如 `Runtime Contract` JSON）。
- 对纯文本即可完成的任务，明确写“直接回答”，不要要求运行脚本。
- 如果确实需要运行 scripts/ 下的脚本，必须使用标准 Markdown fenced code block，且 info string 必须是 bash，例如：
  ```bash
  python scripts/<script-name> '{"topic":"{{topic}}","keywords":"{{keywords}}"}'
  ```
- 命令示例必须与脚本真实接口一致：脚本读 JSON argv 时，示例就传 JSON；脚本读 stdin 时，正文就说明 stdin 内容。禁止让运行时主模型根据脚本名临时猜 CLI flags。
- 参数映射用普通 Markdown 列表说明，例如 `topic` 从用户输入提取、`keywords` 从用户输入提取、可选参数给出默认值；不要使用单独的 JSON contract。
- 只有 assistant 在 Sandbox 当轮回复中输出的 fenced code block 才会被宿主解析和执行；SKILL.md 中的 block 是运行说明/示例，不会在加载时自动执行。
- 如果需要写文件，用普通 Markdown 说明 assistant 应输出 `写入文件：<path>` 或 `保存到：<path>`，并把完整文件内容放在紧随其后的 fenced code block。
- assistant 不得假装脚本已经执行；必须等待宿主返回 stdout/stderr/observation 后，再基于 observation 生成最终回答。
- 禁止在 SKILL.md 中只写“立即调用 `scripts/...`”这种隐式执行描述；应写成“运行时 assistant 输出以下命令块交由宿主执行”，并给出具体命令示例。
- 如果用户要求使用平台内置模型、图像模型或多模态模型，不要写外部 API key、关键词数据库或假 API；应说明由宿主配置的模型完成相关步骤。任何脚本都必须是有实际功能的实现：要么执行确定性的真实计算/转换/文件处理，要么在需要开放式生成、语义理解、视觉/图像能力时使用宿主已配置的模型能力；模型与认证相关参数由平台运行时注入；生成脚本可按需读取 `IMAGE_MODEL`、`IMAGE_BASE_URL`、`IMAGE_SIZE`、`IMAGE_API_KEY` / `LLM_API_KEY` / `OPENAI_API_KEY` 等环境变量，但不要硬编码这些值，也不需要额外校验它们是否存在。
- 如果需要生成图片，SKILL.md 只描述“使用平台稳定扩散图片生成能力”即可；不要把中文 prompt 翻译、TEXT_MODEL 调用、接口字段解析等平台细节写入创建出来的 Skill 正文。平台运行时会静默完成中文 topic 到英文 Stable Diffusion prompt 的转换。
"""

router = APIRouter(prefix="/api/creator", tags=["creator"])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Directories allowed as parents when writing non-SKILL.md files.
_ALLOWED_FOLDERS: frozenset[str] = frozenset({"scripts", "references", "assets"})

# Trailing conversation turns to include in file-generation prompts.
_MAX_HISTORY_TURNS = 6

# Generated files can repair themselves by sending validator/static/trial-run
# failures back to the same routed model before returning content to the frontend.
_MAX_FILE_REPAIR_ATTEMPTS = 10
_SCRIPT_TRIAL_TIMEOUT_SECONDS = 30

# Human-readable language labels indexed by file extension.
_LANG_LABELS: dict[str, str] = {
    ".py":       "Python",
    ".js":       "JavaScript",
    ".mjs":      "JavaScript",
    ".cjs":      "JavaScript",
    ".ts":       "TypeScript",
    ".sh":       "Bash",
    ".bash":     "Bash",
    ".rb":       "Ruby",
    ".go":       "Go",
    ".md":       "Markdown",
    ".yaml":     "YAML",
    ".yml":      "YAML",
    ".json":     "JSON",
    ".toml":     "TOML",
    ".txt":      "Text",
    ".jinja":    "Jinja2 模板",
    ".jinja2":   "Jinja2 模板",
    ".template": "模板文件",
    ".tmpl":     "模板文件",
}


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class AnalyzeBlueprintRequest(BaseModel):
    messages: list[dict]
    model: Optional[str] = None

class SkillMdBlueprintReviewRequest(BaseModel):
    skill_name: str
    content: str
    blueprint_text: str
    model: Optional[str] = None
    skill_plan_entry: Optional[dict[str, Any]] = None


class SkillMdBlueprintReviewResponse(BaseModel):
    passed: bool
    issues: list[dict[str, Any]] = Field(default_factory=list)
    repair_suggestions: str = ""
    fixed_content: Optional[str] = None

class FileSpecOut(BaseModel):
    path: str
    purpose: str
    required: bool
    can_skip: bool
    file_type: Optional[str] = None
    role: Optional[str] = None
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    forbidden_capabilities: list[str] = Field(default_factory=list)
    reference_files: list[str] = Field(default_factory=list)
    skill_local_references: list[str] = Field(default_factory=list)
    creator_internal_references: list[str] = Field(default_factory=list)
    language: str = "text"
    runtime: str = "none"
    entrypoint: str = ""
    command_template: str = ""
    references: list[str] = Field(default_factory=list)
    low_confidence: bool = False
    confidence: float = 0.0
    reason: str = ""
    heuristic_signals: list[str] = Field(default_factory=list)


class AnalyzeBlueprintResponse(BaseModel):
    skill_name: str
    files: list[FileSpecOut]
    warnings: list[str]
    available_tools: list[dict[str, Any]] = Field(default_factory=list)
    missing_tool_configs: list[dict[str, Any]] = Field(default_factory=list)


class InitSkillRequest(BaseModel):
    skill_name: str


class InitSkillResponse(BaseModel):
    success: bool
    path: Optional[str] = None
    message: str


class GenerateFileRequest(BaseModel):
    skill_name: str
    file_path: str
    purpose: str
    blueprint_text: str
    conversation_history: list[dict]
    model: Optional[str] = None
    role: Optional[str] = None
    skill_plan_entry: Optional[dict[str, Any]] = None


class WriteFileRequest(BaseModel):
    skill_name: str
    file_path: str
    content: str
    role: Optional[str] = None
    skill_plan_entry: Optional[dict[str, Any]] = None
    blueprint_text: str = ""


class WriteFileResponse(BaseModel):
    success: bool
    path: Optional[str] = None
    bytes: int = 0
    message: str

class UploadAssetResponse(BaseModel):
    success: bool
    path: Optional[str] = None
    size: int = 0
    message: str

class SkillActionRequest(BaseModel):
    skill_name: str
    model: Optional[str] = None
    auto_repair: bool = True
    max_e2e_repair_attempts: int = 5

class PackageSkillRequest(SkillActionRequest):
    validate_before_package: bool = True

class SkillActionResponse(BaseModel):
    success: bool
    path: Optional[str] = None
    message: str


class ListFilesRequest(BaseModel):
    skill_name: str


class FileInfo(BaseModel):
    path: str
    is_directory: bool
    size: int = 0


class ListFilesResponse(BaseModel):
    success: bool
    files: list[FileInfo]
    message: str


class InitFromBlueprintRequest(BaseModel):
    skill_name: str
    files: list[FileSpecOut]


class InitFromBlueprintResponse(BaseModel):
    success: bool
    path: Optional[str] = None
    files_created: int = 0
    message: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _contains_path_wildcard(file_path: str) -> bool:
    return any(ch in file_path for ch in "*?[]{}")


def _validate_file_path(file_path: str) -> None:
    """Raise HTTP 400 if file_path is outside allowed locations."""
    p = Path(file_path)
    if p.is_absolute() or ".." in p.parts or _contains_path_wildcard(file_path):
        raise HTTPException(
            status_code=400,
            detail=(
                f"非法文件路径: {file_path}。"
                "Creator 只能逐个生成具体文件，不能生成通配符路径。"
            ),
        )

    if file_path == "SKILL.md":
        return

    if not p.parts or p.parts[0] not in _ALLOWED_FOLDERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"文件路径 '{file_path}' 不合法。"
                f"只允许 SKILL.md 或 scripts/*、references/*、assets/* 下的文件。"
            ),
        )

    filename = p.name
    if not filename or filename.startswith(".") or "\x00" in filename or len(filename) > 255:
        raise HTTPException(status_code=400, detail=f"文件名非法: {filename!r}")

_ALLOWED_ASSET_EXTENSIONS: frozenset[str] = frozenset({
    ".pdf",
    ".docx",
    ".xlsx",
    ".csv",
    ".txt",
    ".tex",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".svg",
    ".ttf",
    ".otf",
    ".html",
    ".woff",
    ".woff2",
})

_MAX_ASSET_UPLOAD_BYTES = 50 * 1024 * 1024


def _validate_asset_upload_path(file_path: str) -> str:
    normalized = file_path.strip().replace("\\", "/")
    _validate_file_path(normalized)

    if not normalized.startswith("assets/"):
        raise HTTPException(status_code=400, detail="素材上传只能写入 assets/**。")

    suffix = Path(normalized).suffix.lower()
    if suffix and suffix not in _ALLOWED_ASSET_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"不支持上传该素材类型：{suffix}")

    return normalized

def _validate_skill_name(skill_name: str) -> str:
    """Strip, validate, and return the skill_name or raise HTTP 400."""
    name = skill_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="skill_name 不能为空。")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise HTTPException(
            status_code=400,
            detail="skill_name 只能包含小写字母、数字和连字符，且必须以字母或数字开头。",
        )
    return name


def _fallback_role_for_path(file_path: str, role: str | None = None) -> str:
    explicit = (role or "").strip()
    if explicit:
        return explicit

    if file_path == "SKILL.md":
        return "skill_overview"
    if file_path.startswith("references/"):
        return "reference"
    if file_path.startswith("scripts/"):
        return "generic_script"
    if file_path.startswith("assets/"):
        return "asset"
    return "generic_script"


def _skill_plan_entry_defaults(
    *,
    file_path: str,
    purpose: str = "",
    role: str | None = None,
) -> dict[str, Any]:
    resolved_role = _fallback_role_for_path(file_path, role)
    file_type = file_type_for_path(file_path)
    language = language_for_path(file_path)
    runtime = runtime_for_language(language, file_type)

    required_capabilities, forbidden_capabilities = capabilities_for_role(resolved_role)
    inputs, outputs = default_io_for_role(resolved_role)

    required_capabilities = list(required_capabilities or [])
    forbidden_capabilities = [
        cap for cap in list(forbidden_capabilities or [])
        if cap not in required_capabilities
    ]

    return {
        "path": file_path,
        "purpose": purpose or f"{file_path} 的职责说明",
        "file_type": file_type,
        "role": resolved_role,
        "inputs": list(inputs or []),
        "outputs": list(outputs or []),
        "dependencies": [],
        "required_capabilities": required_capabilities,
        "forbidden_capabilities": forbidden_capabilities,
        "reference_files": [],
        "skill_local_references": [],
        "creator_internal_references": [],
        "language": language,
        "runtime": runtime,
        "entrypoint": file_path if file_path.startswith("scripts/") else "",
        "command_template": "",
        "confidence": 1.0,
        "reason": "fallback path classification",
        "heuristic_signals": ["fallback_path_role"],
    }


def _fill_required_skill_plan_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Make SkillPlanEntry construction resilient to schema drift.

    Only pass fields that SkillPlanEntry actually defines.
    Fill missing required fields using safe neutral defaults.
    """
    result: dict[str, Any] = {}
    for f in dataclass_fields(SkillPlanEntry):
        name = f.name

        if name in data:
            result[name] = data[name]
            continue

        if f.default is not MISSING:
            continue

        if f.default_factory is not MISSING:  # type: ignore[attr-defined]
            continue

        # Required field missing: provide stable fallback by field name.
        if name in {
            "inputs",
            "outputs",
            "dependencies",
            "required_capabilities",
            "forbidden_capabilities",
            "reference_files",
            "skill_local_references",
            "creator_internal_references",
            "heuristic_signals",
        }:
            result[name] = []
        elif name == "confidence":
            result[name] = 1.0
        elif name in {"required", "can_skip", "low_confidence"}:
            result[name] = False
        elif name == "path":
            result[name] = str(data.get("path") or "")
        elif name == "purpose":
            result[name] = str(data.get("purpose") or "")
        elif name == "role":
            result[name] = str(data.get("role") or "generic_script")
        elif name == "file_type":
            result[name] = str(data.get("file_type") or "")
        elif name == "language":
            result[name] = str(data.get("language") or "text")
        elif name == "runtime":
            result[name] = str(data.get("runtime") or "none")
        else:
            result[name] = ""

    return result


def _skill_plan_entry_for_file(
    *,
    file_path: str,
    purpose: str = "",
    blueprint_text: str = "",
    role: str | None = None,
    skill_plan_entry: dict[str, Any] | None = None,
) -> SkillPlanEntry:
    """Return the per-file SkillPlan contract used by Creator.

    This is the only bridge between FileSpecOut/frontend payload and the backend
    SkillPlanEntry dataclass. Do not manually construct SkillPlanEntry elsewhere.
    """

    _validate_file_path(file_path)

    if file_path.startswith("assets/"):
        raise HTTPException(
            status_code=400,
            detail=f"{file_path} 属于 assets 静态素材目录，必须上传，不能生成",
        )

    data = _skill_plan_entry_defaults(
        file_path=file_path,
        purpose=purpose,
        role=role,
    )

    # Frontend passes FileSpecOut as skill_plan_entry. Merge it carefully.
    if skill_plan_entry and skill_plan_entry.get("path") == file_path:
        for key, value in skill_plan_entry.items():
            if value is not None:
                data[key] = value

    # Path-derived role wins when role is absent or wrong for references.
    data["role"] = _fallback_role_for_path(file_path, str(data.get("role") or role or ""))

    if file_path.startswith("references/"):
        data["role"] = "reference"
        data["file_type"] = data.get("file_type") or "reference"
        data["language"] = data.get("language") or "markdown"
        data["runtime"] = data.get("runtime") or "none"

    if file_path == "SKILL.md":
        data["role"] = "skill_overview"
        data["file_type"] = data.get("file_type") or "skill"
        data["runtime"] = data.get("runtime") or "none"

    # Recompute capability defaults only when caller did not provide them, then
    # normalize even explicit frontend/model payloads so resource/meta files and
    # over-broad SkillPlan capabilities do not leak into runtime warnings.
    required = list(data.get("required_capabilities") or [])
    forbidden = list(data.get("forbidden_capabilities") or [])
    if not required and not forbidden:
        required, forbidden = capabilities_for_role(data["role"])

    required = normalize_required_capabilities(
        role=str(data.get("role") or ""),
        path=file_path,
        required_capabilities=list(required or []),
        user_blueprint_text=blueprint_text or purpose or "",
    )
    data["required_capabilities"] = required
    data["forbidden_capabilities"] = [
        cap for cap in list(forbidden or data.get("forbidden_capabilities") or [])
        if cap not in set(required)
    ]

    if not data.get("inputs") or not data.get("outputs"):
        default_inputs, default_outputs = default_io_for_role(data["role"])
        data["inputs"] = list(data.get("inputs") or default_inputs or [])
        data["outputs"] = list(data.get("outputs") or default_outputs or [])

    data["purpose"] = str(data.get("purpose") or purpose or f"{file_path} 的职责说明")

    constructor_kwargs = _fill_required_skill_plan_fields(data)
    return SkillPlanEntry(**constructor_kwargs)


def _extract_first_fenced_block(content: str) -> str | None:
    """Return the first fenced block body from content, or None."""
    lines = content.splitlines(keepends=True)

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        match = re.match(r"(`{3,}|~{3,})([^\n`]*)\n?$", stripped.rstrip("\n"))
        if not match:
            i += 1
            continue

        fence = match.group(1)
        fence_char = fence[0]
        fence_len = len(fence)
        code_lines: list[str] = []
        i += 1

        while i < len(lines):
            close_line = lines[i]
            close_stripped = close_line.lstrip()
            close_match = re.match(
                rf"{re.escape(fence_char)}{{{fence_len},}}\s*$",
                close_stripped.rstrip("\n"),
            )
            if close_match:
                return "".join(code_lines).strip()
            code_lines.append(close_line)
            i += 1

        return "".join(code_lines).strip()

    return None


def _extract_target_file_from_bundle(content: str, file_path: str) -> str | None:
    """Extract the requested file when a model returns a multi-file bundle."""
    escaped_path = re.escape(file_path)
    heading_re = re.compile(
        rf"(?im)^\s*#{{1,6}}\s*(?:[^\n`]*?)`?{escaped_path}`?\s*$"
    )

    for match in heading_re.finditer(content):
        section = content[match.end():]
        block = _extract_first_fenced_block(section)
        if block is not None:
            return block

    return None

def _normalize_skill_path(path: str) -> str:
    return (path or "").replace("\\", "/").strip()


def _path_basename(path: str) -> str:
    normalized = _normalize_skill_path(path).rstrip("/")
    if not normalized:
        return ""
    return normalized.rsplit("/", 1)[-1]


def _has_file_extension(path: str) -> bool:
    """判断是否有扩展名，表示是具体文件"""
    name = _path_basename(path)
    if not name:
        return False
    if "." not in name:
        return False
    stem, ext = name.rsplit(".", 1)
    return bool(stem) and bool(ext)


def _is_directory_like_skill_path(path: str) -> bool:
    """判断路径是否目录（没有扩展名或以 / 结尾）"""
    normalized = _normalize_skill_path(path)
    if not normalized:
        return False
    if normalized.endswith("/"):
        return True
    # scripts/references/assets 下无扩展名视为目录
    if normalized.startswith(("scripts/", "references/", "assets/")):
        return not _has_file_extension(normalized)
    return False


def _is_materialized_skill_resource_path(path: str) -> bool:
    """最终需要存在的资源：只有具体文件"""
    normalized = _normalize_skill_path(path)
    if not normalized.startswith(("scripts/", "references/", "assets/")):
        return False
    return _has_file_extension(normalized)


_MULTI_FILE_MARKER_RE = re.compile(
    r"(?im)^\s*#{1,6}\s*(?:[^\n`]*)(?:SKILL\.md|scripts/|references/|assets/)"
)


_SCRIPT_FAKE_IMPLEMENTATION_RE = re.compile(
    r"placeholder|TODO|your_api_key|api\.example\.com|example\.com|模拟|占位|假装|"
    r"实际使用时|实际开发中|仅为演示|演示目的|空的占位图|纯色图片|ASCII插图|ascii_art|fake",
    re.IGNORECASE,
)
_SKILL_CUSTOM_RUNTIME_CONTRACT_RE = re.compile(r"(?im)^\s*#{1,6}\s*Runtime\s+Contract\s*$")
_HOST_MODEL_CAPABILITY_RE = re.compile(
    r"宿主.{0,12}模型|内置.{0,12}模型|配置.{0,12}模型|文本模型|图像模型|视觉模型|"
    r"多模态|大语言模型|LLM|AI生成|模型生成|调用模型|TEXT_MODEL|IMAGE_MODEL|VISION_MODEL",
    re.IGNORECASE,
)
_CONFIGURED_MODEL_CALL_RE = re.compile(
    r"LLM_BASE_URL|TEXT_MODEL|IMAGE_MODEL|VISION_MODEL|/v1/chat/completions|"
    r"chat/completions|complete_chat_once|stream_chat|openai|"
    r"generate_text_with_llm|generate_stable_diffusion_image|backend\.services\.skill_runtime",
    re.IGNORECASE,
)
_CREATOR_FLOW_LEAK_RE = re.compile(
    r"点击\s*(?:\*\*)?[‘'\"“”]?开始创建[’'\"“”]?(?:\*\*)?|开始生成文件|文件清单预览|确认无误后|"
    r"你也可以在创建后继续编辑内容|确认项列表|系统将自动创建|自动创建以下文件|"
    r"创建文件面板|文件创建面板|若当前无误|已预置|所有路径与命名与蓝图一致|"
    r"不包含任何隐藏逻辑或隐式执行|输出格式符合 Markdown 标准，支持宿主解析|"
    r"(?:先|首先)?输出(?:完整)?(?:架构)?蓝图|等待用户确认|用户确认后(?:再|开始)|蓝图确认后|"
    r"Creator\s*(?:创建|阶段|确认)|Phase\s*[123]\s*(?:创建|蓝图|确认)",
    re.IGNORECASE,
)
_SKILL_FILE_PATH_RE = re.compile(r"(?<![\w./-])((?:scripts|references|assets)/[A-Za-z0-9_./-]+|SKILL\.md)(?![\w./-])")
_KERNEL_RESOURCE_LEAK_RE = re.compile(
    r"(?<![\w./-])(kernel/references/[A-Za-z0-9_./-]+)(?![\w./-])",
    re.IGNORECASE,
)

_IMAGE_MODEL_USAGE_RE = re.compile(r"IMAGE_MODEL|IMAGE_BASE_URL|/v1/images/generations|images/generations|generate_stable_diffusion_image", re.IGNORECASE)
_DIRECT_IMAGE_API_RE = re.compile(r"IMAGE_BASE_URL|/v1/images/generations|images/generations", re.IGNORECASE)
_PLATFORM_IMAGE_HELPER_RE = re.compile(r"generate_stable_diffusion_image", re.IGNORECASE)
_IMAGE_URL_ONLY_RE = re.compile(r'\[0\]\s*\.get\(\s*[\'"]url[\'"]|\[\s*[\'"]url[\'"]\s*\]', re.IGNORECASE)
_DATA_URI_RE = re.compile(r"data:image/[^;]+;base64", re.IGNORECASE)
_REFERENCE_PLACEHOLDER_RE = re.compile(r"placeholder|TODO|待补充|将要生成|仅为示例|空壳|占位", re.IGNORECASE)


def _reject_custom_skill_md_protocol(content: str) -> None:
    """Reject non-standard runtime protocol sections in generated SKILL.md."""
    if _SKILL_CUSTOM_RUNTIME_CONTRACT_RE.search(content):
        raise ValueError(
            "SKILL.md 不应包含自定义 Runtime Contract JSON 协议；"
            "请使用普通 Markdown 说明和 ```bash 命令示例描述运行时动作。"
        )


def _extract_declared_skill_paths(text: str) -> list[str]:
    """Return normalized Skill package file paths mentioned by a blueprint."""
    seen: set[str] = set()
    paths: list[str] = []
    for raw in _SKILL_FILE_PATH_RE.findall(text or ""):
        path = raw.strip().rstrip("`，,。；;:)）]").replace("\\", "/")
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _paths_requiring_skill_md_mentions(blueprint_text: str, *, prefix: str) -> list[str]:
    return [path for path in _extract_declared_skill_paths(blueprint_text) if path.startswith(prefix)]


def _reject_creator_flow_leak(content: str) -> None:
    """Reject Creator UI/workflow text copied into generated SKILL.md."""
    if _CREATOR_FLOW_LEAK_RE.search(content):
        raise ValueError(
            "SKILL.md 包含 Creator 界面流程/确认清单文本（例如“点击开始创建”“确认项列表”“系统将自动创建”），"
            "这是平台创建流程泄露，不属于 Skill 使用说明。请删除这些流程文本，只保留 Skill 的使用说明、资源引用和可执行命令示例。"
        )


@dataclass(frozen=True)
class ContractCheckResult:
    id: str
    passed: bool
    target: str
    message: str
    expected: str
    minimal_edit: str
    matched_paths: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


class ContractValidationError(ValueError):
    """Validation error carrying structured contract check results."""

    def __init__(self, message: str, results: list[ContractCheckResult]):
        super().__init__(message)
        self.results = results


def _infer_script_input_keys_from_blueprint(script_path: str, blueprint_text: str) -> list[str]:
    """Infer stable JSON argv keys from the blueprint for a script path."""
    lowered = (blueprint_text or "").lower()
    key_candidates = ["topic", "prompt", "text", "keywords"]
    keys = [key for key in key_candidates if key in lowered]
    if not keys:
        keys = ["topic"]
    return keys[:3]


def _script_command_template(script_path: str, blueprint_text: str, entry: SkillPlanEntry | None = None) -> str:
    """Render the command template from SkillPlanEntry, the sole execution contract."""
    if entry is None:
        entry = _skill_plan_entry_for_file(file_path=script_path, blueprint_text=blueprint_text)
    return render_script_command_from_skill_plan(entry)



def _command_signature(command: str, script_path: str) -> dict[str, Any] | None:
    """Normalize a command block into runner/script/JSON-argv placeholder contract."""
    try:
        parts = shlex.split((command or "").strip())
    except ValueError:
        return None
    expected_script = script_path.replace("\\", "/")
    for idx, part in enumerate(parts):
        normalized_script = part.replace("\\", "/")
        if normalized_script == expected_script or normalized_script.endswith("/" + expected_script):
            if idx + 1 >= len(parts):
                return {
                    "runner": Path(parts[idx - 1]).name if idx > 0 else "",
                    "script_path": expected_script,
                    "keys": set(),
                    "placeholders": {},
                }
            try:
                payload = json.loads(parts[idx + 1])
            except json.JSONDecodeError:
                return None
            if not isinstance(payload, dict):
                return None
            placeholders: dict[str, str] = {}
            for key, value in payload.items():
                if isinstance(value, str):
                    match = re.fullmatch(r"\{\{\s*([A-Za-z_][\w-]*)\s*\}\}", value.strip())
                    placeholders[str(key)] = match.group(1) if match else value.strip()
                else:
                    placeholders[str(key)] = ""
            return {
                "runner": Path(parts[idx - 1]).name if idx > 0 else "",
                "script_path": expected_script,
                "keys": set(str(key) for key in payload.keys()),
                "placeholders": placeholders,
            }
    return None


def _command_template_equivalent(command: str, script_path: str, entry: SkillPlanEntry) -> bool:
    """Compare command blocks by normalized execution contract, not raw bytes."""
    command_sig = _command_signature(command, script_path)
    template_sig = _command_signature(_script_command_template(script_path, "", entry), script_path)
    if not command_sig or not template_sig:
        return False
    if command_sig["runner"] != template_sig["runner"]:
        return False
    if command_sig["script_path"] != template_sig["script_path"]:
        return False
    if command_sig["keys"] != template_sig["keys"]:
        return False
    return command_sig["placeholders"] == template_sig["placeholders"]



def _command_payload_object(command: str, script_path: str) -> dict[str, Any] | None:
    """Return the JSON argv object passed to script_path, or None if unparsable."""
    try:
        parts = shlex.split(command or "")
    except ValueError:
        return None
    expected = script_path.replace("\\", "/")
    for idx, part in enumerate(parts):
        normalized = part.replace("\\", "/")
        if normalized == expected or normalized.endswith("/" + expected):
            if idx + 1 >= len(parts):
                return {}
            try:
                payload = json.loads(parts[idx + 1])
            except json.JSONDecodeError:
                return None
            if not isinstance(payload, dict):
                return None
            return {str(key): value for key, value in payload.items()}
    return None

def _command_payload_keys(command: str, script_path: str) -> set[str] | None:
    """Return JSON argv keys passed to script_path, or None if unparsable/non-JSON."""
    payload = _command_payload_object(command, script_path)
    if payload is None:
        return None
    return set(payload.keys())


def _command_runtime_matches(command: str, script_path: str, entry: SkillPlanEntry) -> bool:
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    for idx, part in enumerate(parts):
        normalized = part.replace("\\", "/")
        if normalized == script_path or normalized.endswith("/" + script_path):
            runner = parts[idx - 1] if idx > 0 else ""
            if entry.runtime == "python":
                return Path(runner).name.startswith("python")
            if entry.runtime == "node":
                return Path(runner).name == "node"
            if entry.runtime == "bash":
                return Path(runner).name in {"bash", "sh"}
            if entry.runtime == "shell":
                return Path(runner).name in {"sh", "bash"}
            return True
    return False

def _check_command_block_contract(script_path: str, commands: list[str], entry: SkillPlanEntry) -> list[ContractCheckResult]:
    """Validate command blocks as workflow-local execution contracts.

    - Enforce parseable JSON argv and runtime consistency.
    - JSON argv field names remain skill-specific, but each command must exactly
      match that script's SkillPlan.inputs.
    """
    results: list[ContractCheckResult] = []

    for idx, command in enumerate(commands, start=1):
        command = command.strip()
        target = f"{script_path}#command-{idx}"

        command_sig = _command_signature(command, script_path)
        parsed_ok = command_sig is not None
        results.append(ContractCheckResult(
            id="command_block.signature.parseable",
            passed=parsed_ok,
            target=target,
            message="命令块可解析为 runner/script_path/JSON argv。" if parsed_ok else f"{script_path} 命令块无法解析为有效执行命令。",
            expected="命令块应形如：python scripts/name.py '{\"some_key\":\"{{some_key}}\"}'。",
            minimal_edit="保留脚本路径并确保 JSON argv 可解析；字段名可由 workflow 自由定义。",
        ))
        if not command_sig:
            continue

        runtime_matches = _command_runtime_matches(command, script_path, entry)
        results.append(ContractCheckResult(
            id="command_block.runtime.matches_skillplan",
            passed=runtime_matches,
            target=target,
            message="命令块 runner 与脚本 runtime 一致。" if runtime_matches else f"命令块 runner 与脚本 runtime={entry.runtime} 不一致。",
            expected="Python 用 python，Node 用 node，Bash/Shell 用 bash/sh；JSON keys 由当前脚本 SkillPlan.inputs 决定。",
            minimal_edit="修正 runner 或脚本路径；不要自行发明 JSON 参数名。",
        ))

        keys = _command_payload_keys(command, script_path)
        json_ok = keys is not None
        results.append(ContractCheckResult(
            id="command_block.json_argv.parseable",
            passed=json_ok,
            target=target,
            message="命令块使用可解析 JSON argv。" if json_ok else f"{script_path} 命令块必须在脚本路径后传入 JSON object argv。",
            expected="脚本路径后跟一个 JSON object argv；JSON keys 由当前脚本 SkillPlan.inputs 决定。",
            minimal_edit="确保 JSON 可解析；不要自行发明参数名。",
        ))

        declared_inputs = set(entry.inputs or [])
        if declared_inputs and keys is not None:
            missing = sorted(declared_inputs - keys)
            extra = sorted(keys - declared_inputs)
            results.append(ContractCheckResult(
                id="command_block.skillplan_inputs.exact",
                passed=not missing and not extra,
                target=target,
                message=(
                    "命令 JSON keys 与当前脚本 SkillPlan.inputs 一致。"
                    if not missing and not extra
                    else "命令 JSON keys 必须与当前脚本在 SkillPlan 中声明的 inputs 一致；平台不固定业务字段名。"
                ),
                expected="字段名由该 Skill 的 SkillPlan 决定；命令 JSON object keys 必须逐字等于 SkillPlan.inputs。",
                minimal_edit="只修改该命令 JSON object：删除 extra_keys，补齐 missing_keys，保留 runner 和 script path。",
                details={
                    "target_script": script_path,
                    "expected_keys": sorted(declared_inputs),
                    "actual_keys": sorted(keys),
                    "missing_keys": missing,
                    "extra_keys": extra,
                    "expected_payload_shape": {key: f"{{{{{key}}}}}" for key in sorted(declared_inputs)},
                    "upstream_available_outputs": [],
                },
            ))

    return results

def _build_skill_md_contract_text(blueprint_text: str) -> str:
    """Hard-format authoring contract for SKILL.md.

    Semantic coverage is checked by model review. This contract only defines
    document format and fenced block norms.
    """
    return "\n".join([
        "必须满足以下 SKILL.md 硬格式合同：",
        "",
        "A. YAML frontmatter:",
        "- 文件必须以 YAML frontmatter 开始。",
        "- frontmatter 必须包含 name 和 description。",
        "- frontmatter 在 metadata 后用 --- 关闭；不要要求文件末尾以 --- 结束。",
        "",
        "B. Markdown fenced block 规范:",
        "- 所有脚本执行命令必须使用标准 Markdown fenced code block。",
        "- shell 命令必须使用 ```bash 作为 info string，不要使用普通文本、行内代码或缩进代码块表达执行命令。",
        "- 机器可读 JSON 示例、配置、stdout 示例必须使用 ```json fenced code block。",
        "- 不要使用 '''bash 或 '''json；Markdown 标准 fence 使用三个反引号 ```。",
        "",
        "C. scripts 命令块:",
        "- 对蓝图真实规划的每个脚本，SKILL.md 应提供一个独立的 ```bash fenced code block。",
        "- 每个 ```bash block 内只放一条命令。",
        "- 命令必须直接调用真实 scripts/ 路径。",
        "- 脚本路径后应传入一个 json.loads 可解析的 JSON object argv。",
        "- 动态占位符必须作为 JSON 字符串值出现，例如 \"theme\":\"{{theme}}\"。",
        "- 禁止裸占位符，例如禁止 \"scene_count\":{{scene_count}}。",
        "",
        "D. workflow:",
        "- SKILL.md 必须描述完整 workflow、脚本顺序、输入输出数据流和最终产物。",
        "- 第一条命令可以使用用户输入 placeholder。",
        "- 后续命令 placeholder 必须来自前序脚本 stdout JSON 字段；如果无法找到某个 input 的来源，不要猜字段名，应报告 SkillPlan dataflow 不完整。",
        "- 多场景、多图片、多页 PDF 等循环必须在脚本内部完成；SKILL.md 只写一次脚本调用。",
        "",
        "E. references/assets:",
        "- references 应在资源/参考资料小节说明用途和按需读取时机。",
        "- reference 正文不要全文塞进 SKILL.md。",
        "- assets/** 只能作为上传素材/静态资源引用，不能描述为模型生成。",
        "",
        "F. 禁止项:",
        "- 不输出 placeholder/mock/fake API。",
        "- 不要泄露 Creator 内部流程或 kernel references。",
        "- 不要把蓝图说明文字中的示例/反例路径当成真实文件计划。",
    ])

def _build_skill_md_e2e_authoring_guide(blueprint_text: str) -> str:
    """Build deterministic authoring guidance for SKILL.md workflow commands.

    This is creator strategy, not artifact post-processing.  The generated
    SKILL.md must be born as an E2E-runnable linear workflow.
    """
    script_paths = _paths_requiring_skill_md_mentions(blueprint_text, prefix="scripts/")
    reference_paths = _paths_requiring_skill_md_mentions(blueprint_text, prefix="references/")

    if not script_paths:
        return (
            "E2E workflow authoring guide:\n"
            "- 当前蓝图没有 scripts/ 文件；SKILL.md 不要编造脚本命令块。\n"
            "- 若任务可直接回答，明确写“直接回答用户问题”，不要生成伪脚本流程。"
        )

    lines: list[str] = [
        "E2E workflow authoring guide（强制生成策略，必须反映到最终 SKILL.md 正文）:",
        "A. 命令块形态:",
        "- 每个 scripts/ 文件必须且只能有一个主要可执行 bash fenced code block。",
        "- 命令块使用标准 Markdown 独立 fence；不要把 ```bash 缩进在列表项内部。",
        "- fence 内只放一条命令，不写解释、不写循环说明、不写多条命令。",
        "- 命令必须形如：python scripts/name.py '{\"key\":\"{{key}}\"}'。",
        "- JSON argv keys 必须由当前 scripts/ 文件的 SkillPlanEntry.inputs 直接决定；不要自行发明、翻译或猜测参数名。",
        "- 字段名以当前 SkillPlan.inputs / outputs 为准；平台不固定业务字段名，但同一 Skill 内部必须自洽。",
        "- JSON argv 必须先能被 json.loads 解析；所有 {{placeholder}} 都必须放在 JSON 字符串里，禁止裸写 {{count}}、{{scene_count}}。",
        "- 若需要数值默认值，直接写固定 JSON 数字，例如 \"scene_count\":3，而不是 \"scene_count\":{{scene_count}}。",
        "",
        "B. 线性数据流:",
        "- E2E 只按 SKILL.md/reference 中的命令块顺序逐条执行；不会理解“每幕一次/循环三次/对每个场景调用”等自然语言循环。",
        "- 多场景、多图片、多页 PDF 等循环必须在对应脚本内部完成，SKILL.md 只写一次脚本调用。",
        "- 第一条命令可以使用用户输入 placeholder；后续命令只能使用之前脚本 stdout JSON 已输出的字段。",
        "- 禁止发明 image_path_1/image_path_2/image_path_3 等编号字段，除非上游脚本 stdout 明确输出这些字段。",
        "- 列表/对象字段必须用整值占位符传递，例如 \"image_paths\":\"{{image_paths}}\"，不要写 [\"{{image_path_1}}\", \"{{image_path_2}}\"]。",
        "- 每个脚本说明中必须写清 stdout JSON 输出字段，且下游命令 placeholder 必须与这些字段逐字一致。",
        "",
        "C. 推荐线性命令顺序与接口:",
    ]

    available_after_previous: set[str] = set()
    for idx, script_path in enumerate(script_paths, start=1):
        entry = _skill_plan_entry_for_file(file_path=script_path, blueprint_text=blueprint_text)
        command = _script_command_template(script_path, blueprint_text, entry)
        lines.extend([
            f"{idx}. {script_path}",
            f"   role: {entry.role}",
            f"   declared inputs: {', '.join(entry.inputs or ['payload'])}",
            f"   declared outputs: {', '.join(entry.outputs or ['text'])}",
            f"   required command block:",
            "```bash",
            command,
            "```",
        ])

        if idx == 1:
            lines.append("   placeholder rule: 第一条命令可以从用户输入提取这些 inputs。")
        else:
            available_text = ", ".join(sorted(available_after_previous)) or "（暂无上游字段）"
            lines.append(
                "   placeholder rule: 这一条命令只能使用前序 stdout 已产生字段；"
                f"当前前序可用字段包括：{available_text}。"
            )

        available_after_previous.update(entry.outputs or [])

    if reference_paths:
        lines.extend([
            "",
            "D. references:",
            "- SKILL.md 必须在参考资料/资源小节逐字引用以下本地 reference，并说明何时读取；reference 不替代主流程命令块。",
        ])
        for path in reference_paths:
            lines.append(f"- {path}")

    lines.extend([
        "",
        "E. 对多场景/多图片/PDF 任务的固定策略:",
        "- 文本脚本一次生成完整结构化文本，并输出 script_text 或 text。",
        "- 图片脚本一次接收完整 script_text/text，内部拆分场景并生成多张图，stdout 输出 image_paths/images。",
        "- PDF 脚本一次接收 script_text/text 和 image_paths，内部完成排版和文件生成，stdout 输出 pdf_path/file_paths。",
        "- SKILL.md 不写三条图片命令，不写 image_path_1/2/3，不写自然语言循环。",
    ])

    return "\n".join(lines)

def _declared_skill_paths_from_blueprint(blueprint_text: str) -> set[str]:
    """Extract all skill-local paths declared in the blueprint.

    This must represent the generation plan, not files already on disk.
    SKILL.md is generated before scripts/references, so scripts/references
    mentioned by SKILL.md are valid as long as they are declared here.
    """
    text = blueprint_text or ""
    paths: set[str] = set()

    # 1. Existing parser path extraction.
    for prefix in ("scripts/", "references/", "assets/"):
        paths.update(_paths_requiring_skill_md_mentions(text, prefix=prefix))

    # 2. Explicit SkillPlan lines:
    # - path: `scripts/generate_story.py`
    # - path: scripts/generate_story.py
    for match in re.finditer(
        r"(?im)^\s*[-*]?\s*path\s*:\s*`?([A-Za-z0-9_.\-/]+)`?\s*$",
        text,
    ):
        path = match.group(1).strip().strip("`")
        if path.startswith(("scripts/", "references/", "assets/")):
            paths.add(path)

    # 3. Inline local resource paths anywhere in blueprint.
    for match in re.finditer(
        r"(?<![A-Za-z0-9_./-])((?:scripts|references|assets)/[A-Za-z0-9_.\-/]+)",
        text,
    ):
        path = match.group(1).strip().rstrip("`，,。；;:)）]}")
        if path.startswith(("scripts/", "references/", "assets/")):
            paths.add(path)

    # 4. Directory tree fallback:
    # love-skill/
    # ├── scripts/
    # │   ├── generate_story.py
    # ├── references/
    # │   └── output-patterns.md
    # └── assets/
    #     └── placeholder-logo.png
    current_dir: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()

        dir_match = re.search(r"(scripts|references|assets)/\s*$", line)
        if dir_match:
            current_dir = dir_match.group(1)
            continue

        file_match = re.search(r"(?:├──|└──|[-*])\s*([A-Za-z0-9_.-]+\.[A-Za-z0-9]+)\s*$", line)
        if file_match and current_dir:
            filename = file_match.group(1).strip()
            paths.add(f"{current_dir}/{filename}")

    return {p.replace("\\", "/").strip("/") for p in paths if p}


def _skill_local_paths_in_markdown(content: str) -> set[str]:
    return {match.group(1).strip() for match in _SKILL_FILE_PATH_RE.finditer(content or "")}


def _kernel_resource_leak_paths(content: str) -> list[str]:
    """Return explicit kernel/references paths mentioned in final Skill text."""
    seen: set[str] = set()
    paths: list[str] = []
    for match in _KERNEL_RESOURCE_LEAK_RE.finditer(content or ""):
        path = match.group(1).rstrip("`，,。；;:)）]")
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _normalize_similarity_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _kernel_reference_content_copy_paths(content: str) -> list[str]:
    """Detect large verbatim copying from kernel references without banning same names."""
    import difflib

    candidate = _normalize_similarity_text(content)
    if len(candidate) < 500:
        return []
    matches: list[str] = []
    kernel_refs = settings.kernel_path / "references"
    if not kernel_refs.is_dir():
        return []
    for ref in sorted(kernel_refs.glob("*.md")):
        try:
            kernel_text = _normalize_similarity_text(ref.read_text(encoding="utf-8"))
        except OSError:
            continue
        if len(kernel_text) < 500:
            continue
        matcher = difflib.SequenceMatcher(None, candidate, kernel_text, autojunk=False)
        longest = max((block.size for block in matcher.get_matching_blocks()), default=0)
        # A contiguous 500+ char copy is almost certainly a leaked reference;
        # for shorter kernel refs, also catch near-whole-file copies.
        copied_ratio = longest / max(1, min(len(candidate), len(kernel_text)))
        if longest >= 500 or (longest >= 300 and copied_ratio >= 0.60):
            matches.append(f"kernel/references/{ref.name}")
    return matches


def _existing_skill_local_paths_for_skill(skill_name: str) -> set[str]:
    skill_dir = settings.skills_path / skill_name
    paths: set[str] = set()
    if not skill_dir.exists():
        return paths
    for folder in ("scripts", "references", "assets"):
        folder_path = skill_dir / folder
        if not folder_path.is_dir():
            continue
        for child in folder_path.rglob("*"):
            if child.is_file():
                paths.add(child.relative_to(skill_dir).as_posix())
    return paths

def _has_valid_skill_md_frontmatter(content: str) -> bool:
    """Validate SKILL.md YAML frontmatter.

    Correct rule:
    - SKILL.md starts with YAML frontmatter.
    - frontmatter contains name and description.
    - frontmatter is closed after metadata with ---.
    - The whole file does NOT need to end with ---.
    """
    text = (content or "").lstrip("\ufeff").lstrip()
    match = re.match(r"^---\s*\n([\s\S]*?)\n---\s*(?:\n|$)", text)
    if not match:
        return False

    raw_meta = match.group(1)
    try:
        meta = yaml.safe_load(raw_meta) or {}
    except Exception:
        return False

    if not isinstance(meta, dict):
        return False

    return bool(str(meta.get("name") or "").strip()) and bool(
        str(meta.get("description") or "").strip()
    )


def _check_skill_md_command_dataflow(content: str, blueprint_text: str) -> list[ContractCheckResult]:
    """Validate SKILL.md command placeholders against parsed SkillPlan dataflow."""
    try:
        parsed = parse_blueprint([{"role": "assistant", "content": blueprint_text}])
        entries = [entry for entry in (parsed.skill_plan.files if parsed.skill_plan else []) if entry.file_type == "script"]
    except Exception as exc:
        return [ContractCheckResult(
            id="skill_md.dataflow.plan_parseable",
            passed=False,
            target="SKILL.md",
            message=f"无法解析蓝图脚本数据流：{exc}",
            expected="蓝图应包含可解析的 SkillPlan / 文件职责计划。",
            minimal_edit="补充每个 scripts/* 的 role、inputs、outputs。",
        )]

    results: list[ContractCheckResult] = []
    produced: set[str] = set()
    consumed: set[str] = set()
    initial_user_inputs: set[str] = set(entries[0].inputs or []) if entries else set()
    available_values: set[str] = set(initial_user_inputs)

    for idx, entry in enumerate(entries, start=1):
        commands = _extract_script_command_templates(content, entry.path)
        if not commands:
            # A reference may intentionally hold command details; existing
            # command-existence checks decide that style, so dataflow skips it.
            continue
        placeholders = command_payload_placeholders(commands[0].strip().splitlines()[0], entry.path)
        if placeholders is None:
            results.append(ContractCheckResult(
                id="skill_md.dataflow.command_json_parseable",
                passed=False,
                target=entry.path,
                message=f"{entry.path} 命令无法解析 JSON argv，无法校验输入输出链路。",
                expected="命令必须向脚本传入 JSON object argv。",
                minimal_edit=f"改为 python {entry.path} '{{\"payload\":\"{{{{user_request}}}}\"}}' 形态。",
            ))
            continue
        for input_name in entry.inputs:
            placeholder = placeholders.get(input_name)
            passed = placeholder is not None
            if input_name in produced:
                passed = placeholder == input_name or placeholder in produced
            results.append(ContractCheckResult(
                id="skill_md.dataflow.input_available",
                passed=passed,
                target=f"{entry.path}:{input_name}",
                message=(
                    f"{entry.path} 输入 {input_name} 已通过 JSON argv 传递，且前序 stdout 依赖保持对齐。"
                    if passed
                    else f"{entry.path} 输入 {input_name} 未通过 JSON argv 或前序 stdout 字段正确传递。"
                ),
                expected="脚本 inputs 必须出现在命令 JSON argv 中；若 input 对应前序 stdout 字段，placeholder 必须引用该前序字段。",
                minimal_edit=(
                    f"为 {entry.path} 的 JSON argv 补齐 {input_name}；"
                    "如果该字段来自前序 stdout，请直接引用对应 stdout 字段 placeholder。"
                ),
                details={
                    "target_script": entry.path,
                    "input_name": input_name,
                    "placeholder": placeholder,
                    "upstream_available_outputs": sorted(produced),
                    "available_values": sorted(available_values),
                },
            ))
            if placeholder in produced:
                consumed.add(placeholder)

            unresolved_plan_input = idx > 1 and input_name not in available_values and (placeholder not in available_values if placeholder else True)
            if unresolved_plan_input:
                results.append(ContractCheckResult(
                    id="skill_plan.dataflow_unresolved",
                    passed=False,
                    target=f"{entry.path}:{input_name}",
                    message=f"{entry.path} 的 SkillPlan input {input_name} 无法从初始用户输入或前序 outputs 解析。",
                    expected="SkillPlan.inputs 必须来自 user_request/首步用户字段、references/assets 静态资源或前序 SkillPlan.outputs；后续 inputs/outputs 命名不能断链。",
                    minimal_edit="修 SkillPlan 或重新生成蓝图/文件计划；不要反复只修 SKILL.md 命令块。",
                    details={
                        "target_script": entry.path,
                        "input_name": input_name,
                        "available_values": sorted(available_values),
                        "upstream_available_outputs": sorted(produced),
                    },
                ))
        produced.update(entry.outputs or [])
        available_values.update(entry.outputs or [])

    final_outputs = set(entries[-1].outputs or []) if entries else set()
    for output in sorted(produced - consumed - final_outputs):
        results.append(ContractCheckResult(
            id="skill_md.dataflow.output_consumed_or_final",
            passed=False,
            target=output,
            message=f"脚本输出 {output} 既未被后续脚本引用，也不是最终输出。",
            expected="每个脚本 outputs 必须被后续命令引用，或属于最终结果 metadata。",
            minimal_edit="让后续脚本接收该 stdout 字段，或把它声明为最终输出。",
        ))

    return results


def _check_skill_md_contract(content: str, blueprint_text: str) -> list[ContractCheckResult]:
    """Hard format checks for SKILL.md.

    This function deliberately does NOT decide semantic blueprint coverage.
    Model review decides:
    - which scripts are real blueprint tasks
    - whether SKILL.md covers all planned tasks
    - whether a path is an example/anti-example or a true file

    Deterministic checks here only enforce:
    - YAML frontmatter
    - no Creator/runtime leakage
    - scripts mentioned in SKILL.md are represented with ```bash fenced blocks
    - command argv is parseable JSON object
    """
    stripped = content.strip()
    results: list[ContractCheckResult] = []

    has_frontmatter = _has_valid_skill_md_frontmatter(stripped)
    results.append(ContractCheckResult(
        id="skill_md.frontmatter",
        passed=has_frontmatter,
        target="SKILL.md",
        message=(
            "SKILL.md frontmatter 合格。"
            if has_frontmatter
            else "SKILL.md 必须以 YAML frontmatter 开始，并在 metadata 后用 --- 关闭，且包含 name 和 description。"
        ),
        expected="文件开头格式：--- / name: ... / description: ... / ---；不要求文件末尾以 --- 结束。",
        minimal_edit="只修正文件开头 YAML frontmatter；不要在文件末尾追加 ---。",
    ))

    has_runtime_contract = bool(_SKILL_CUSTOM_RUNTIME_CONTRACT_RE.search(content))
    results.append(ContractCheckResult(
        id="skill_md.forbidden_runtime_contract",
        passed=not has_runtime_contract,
        target="SKILL.md",
        message=(
            "未包含自定义 Runtime Contract JSON 协议。"
            if not has_runtime_contract
            else "SKILL.md 不应包含自定义 Runtime Contract JSON 协议；请使用普通 Markdown 说明和 fenced command block。"
        ),
        expected="不要包含 Runtime Contract JSON。",
        minimal_edit="删除 Runtime Contract JSON/协议小节，改为普通 Markdown 说明和 ```bash 命令块。",
    ))

    has_creator_flow = bool(_CREATOR_FLOW_LEAK_RE.search(content))
    results.append(ContractCheckResult(
        id="skill_md.forbidden_creator_flow",
        passed=not has_creator_flow,
        target="SKILL.md",
        message=(
            "未包含 Creator 界面流程文案。"
            if not has_creator_flow
            else "SKILL.md 包含 Creator 界面流程/确认清单文本，这属于平台创建流程泄露。"
        ),
        expected="不要包含 Creator 创建流程、确认清单、点击开始创建等平台流程文案。",
        minimal_edit="删除 Creator UI/确认清单/点击开始创建相关文案，只保留 Skill 使用说明。",
    ))

    kernel_leak_paths = _kernel_resource_leak_paths(content)
    results.append(ContractCheckResult(
        id="skill_md.resource.no_kernel_leak",
        passed=not kernel_leak_paths,
        target="SKILL.md",
        message=(
            "SKILL.md 未引用 Creator 内部 kernel resources。"
            if not kernel_leak_paths
            else "SKILL.md 显式引用了 Creator 内部 kernel resources：" + ", ".join(kernel_leak_paths)
        ),
        expected="最终业务 SKILL.md 只能引用业务 Skill 本地 resources；不得引用 kernel/references 等内部资源。",
        minimal_edit="删除 kernel/references/... 内部 Creator 资源引用。",
        matched_paths=kernel_leak_paths,
    ))

    kernel_copy_paths = _kernel_reference_content_copy_paths(content)
    results.append(ContractCheckResult(
        id="skill_md.resource.no_kernel_content_copy",
        passed=not kernel_copy_paths,
        target="SKILL.md",
        message=(
            "SKILL.md 未大段复制 Creator kernel reference 内容。"
            if not kernel_copy_paths
            else "SKILL.md 大段复制了 Creator kernel reference 内容：" + ", ".join(kernel_copy_paths)
        ),
        expected="最终业务 SKILL.md 不得大段复制 kernel/references 中的 Creator 内部说明。",
        minimal_edit="删除复制的 kernel 内部说明，改写为面向该业务 Skill 的使用说明。",
        matched_paths=kernel_copy_paths,
    ))

    results.extend(_check_skill_md_fenced_command_contracts(
        content=content,
        blueprint_text=blueprint_text,
        required_script_paths=None,
    ))
    results.extend(_check_skill_md_command_dataflow(content, blueprint_text))

    return results


def _format_contract_checks(results: list[ContractCheckResult], *, passed: bool) -> str:
    selected = [result for result in results if result.passed is passed]
    if not selected:
        return "- 无"
    lines: list[str] = []
    for result in selected:
        matched = f"\n  matched_paths: {', '.join(result.matched_paths)}" if result.matched_paths else ""
        details = (
            "\n  details: " + json.dumps(result.details, ensure_ascii=False, sort_keys=True)
            if result.details else ""
        )
        lines.append(
            f"- {result.id} target={result.target}: {result.message}\n"
            f"  expected: {result.expected}\n"
            f"  minimal_edit: {result.minimal_edit}"
            f"{matched}"
            f"{details}"
        )
    return "\n".join(lines)


def _format_contract_failures(results: list[ContractCheckResult]) -> str:
    failed = [result for result in results if not result.passed]
    if not failed:
        return ""
    return (
        "SKILL.md contract 未通过：\n"
        + _format_contract_checks(failed, passed=False)
    )

def _format_contract_failures_safe(results: list[ContractCheckResult]) -> str:
    """Format contract failures without letting formatter bugs crash repair flow."""
    try:
        return _format_contract_failures(results)
    except Exception as exc:
        lines = [f"合同失败格式化异常：{exc}"]
        for result in results or []:
            try:
                if getattr(result, "passed", False):
                    continue
                lines.append(
                    f"- {getattr(result, 'id', 'unknown')}: "
                    f"target={getattr(result, 'target', '')}; "
                    f"message={getattr(result, 'message', '')}; "
                    f"expected={getattr(result, 'expected', '')}; "
                    f"minimal_edit={getattr(result, 'minimal_edit', '')}"
                )
            except Exception as inner_exc:
                lines.append(f"- 无法格式化某个失败项：{inner_exc}")
        return "\n".join(lines)

def _validate_skill_md_contract(content: str, blueprint_text: str) -> None:
    """Validate generated SKILL.md against blueprint-declared resources."""
    results = _check_skill_md_contract(content, blueprint_text)
    failed = [result for result in results if not result.passed]
    if failed:
        raise ContractValidationError(_format_contract_failures(results), results)


def _json_loads_loose_object(text: str) -> dict[str, Any]:
    """Parse a JSON object from model output.

    Accepts raw JSON, ```json fenced JSON, or text containing one JSON object.
    """
    raw = (text or "").strip()
    if not raw:
        return {}

    parsed = _parse_validator_json_object(raw)
    if isinstance(parsed, dict):
        return parsed

    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I).strip()
    raw = re.sub(r"\s*```$", "", raw).strip()

    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(raw[start:end + 1])
            return obj if isinstance(obj, dict) else {}
        except json.JSONDecodeError:
            return {}

    return {}


def _collect_blueprint_skillplan_constraints(
    *,
    blueprint_text: str,
    skill_plan_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect hard constraints that SKILL.md must reflect.

    This intentionally combines:
    - declared file paths parsed from blueprint text
    - SKILL.md FileSpecOut / SkillPlanEntry passed by frontend
    """
    declared_paths = sorted(_extract_declared_skill_paths(blueprint_text))
    declared_scripts = sorted(p for p in declared_paths if p.startswith("scripts/"))
    declared_references = sorted(p for p in declared_paths if p.startswith("references/"))
    declared_assets = sorted(p for p in declared_paths if p.startswith("assets/"))

    entry = skill_plan_entry or {}

    def as_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, tuple):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, str):
            return [v.strip() for v in re.split(r"[,，、\n]+", value) if v.strip()]
        return [str(value).strip()] if str(value).strip() else []

    required_capabilities = as_list(entry.get("required_capabilities"))
    forbidden_capabilities = as_list(entry.get("forbidden_capabilities"))
    inputs = as_list(entry.get("inputs"))
    outputs = as_list(entry.get("outputs"))
    dependencies = as_list(entry.get("dependencies"))
    reference_files = as_list(entry.get("reference_files") or entry.get("references"))

    for p in reference_files:
        if p.startswith("references/") and p not in declared_references:
            declared_references.append(p)

    for p in dependencies:
        if p.startswith("scripts/") and p not in declared_scripts:
            declared_scripts.append(p)
        if p.startswith("references/") and p not in declared_references:
            declared_references.append(p)
        if p.startswith("assets/") and p not in declared_assets:
            declared_assets.append(p)

    return {
        "declared_paths": sorted(set(declared_paths)),
        "declared_scripts": sorted(set(declared_scripts)),
        "declared_references": sorted(set(declared_references)),
        "declared_assets": sorted(set(declared_assets)),
        "required_capabilities": required_capabilities,
        "forbidden_capabilities": forbidden_capabilities,
        "inputs": inputs,
        "outputs": outputs,
        "dependencies": dependencies,
        "reference_files": reference_files,
    }


def _deterministic_skill_md_blueprint_alignment_checks(
    *,
    content: str,
    blueprint_text: str,
    skill_plan_entry: dict[str, Any] | None = None,
) -> list[ContractCheckResult]:
    """Do not use regex to judge SKILL.md blueprint semantic alignment.

    SKILL.md 是否覆盖蓝图规划任务、是否误把示例路径当真实文件、
    capability 是否越界、workflow 是否完整，全部由模型审查。
    """
    return []


async def _review_skill_md_blueprint_intent_with_model(
    *,
    skill_name: str,
    content: str,
    blueprint_text: str,
    skill_plan_entry: dict[str, Any] | None,
    model: str | None,
) -> dict[str, Any]:
    """Model review for semantic blueprint alignment.

    The model decides semantic coverage and which scripts are real blueprint tasks.
    Deterministic code later validates fenced block format for those scripts.
    """
    constraints = _collect_blueprint_skillplan_constraints(
        blueprint_text=blueprint_text,
        skill_plan_entry=skill_plan_entry,
    )

    route = route_model(VALIDATOR_TASK, requested_model=model)
    _log_creator_model_usage(
        phase="skill_md.intent_review.schema.route",
        file_path="SKILL.md",
        route=route,
        model=model,
        skill_name=skill_name,
    )

    parser_paths = _extract_declared_skill_paths(blueprint_text)

    prompt = (
        "你是 superskills Creator 的 SKILL.md 蓝图一致性审查器。\n"
        "你的任务是判断 SKILL.md 是否完整覆盖蓝图规划任务，并返回严格 JSON object。\n\n"

        "核心原则：\n"
        "1. SKILL.md 必须覆盖蓝图真实规划的任务、workflow、脚本顺序、输入输出数据流、资源使用和最终产物。\n"
        "2. 真实文件计划通常来自目录结构、SkillPlan path、dependencies、references 字段。\n"
        "3. 如果蓝图在“禁止隐式执行/示例/反例/例如/比如”语境下提到某个 scripts/*.py、references/*.md 或 assets/*，该路径只是解释性示例，不是实际文件计划。\n"
        "4. 但是，如果某个路径出现在目录结构或 SkillPlan path 字段中，则必须视为真实文件，不能误杀。\n"
        "5. SKILL.md 生成阶段 scripts/references 可能尚未落盘；不要因为文件暂时不存在而判失败。\n"
        "6. 对每个真实规划脚本，SKILL.md 必须有标准 ```bash fenced code block。\n"
        "7. 命令块应传入 json.loads 可解析的 JSON object argv。\n"
        "8. 如果 SKILL.md 中给 stdout 示例、配置示例或机器可读数据，必须使用 ```json fenced code block。\n"
        "9. 不要要求文件末尾追加 ---；YAML frontmatter 只需要在文件开头关闭。\n"
        "10. assets/** 只能作为上传素材/静态资源引用，不能描述为模型生成。\n"
        "11. references 应被描述为按需读取，不能把 reference 正文全文塞进 SKILL.md。\n"
        "12. SKILL.md 必须面向 Skill 使用者，不要包含 Creator UI 创建流程。\n\n"

        "你必须从以下角度审查：\n"
        "- intent_reviewer: 是否覆盖业务意图、输入、输出、触发方式、最终产物。\n"
        "- file_plan_reviewer: 是否覆盖真实 scripts/references/assets；是否误用了示例/反例路径。\n"
        "- workflow_reviewer: 脚本顺序、命令块、参数传递、stdout 数据流是否能端到端执行。\n"
        "- capability_reviewer: required_capabilities 是否体现，forbidden_capabilities 是否被引入。\n"
        "- resource_reviewer: references/assets 的使用方式是否正确。\n"
        "- user_facing_reviewer: 是否是最终 Skill 使用说明，而不是 Creator 创建流程。\n\n"

        "只返回 JSON object，不要 Markdown，不要解释。格式必须是：\n"
        "{\n"
        '  "passed": true,\n'
        '  "required_script_paths": ["scripts/example.py"],\n'
        '  "required_reference_paths": ["references/example.md"],\n'
        '  "required_asset_paths": ["assets/example.png"],\n'
        '  "reviewers": {\n'
        '    "intent_reviewer": {"passed": true, "issues": []},\n'
        '    "file_plan_reviewer": {"passed": true, "issues": []},\n'
        '    "workflow_reviewer": {"passed": true, "issues": []},\n'
        '    "capability_reviewer": {"passed": true, "issues": []},\n'
        '    "resource_reviewer": {"passed": true, "issues": []},\n'
        '    "user_facing_reviewer": {"passed": true, "issues": []}\n'
        "  },\n"
        '  "issues": [\n'
        '    {"severity":"error|warning","field":"intent|file_plan|workflow|capabilities|resources|user_facing","message":"...","expected":"...","minimal_edit":"..."}\n'
        "  ],\n"
        '  "repair_suggestions": "给修复模型的最小编辑建议"\n'
        "}\n\n"

        f"Skill 名称：{skill_name}\n\n"

        "【蓝图约束 JSON，供参考；如和蓝图原文语境冲突，以蓝图原文语境为准】\n"
        f"{json.dumps(constraints, ensure_ascii=False, indent=2)}\n\n"

        "【解析器提取路径，供参考；不是最终裁决】\n"
        f"{json.dumps(parser_paths, ensure_ascii=False, indent=2)}\n\n"

        "【蓝图原文】\n"
        f"{blueprint_text[-16000:]}\n\n"

        "【待审查 SKILL.md】\n"
        f"{content[-20000:]}\n"
    )

    raw = await complete_chat_once(
        [
            {
                "role": "system",
                "content": (
                    "你是严格 JSON 输出的 SKILL.md 蓝图一致性审查器。"
                    "只输出 JSON object，不要输出 Markdown。"
                ),
            },
            {"role": "user", "content": prompt},
        ],
        route.model,
    )

    data = _json_loads_loose_object(raw)
    if not isinstance(data, dict) or not data:
        return {
            "passed": False,
            "required_script_paths": [],
            "required_reference_paths": [],
            "required_asset_paths": [],
            "reviewers": {},
            "issues": [{
                "severity": "error",
                "field": "validator",
                "message": "蓝图意图审查模型未返回有效 JSON。",
                "expected": "返回 passed/required_script_paths/reviewers/issues/repair_suggestions JSON object。",
                "minimal_edit": "重新审查 SKILL.md，并输出结构化 JSON。",
            }],
            "repair_suggestions": "审查模型输出无效；请按蓝图真实文件计划和业务意图最小修复 SKILL.md。",
        }

    data.setdefault("passed", False)
    data.setdefault("required_script_paths", [])
    data.setdefault("required_reference_paths", [])
    data.setdefault("required_asset_paths", [])
    data.setdefault("reviewers", {})
    data.setdefault("issues", [])
    data.setdefault("repair_suggestions", "")

    if not isinstance(data["required_script_paths"], list):
        data["required_script_paths"] = []
    if not isinstance(data["required_reference_paths"], list):
        data["required_reference_paths"] = []
    if not isinstance(data["required_asset_paths"], list):
        data["required_asset_paths"] = []
    if not isinstance(data["reviewers"], dict):
        data["reviewers"] = {}
    if not isinstance(data["issues"], list):
        data["issues"] = []

    reviewer_failures: list[dict[str, Any]] = []
    for reviewer_name, reviewer_result in data["reviewers"].items():
        if not isinstance(reviewer_result, dict):
            continue

        if reviewer_result.get("passed") is False:
            for issue in reviewer_result.get("issues") or []:
                if isinstance(issue, dict):
                    reviewer_failures.append({
                        "severity": issue.get("severity", "error"),
                        "field": issue.get("field", reviewer_name),
                        "message": issue.get("message", f"{reviewer_name} 审查未通过"),
                        "expected": issue.get("expected", "该角度审查通过"),
                        "minimal_edit": issue.get("minimal_edit", "按该角度问题最小修复"),
                    })

    if reviewer_failures:
        data["passed"] = False
        data["issues"].extend(reviewer_failures)

    data["required_script_paths"] = [
        str(path).replace("\\", "/").strip()
        for path in data["required_script_paths"]
        if str(path).replace("\\", "/").strip().startswith("scripts/")
    ]

    data["required_reference_paths"] = [
        str(path).replace("\\", "/").strip()
        for path in data["required_reference_paths"]
        if str(path).replace("\\", "/").strip().startswith("references/")
    ]

    data["required_asset_paths"] = [
        str(path).replace("\\", "/").strip()
        for path in data["required_asset_paths"]
        if str(path).replace("\\", "/").strip().startswith("assets/")
    ]

    return data


def _format_skill_md_intent_review_failure(review: dict[str, Any]) -> str:
    """Format model review failure for UI/repair prompt.

    Must never crash. If this crashes, auto-repair flow may break.
    """
    try:
        if not isinstance(review, dict):
            return (
                "SKILL.md 与蓝图意图不一致：审查结果不是 JSON object。\n"
                f"actual_type: {type(review).__name__}"
            )

        issues = review.get("issues")
        if not isinstance(issues, list):
            issues = []

        lines = ["SKILL.md 与蓝图意图不一致："]

        if not issues:
            reviewers = review.get("reviewers")
            if isinstance(reviewers, dict):
                for reviewer_name, reviewer_result in reviewers.items():
                    if not isinstance(reviewer_result, dict):
                        continue
                    reviewer_issues = reviewer_result.get("issues")
                    if isinstance(reviewer_issues, list):
                        for issue in reviewer_issues:
                            issues.append(issue)

        if not issues:
            lines.append("模型审查未通过，但未返回具体 issues。")
            lines.append("请检查蓝图真实文件计划、workflow、资源说明和命令块。")
        else:
            for idx, issue in enumerate(issues, start=1):
                if isinstance(issue, dict):
                    severity = issue.get("severity", "error")
                    field = issue.get("field", "unknown")
                    message = issue.get("message", "")
                    expected = issue.get("expected", "")
                    minimal_edit = issue.get("minimal_edit", "")

                    lines.append(f"{idx}. [{severity}] {field}: {message}")
                    if expected:
                        lines.append(f"   expected: {expected}")
                    if minimal_edit:
                        lines.append(f"   minimal_edit: {minimal_edit}")
                else:
                    lines.append(f"{idx}. {issue}")

        repair = str(review.get("repair_suggestions") or "").strip()
        if repair:
            lines.append("给修复模型的建议：")
            lines.append(repair)

        return "\n".join(lines)

    except Exception as exc:
        logger.exception("[Creator][skill_md] failed to format intent review failure")
        return (
            "SKILL.md 与蓝图意图不一致，但格式化失败信息时发生异常。\n"
            f"格式化异常：{exc}\n"
            f"原始审查结果：{review}"
        )


async def _validate_skill_md_blueprint_alignment(
    *,
    skill_name: str,
    content: str,
    blueprint_text: str,
    skill_plan_entry: dict[str, Any] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Validate SKILL.md against blueprint intent.

    设计目标：
    - 校验不通过时，抛出可被外层自动修复循环捕获的异常；
    - 校验器自身异常时，也包装成普通 ValueError，避免直接崩溃；
    - 模型审查通过后，仍然执行 fenced command 后置校验；
    - 后置校验失败也进入自动修复，而不是静默放过。
    """

    # 1. 基础硬格式 / 资源边界校验
    try:
        hard_results = _check_skill_md_contract(content, blueprint_text)
    except Exception as exc:
        logger.exception(
            "[Creator][skill_md] hard contract validator crashed skill=%s",
            skill_name,
        )
        raise ValueError(
            "SKILL.md 基础合同校验器内部异常，已转为可修复错误。\n"
            f"错误：{exc}\n"
            "请检查 frontmatter、Creator 流程泄露、Runtime Contract 泄露、"
            "scripts 命令块格式等基础结构。"
        ) from exc

    hard_failed = [result for result in hard_results if not result.passed]
    if hard_failed:
        message = (
            "SKILL.md 基础格式/资源合同校验未通过。\n"
            + _format_contract_failures_safe(hard_results)
        )
        logger.info(
            "[Creator][skill_md] hard contract failed skill=%s failures=\n%s",
            skill_name,
            message,
        )
        raise ContractValidationError(message, hard_results)

    # 2. 模型蓝图语义审查
    try:
        review = await _review_skill_md_blueprint_intent_with_model(
            skill_name=skill_name,
            content=content,
            blueprint_text=blueprint_text,
            skill_plan_entry=skill_plan_entry,
            model=model,
        )
    except Exception as exc:
        logger.exception(
            "[Creator][skill_md] model blueprint intent review crashed skill=%s",
            skill_name,
        )
        raise ValueError(
            "SKILL.md 蓝图意图审查调用异常，已转为可修复错误。\n"
            f"错误：{exc}\n"
            "请检查审查模型返回 JSON、蓝图文件计划、SKILL.md workflow 和资源说明。"
        ) from exc

    if not isinstance(review, dict):
        raise ValueError(
            "SKILL.md 蓝图意图审查返回类型错误。\n"
            f"expected: dict JSON object\n"
            f"actual: {type(review).__name__}\n"
            "请重新审查 SKILL.md，并返回 passed/issues/required_script_paths 等字段。"
        )

    if review.get("passed") is not True:
        try:
            message = _format_skill_md_intent_review_failure(review)
        except Exception as exc:
            logger.exception(
                "[Creator][skill_md] intent review failure formatter crashed skill=%s",
                skill_name,
            )
            message = (
                "SKILL.md 与蓝图意图不一致，但格式化审查失败信息时发生异常。\n"
                f"格式化错误：{exc}\n"
                f"原始审查结果：{json.dumps(review, ensure_ascii=False, indent=2)}"
            )

        logger.info(
            "[Creator][skill_md] model blueprint intent review failed skill=%s message=\n%s",
            skill_name,
            message,
        )
        raise ValueError(message)

    # 3. fenced command 后置校验
    required_script_paths = [
        path
        for path in review.get("required_script_paths", [])
        if isinstance(path, str) and path.startswith("scripts/")
    ]

    try:
        fenced_results = _check_skill_md_fenced_command_contracts(
            content=content,
            blueprint_text=blueprint_text,
            required_script_paths=required_script_paths,
        )
    except Exception as exc:
        logger.exception(
            "[Creator][skill_md] fenced command validator crashed skill=%s",
            skill_name,
        )
        raise ValueError(
            "SKILL.md 命令块校验器内部异常，已转为可修复错误。\n"
            f"错误：{exc}\n"
            "请检查 SKILL.md 中真实脚本是否使用标准 ```bash fenced code block，"
            "且脚本参数是否为 json.loads 可解析的 JSON object。"
        ) from exc

    fenced_failed = [result for result in fenced_results if not result.passed]
    if fenced_failed:
        message = (
            "SKILL.md 命令块格式校验未通过。\n"
            "蓝图意图模型审查已通过，但真实脚本命令块仍不满足后台可解析规范。\n"
            "请只修复以下命令块问题，不要新增蓝图外脚本。\n"
            + _format_contract_failures_safe(fenced_results)
        )
        logger.info(
            "[Creator][skill_md] fenced command contract failed skill=%s failures=\n%s",
            skill_name,
            message,
        )
        raise ContractValidationError(message, fenced_results)

    review["passed"] = True
    review["fenced_check_passed"] = True
    review["fenced_check_failed"] = []

    logger.info(
        "[Creator][skill_md] blueprint alignment passed skill=%s required_scripts=%s",
        skill_name,
        required_script_paths,
    )

    return review


def _strip_orphan_trailing_fence(content: str) -> str:
    """Remove isolated Markdown fence markers at file boundaries.

    This is intentionally narrower than generic fence stripping: it deletes only
    standalone trailing ```/~~~ lines left by model output, plus an optional
    standalone opening fence when no matching closing fence remains.
    """
    lines = content.strip().splitlines()
    changed = False
    while lines and re.fullmatch(r"\s*(`{3,}|~{3,})\s*", lines[-1]):
        lines.pop()
        changed = True
    if lines and re.fullmatch(r"\s*(`{3,}|~{3,})[A-Za-z0-9_-]*\s*", lines[0]):
        body = "\n".join(lines[1:])
        if "```" not in body and "~~~" not in body:
            lines = lines[1:]
            changed = True
    return ("\n".join(lines).strip() if changed else content.strip())


def _build_script_file_contract_text(
    file_path: str,
    blueprint_text: str,
    *,
    purpose: str = "",
    role: str | None = None,
    skill_plan_entry: dict[str, Any] | None = None,
) -> str:
    entry = _skill_plan_entry_for_file(file_path=file_path, blueprint_text=blueprint_text, role=role, skill_plan_entry=skill_plan_entry)
    recommended_command = _script_command_template(file_path, blueprint_text, entry)

    lines = [
        f"脚本合同：{file_path}",
        f"Role: {entry.role}",
        f"file_type: {entry.file_type}",
        f"runtime: {entry.runtime}",
        f"entrypoint: {entry.entrypoint or file_path}",
        f"recommended_command_template: {recommended_command}",
        f"suggested_inputs: {', '.join(entry.inputs or ['payload'])}",
        f"declared_outputs: {', '.join(entry.outputs)}",
        "A. 输出形态:",
        "- 单文件源码，Python 脚本必须通过 ast.parse。",
        "B. 参数接口:",
        "- 默认 JSON argv，字段名由 workflow 决定，不强制 SkillPlan inputs。",
        "- 必须读取 SKILL.md/reference 命令块传入的 keys。",
        "- 内部 workflow 字段名可用 payload/context 或上游 stdout 字段。",
        "C. 角色输出合同:",
    ]

    if entry.role == "text_generator":
        lines.append("- stdout JSON 至少有一个非空字段；字段名由 workflow 决定；必须调用 text_generation helper。")
    elif entry.role == "image_generator":
        lines.append("- stdout JSON 至少有一个非空字段；必须调用 generate_stable_diffusion_image helper；字段名由 workflow 决定。")
    elif entry.role == "pdf_builder":
        lines.append(
            "- stdout JSON 必须返回真实存在的 PDF 文件路径；禁止调用图片 helper；字段名由 workflow 决定。"
            "本系统面向中文/UTF-8 场景，PDF builder 必须支持中文正文。"
            "推荐 reportlab + UnicodeCIDFont('STSong-Light')，或 reportlab + TTFont，或 fpdf2 + add_font 加载 TTF/OTF。"
            "禁止 FPDF 默认 Helvetica/Arial/Times/Courier 直接写 payload 文本；"
            "禁止只写 canvas.setFont('STSong-Light') 但不先 registerFont(UnicodeCIDFont('STSong-Light'))；"
            "禁止 raw %PDF 字符串拼接、空 PDF、假路径。"
        )
    else:
        lines.append("- stdout JSON 至少有一个非空字段；字段名由 workflow 决定。")

    lines.append("D. 能力边界:")
    lines.append("- required_capabilities 必须真实调用，forbidden_capabilities 禁止调用。")
    lines.append("- 内部 workflow 字段名不强制，但 SKILL.md/reference 与脚本读取必须自洽。")
    lines.append("E. 禁止项:")
    lines.append("- 不输出 placeholder/mock/fake API；不要通过 print {'error':...}、{}、空路径等绕过校验。")

    return "\n".join(lines)

_REFERENCE_FRONTMATTER_RE = re.compile(r"^---\s*\n([\s\S]*?)\n---\s*\n?", re.M)


def _slug_from_reference_path(file_path: str) -> str:
    stem = Path(file_path).stem.strip().lower()
    slug = re.sub(r"[^a-z0-9\u4e00-\u9fff-]+", "-", stem).strip("-")
    return slug or "reference"


def _reference_frontmatter_metadata(content: str) -> tuple[dict[str, Any], str]:
    """Return YAML frontmatter metadata and body."""
    text = content or ""
    match = _REFERENCE_FRONTMATTER_RE.match(text.strip())
    if not match:
        return {}, text.strip()

    raw_meta = match.group(1).strip()
    body = text.strip()[match.end():].strip()
    try:
        meta = yaml.safe_load(raw_meta) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}

    return meta, body


def _reference_metadata_defaults(
    *,
    file_path: str,
    purpose: str = "",
    skill_plan_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry = skill_plan_entry or {}
    title = _slug_from_reference_path(file_path)

    def as_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, tuple):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, str):
            return [v.strip() for v in re.split(r"[,，、\n]+", value) if v.strip()]
        return [str(value).strip()] if str(value).strip() else []

    return {
        "name": title,
        "description": (purpose or entry.get("purpose") or f"{file_path} reference").strip(),
        "role": "reference",
        "type": "reference",
        "path": file_path,
        "scope": "skill-local",
        "loading": "metadata-first-body-on-demand",
        "when_to_use": (purpose or entry.get("purpose") or "按 SKILL.md 工作流需要读取正文").strip(),
        "inputs": as_list(entry.get("inputs")),
        "outputs": as_list(entry.get("outputs")),
        "dependencies": as_list(entry.get("dependencies")),
        "required_capabilities": as_list(entry.get("required_capabilities")),
        "forbidden_capabilities": as_list(entry.get("forbidden_capabilities")),
        "tags": ["creator-generated", "reference"],
    }


def _ensure_reference_metadata_frontmatter(
    *,
    file_path: str,
    content: str,
    purpose: str = "",
    skill_plan_entry: dict[str, Any] | None = None,
) -> str:
    """Ensure references/*.md has YAML frontmatter metadata.

    This supports metadata-first loading: loader can inspect frontmatter before
    reading the full body.
    """
    if not file_path.startswith("references/") or Path(file_path).suffix.lower() != ".md":
        return content

    defaults = _reference_metadata_defaults(
        file_path=file_path,
        purpose=purpose,
        skill_plan_entry=skill_plan_entry,
    )
    meta, body = _reference_frontmatter_metadata(content)

    merged = dict(defaults)
    for key, value in meta.items():
        if value not in (None, "", [], {}):
            merged[key] = value

    # Normalize required metadata fields. Do not allow wrong path/role/type.
    merged["role"] = "reference"
    merged["type"] = "reference"
    merged["path"] = file_path
    merged["scope"] = "skill-local"
    merged["loading"] = "metadata-first-body-on-demand"

    body = body.strip()
    if not body:
        body = "# 参考资料\n\n## 规范\n\n请根据蓝图补充任务规范。\n"

    yaml_text = yaml.safe_dump(
        merged,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()

    return f"---\n{yaml_text}\n---\n\n{body}\n"


def _reference_metadata_contract_checks(
    *,
    file_path: str,
    content: str,
    purpose: str = "",
) -> list[ContractCheckResult]:
    meta, body = _reference_frontmatter_metadata(content)

    required_keys = [
        "name",
        "description",
        "role",
        "type",
        "path",
        "scope",
        "loading",
        "when_to_use",
    ]

    missing = [
        key for key in required_keys
        if key not in meta or meta.get(key) in (None, "", [], {})
    ]

    results: list[ContractCheckResult] = [
        ContractCheckResult(
            id="reference.metadata.frontmatter_exists",
            passed=bool(meta),
            target=file_path,
            message=(
                "reference 包含 YAML frontmatter metadata。"
                if meta
                else f"{file_path} 缺少 YAML frontmatter metadata。"
            ),
            expected="reference 文件必须以 YAML frontmatter 开始：--- / name, description, role, type, path, scope, loading, when_to_use / ---。",
            minimal_edit="在文件开头补充 YAML frontmatter metadata。",
        ),
        ContractCheckResult(
            id="reference.metadata.required_keys",
            passed=not missing,
            target=file_path,
            message=(
                "reference metadata 必需字段齐全。"
                if not missing
                else f"{file_path} metadata 缺少字段：{', '.join(missing)}。"
            ),
            expected="metadata 至少包含 name/description/role/type/path/scope/loading/when_to_use。",
            minimal_edit="补齐缺失 metadata 字段，正文保持不变。",
        ),
        ContractCheckResult(
            id="reference.metadata.path_matches",
            passed=meta.get("path") == file_path,
            target=file_path,
            message=(
                "reference metadata.path 与文件路径一致。"
                if meta.get("path") == file_path
                else f"reference metadata.path={meta.get('path')!r} 与文件路径 {file_path!r} 不一致。"
            ),
            expected=f"metadata.path 必须等于 {file_path}",
            minimal_edit=f"把 metadata.path 改为 {file_path}",
        ),
        ContractCheckResult(
            id="reference.metadata.role_type",
            passed=meta.get("role") == "reference" and meta.get("type") == "reference",
            target=file_path,
            message=(
                "reference metadata role/type 合法。"
                if meta.get("role") == "reference" and meta.get("type") == "reference"
                else "reference metadata.role/type 必须都是 reference。"
            ),
            expected="role: reference 且 type: reference。",
            minimal_edit="把 role/type 改为 reference。",
        ),
        ContractCheckResult(
            id="reference.metadata.loading_strategy",
            passed=meta.get("loading") == "metadata-first-body-on-demand",
            target=file_path,
            message=(
                "reference metadata 声明 metadata-first 按需正文加载。"
                if meta.get("loading") == "metadata-first-body-on-demand"
                else "reference metadata.loading 缺少按需加载策略。"
            ),
            expected="loading: metadata-first-body-on-demand。",
            minimal_edit="补充 loading: metadata-first-body-on-demand。",
        ),
        ContractCheckResult(
            id="reference.metadata.body_exists",
            passed=bool(body.strip()),
            target=file_path,
            message=(
                "reference metadata 后存在正文。"
                if body.strip()
                else "reference 只有 metadata，没有正文。"
            ),
            expected="frontmatter 后必须有 Markdown 正文。",
            minimal_edit="在 frontmatter 后补充 reference 正文。",
        ),
    ]

    return results

def _build_reference_file_contract_text(file_path: str, purpose: str, blueprint_text: str) -> str:
    script_paths = _paths_requiring_skill_md_mentions(blueprint_text, prefix="scripts/")
    script_lines: list[str] = []

    for script_path in script_paths:
        entry = _skill_plan_entry_for_file(file_path=script_path, blueprint_text=blueprint_text)
        if file_path in entry.reference_files or not entry.reference_files:
            script_lines.extend([
                f"- 本 reference 默认只写规范、风格、正例、反例和质量标准；不要重新定义 {script_path} 的 role/inputs/outputs/capabilities/command_template。",
                f"- 如果 SKILL.md 已包含 {script_path} 的可执行命令块，本 reference 不要再写命令块。",
                f"- 若 reference 必须提供命令块，只能复用 SkillPlan.command_template 的等价执行合同：```bash\n{_script_command_template(script_path, blueprint_text, entry)}\n```",
                f"- 禁止把上述正确命令、JSON keys（{', '.join(entry.inputs or ['payload'])}）或 role={entry.role} 写成反例。",
            ])

    if not script_lines:
        script_lines.append("- 本 reference 对应一个独立子任务/模块；必须写清 inputs、outputs、执行步骤、约束和示例。")

    metadata_example = yaml.safe_dump(
        {
            "name": _slug_from_reference_path(file_path),
            "description": purpose or f"{file_path} reference",
            "role": "reference",
            "type": "reference",
            "path": file_path,
            "scope": "skill-local",
            "loading": "metadata-first-body-on-demand",
            "when_to_use": purpose or "按 SKILL.md 工作流需要读取正文",
            "inputs": [],
            "outputs": [],
            "dependencies": [],
            "required_capabilities": [],
            "forbidden_capabilities": [],
            "tags": ["creator-generated", "reference"],
        },
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()

    return "\n".join([
        f"必须满足以下参考资料文件合同：{file_path}",
        "A. 输出形态:",
        "- 只输出该 reference 的 Markdown 文档内容，不要写入文件标签、Creator 流程说明或多文件包。",
        "- 文件必须以 YAML frontmatter metadata 开始，metadata 后才是正文。",
        "- metadata 用于运行时 metadata-first 加载；正文只在需要时按需读取。",
        "- metadata 必须至少包含：name、description、role、type、path、scope、loading、when_to_use。",
        "- metadata.role 必须是 reference；metadata.type 必须是 reference；metadata.path 必须等于当前文件路径。",
        "- metadata.loading 必须是 metadata-first-body-on-demand。",
        "metadata 示例:",
        "---",
        metadata_example,
        "---",
        "",
        "B. 内容职责:",
        f"- 职责说明：{purpose or '根据蓝图提供可操作参考资料'}",
        "- 每个 reference 只对应一个子任务/模块，不要把整个 Skill 包打包到一个 reference。",
        "- 默认只写写作规范、风格要求、正例、反例、质量标准；如果 SKILL.md 已有可执行命令块，reference 不要重复写命令块。",
        "- 不要重新定义 role / inputs / outputs / capabilities / command_template。",
        *script_lines,
        "- 内容必须是有实际指导价值的参考资料，不是对‘将要生成参考资料’的再描述。",
        "- 必须包含任务规范/步骤、示例、反例、约束/禁止项等章节，且正文长度足以指导子任务。",
        "C. 禁止项:",
        "- 不要包含 Creator 创建流程、确认清单、点击开始创建等平台流程文案。",
        "- 不要包含其它 SKILL.md/scripts/assets/references 文件的打包内容。",
        "- 不要包含 placeholder/TODO/待补充等占位文本。",
    ])


def _build_asset_file_contract_text(file_path: str, purpose: str) -> str:
    return "\n".join([
        f"必须满足以下 asset 文件合同：{file_path}",
        "A. 输出形态:",
        "- 只输出当前 asset 文件内容，不要写入文件标签、说明文字或多文件包。",
        "- 文件必须非空；JSON 资源必须可被 json.loads 解析。",
        "B. 内容职责:",
        f"- 职责说明：{purpose or '根据蓝图提供模板或静态资源'}",
        "C. 禁止项:",
        "- asset 是模板或静态资源，不得包含运行时代码、图片生成调用或 Creator 创建流程文案。",
    ])


def _build_generated_file_contract_text(
    file_path: str,
    blueprint_text: str,
    purpose: str = "",
    *,
    role: str | None = None,
    skill_plan_entry: dict[str, Any] | None = None,
) -> str:
    if file_path == "SKILL.md":
        return _build_skill_md_contract_text(blueprint_text)
    if file_path.startswith("scripts/"):
        return _build_script_file_contract_text(file_path, blueprint_text, purpose=purpose, role=role, skill_plan_entry=skill_plan_entry)
    if file_path.startswith("references/"):
        return _build_reference_file_contract_text(file_path, purpose, blueprint_text)
    if file_path.startswith("assets/"):
        return _build_asset_file_contract_text(file_path, purpose)
    return ""


def _check_reference_file_contract(file_path: str, content: str, purpose: str = "") -> list[ContractCheckResult]:
    """Validate reference markdown as documentation/resource only.

    Reference design:
    - references/*.md can provide rules, examples, anti-examples and quality checks.
    - references/*.md must not define executable workflow steps.
    - references/*.md must not redefine scripts/*.py SkillPlan capabilities.
    - E2E never executes reference command examples.
    """
    content = _ensure_reference_metadata_frontmatter(
        file_path=file_path,
        content=content,
        purpose=purpose,
        skill_plan_entry=None,
    )
    meta, body = _reference_frontmatter_metadata(content)
    stripped = body.strip()

    results: list[ContractCheckResult] = []

    results.extend(_reference_metadata_contract_checks(
        file_path=file_path,
        content=content,
        purpose=purpose,
    ))

    results.extend([
        ContractCheckResult(
            id="reference.not_empty",
            passed=bool(stripped),
            target=file_path,
            message=("参考资料正文非空。" if stripped else f"{file_path} 参考资料正文为空。"),
            expected="frontmatter 后必须输出该 reference 的 Markdown 正文。",
            minimal_edit="补充有实际指导价值的 Markdown 参考资料正文。",
        ),
        ContractCheckResult(
            id="reference.no_creator_flow",
            passed=not bool(_CREATOR_FLOW_LEAK_RE.search(content)),
            target=file_path,
            message=(
                "未包含 Creator 创建流程文案。"
                if not _CREATOR_FLOW_LEAK_RE.search(content)
                else f"{file_path} 包含 Creator 创建流程/确认清单/点击开始创建等平台流程文案。"
            ),
            expected="不要包含 Creator 创建流程、确认清单、点击开始创建等平台流程文案。",
            minimal_edit="删除平台创建流程文案，只保留 metadata 和参考资料正文。",
        ),
        ContractCheckResult(
            id="reference.single_file",
            passed=not bool(_MULTI_FILE_MARKER_RE.search(content)) and not bool(re.search(r"(?im)^\s*写入文件[:：]", content)),
            target=file_path,
            message=(
                "参考资料是单文件内容。"
                if not _MULTI_FILE_MARKER_RE.search(content) and not re.search(r"(?im)^\s*写入文件[:：]", content)
                else f"{file_path} 包含多文件包、其它文件路径标题或写入文件标签。"
            ),
            expected="只输出当前 reference 文件内容，不要包含 SKILL.md/scripts/assets/references 的多文件包。",
            minimal_edit="删除其它文件内容、路径标题和写入文件标签，只保留当前 reference metadata 和正文。",
        ),
    ])

    min_chars = 120
    has_min_length = len(stripped) >= min_chars
    results.append(ContractCheckResult(
        id="reference.min_quality_length",
        passed=has_min_length,
        target=file_path,
        message=(
            "参考资料正文长度满足最低质量要求。"
            if has_min_length
            else f"{file_path} 正文过短，无法作为子任务参考资料。"
        ),
        expected=f"frontmatter 后正文至少 {min_chars} 个字符，包含任务规则、示例和约束。",
        minimal_edit="扩充 reference 正文，加入规范、示例、反例和质量标准。",
    ))

    required_sections = {
        "rules": bool(re.search(r"(?im)^#{1,3}.*(规范|规则|步骤|流程|要求|Rules|Steps)", stripped)),
        "examples": bool(re.search(r"(?im)^#{1,3}.*(示例|例子|Examples?)", stripped)),
        "anti_examples": bool(re.search(r"(?im)^#{1,3}.*(反例|错误示例|Anti[- ]?examples?)", stripped)),
        "constraints": bool(re.search(r"(?im)^#{1,3}.*(约束|限制|禁止|Constraints?)", stripped)),
    }
    sections_ok = all(required_sections.values())
    missing_sections = [name for name, present in required_sections.items() if not present]
    results.append(ContractCheckResult(
        id="reference.required_sections",
        passed=sections_ok,
        target=file_path,
        message=(
            "参考资料正文包含规范/示例/反例/约束章节。"
            if sections_ok
            else f"{file_path} 正文缺少必要章节：{', '.join(missing_sections)}。"
        ),
        expected="正文包含规范/步骤、示例、反例、约束/禁止项章节。",
        minimal_edit="在正文补齐 Markdown 标题章节：## 规范、## 示例、## 反例、## 约束。",
    ))

    role_sections = {
        "io": bool(re.search(r"(?im)^#{1,3}.*(输入|输出|Inputs?|Outputs?)", stripped)),
        "quality": bool(re.search(r"(?im)^#{1,3}.*(质量|验收|检查|Quality|Acceptance)", stripped)),
    }
    missing_role_sections = [name for name, present in role_sections.items() if not present]
    results.append(ContractCheckResult(
        id="reference.role_sections",
        passed=not missing_role_sections,
        target=file_path,
        message=(
            "参考资料正文包含输入输出/质量验收章节。"
            if not missing_role_sections
            else f"{file_path} 正文缺少角色相关章节：{', '.join(missing_role_sections)}。"
        ),
        expected="正文应包含输入/输出说明和质量/验收标准。",
        minimal_edit="补充 ## 输入输出 和 ## 质量验收 章节。",
    ))

    # references may mention scripts/** in prose, but should not preserve executable shell blocks.
    executable_reference_blocks: list[str] = []
    for info, body in _iter_markdown_fenced_blocks(stripped):
        if _is_shell_fence_info(info) and re.search(
            r"(?m)^\s*(?:python|python3|node|bash|sh)\s+scripts/[A-Za-z0-9_./-]+\b",
            body,
        ):
            executable_reference_blocks.append(body.strip())

    results.append(ContractCheckResult(
        id="reference.no_executable_script_blocks",
        passed=not executable_reference_blocks,
        target=file_path,
        message=(
            "reference 未包含可执行 scripts/** shell 命令块。"
            if not executable_reference_blocks
            else f"{file_path} 包含可执行 scripts/** shell 命令块，reference 只能作为说明资源。"
        ),
        expected=(
            "references/*.md 可以包含 ```json 或 ```text 示例，"
            "但不得包含 ```bash/```sh/```shell 中调用 scripts/** 的可执行命令。"
        ),
        minimal_edit="把 reference 中的可执行命令示例改为 ```text，或改写为普通说明，不要使用 bash/sh/shell fence。",
    ))

    # references must not redefine script capability contracts.
    capability_contract_patterns = [
        r"(?i)\brequired_capabilities\b",
        r"(?i)\bforbidden_capabilities\b",
        r"(?i)\btext_generation\b",
        r"(?i)\bimage_generation\b",
        r"(?i)\bpdf_generation\b",
        r"(?i)\bruntime_execution\b",
    ]
    mentions_script_capability_contract = bool(
        re.search(r"scripts/[A-Za-z0-9_./-]+\.py", stripped)
        and any(re.search(pattern, stripped) for pattern in capability_contract_patterns)
    )
    results.append(ContractCheckResult(
        id="reference.no_script_capability_redefinition",
        passed=not mentions_script_capability_contract,
        target=file_path,
        message=(
            "reference 未重新定义脚本能力边界。"
            if not mentions_script_capability_contract
            else f"{file_path} 在 reference 正文中重新定义 scripts/*.py 的能力边界。"
        ),
        expected=(
            "reference 只能描述内容结构、格式、风格和质量标准；"
            "scripts/*.py 的 required_capabilities/forbidden_capabilities 只能来自 SkillPlan。"
        ),
        minimal_edit=(
            "删除 reference 中关于某个脚本必须/禁止 text_generation、image_generation、pdf_generation、"
            "runtime_execution 的描述，改为内容规范或输出格式要求。"
        ),
    ))

    has_placeholder = bool(_REFERENCE_PLACEHOLDER_RE.search(stripped))
    results.append(ContractCheckResult(
        id="reference.no_placeholder_phrases",
        passed=not has_placeholder,
        target=file_path,
        message=(
            "参考资料正文未包含占位短语。"
            if not has_placeholder
            else f"{file_path} 正文包含 placeholder/TODO/待补充等占位短语。"
        ),
        expected="不要使用 placeholder、TODO、待补充、将要生成等占位表达。",
        minimal_edit="删除占位短语并替换为实际任务规则和示例。",
    ))

    return results



def _reference_script_commands(content: str) -> list[tuple[str, str]]:
    """Return executable script commands declared by references.

    Current design intentionally returns no executable commands:
    references/*.md are documentation resources, not workflow sources.
    They may mention scripts/** in prose or examples, but those mentions must
    never create an executable command contract.
    """
    return []


def _declared_list_in_text(field_name: str, content: str) -> list[str] | None:
    pattern = re.compile(rf"(?:^|\b){re.escape(field_name)}\s*[：:=]\s*\[?([^\]\n;]+)\]?", re.I | re.M)
    match = pattern.search(content or "")
    if not match:
        return None
    return [re.sub(r"[^A-Za-z0-9_./-]", "", item.strip().strip("'\"")) for item in re.split(r"[,，、]\s*", match.group(1)) if item.strip()]


def _declared_role_in_text(content: str) -> str | None:
    match = re.search(r"(?:^|\b)role\s*[：:=]\s*(text_generator|image_generator|composite_generator|pdf_builder|docx_builder|pptx_builder|html_asset_builder|asset_builder|generic_script)", content or "", re.I | re.M)
    return match.group(1) if match else None


def _anti_example_sections(content: str) -> str:
    chunks: list[str] = []
    matches = list(re.finditer(r"(?im)^#{1,3}.*(?:反例|错误示例|Anti[- ]?examples?).*$", content or ""))
    for idx, match in enumerate(matches):
        start = match.end()
        next_heading = re.search(r"(?m)^#{1,3}\s+", content[start:])
        end = start + next_heading.start() if next_heading else len(content)
        chunks.append(content[start:end])
    return "\n".join(chunks)


def _check_reference_skillplan_redefinitions(file_path: str, content: str, entry: SkillPlanEntry) -> list[ContractCheckResult]:
    """Ensure references do not invent a second script interface contract."""
    results: list[ContractCheckResult] = []
    declared_role = _declared_role_in_text(content)
    role_ok = declared_role is None or declared_role == entry.role
    results.append(ContractCheckResult(
        id="reference.role.matches_skillplan",
        passed=role_ok,
        target=f"{file_path}#{entry.path}",
        message=("reference 未重新定义冲突 role。" if role_ok else f"reference 重新定义 role={declared_role}，与 SkillPlan.role={entry.role} 冲突。"),
        expected=f"reference 默认不要定义 role；如提及只能是 role={entry.role}。",
        minimal_edit="删除 reference 中的 role/能力合同定义，改写为写作规范、风格要求、示例和质量标准。",
    ))
    for field_name, expected_values in (
        ("inputs", entry.inputs or ["payload"]),
        ("outputs", entry.outputs),
        ("required_capabilities", entry.required_capabilities),
        ("forbidden_capabilities", entry.forbidden_capabilities),
    ):
        declared = _declared_list_in_text(field_name, content)
        ok = declared is None or declared == expected_values
        results.append(ContractCheckResult(
            id=f"reference.{field_name}.matches_skillplan",
            passed=ok,
            target=f"{file_path}#{entry.path}",
            message=(f"reference 未重新定义冲突 {field_name}。" if ok else f"reference {field_name}={declared} 与 SkillPlan {field_name}={expected_values} 冲突。"),
            expected=f"reference 默认不要定义 {field_name}；如提及必须逐字等于 SkillPlan: {expected_values}。",
            minimal_edit=f"删除或修正 {field_name} 小节，避免产生第二套接口合同。",
        ))
    anti = _anti_example_sections(content)
    correct_command_in_anti = bool(anti and entry.command_template and entry.command_template in anti)
    correct_keys_in_anti = False
    for command in re.findall(r"```(?:bash|sh|shell)?\s*\n([\s\S]*?)\n```", anti, flags=re.I):
        keys = _command_payload_keys(command.strip(), entry.path)
        if keys == set(entry.inputs or ["payload"]):
            correct_keys_in_anti = True
    results.append(ContractCheckResult(
        id="reference.anti_example.not_skillplan_command",
        passed=not correct_command_in_anti and not correct_keys_in_anti,
        target=f"{file_path}#anti-examples",
        message=("reference 未把 SkillPlan 正确命令/JSON keys 写成反例。" if not correct_command_in_anti and not correct_keys_in_anti else "reference 把 SkillPlan.command_template 或正确 JSON keys 写入反例，导致合同冲突。"),
        expected="反例只能展示 extra key、缺失 key、错误 runner 或非 JSON argv；不得否定 SkillPlan.command_template。",
        minimal_edit="从反例中移除正确命令，改为错误示例例如 extra 参数或 payload 包装。",
    ))
    return results

def _validate_reference_file_contract(file_path: str, content: str, purpose: str = "") -> None:
    results = _check_reference_file_contract(file_path, content, purpose)
    if any(not result.passed for result in results):
        raise ContractValidationError(
            _format_contract_failures(results).replace("SKILL.md contract", f"{file_path} contract"),
            results,
        )


def _asset_extension_check(file_path: str, stripped: str) -> tuple[bool, str, str]:
    ext = Path(file_path).suffix.lower()
    if not stripped:
        return True, "空内容由 asset.not_empty 检查处理。", "当前 asset 文件内容非空。"
    if ext == ".json":
        try:
            json.loads(stripped)
        except json.JSONDecodeError as exc:
            return False, f"{file_path} 不是合法 JSON: {exc.msg}", "JSON asset 必须可被 json.loads 解析。"
        return True, "JSON asset 可解析。", "JSON asset 必须可被 json.loads 解析。"
    if ext in {".yaml", ".yml"}:
        try:
            yaml.safe_load(stripped)
        except yaml.YAMLError as exc:
            return False, f"{file_path} 不是合法 YAML: {exc}", "YAML asset 必须可被 yaml.safe_load 解析。"
        return True, "YAML asset 可解析。", "YAML asset 必须可被 yaml.safe_load 解析。"
    if ext == ".csv":
        rows = list(csv.reader(io.StringIO(stripped)))
        header = rows[0] if rows else []
        if len(rows) < 3 or not header or any(not cell.strip() for cell in header):
            return False, f"{file_path} CSV 必须包含非空表头和至少 2 行数据。", "CSV asset 必须包含 header 和至少 2 行数据。"
        return True, "CSV asset 包含表头和至少 2 行数据。", "CSV asset 必须包含 header 和至少 2 行数据。"
    if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        data = stripped.encode("latin1", errors="ignore")
        if re.fullmatch(r"[A-Za-z0-9+/=\s]+", stripped) and len(stripped) > 24:
            try:
                data = base64.b64decode(stripped, validate=True)
            except ValueError:
                data = stripped.encode("latin1", errors="ignore")
        magic_ok = (
            data.startswith(b"\x89PNG\r\n\x1a\n")
            or data.startswith(b"\xff\xd8\xff")
            or data.startswith(b"GIF87a")
            or data.startswith(b"GIF89a")
            or data.startswith(b"RIFF") and b"WEBP" in data[:16]
        )
        dims = _image_dimensions(data)
        size_ok = dims is not None and dims[0] >= 64 and dims[1] >= 64
        image_ok = magic_ok and size_ok
        return (
            image_ok,
            "image asset 头部和尺寸合法。" if image_ok else f"{file_path} 不是有效图片或尺寸小于 64x64。",
            "图片 asset 必须是有效图片字节或 base64，且尺寸 >= 64x64。",
        )
    if ext == ".pdf":
        pdf_ok = stripped.startswith("%PDF-") and "%%EOF" in stripped and len(stripped.encode("latin1", errors="ignore")) > 100
        return (
            pdf_ok,
            "PDF asset 结构合法。" if pdf_ok else f"{file_path} 必须以 %PDF- 开头、包含 %%EOF 且大于 100 bytes。",
            "PDF asset 必须是有效、非空 PDF 内容。",
        )
    if ext in {".md", ".txt"}:
        quality_ok = len(stripped) >= 40 and not _REFERENCE_PLACEHOLDER_RE.search(stripped)
        return (
            quality_ok,
            "Markdown/text asset 满足最低质量要求。" if quality_ok else f"{file_path} 文本资源过短或包含占位短语。",
            "Markdown/text asset 至少 40 个字符且不能包含占位短语。",
        )
    return True, "asset 格式可解析。", "当前 asset 文件内容必须符合其扩展名对应格式。"



def _image_dimensions(data: bytes) -> tuple[int, int] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
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
                height = int.from_bytes(data[idx + 3:idx + 5], "big")
                width = int.from_bytes(data[idx + 5:idx + 7], "big")
                return width, height
            idx += length
    return None


def _validate_pdf_trial_outputs(payload: dict[str, Any], *, skill_dir: Path | None, args: list[str], stdout: str) -> None:
    candidates: list[str] = []
    pdf_path = payload.get("pdf_path")
    if isinstance(pdf_path, str) and pdf_path.strip():
        candidates.append(pdf_path.strip())
    file_paths = payload.get("file_paths")
    if isinstance(file_paths, list):
        candidates.extend(p.strip() for p in file_paths if isinstance(p, str) and p.strip())

    checked: list[Path] = []
    for candidate in candidates:
        path = Path(candidate)
        if not path.is_absolute() and skill_dir is not None:
            path = (skill_dir / "scripts" / path).resolve()
        checked.append(path)
        if path.is_file():
            data = path.read_bytes()
            if len(data) > 100 and data.startswith(b"%PDF-") and b"%%EOF" in data[-2048:]:
                return
    raise ValueError(
        "pdf_builder 试运行必须生成真实可用 PDF：文件存在、大小 > 100 bytes、以 %PDF- 开头且包含 %%EOF。"
        f" argv={args!r} stdout={stdout[-4000:]} checked={[str(p) for p in checked]}"
    )

def _check_asset_file_contract(file_path: str, content: str) -> list[ContractCheckResult]:
    stripped = content.strip()
    has_runtime_code = bool(_PLATFORM_IMAGE_HELPER_RE.search(stripped))
    parse_ok, parse_message, parse_expected = _asset_extension_check(file_path, stripped)
    return [
        ContractCheckResult(
            id="asset.not_empty",
            passed=bool(stripped),
            target=file_path,
            message=("asset 内容非空。" if stripped else f"{file_path} asset 内容为空。"),
            expected="输出当前 asset 的模板或静态资源内容。",
            minimal_edit="补充真实模板/静态资源内容，不要输出空壳。",
        ),
        ContractCheckResult(
            id="asset.parseable",
            passed=parse_ok,
            target=file_path,
            message=parse_message,
            expected=parse_expected,
            minimal_edit="按文件扩展名修正格式：JSON/YAML/CSV/image/PDF/Markdown 文本必须可解析且非空。",
        ),
        ContractCheckResult(
            id="asset.no_runtime_capability",
            passed=not has_runtime_code,
            target=file_path,
            message=(
                "asset 未包含运行时图片生成能力。"
                if not has_runtime_code
                else f"{file_path} 是 asset，但包含图片生成 helper/运行时代码。"
            ),
            expected="asset 只能是模板或静态资源，不得执行 image_generation 等能力。",
            minimal_edit="删除运行时代码或将该职责拆分为 scripts/ 文件。",
        ),
    ]


def _validate_asset_file_contract(file_path: str, content: str) -> None:
    results = _check_asset_file_contract(file_path, content)
    if any(not result.passed for result in results):
        raise ContractValidationError(_format_contract_failures(results).replace("SKILL.md contract", f"{file_path} contract"), results)



def _script_uses_registry_helpers(content: str, capability: str) -> bool:
    cap = get_tool_capability(capability)
    if not cap or not cap.helper_imports:
        return False
    helper_pattern = "|".join(re.escape(helper) for helper in sorted(cap.helper_imports, key=len, reverse=True))
    return bool(re.search(rf"\b(?:{helper_pattern})\b", content, re.IGNORECASE))


def _script_satisfies_required_capability(content: str, capability: str) -> bool:
    capability = capability.lower()
    if _script_uses_registry_helpers(content, capability):
        return True
    if capability == "text_generation":
        return bool(re.search(r"generate_text_with_llm|LLM_BASE_URL|TEXT_MODEL|chat/completions|complete_chat_once|stream_chat", content, re.IGNORECASE))
    if capability == "image_generation":
        # Direct image API usage is rejected later with a more specific error
        # (for example VISION_MODEL misuse).  Treat it as an attempted image
        # capability here so the repair loop sees the precise role/API failure
        # instead of a generic "missing required_capabilities" message.
        return bool(_PLATFORM_IMAGE_HELPER_RE.search(content) or _DIRECT_IMAGE_API_RE.search(content))
    if capability == "pdf_generation":
        return bool(re.search(r"FPDF|reportlab|PdfWriter|pdf\.output|canvas\.Canvas|build_pdf|%PDF-|pdf_path|\.pdf[\"\']", content, re.IGNORECASE))
    if capability == "docx_generation":
        return bool(re.search(r"Document\(|python-docx|word/document.xml|ZipFile\(|build_docx", content, re.IGNORECASE))
    if capability == "pptx_generation":
        return bool(re.search(r"Presentation\(|python-pptx|ppt/presentation.xml|ZipFile\(|build_pptx", content, re.IGNORECASE))
    if capability in {"html_generation", "html_asset_generation"}:
        return bool(re.search(r"write_text|open\s*\(|<html|<!DOCTYPE html|assets/generated|build_html", content, re.IGNORECASE))
    if capability == "file_output":
        return bool(re.search(r"write_text|write_bytes|open\s*\(|fs\.writeFile|pdf\.output|prs\.save|ZipFile\(|build_(?:pdf|docx|pptx|html)", content, re.IGNORECASE))
    cap = get_tool_capability(capability)
    if cap and cap.helper_imports:
        return False
    # Unknown capabilities stay permissive so older plans are not rejected.
    return True



_ARTIFACT_OUTPUT_KEYS = {"pdf_path", "docx_path", "pptx_path", "html_path", "file_paths"}
_ARTIFACT_CAPABILITIES = {"pdf_generation", "docx_generation", "pptx_generation", "html_generation", "html_asset_generation", "file_output"}


def _script_has_real_file_creation_logic(content: str, *, outputs: list[str], capabilities: list[str]) -> bool:
    declared_artifacts = _ARTIFACT_OUTPUT_KEYS & set(outputs or [])
    required_artifacts = _ARTIFACT_CAPABILITIES & set(capabilities or [])
    if not declared_artifacts and not required_artifacts:
        return True
    has_writer = bool(re.search(
        r"write_text|write_bytes|open\s*\([^)]*,\s*[rbu'\"]*w|pdf\.output|prs\.save|ZipFile\s*\(|shutil\.copy|Path\([^)]*\)\.write_|build_(?:pdf|docx|pptx|html)",
        content,
        re.IGNORECASE,
    ))
    returns_only_paths = bool(re.search(
        r"return\s*\{[^}]*['\"](?:pdf_path|docx_path|pptx_path|html_path|file_paths)['\"][^}]*\}",
        content,
        re.IGNORECASE | re.DOTALL,
    )) and not has_writer
    return has_writer and not returns_only_paths

_MODEL_CAPABILITIES = {"text_generation", "image_generation"}
_DETERMINISTIC_BUILDER_ROLES = {"pdf_builder", "docx_builder", "pptx_builder", "html_asset_builder", "asset_builder"}


def _effective_required_capabilities_for_script(plan_entry: SkillPlanEntry) -> list[str]:
    """Return capabilities that this script source must visibly exercise.

    File builders/exporters are deterministic by default.  If a global
    SKILL.md/blueprint model declaration was accidentally copied into a
    builder's required_capabilities, do not turn that into a requirement for
    ``build_pdf.py`` (or sibling exporters) to call LLM/IMAGE_MODEL.  Model
    scripts keep their text/image requirements through their own generator
    roles, while builders are validated for real artifact creation.
    """
    capabilities = list(plan_entry.required_capabilities or [])
    if plan_entry.role in _DETERMINISTIC_BUILDER_ROLES:
        capabilities = [capability for capability in capabilities if capability not in _MODEL_CAPABILITIES]
    return capabilities


def _script_required_capability_failures(content: str, capabilities: list[str]) -> list[str]:
    return [capability for capability in capabilities if not _script_satisfies_required_capability(content, capability)]

def _pdf_unicode_strategy_status(content: str) -> tuple[bool, str]:
    """Detect whether a PDF builder has a real Chinese/UTF-8 text strategy.

    This check intentionally does not require one fixed code structure.
    It accepts:
    - reportlab + UnicodeCIDFont('STSong-Light') registration
    - reportlab + TTFont registration
    - fpdf/fpdf2 + add_font
    It rejects:
    - FPDF core fonts for payload text
    - reportlab setFont('STSong-Light') without registerFont
    - raw hand-written %PDF bytes
    """
    stripped = content or ""

    uses_fpdf = bool(re.search(
        r"\bFPDF\b|from\s+fpdf\s+import|import\s+fpdf",
        stripped,
        re.I,
    ))
    uses_fpdf_core_font = bool(re.search(
        r"\.set_font\s*\(\s*['\"](?:Helvetica|Arial|Times|Courier|Symbol|ZapfDingbats)['\"]",
        stripped,
        re.I,
    ))
    uses_fpdf_add_font = bool(re.search(
        r"\.add_font\s*\(",
        stripped,
        re.I,
    ))

    uses_reportlab = bool(re.search(
        r"reportlab|canvas\.Canvas",
        stripped,
        re.I,
    ))
    uses_stsong = bool(re.search(
        r"STSong-Light",
        stripped,
        re.I,
    ))
    imports_unicode_cid_font = bool(re.search(
        r"UnicodeCIDFont|reportlab\.pdfbase\.cidfonts",
        stripped,
        re.I,
    ))
    registers_stsong_cid = bool(re.search(
        r"pdfmetrics\.registerFont\s*\(\s*UnicodeCIDFont\s*\(\s*['\"]STSong-Light['\"]\s*\)\s*\)",
        stripped,
        re.I,
    ))

    uses_ttf_registration = bool(re.search(
        r"pdfmetrics\.registerFont\s*\(\s*TTFont\s*\(|TTFont\s*\(",
        stripped,
        re.I,
    ))

    raw_pdf_bytes = bool(re.search(
        r"write_bytes\s*\(\s*b?[\"']%PDF-|open\s*\([^)]*['\"]wb['\"][^)]*\).*%PDF-",
        stripped,
        re.I | re.S,
    ))

    if raw_pdf_bytes:
        return (
            False,
            "脚本疑似手写 raw %PDF 字节。PDF builder 必须用真实 PDF 库生成可用 PDF，不能拼接占位 PDF。",
        )

    if uses_fpdf:
        if uses_fpdf_add_font:
            return (
                True,
                "使用 fpdf/fpdf2 且调用 add_font，具备 Unicode 字体加载策略。",
            )
        if uses_fpdf_core_font:
            return (
                False,
                "使用 FPDF 默认核心字体 Helvetica/Arial/Times/Courier 写正文，中文会触发 latin-1 UnicodeEncodeError。",
            )
        return (
            False,
            "使用 fpdf/fpdf2 但没有发现 add_font；中文/UTF-8 PDF 必须加载 TTF/OTF 字体后再 set_font。",
        )

    if uses_reportlab:
        if uses_stsong and registers_stsong_cid and imports_unicode_cid_font:
            return (
                True,
                "使用 reportlab 并注册 UnicodeCIDFont('STSong-Light')，支持中文。",
            )
        if uses_ttf_registration:
            return (
                True,
                "使用 reportlab 并注册 TTFont，具备 Unicode 字体策略。",
            )
        if uses_stsong and not registers_stsong_cid:
            return (
                False,
                "使用了 STSong-Light，但没有先 pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))，运行时会 KeyError: 'STSong-Light'。",
            )
        return (
            False,
            "使用 reportlab 写 PDF，但没有发现 UnicodeCIDFont 或 TTFont 注册；中文正文可能无法显示或运行时报错。",
        )

    return (
        False,
        "未发现可靠 PDF 中文字体方案。推荐 reportlab + UnicodeCIDFont('STSong-Light')，或 reportlab + TTFont，或 fpdf2 + add_font。",
    )

def _check_script_file_contract(
    file_path: str,
    content: str,
    role: str | None = None,
    skill_plan_entry: dict[str, Any] | None = None,
) -> list[ContractCheckResult]:
    plan_entry = _skill_plan_entry_for_file(
        file_path=file_path,
        role=role,
        skill_plan_entry=skill_plan_entry,
    )
    strict_interface = skill_plan_entry is not None
    stripped = content.strip()
    effective_required_capabilities = _effective_required_capabilities_for_script(plan_entry)

    has_markdown_or_bundle = (
        "```" in stripped
        or "~~~" in stripped
        or bool(_MULTI_FILE_MARKER_RE.search(stripped))
    )
    raw_ok = bool(stripped) and not has_markdown_or_bundle

    results: list[ContractCheckResult] = [
        ContractCheckResult(
            id="script.raw_source.single_file",
            passed=raw_ok,
            target=file_path,
            message=(
                "脚本是单个裸源码文件。"
                if raw_ok
                else f"{file_path} 生成内容包含 Markdown 代码块或多文件包，不是单个脚本源码。请重新生成该文件。"
            ),
            expected="只输出单个脚本源码本身，不要 Markdown fence、说明文字、写入文件标签或多文件包。",
            minimal_edit="从上一次内容中只保留目标脚本源码；删除所有 ``` fence、文件路径标题、写入文件标签和说明文字。",
        )
    ]

    if not raw_ok:
        return results

    syntax_ok = True
    syntax_message = f"{plan_entry.language} 源码基础校验通过。"
    syntax_expected = "脚本源码必须符合 language/runtime 的基础语法与入口约定。"

    if plan_entry.language == "python":
        try:
            ast.parse(stripped)
        except SyntaxError as exc:
            syntax_ok = False
            syntax_message = f"{file_path} 生成内容不是合法 Python 源码: {exc.msg}"
        syntax_expected = "Python 脚本必须能通过 ast.parse 语法检查。"
    elif plan_entry.runtime == "node":
        syntax_ok = "process.argv" in stripped and "console.log" in stripped
        syntax_message = (
            "Node/JS 脚本包含 process.argv 和 stdout JSON 输出。"
            if syntax_ok
            else f"{file_path} Node/JS 脚本必须使用 process.argv 读取 JSON argv 并 console.log 输出 JSON。"
        )
        syntax_expected = "Node/JS 脚本必须使用 process.argv[2] + JSON.parse，并通过 console.log(JSON.stringify(...)) 输出 JSON。"
    elif plan_entry.runtime in {"bash", "shell"}:
        syntax_ok = "$1" in stripped or "${1" in stripped
        syntax_message = (
            "Shell/Bash 脚本读取 $1 JSON argv。"
            if syntax_ok
            else f"{file_path} Shell/Bash 脚本必须读取 $1 JSON argv。"
        )
        syntax_expected = "Shell/Bash 脚本必须读取 $1 JSON argv，并向 stdout 输出 JSON 或写入声明的文件产物。"

    results.append(
        ContractCheckResult(
            id="script.source.syntax",
            passed=syntax_ok,
            target=file_path,
            message=syntax_message,
            expected=syntax_expected,
            minimal_edit="修正源码语法/入口错误，同时保持 stdout JSON 和参数接口不变。",
        )
    )

    if strict_interface:
        reads_json = _script_reads_json_argv(stripped, plan_entry.runtime)
        results.append(
            ContractCheckResult(
                id="script.json_argv.runtime",
                passed=reads_json,
                target=file_path,
                message=(
                    f"脚本按 {plan_entry.runtime} runtime 读取 JSON argv。"
                    if reads_json
                    else f"{file_path} 必须按 {plan_entry.runtime} runtime 读取 JSON argv。"
                ),
                expected="Python: sys.argv[1]+json.loads；Node: process.argv[2]+JSON.parse；Bash: $1 JSON。",
                minimal_edit="补充 runtime 对应 JSON argv 解析入口。",
            )
        )

        uses_keys, missing_keys = _script_uses_input_keys(
            stripped,
            list(plan_entry.inputs or ["payload"]),
        )
        results.append(
            ContractCheckResult(
                id="script.skillplan_inputs.used",
                passed=uses_keys,
                target=file_path,
                message=(
                    "脚本源码使用了所有 SkillPlan inputs。"
                    if uses_keys
                    else f"脚本源码未使用这些 SkillPlan inputs：{', '.join(missing_keys)}。"
                ),
                expected=f"源码必须实际读取/使用 inputs：{', '.join(plan_entry.inputs or ['payload'])}。",
                minimal_edit="在参数解析或业务逻辑中读取并使用缺失的 payload key。",
            )
        )

        has_entry = _script_has_main_entry(stripped, plan_entry.runtime)
        results.append(
            ContractCheckResult(
                id="script.runtime.entrypoint",
                passed=has_entry,
                target=file_path,
                message=(
                    "脚本包含 runtime 入口与 stdout 输出。"
                    if has_entry
                    else f"{file_path} 缺少 {plan_entry.runtime} 入口或 stdout 输出。"
                ),
                expected="脚本包含对应 runtime 的入口函数/语句，并向 stdout 输出 JSON。",
                minimal_edit="补齐 main/入口调用和 JSON stdout 输出。",
            )
        )

    missing_capabilities = _script_required_capability_failures(
        stripped,
        effective_required_capabilities,
    )
    results.append(
        ContractCheckResult(
            id="script.required_capabilities.called",
            passed=not missing_capabilities,
            target=file_path,
            message=(
                "脚本调用了 role.required_capabilities 对应的平台/文件能力。"
                if not missing_capabilities
                else f"脚本没有调用这些 required_capabilities 对应接口：{', '.join(missing_capabilities)}。"
            ),
            expected=(
                "text_generation 调用 generate_text_with_llm/平台 LLM；"
                "image_generation 调用 generate_stable_diffusion_image；"
                "pdf_generation 使用支持中文/UTF-8 的 reportlab/fpdf2/PDF 构建；"
                "file_output 写入声明文件。"
            ),
            minimal_edit="按 role + runtime 注入对应平台 helper 或真实文件/PDF 构建逻辑，不要返回固定占位文本。",
        )
    )

    enforce_artifact_outputs = strict_interface or bool(
        _ARTIFACT_CAPABILITIES & set(effective_required_capabilities)
    )
    has_real_file_output = _script_has_real_file_creation_logic(
        stripped,
        outputs=list(plan_entry.outputs or []) if enforce_artifact_outputs else [],
        capabilities=effective_required_capabilities if enforce_artifact_outputs else [],
    )
    results.append(
        ContractCheckResult(
            id="script.file_outputs.real_creation_logic",
            passed=has_real_file_output,
            target=file_path,
            message=(
                "脚本声明文件输出时包含真实文件创建逻辑。"
                if has_real_file_output
                else f"{file_path} 声明 pdf/docx/pptx/html/file_paths 输出或文件生成能力，但只返回路径字符串或缺少真实写文件逻辑。"
            ),
            expected="声明 pdf_path/docx_path/pptx_path/html_path/file_paths 或文件生成 required_capabilities 时，必须真实创建对应文件，禁止只返回路径占位。",
            minimal_edit="拆出 build_pdf/build_docx/build_pptx/build_html 等函数，在其中写入真实文件并只返回已创建文件路径。",
        )
    )

    has_fake = bool(_SCRIPT_FAKE_IMPLEMENTATION_RE.search(stripped))
    results.append(
        ContractCheckResult(
            id="script.no_fake_implementation",
            passed=not has_fake,
            target=file_path,
            message=(
                "脚本未包含占位/模拟/假 API 实现。"
                if not has_fake
                else f"{file_path} 包含占位/模拟/假 API 实现。Creator 生成的脚本必须具备真实可执行功能。"
            ),
            expected="不得使用 placeholder/mock/fake API/固定模板冒充真实能力。",
            minimal_edit="替换占位或模拟逻辑，实现真实可执行算法或调用平台配置模型/helper。",
        )
    )

    uses_image_helper = bool(_PLATFORM_IMAGE_HELPER_RE.search(stripped))
    if plan_entry.role in {
        "pdf_builder",
        "docx_builder",
        "pptx_builder",
        "html_asset_builder",
        "asset_builder",
    }:
        results.append(
            ContractCheckResult(
                id="script.capability.forbidden_image_generation",
                passed=not uses_image_helper,
                target=file_path,
                message=(
                    "文件构建脚本未调用图片生成 helper。"
                    if not uses_image_helper
                    else f"{file_path} 是文件构建脚本，但调用了图片生成 helper。"
                ),
                expected="文件构建脚本只能消费已有数据并真实创建文件；不得调用 generate_stable_diffusion_image。",
                minimal_edit="移除图片 helper 调用，只保留文件构建逻辑。",
            )
        )

    if "image_generation" in (plan_entry.forbidden_capabilities or []):
        results.append(
            ContractCheckResult(
                id="script.capability.forbidden_image_generation",
                passed=not uses_image_helper,
                target=file_path,
                message=(
                    "脚本未调用 forbidden_capabilities 中禁止的 image_generation。"
                    if not uses_image_helper
                    else f"{file_path} 的 SkillPlan forbidden_capabilities 包含 image_generation，但脚本调用了图片生成 helper。"
                ),
                expected="只有 required_capabilities 包含 image_generation 且未被 forbidden_capabilities 禁止时，脚本才可调用 generate_stable_diffusion_image。",
                minimal_edit="蓝图和 SKILL.md 确定后不要修改能力声明；修当前脚本：移除图片 helper 调用，只保留与当前 role/required_capabilities 一致的逻辑。",
            )
        )

    uses_text_helper = bool(
        re.search(
            r"generate_text_with_llm|LLM_BASE_URL|TEXT_MODEL|chat/completions|complete_chat_once|stream_chat",
            stripped,
            re.IGNORECASE,
        )
    )
    if "text_generation" in (plan_entry.forbidden_capabilities or []):
        results.append(
            ContractCheckResult(
                id="script.capability.forbidden_text_generation",
                passed=not uses_text_helper,
                target=file_path,
                message=(
                    "脚本未调用 forbidden_capabilities 中禁止的 text_generation。"
                    if not uses_text_helper
                    else f"{file_path} 的 SkillPlan forbidden_capabilities 包含 text_generation，但脚本调用了文本生成 helper/LLM。"
                ),
                expected="只有 required_capabilities 包含 text_generation 且未被 forbidden_capabilities 禁止时，脚本才可调用 generate_text_with_llm 或平台文本模型。",
                minimal_edit="蓝图和 SKILL.md 确定后不要修改能力声明；修当前脚本：移除文本模型调用，只保留与当前 role/required_capabilities 一致的逻辑。",
            )
        )

    writes_pdf = bool(
        re.search(
            r"\.pdf[\"']|pdf_path|FPDF|reportlab|PdfWriter|write_bytes\s*\(\s*b?[\"']%PDF-",
            stripped,
            re.IGNORECASE,
        )
    )
    if "pdf_generation" in (plan_entry.forbidden_capabilities or []):
        results.append(
            ContractCheckResult(
                id="script.capability.forbidden_pdf_generation",
                passed=not writes_pdf,
                target=file_path,
                message=(
                    "脚本未调用 forbidden_capabilities 中禁止的 pdf_generation。"
                    if not writes_pdf
                    else f"{file_path} 的 SkillPlan forbidden_capabilities 包含 pdf_generation，但源码包含 PDF 生成/输出逻辑。"
                ),
                expected="只有 required_capabilities 包含 pdf_generation 且未被 forbidden_capabilities 禁止时，脚本才可构建 PDF。",
                minimal_edit="蓝图和 SKILL.md 确定后不要修改能力声明；修当前脚本：若当前脚本禁止 PDF 能力则移除 PDF 生成逻辑。",
            )
        )

    registry_forbidden_helper_hits: list[str] = []
    for forbidden_capability in plan_entry.forbidden_capabilities or []:
        if forbidden_capability in {"image_generation", "text_generation", "pdf_generation"}:
            continue
        if _script_uses_registry_helpers(stripped, str(forbidden_capability)):
            registry_forbidden_helper_hits.append(str(forbidden_capability))
    results.append(
        ContractCheckResult(
            id="script.capability.forbidden_registry_helpers",
            passed=not registry_forbidden_helper_hits,
            target=file_path,
            message=(
                "脚本未调用 forbidden_capabilities 中禁止的 registry helper。"
                if not registry_forbidden_helper_hits
                else f"{file_path} 调用了这些 forbidden_capabilities 对应的 registry helper：{', '.join(registry_forbidden_helper_hits)}。"
            ),
            expected="脚本只能调用 SkillPlan required/optional/allowed capabilities 对应的平台 helper。",
            minimal_edit="移除被 forbidden_capabilities 禁止的 helper 调用，或在蓝图阶段明确声明该能力。",
        )
    )

    if "database_read" in set(effective_required_capabilities):
        write_sql = bool(re.search(
            r"\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|REPLACE|MERGE)\b",
            stripped,
            re.IGNORECASE,
        ))
        multi_statement_sql = bool(re.search(r";\s*(?:SELECT|WITH|INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE)\b", stripped, re.IGNORECASE))
        results.append(
            ContractCheckResult(
                id="script.database_read.readonly_sql",
                passed=not write_sql and not multi_statement_sql,
                target=file_path,
                message=(
                    "数据库读取脚本仅包含只读 SQL 形态。"
                    if not write_sql and not multi_statement_sql
                    else f"{file_path} 的 database_read 能力包含写操作或多语句 SQL 风险。"
                ),
                expected="database_read 只能通过 query_database_readonly 执行 SELECT/WITH 只读查询，禁止写操作和多语句。",
                minimal_edit="将 SQL 改为单条 SELECT/WITH 查询，并通过 query_database_readonly 执行。",
            )
        )

    pdf_outputs_declared = bool({"pdf_path", "file_paths"} & set(plan_entry.outputs or []))
    pdf_required = "pdf_generation" in set(effective_required_capabilities)
    is_pdf_builder = plan_entry.role == "pdf_builder" or pdf_outputs_declared or pdf_required

    if is_pdf_builder:
        pdf_unicode_ok, pdf_unicode_message = _pdf_unicode_strategy_status(stripped)
        results.append(
            ContractCheckResult(
                id="script.pdf_builder.unicode_text_supported",
                passed=pdf_unicode_ok,
                target=file_path,
                message=(
                    "pdf_builder 已声明并实现可靠中文/UTF-8 PDF 字体策略。"
                    if pdf_unicode_ok
                    else f"{file_path} PDF 中文/UTF-8 字体方案不合格：{pdf_unicode_message}"
                ),
                expected=(
                    "PDF 构建脚本必须支持中文/UTF-8 文本。"
                    "允许 reportlab + UnicodeCIDFont('STSong-Light')，"
                    "允许 reportlab + TTFont，"
                    "允许 fpdf2 + add_font 加载 TTF/OTF。"
                    "禁止 FPDF 默认 Helvetica/Arial/Times/Courier 直接写正文；"
                    "禁止 reportlab 只 setFont('STSong-Light') 但不 registerFont；"
                    "禁止 raw %PDF 字节拼接、空 PDF、假路径。"
                ),
                minimal_edit=(
                    "保留脚本自由结构，但必须加入真实 Unicode 字体注册/加载逻辑；"
                    "保持 JSON argv 输入和 stdout JSON 输出；"
                    "stdout 返回真实存在的 pdf_path/file_paths；"
                    "不要通过 try/except 输出 error、{} 或空路径绕过试运行。"
                ),
            )
        )

    if plan_entry.role == "image_generator":
        pdf_only = (
            ("pdf_path" in stripped or "file_paths" in stripped)
            and "image_paths" not in stripped
            and "images" not in stripped
        )
        results.append(
            ContractCheckResult(
                id="script.role.image_forbidden_pdf_only_outputs",
                passed=not pdf_only and not writes_pdf,
                target=file_path,
                message=(
                    "image_generator 未输出或生成 PDF-only 结果。"
                    if not pdf_only and not writes_pdf
                    else f"{file_path} 是 image_generator，但源码包含 PDF-only 输出或 PDF 生成逻辑。"
                ),
                expected="image_generator 必须输出 image_paths/images，不得写 PDF 或只输出 pdf_path/file_paths；需要文本+图片时请使用 composite_generator。",
                minimal_edit="返回 image_paths/images 并删除 PDF 写入逻辑；若要同时生成文本和图片，请将 role/required_capabilities 改为 composite_generator + text_generation/image_generation。",
            )
        )

    return results

def _validate_script_file_source_contract(file_path: str, content: str, role: str | None = None, skill_plan_entry: dict[str, Any] | None = None) -> None:
    # Accept otherwise-valid raw source with a dangling orphan fence marker at
    # the boundary.  Full fenced/bundled responses are still rejected by the
    # lower-level checker unless the sanitize path extracted a single code block.
    candidate = _strip_orphan_trailing_fence(content)
    results = _check_script_file_contract(file_path, candidate, role=role, skill_plan_entry=skill_plan_entry)
    if any(not result.passed for result in results):
        raise ContractValidationError(_format_contract_failures(results).replace("SKILL.md contract", f"{file_path} contract"), results)

def _validate_skill_md_against_existing_files(
    skill_name: str,
    content: str,
    *,
    blueprint_text: str = "",
    require_existing: bool = True,
) -> None:
    skill_name = _validate_skill_name(skill_name)
    skill_root = settings.skills_path / skill_name

    try:
        referenced_paths = sorted(set(_skill_local_paths_in_markdown(content)))
    except Exception as exc:
        raise ValueError(f"SKILL.md 本地路径扫描失败：{exc}") from exc

    ignored_dirs = [
        path for path in referenced_paths if _is_directory_like_skill_path(path)
    ]
    materialized_paths = [
        path for path in referenced_paths if _is_materialized_skill_resource_path(path)
    ]

    if ignored_dirs:
        logger.info(
            "[Creator][skill_md] 忽略目录型路径，不做上传校验 skill=%s paths=%s",
            skill_name,
            ignored_dirs,
        )

    if not require_existing:
        return

    missing: list[str] = []
    for rel_path in materialized_paths:
        abs_path = (skill_root / rel_path).resolve()
        try:
            abs_path.relative_to(skill_root.resolve())
        except ValueError:
            missing.append(rel_path)
            continue
        if not abs_path.exists() or not abs_path.is_file():
            missing.append(rel_path)

    if missing:
        result = ContractCheckResult(
            id="skill_md.resource.exists_on_disk",
            passed=False,
            target="SKILL.md",
            message="SKILL.md 引用了最终打包时仍不存在的本地资源：" + ", ".join(missing),
            expected="最终打包前，SKILL.md 引用的 scripts/references/assets 具体文件必须生成或上传；目录路径不检查。",
            minimal_edit="生成缺失的 scripts/references，上传缺失 assets 文件，或删除 SKILL.md 中对应引用。",
        )
        raise ContractValidationError(
            "SKILL.md contract 未通过：\n" + _format_contract_failures_safe([result]),
            [result],
        )


def _clean_blueprint_for_file_prompt(blueprint_text: str) -> str:
    """Remove Creator UI confirmation text from blueprint context before generation."""
    cleaned_lines: list[str] = []
    in_confirmation_block = False
    for line in (blueprint_text or "").splitlines():
        stripped = line.strip()
        if _CREATOR_FLOW_LEAK_RE.search(stripped):
            in_confirmation_block = True
            continue
        if in_confirmation_block:
            if stripped.startswith("```") or stripped.startswith("- [") or stripped.startswith(">"):
                continue
            if not stripped:
                in_confirmation_block = False
                continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip() or blueprint_text


def _reject_fake_script_implementation(file_path: str, content: str) -> None:
    """Reject placeholder/mock scripts that pretend to implement capabilities."""
    if _SCRIPT_FAKE_IMPLEMENTATION_RE.search(content):
        raise ValueError(
            f"{file_path} 包含占位/模拟/假 API 实现。"
            "Creator 生成的脚本必须具备真实可执行功能；"
            "如需图像或多模态能力，应通过宿主配置的模型/服务完成，不能写 placeholder 文件或假装调用 API。"
        )


def _requires_configured_model_call(*, plan_entry: SkillPlanEntry | None) -> bool:
    """Return whether the current script contract requires host model use.

    Model-call requirements are scoped to this script's SkillPlanEntry.
    Whole-SKILL.md wording about LLM/image models can describe earlier or later
    steps, but must not force deterministic exporter/builder scripts to call a
    model unless their own required_capabilities declare text/image generation.
    """
    if plan_entry is None:
        return False
    return bool({"text_generation", "image_generation"} & set(_effective_required_capabilities_for_script(plan_entry)))


def _script_uses_configured_model(content: str) -> bool:
    """Detect whether script calls the configured host LLM/VL endpoint."""
    return bool(_CONFIGURED_MODEL_CALL_RE.search(content))


def _validate_configured_model_usage_static(*, file_path: str, content: str, skill_md: str, plan_entry: SkillPlanEntry | None = None) -> None:
    """Reject scripts whose own SkillPlanEntry requires host-model behavior but do not call models."""
    if _DIRECT_IMAGE_API_RE.search(content) and "VISION_MODEL" in content:
        raise ValueError(
            f"{file_path} 将 VISION_MODEL 与图片生成接口混用。"
            "生成图片必须使用平台 Stable Diffusion 图片运行时或 IMAGE_MODEL；"
            "VISION_MODEL 只用于看图理解/OCR/多模态问答。"
        )

    if _DIRECT_IMAGE_API_RE.search(content) and not _PLATFORM_IMAGE_HELPER_RE.search(content):
        raise ValueError(
            f"{file_path} 直接调用图片生成接口。"
            "Creator 生成的图片脚本必须调用 backend.services.skill_runtime.generate_stable_diffusion_image，"
            "由平台侧静默完成中文 topic 翻译、Stable Diffusion IMAGE_MODEL 选择、b64_json 解析和图片落盘。"
        )

    if _IMAGE_MODEL_USAGE_RE.search(content) and _IMAGE_URL_ONLY_RE.search(content) and "b64_json" not in content:
        raise ValueError(
            f"{file_path} 假设图片接口只返回 url。"
            "平台图片运行时默认使用 b64_json，并会落盘为文件路径；请调用平台图片运行时 helper。"
        )

    if _DATA_URI_RE.search(content):
        raise ValueError(
            f"{file_path} 输出 base64 data URI。"
            "图片结果必须由平台运行时写入 OUTPUT_DIR，并在 stdout JSON 中返回 image_paths/images。"
        )

    if re.search(r"(?m)^\s*image_path\s*=\s*generate_stable_diffusion_image\s*\(", content):
        raise ValueError(
            f"{file_path} 将 helper 返回 dict 直接赋给 image_path。"
            "图片脚本必须先保存 result = generate_stable_diffusion_image(desc)，"
            "再执行 image_paths.append(result.get(\"image_path\")) 和 images.append(result)。"
        )

    effective_required_capabilities = _effective_required_capabilities_for_script(plan_entry) if plan_entry else []
    if plan_entry and plan_entry.role in {"pdf_builder", "docx_builder", "pptx_builder", "html_asset_builder", "asset_builder"} and not ({"text_generation", "image_generation"} & set(effective_required_capabilities)):
        return
    skill_md_declares_model = bool(re.search(r"宿主|内置|配置模型|LLM|大语言|文本模型|图像模型|vision|TEXT_MODEL|IMAGE_MODEL", skill_md or "", re.IGNORECASE))
    if not _requires_configured_model_call(plan_entry=plan_entry):
        # Keep backward compatibility for legacy generic scripts whose SKILL.md
        # has no local SkillPlan block but clearly says the script is model-backed.
        # Deterministic builders/exporters returned above, so global model prose
        # still cannot force build_pdf.py to call LLM/IMAGE_MODEL.
        if not (plan_entry and plan_entry.role == "generic_script" and skill_md_declares_model):
            return
    if _script_uses_configured_model(content):
        return
    raise ValueError(
        f"{file_path} 的当前脚本职责/SkillPlan.required_capabilities 声明需要使用宿主/内置/配置模型，但脚本没有调用这些模型。"
        "脚本不能用固定模板、随机词表或 ASCII 图替代模型能力；"
        "请通过 LLM_BASE_URL + TEXT_MODEL 调用文本模型，需要图像/视觉能力时使用 IMAGE_MODEL/VISION_MODEL。"
    )

def _validate_generated_file_content(file_path: str, content: str, role: str | None = None, skill_plan_entry: dict[str, Any] | None = None) -> None:
    """Reject content that is clearly not the requested single file."""
    if file_path == "SKILL.md":
        _reject_custom_skill_md_protocol(content)
        return

    if file_path.startswith("scripts/"):
        _validate_script_file_source_contract(file_path, content, role=role, skill_plan_entry=skill_plan_entry)
        return

    if file_path.startswith("references/"):
        _validate_reference_file_contract(file_path, content)
        return

    if file_path.startswith("assets/"):
        _validate_asset_file_contract(file_path, content)
        return

def _script_paths_in_shell_fenced_blocks(skill_md: str) -> set[str]:
    """Return scripts/*.py paths that appear inside shell fenced blocks."""
    paths: set[str] = set()

    for info, body in _iter_markdown_fenced_blocks(skill_md):
        if not _is_shell_fence_info(info):
            continue

        for match in re.finditer(
            r"(?<![\w./-])(scripts/[A-Za-z0-9_./-]+\.py)(?![\w./-])",
            body.replace("\\", "/"),
        ):
            paths.add(match.group(1))

    return paths


def _script_paths_outside_shell_fenced_blocks(skill_md: str) -> set[str]:
    """Return scripts/*.py paths mentioned outside shell fenced blocks.

    This is not used to decide whether a script is part of the blueprint.
    It only catches a bad SKILL.md style:
    mentioning scripts/foo.py in prose without an executable ```bash block.
    """
    text = skill_md or ""

    shell_block_bodies: list[str] = []
    for info, body in _iter_markdown_fenced_blocks(text):
        if _is_shell_fence_info(info):
            shell_block_bodies.append(body)

    text_without_shell_blocks = text
    for body in shell_block_bodies:
        text_without_shell_blocks = text_without_shell_blocks.replace(body, "")

    paths: set[str] = set()
    for match in re.finditer(
        r"(?<![\w./-])(scripts/[A-Za-z0-9_./-]+\.py)(?![\w./-])",
        text_without_shell_blocks.replace("\\", "/"),
    ):
        paths.add(match.group(1))

    return paths


def _validate_command_is_single_shell_json_invocation(
    *,
    command: str,
    script_path: str,
    entry: SkillPlanEntry,
    upstream_available_outputs: set[str] | None = None,
) -> list[ContractCheckResult]:
    """Validate one shell fenced command.

    阻断项：
    - 一个 fenced block 内只能有一条命令；
    - 命令必须能解析到目标 scripts/*.py；
    - 脚本路径后的参数必须是 json.loads 可解析的 JSON object。

    非阻断项：
    - runtime 推断不一致。这个交给最终 E2E/smoke，而不是在 SKILL.md 阶段误杀。
    """
    results: list[ContractCheckResult] = []
    target = script_path

    raw_command = command or ""
    lines = [line.strip() for line in raw_command.strip().splitlines() if line.strip()]
    one_line = len(lines) == 1

    results.append(ContractCheckResult(
        id="skill_md.command_block.single_command",
        passed=one_line,
        target=target,
        message=(
            f"{script_path} 命令块只包含一条命令。"
            if one_line
            else f"{script_path} 命令块应只包含一条命令，不要在一个 block 里写多条命令或解释。"
        ),
        expected="每个 ```bash fenced block 内只放一条命令。",
        minimal_edit="把解释移出 fenced block；一个 block 只保留一条 python scripts/... 命令。",
    ))

    if not one_line:
        return results

    command_line = lines[0]

    try:
        command_sig = _command_signature(command_line, script_path)
    except Exception as exc:
        logger.warning(
            "[Creator][skill_md] command signature parser crashed script=%s command=%s error=%s",
            script_path,
            command_line,
            exc,
        )
        command_sig = None

    parsed_ok = command_sig is not None

    results.append(ContractCheckResult(
        id="skill_md.command_block.signature_parseable",
        passed=parsed_ok,
        target=target,
        message=(
            f"{script_path} 命令块可解析为 runner/script_path/JSON argv。"
            if parsed_ok
            else f"{script_path} 命令块无法解析为标准执行命令。"
        ),
        expected=f"命令应形如：python {script_path} '{{\"key\":\"{{{{value}}}}\"}}'。",
        minimal_edit=f"改为标准命令形态：python {script_path} '{{\"payload\":\"{{{{user_request}}}}\"}}'。",
    ))

    if not command_sig:
        return results

    try:
        keys = _command_payload_keys(command_line, script_path)
    except Exception as exc:
        logger.warning(
            "[Creator][skill_md] command JSON argv parser crashed script=%s command=%s error=%s",
            script_path,
            command_line,
            exc,
        )
        keys = None

    json_ok = keys is not None

    results.append(ContractCheckResult(
        id="skill_md.command_block.json_argv_object",
        passed=json_ok,
        target=target,
        message=(
            f"{script_path} 使用可解析 JSON object argv。"
            if json_ok
            else f"{script_path} 脚本路径后必须跟一个 json.loads 可解析的 JSON object argv。"
        ),
        expected=(
            "脚本路径后跟一个 JSON object argv，例如 "
            "'{\"topic\":\"{{topic}}}'。外层单引号是 shell quoting，不是 JSON 错误。"
        ),
        minimal_edit="把脚本参数改成一个可解析 JSON object；JSON 内部 key/value 使用双引号。",
    ))

    try:
        runtime_matches = _command_runtime_matches(command_line, script_path, entry)
    except Exception as exc:
        runtime_matches = False
        logger.warning(
            "[Creator][skill_md] runtime match check crashed script=%s command=%s error=%s",
            script_path,
            command_line,
            exc,
        )

    # JSON argv keys are the local protocol between SKILL.md and this script.
    # They are flexible across skills, but inside one Skill they must be exactly
    # the current script's SkillPlan.inputs.
    declared_inputs = set(entry.inputs or [])
    actual_keys = set(keys or []) if keys is not None else set()
    exact_inputs = bool(declared_inputs) and actual_keys == declared_inputs
    if declared_inputs:
        missing = sorted(declared_inputs - actual_keys)
        extra = sorted(actual_keys - declared_inputs)
        upstream = sorted(upstream_available_outputs or set())
        results.append(ContractCheckResult(
            id="command_block.skillplan_inputs.exact",
            passed=exact_inputs,
            target=target,
            message=(
                f"{script_path} 命令 JSON keys 与 SkillPlan inputs 一致。"
                if exact_inputs
                else f"{script_path} 命令 JSON keys 与 SkillPlan inputs 不一致。"
            ),
            expected="命令 JSON keys 必须与当前脚本在 SkillPlan 中声明的 inputs 一致。平台不要求固定字段名；字段名由该 Skill 的 SkillPlan 决定。",
            minimal_edit="只修改该命令 JSON object；删除 extra_keys；补齐 missing_keys；保留脚本路径和 runner；value 使用用户输入或前序 stdout placeholder；不要改 SkillPlan 除非 SkillPlan 本身被判定错误。",
            details={
                "target_script": script_path,
                "expected_keys": sorted(declared_inputs),
                "actual_keys": sorted(actual_keys),
                "missing_keys": missing,
                "extra_keys": extra,
                "expected_payload_shape": {key: f"{{{{{key}}}}}" for key in sorted(declared_inputs)},
                "upstream_available_outputs": upstream,
            },
        ))

    if not runtime_matches:
        logger.info(
            "[Creator][skill_md] non-blocking runtime mismatch script=%s inferred_runtime=%s command=%s",
            script_path,
            getattr(entry, "runtime", ""),
            command_line,
        )

    return results


def _check_skill_md_fenced_command_contracts(
    *,
    content: str,
    blueprint_text: str,
    required_script_paths: list[str] | None = None,
) -> list[ContractCheckResult]:
    """Validate fenced command style for SKILL.md.

    这里是格式/可解析性校验，不做蓝图语义判断。
    任何内部异常都转换成 ContractCheckResult，避免直接崩溃。
    """
    results: list[ContractCheckResult] = []

    required = {
        path.replace("\\", "/").strip()
        for path in (required_script_paths or [])
        if isinstance(path, str) and path.replace("\\", "/").strip().startswith("scripts/")
    }

    mentioned = {
        path
        for path in _skill_local_paths_in_markdown(content)
        if isinstance(path, str) and path.startswith("scripts/")
    }

    scripts_to_check = sorted(required or mentioned)

    entries_by_path: dict[str, SkillPlanEntry] = {}
    try:
        parsed = parse_blueprint([{"role": "assistant", "content": blueprint_text}])
        if parsed.skill_plan:
            entries_by_path = {
                entry.path: entry
                for entry in parsed.skill_plan.files
                if entry.file_type == "script"
            }
    except Exception as exc:
        logger.warning("[Creator][skill_md] failed to parse blueprint SkillPlan for command validation: %s", exc)

    if entries_by_path:
        scripts_to_check = [entry.path for entry in entries_by_path.values() if entry.path in scripts_to_check]

    prior_outputs: set[str] = set()

    for script_path in scripts_to_check:
        try:
            commands = _extract_script_command_templates(content, script_path)
        except Exception as exc:
            results.append(ContractCheckResult(
                id="skill_md.command_block.extract_crashed",
                passed=False,
                target=script_path,
                message=f"{script_path} 命令块提取失败：{exc}",
                expected="能够从 SKILL.md 中提取该脚本对应的标准 ```bash fenced code block。",
                minimal_edit=(
                    f"为 {script_path} 添加独立、无缩进的标准命令块，例如：\n"
                    f"```bash\npython {script_path} '{{\"payload\":\"{{{{user_request}}}}\"}}'\n```"
                ),
            ))
            continue

        has_fenced = bool(commands)

        results.append(ContractCheckResult(
            id="skill_md.command_block.fenced_exists",
            passed=has_fenced,
            target=script_path,
            message=(
                f"{script_path} 已使用 ```bash fenced code block 表达可执行命令。"
                if has_fenced
                else f"{script_path} 缺少标准 ```bash fenced code block。"
            ),
            expected=(
                "真实脚本必须用标准 Markdown fenced code block 表示，例如：\n"
                f"```bash\npython {script_path} '{{\"payload\":\"{{{{user_request}}}}\"}}'\n```"
            ),
            minimal_edit=(
                f"为 {script_path} 添加独立、无缩进的 ```bash fenced block；"
                "不要只在正文中写“调用脚本”。"
            ),
        ))

        if not commands:
            continue

        try:
            entry = entries_by_path.get(script_path) or _skill_plan_entry_for_file(
                file_path=script_path,
                blueprint_text=blueprint_text,
            )
        except Exception as exc:
            logger.warning(
                "[Creator][skill_md] failed to infer SkillPlanEntry for %s: %s",
                script_path,
                exc,
            )
            entry = SkillPlanEntry(
                path=script_path,
                role="generic_script",
                file_type="python",
                purpose="Inferred fallback entry for command validation.",
                runtime="python",
                inputs=[],
                outputs=[],
                dependencies=[],
            )

        for command in commands:
            try:
                results.extend(_validate_command_is_single_shell_json_invocation(
                    command=command,
                    script_path=script_path,
                    entry=entry,
                    upstream_available_outputs=prior_outputs,
                ))
            except Exception as exc:
                logger.exception(
                    "[Creator][skill_md] command validation crashed script=%s command=%s",
                    script_path,
                    command,
                )
                results.append(ContractCheckResult(
                    id="skill_md.command_block.validation_crashed",
                    passed=False,
                    target=script_path,
                    message=f"{script_path} 命令块校验内部异常：{exc}",
                    expected="命令块应能被解析为 runner + scripts 路径 + JSON object argv。",
                    minimal_edit=(
                        f"将命令改为标准形式：\n"
                        f"```bash\npython {script_path} '{{\"payload\":\"{{{{user_request}}}}\"}}'\n```"
                    ),
                ))

        prior_outputs.update(entry.outputs or [])

    return results

def _extract_script_command_templates(skill_md: str, script_path: str) -> list[str]:
    """Return shell command templates in SKILL.md that invoke script_path."""
    commands: list[str] = []
    normalized_script_path = script_path.replace("\\", "/")

    for info, body in _iter_markdown_fenced_blocks(skill_md):
        if not _is_shell_fence_info(info):
            continue

        command = body.strip()
        if not command:
            continue

        normalized_command = command.replace("\\", "/")
        if normalized_script_path in normalized_command:
            commands.append(command)

    return commands


def _command_uses_json_argv(command: str) -> bool:
    return "{" in command and "}" in command


def _script_reads_json_argv(content: str, runtime: str = "python") -> bool:
    if runtime == "node":
        return "JSON.parse" in content and "process.argv" in content
    if runtime in {"bash", "shell"}:
        return "$1" in content or "${1" in content or "jq" in content
    return "json.loads" in content and "sys.argv" in content


def _script_uses_input_keys(content: str, keys: list[str]) -> tuple[bool, list[str]]:
    missing = [key for key in keys if key not in content]
    return not missing, missing


def _script_has_main_entry(content: str, runtime: str) -> bool:
    if runtime == "python":
        return "def main" in content and "__main__" in content
    if runtime == "node":
        return "process.argv" in content and "console.log" in content
    if runtime in {"bash", "shell"}:
        return ("$1" in content or "${1" in content) and ("echo" in content or "printf" in content or "print(json.dumps" in content)
    return True


def _validate_script_contract_static(*, file_path: str, content: str, skill_md: str) -> None:
    """Validate script source against SKILL.md contract locally.

    Creator 单文件阶段只做脚本入口级校验：
    - 禁止 fake/mock 壳代码
    - 检查模型/能力使用
    - 如果 SKILL.md 命令传入 JSON argv，脚本必须读取 JSON argv
    - 不强制脚本源码逐字出现每个 argv key

    字段级对齐交给最终 E2E：
    command JSON argv -> script stdout JSON -> next command placeholder。
    """
    _reject_fake_script_implementation(file_path, content)

    plan_entry = _skill_plan_entry_for_file(
        file_path=file_path,
        blueprint_text=skill_md,
    )

    _validate_configured_model_usage_static(
        file_path=file_path,
        content=content,
        skill_md=skill_md,
        plan_entry=plan_entry,
    )

    commands = _extract_script_command_templates(skill_md, file_path)
    if not commands:
        return

    command_results = _check_command_block_contract(file_path, commands, plan_entry)
    failed_command_results = [r for r in command_results if not r.passed]
    if failed_command_results:
        raise ValueError(
            "SKILL.md 命令块不合法，属于 workflow 局部合同问题，不要改脚本字段强制对齐 SkillPlan:\n"
            + _format_contract_checks(failed_command_results, passed=False)
        )

    json_argv_commands = [c for c in commands if _command_uses_json_argv(c)]
    if json_argv_commands and not _script_reads_json_argv(content, plan_entry.runtime):
        raise ValueError(
            f"{file_path} SKILL.md 命令传入 JSON argv，但脚本未按 runtime 读取 JSON argv。"
        )



def _validate_script_against_existing_skill_contract(skill_name: str, file_path: str, content: str) -> None:
    """Refuse saving scripts that do not match the current SKILL.md contract."""
    if not file_path.startswith("scripts/"):
        return
    skill_md_path = settings.skills_path / skill_name / "SKILL.md"
    if not skill_md_path.is_file():
        return
    skill_md = skill_md_path.read_text(encoding="utf-8")
    _validate_script_contract_static(file_path=file_path, content=content, skill_md=skill_md)


def _sample_value_for_placeholder(key: str) -> str:
    """Return realistic multilingual trial input.

    Creator is primarily used in Chinese/UTF-8 scenarios.  Trial inputs must
    include Chinese characters so PDF/FPDF/reportlab/font bugs are caught in the
    first file-level validation round, instead of leaking to later E2E/runtime.
    """
    lowered = key.lower()

    if any(token in lowered for token in ("prompt", "diffusion", "image", "picture", "photo", "scene")):
        return "一只会飞的橘猫在暖色夕阳下看着古城，cinematic watercolor style"

    if any(token in lowered for token in ("text", "content", "input", "query", "article", "story", "body", "summary")):
        return "测试输入：这是包含中文、English words 和标点符号的正文，用于验证 UTF-8/PDF 生成。"

    if any(token in lowered for token in ("topic", "theme", "subject", "title", "name")):
        return "中文主题：会飞的猫与古代小镇"
    return f"sample {key}"


def _render_trial_command_args(command: str, script_path: str) -> list[str] | None:
    """Extract argv for script_path from a SKILL.md command template."""
    rendered = re.sub(
        r"{{\s*([a-zA-Z_][\w-]*)\s*}}",
        lambda m: _sample_value_for_placeholder(m.group(1)),
        command,
    )
    try:
        parts = shlex.split(rendered)
    except ValueError:
        return None

    for idx, part in enumerate(parts):
        normalized = part.replace("\\", "/")
        if normalized == script_path or normalized.endswith("/" + script_path):
            return parts[idx + 1:]
    return None


def _dedupe_trial_arg_sets(arg_sets: list[list[str]]) -> list[list[str]]:
    seen: set[tuple[str, ...]] = set()
    deduped: list[list[str]] = []
    for args in arg_sets:
        key = tuple(args)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(args)
    return deduped


def _json_argv_text_optional_variants(args: list[str]) -> list[list[str]]:
    """Add trial cases proving optional text can be omitted or empty."""
    if len(args) != 1:
        return []
    try:
        payload = json.loads(args[0])
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict) or "text" not in payload:
        return []

    variants: list[list[str]] = []
    without_text = dict(payload)
    without_text.pop("text", None)
    if without_text:
        variants.append([json.dumps(without_text, ensure_ascii=False)])

    empty_text = dict(payload)
    empty_text["text"] = ""
    variants.append([json.dumps(empty_text, ensure_ascii=False)])
    return variants


def _trial_args_for_script(skill_md: str, file_path: str, content: str) -> list[list[str]]:
    commands = _extract_script_command_templates(skill_md, file_path)
    arg_sets = [args for cmd in commands if (args := _render_trial_command_args(cmd, file_path)) is not None]
    if not arg_sets and _script_reads_json_argv(content, runtime_for_language(language_for_path(file_path), file_type_for_path(file_path))):
        arg_sets = [[json.dumps({
            "prompt": _sample_value_for_placeholder("prompt"),
            "text": _sample_value_for_placeholder("text"),
            "topic": _sample_value_for_placeholder("topic"),
        }, ensure_ascii=False)]]
    if not arg_sets:
        arg_sets = [[]]

    expanded: list[list[str]] = []
    for args in arg_sets:
        expanded.append(args)
        expanded.extend(_json_argv_text_optional_variants(args))
    return _dedupe_trial_arg_sets(expanded)


def _format_trial_failure(*, args: list[str], returncode: int, stdout: str, stderr: str) -> str:
    return (
        "脚本试运行失败：\n"
        f"argv={args!r}\n"
        f"exit_code={returncode}\n"
        f"stdout={stdout[-4000:]}\n"
        f"stderr={stderr[-4000:]}"
    )


def _validate_image_payload_shape(payload: dict[str, Any]) -> bool:
    image_path = payload.get("image_path")
    if isinstance(image_path, str) and image_path.strip():
        return True

    image_paths = payload.get("image_paths")
    if isinstance(image_paths, list) and image_paths and all(isinstance(p, str) and p.strip() for p in image_paths):
        return True

    images = payload.get("images")
    if (
        isinstance(images, list)
        and images
        and all(isinstance(item, dict) and isinstance(item.get("image_path"), str) and item.get("image_path").strip() for item in images)
    ):
        return True

    return False



def _html_output_candidates(payload: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    html_path = payload.get("html_path")
    if isinstance(html_path, str) and html_path.strip():
        candidates.append(html_path.strip())
    asset_paths = payload.get("asset_paths")
    if isinstance(asset_paths, list):
        candidates.extend(path.strip() for path in asset_paths if isinstance(path, str) and path.strip())
    return candidates


def _validate_html_trial_outputs(payload: dict[str, Any], *, skill_dir: Path | None, args: list[str], stdout: str) -> None:
    checked: list[Path] = []
    for candidate in _html_output_candidates(payload):
        candidate_path = Path(candidate)
        if not candidate_path.is_absolute() and skill_dir is not None:
            if candidate.startswith("assets/"):
                candidate_path = (skill_dir / candidate_path).resolve()
            else:
                candidate_path = (skill_dir / "scripts" / candidate_path).resolve()
        checked.append(candidate_path)
        if skill_dir is not None:
            generated_root = (skill_dir / "assets" / "generated").resolve()
            try:
                candidate_path.relative_to(generated_root)
            except ValueError:
                raise ValueError(
                    "html_asset_builder 输出路径必须位于当前 Skill 的 assets/generated/ 下："
                    f"argv={args!r} stdout={stdout[-4000:]} checked={candidate_path}"
                )
        if candidate_path.is_file() and candidate_path.suffix.lower() in {".html", ".htm"}:
            text = candidate_path.read_text(encoding="utf-8", errors="replace").lower()
            if "<html" in text or "<!doctype html" in text:
                return
    raise ValueError(
        "html_asset_builder 试运行必须输出 html_path 或 asset_paths，并生成真实 HTML 文件："
        f"argv={args!r} stdout={stdout[-4000:]} checked={[str(p) for p in checked]}"
    )

def _validate_file_payload_shape(payload: dict[str, Any]) -> bool:
    pdf_path = payload.get("pdf_path")
    if isinstance(pdf_path, str) and pdf_path.strip():
        return True

    file_paths = payload.get("file_paths")
    if isinstance(file_paths, list) and file_paths and all(isinstance(p, str) and p.strip() for p in file_paths):
        return True

    return False



_LEGACY_OUTPUT_ALIASES: dict[str, tuple[str, ...]] = {
    "text": ("text_content", "story_text", "content"),
    "image_paths": ("image_path", "images"),
    "images": ("image_path", "image_paths"),
    "pdf_path": ("file_path", "file_paths"),
    "docx_path": ("file_path", "file_paths"),
    "pptx_path": ("file_path", "file_paths"),
    "html_path": ("file_path", "asset_paths"),
    "file_paths": ("file_path", "pdf_path", "docx_path", "pptx_path"),
}


def _payload_has_declared_output(payload: dict[str, Any], output_key: str) -> bool:
    if output_key in payload:
        return True
    return any(alias in payload for alias in _LEGACY_OUTPUT_ALIASES.get(output_key, ()))


def _payload_output_value(payload: dict[str, Any], output_key: str) -> Any:
    if output_key in payload:
        return payload.get(output_key)
    for alias in _LEGACY_OUTPUT_ALIASES.get(output_key, ()):
        if alias in payload:
            return payload.get(alias)
    return None

def _json_value_non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set)):
        return any(_json_value_non_empty(item) for item in value)
    if isinstance(value, dict):
        return any(_json_value_non_empty(item) for item in value.values())
    return True


def _validate_trial_stdout_json(*, stdout: str, content: str, args: list[str], role: str | None = None, skill_dir: Path | None = None, skill_plan_entry: dict[str, Any] | None = None) -> None:
    """Validate trial stdout with dynamic, field-name-agnostic rules.

    SkillPlan.outputs is a blueprint hint, not the sole runtime contract.  The
    hard requirements here are: stdout is a JSON object, it has at least one
    non-empty value, it does not report an error, and any file-looking values it
    declares point at real files.
    """
    stripped = (stdout or "").strip()
    if not stripped:
        raise ValueError(f"脚本试运行 stdout 为空：argv={args!r}")
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"脚本试运行 stdout 不是合法 JSON object：argv={args!r} stdout={stripped[-4000:]}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"脚本试运行 stdout 必须是 JSON object：argv={args!r} stdout={stripped[-4000:]}")
    if "error" in payload:
        raise ValueError(f"脚本试运行 stdout JSON 不得包含 error 字段：argv={args!r} stdout={stripped[-4000:]}")
    if not any(_json_value_non_empty(value) for value in payload.values()):
        raise ValueError(f"脚本试运行 stdout JSON 至少需要一个非空字段：argv={args!r} stdout={stripped[-4000:]}")

    try:
        if skill_dir is not None:
            validate_stdout_file_outputs(stripped, skill_dir=skill_dir, cwd=skill_dir / "scripts")
    except FileOutputValidationError as exc:
        raise ValueError(str(exc)) from exc



def _install_capability_dependencies(venv_python: Path, required_capabilities: list[str]) -> None:
    """Install platform-owned runtime dependencies for required capabilities.

    Creator trial runs execute generated scripts in a per-skill venv.  Scripts
    commonly import only ``backend.services.skill_runtime`` while the helper
    itself lazy-imports packages such as reportlab/docx/pptx/pypdf.  Static
    script import scanning cannot see those helper internals, so install the
    dependencies declared by the Creator tool registry before execution.
    """
    dependencies: list[str] = []
    seen: set[str] = set()
    for capability_name in required_capabilities or []:
        capability = get_tool_capability(capability_name)
        if capability is None:
            continue
        for dependency in capability.dependencies or []:
            if dependency and dependency not in seen:
                seen.add(dependency)
                dependencies.append(dependency)

    if not dependencies:
        return

    missing: list[str] = []
    dependency_import_names = {"python-docx": "docx", "python-pptx": "pptx"}
    for dependency in dependencies:
        module_name = dependency_import_names.get(dependency, dependency).replace("-", "_")
        check = subprocess.run(
            [
                str(venv_python),
                "-c",
                "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec(sys.argv[1]) is not None else 1)",
                module_name,
            ],
            capture_output=True,
            timeout=10,
        )
        if check.returncode != 0:
            missing.append(dependency)

    if not missing:
        return

    logger.info("skill-env: pip installing capability deps into venv: %s", missing)
    result = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--quiet", *missing],
        timeout=180,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"安装 capability 依赖失败 ({', '.join(missing)}): {result.stderr[:500]}")


def _trial_run_generated_script_with_plan(
    skill_name: str,
    file_path: str,
    content: str,
    *,
    role: str | None = None,
    skill_plan_entry: dict[str, Any] | None = None,
) -> None:
    try:
        _trial_run_generated_script(skill_name, file_path, content, role, skill_plan_entry)
    except TypeError as exc:
        # Some tests monkeypatch the trial runner with the historical
        # 4-argument signature; keep production role-aware calls while allowing
        # those narrow fakes to exercise the repair loop.
        if "positional" not in str(exc) and "unexpected keyword" not in str(exc):
            raise
        _trial_run_generated_script(skill_name, file_path, content, role)


def _trial_run_generated_script(
    skill_name: str,
    file_path: str,
    content: str,
    role: str | None = None,
    skill_plan_entry: dict[str, Any] | None = None,
) -> None:
    """Run a generated Python script before accepting it from Creator.

    Python scripts are executed in a temporary per-skill virtual environment.
    Before each trial run, imports are statically scanned and missing common
    third-party packages are installed into that venv, matching sandbox runtime
    behavior and allowing generation-test-repair-test loops to focus on real
    script defects instead of missing packages.
    """
    if not file_path.startswith("scripts/") or Path(file_path).suffix.lower() != ".py":
        return

    skill_md_path = settings.skills_path / skill_name / "SKILL.md"
    skill_md = skill_md_path.read_text(encoding="utf-8") if skill_md_path.is_file() else ""
    _validate_script_contract_static(file_path=file_path, content=content, skill_md=skill_md)

    with tempfile.TemporaryDirectory(prefix="creator-script-trial-") as tmp:
        skill_dir = Path(tmp) / skill_name
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            skill_md or f"---\nname: {skill_name}\ndescription: trial\n---\n",
            encoding="utf-8",
        )
        script_path = scripts_dir / Path(file_path).name
        script_path.write_text(content, encoding="utf-8")

        try:
            venv_python = _get_skill_venv_python(skill_dir)
            entry = _skill_plan_entry_for_file(
                file_path=file_path,
                blueprint_text=skill_md,
                role=role,
                skill_plan_entry=skill_plan_entry,
            )
            _install_capability_dependencies(venv_python, entry.required_capabilities)
            _scan_and_install_python_deps(script_path, venv_python)
        except RuntimeError as exc:
            raise ValueError(f"脚本试运行环境准备失败：{exc}") from exc

        for args in _trial_args_for_script(skill_md, file_path, content):
            try:
                proc = subprocess.run(
                    [str(venv_python), str(script_path), *args],
                    cwd=str(scripts_dir),
                    capture_output=True,
                    text=True,
                    timeout=_SCRIPT_TRIAL_TIMEOUT_SECONDS,
                    env={**_build_script_runtime_env(skill_dir), "SKILL_TRIAL_RUN": "1"},
                )
            except subprocess.TimeoutExpired as exc:
                raise ValueError(f"脚本试运行超时（超过 {_SCRIPT_TRIAL_TIMEOUT_SECONDS} 秒）：argv={args!r}") from exc
            if proc.returncode != 0:
                raise ValueError(_format_trial_failure(
                    args=args,
                    returncode=proc.returncode,
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                ))
            inferred_entry = _skill_plan_entry_for_file(
                file_path=file_path,
                blueprint_text=skill_md,
                skill_plan_entry=skill_plan_entry,
            )
            inferred_role = role or inferred_entry.role
            _validate_trial_stdout_json(
                stdout=proc.stdout,
                content=content,
                args=args,
                role=inferred_role,
                skill_dir=skill_dir,
                skill_plan_entry=skill_plan_entry,
            )


async def _repair_generated_file_with_feedback(
    *,
    prompt_messages: list[dict],
    model: str,
    file_path: str,
    previous_content: str,
    validation_error: str,
    targeted_repair: str = "",
    contract_text: str = "",
    passed_checks_text: str = "",
    failed_checks_text: str = "",
    repair_mode: str = "minimal_edit",
    skill_plan_entry: dict[str, Any] | None = None,
) -> str:
    """Ask the routed generation model to fix one file using validator feedback.

    The repaired model output is intentionally not validated here. Validation
    happens at the top of the generate-file retry loop so format errors in the
    repaired response consume one retry attempt and can be sent back as feedback.
    Previous content is passed as user-quoted data instead of an assistant turn
    so Markdown-wrapped failures are not reinforced as the desired answer shape.
    """
    is_script = file_path.startswith("scripts/")
    plan_entry = _skill_plan_entry_for_file(file_path=file_path, skill_plan_entry=skill_plan_entry) if is_script else None
    repair_language = plan_entry.language if plan_entry is not None else language_for_path(file_path)
    repair_runtime = plan_entry.runtime if plan_entry is not None else runtime_for_language(repair_language, file_type_for_path(file_path))
    runtime_rule = {
        "python": "Python: parse sys.argv[1] as JSON，保留 main() 入口并 print JSON。",
        "node": "Node: parse process.argv[2] as JSON，并 console.log(JSON.stringify(...))。",
        "bash": "Bash: parse $1 as JSON，并向 stdout 输出 JSON 或声明的文件产物。",
        "shell": "Shell: parse $1 as JSON，并向 stdout 输出 JSON 或声明的文件产物。",
    }.get(repair_runtime, "只输出该文件类型的原始内容；不得包含 Markdown fence。")
    output_contract = (
        f"Rewrite as raw {repair_language} source. Remove any fenced code blocks or file labels. Do NOT include Markdown fences, explanations, file headers, or multi-file content. Align JSON argv keys with the existing SKILL.md command placeholders; do not change the blueprint or SKILL.md. {runtime_rule}"
        if is_script
        else "最终只返回 SKILL.md 文件正文；不要在文件外层套 Markdown 代码块，不要输出 Creator 创建流程、确认清单或 `点击开始创建` 文案。"
    )
    local_edit_scope = (
        "保留其它已经正确的导入、函数、参数解析、stdout JSON 协议和业务逻辑。"
        if is_script
        else "保留已经正确的 frontmatter、章节结构、脚本命令示例和 reference 引用。"
    )
    if is_script:
        role_rule = ""
        if plan_entry is not None and plan_entry.role == "composite_generator":
            caps = set(plan_entry.required_capabilities or [])
            role_rule = "role=composite_generator：表示多能力组合脚本；具体 helper 由 SkillPlan.required_capabilities 决定；stdout 字段名由现有 SKILL.md 后续变量引用/业务语义决定，不要把 composite 固定理解为 text+image。"
            if {"text_generation", "image_generation"} <= caps:
                role_rule += " 当前合同同时要求文本和图片能力，必须保留 generate_text_with_llm 与 generate_stable_diffusion_image。"
        elif plan_entry is not None and plan_entry.role == "image_generator":
            role_rule = "role=image_generator：必须保留并调用 generate_stable_diffusion_image；stdout 输出非空 JSON，并使用现有 SKILL.md/脚本链路会消费的字段名；禁止占位图片或删除真实 helper。"
        elif plan_entry is not None and plan_entry.role == "text_generator":
            role_rule = "role=text_generator：必须调用 generate_text_with_llm 或平台 LLM，禁止调用图片 helper 或输出固定 template-only 文本。"
        elif plan_entry is not None and plan_entry.role == "pdf_builder":
            role_rule = "role=pdf_builder：默认是纯文件合并/排版/PDF 构建脚本，只需真实构建文件并在 stdout JSON 返回实际存在路径；不要因为全局 SKILL.md 提到模型就调用 LLM/IMAGE_MODEL，除非当前脚本的有效 required_capabilities 明确要求模型。"
        elif plan_entry is not None and plan_entry.role == "generic_script":
            role_rule = "role=generic_script：只能调用 SkillPlan.required_capabilities 声明的能力；若现有 SkillPlan.required_capabilities 已要求文本+图片，只能修当前脚本实现以匹配，禁止修改蓝图或 SKILL.md。"
        extra_rules = (
            "Python / Node / Bash 必须按 SkillPlan.runtime 读取单个 JSON argv，并且 JSON argv keys 匹配现有 SKILL.md 命令占位符；"
            "禁止生成 topicstring / tonehumorous / stylepopular-science 这类把 key、类型或默认值拼接起来的字段；"
            f"{role_rule}"
            "不要直接调用 /v1/images/generations，不要用 VISION_MODEL 生成图片，不要写 placeholder/模拟图片。"
        )
    else:
        extra_rules = (
            "如果蓝图包含 scripts/，必须包含调用对应 scripts/ 路径的 ```bash fenced code block；"
            "如果蓝图包含 references/，必须在正文中明确引用对应 reference 路径；"
            "不得复制 Creator UI 流程、待确认清单、文件创建面板说明或系统自动创建文件提示。"
        )

    repair_messages = [*prompt_messages]
    previous_for_prompt = previous_content[-16000:]
    previous_label = "待编辑草稿"
    if is_script and repair_mode == "strict_contract_rewrite":
        previous_for_prompt = (_extract_probable_python_source(previous_content) if repair_language == "python" else _drop_common_non_code_lines(_extract_single_wrapping_fence(previous_content) or previous_content)) or ""
        previous_label = "可参考的源码候选（已移除 Markdown 外壳；若为空，请根据原始任务重新生成）"
    repair_messages.append({
        "role": "user",
        "content": (
            f"以下是上一次生成但未通过校验的 {file_path} 内容。它可能包含错误示范（例如 Markdown fence 或 Creator 流程泄露），"
            f"不要模仿错误格式，只把它当作{previous_label}：\n"
            "<previous_content>\n"
            f"{previous_for_prompt}\n"
            "</previous_content>"
        ),
    })
    edit_strategy = (
        "这是 scripts/ 首次失败后的严格重写：不要走 minimal_edit，不要修补错误外壳；"
        "请根据合同、蓝图和骨架重新输出完整可运行源码。"
        if is_script and repair_mode == "strict_contract_rewrite"
        else (
            "请优先做局部修改：只修改校验意见指出的最小错误片段，"
            f"{local_edit_scope}"
            "修改完成后可以整合输出。"
        )
    )
    repair_messages.append({
        "role": "user",
        "content": (
            f"上一次生成的 {file_path} 没有通过校验模型/静态校验/试运行。"
            f"{edit_strategy}"
            f"{output_contract}\n"
            f"{extra_rules}\n\n"
            f"错误信息：\n{validation_error}"
            + (f"\n\n完整 contract（最终输出必须满足全部条目）：\n{contract_text}" if contract_text else "")
            + (f"\n\n已通过检查（必须保留，不要重写或删除对应内容）：\n{passed_checks_text}" if passed_checks_text else "")
            + (f"\n\n未通过检查（本轮只修这些项）：\n{failed_checks_text}" if failed_checks_text else "")
            + (f"\n\n本轮修复模式：{repair_mode}" if repair_mode else "")
            + ("\n- minimal_edit：只做最小编辑；strict_contract_rewrite：上一轮仍未通过同一 contract，必须重写目标小节但保留已通过项。")
            + ("\n- scripts/ 修复示例：Rewrite as raw <language> source, remove any fenced code blocks or file labels, align JSON argv keys with SkillPlan inputs." if is_script else "")
            + ("\n- 如果这是 scripts/ 文件且进入 strict_contract_rewrite：不要继续修补 Markdown 包裹草稿；必须重新输出会被直接保存的单文件源码，第一行必须是当前 runtime 的源码字符，全文不得出现 ``` 或 ~~~。" if is_script and repair_mode == "strict_contract_rewrite" else "")
            + (f"\n\n后端根据确定性错误生成的必做修复步骤：\n{targeted_repair}" if targeted_repair else "")
        ),
    })
    logger.info(
        "[Creator][model] phase=repair.request file=%s model=%s repair_mode=%s messages=%d previous_chars=%d contract_chars=%d failed_checks_chars=%d",
        file_path,
        model,
        repair_mode,
        len(repair_messages),
        len(previous_content or ""),
        len(contract_text or ""),
        len(failed_checks_text or ""),
    )
    repaired = await complete_chat_once(repair_messages, model)
    if is_script:
        normalized = _normalize_generated_file_content(file_path, repaired)
        logger.info(
            "[Creator][model] phase=repair.response file=%s model=%s repair_mode=%s raw_chars=%d normalized_chars=%d normalized=%s",
            file_path,
            model,
            repair_mode,
            len(repaired or ""),
            len(normalized or ""),
            normalized != repaired,
        )
        return normalized
    logger.info(
        "[Creator][model] phase=repair.response file=%s model=%s repair_mode=%s raw_chars=%d",
        file_path,
        model,
        repair_mode,
        len(repaired or ""),
    )
    return repaired


def _parse_validator_json_object(text: str) -> dict | None:
    stripped = (text or "").strip()
    if stripped.startswith("```json"):
        stripped = stripped[7:].strip()
    if stripped.startswith("```"):
        stripped = stripped[3:].strip()
    if stripped.endswith("```"):
        stripped = stripped[:-3].strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _deterministic_failed_check_ids(failed_checks_text: str) -> set[str]:
    ids: set[str] = set()
    for match in re.finditer(r"^-\s+([^\s]+)\s+target=", failed_checks_text or "", re.M):
        ids.add(match.group(1))
    return ids


def _filter_validator_failed_checks(failed_checks: list[Any], failed_checks_text: str) -> list[Any]:
    """Keep only model failed_checks backed by deterministic failed checks."""
    allowed_ids = _deterministic_failed_check_ids(failed_checks_text)
    if not allowed_ids:
        return []
    filtered: list[Any] = []
    for check in failed_checks:
        if not isinstance(check, dict):
            continue
        check_id = str(check.get("id") or "")
        if check_id in allowed_ids:
            filtered.append(check)
    return filtered


_MISSING_SKILL_SCRIPT_BLOCK_RE = re.compile(
    r"SKILL\.md 缺少调用 (?P<script>scripts/[A-Za-z0-9_./-]+) 的可执行 Markdown 命令块"
)
_MISSING_SKILL_REFERENCE_RE = re.compile(
    r"SKILL\.md 缺少对参考资料 (?P<reference>references/[A-Za-z0-9_./-]+) 的引用"
)


def _targeted_generated_file_repair_instructions(*, file_path: str, deterministic_error: str) -> str:
    """Return deterministic, actionable instructions for recurring validation failures."""
    error_text = deterministic_error or ""

    if file_path == "SKILL.md":
        if (
                "workflow_missing" in error_text
                or "没有可执行 bash/sh/shell 命令块" in error_text
                or "缺少调用" in error_text
                or "script_command.exists" in error_text
                or "command_block.fenced_exists" in error_text
                or "fenced code block" in error_text
        ):
            return (
                "按严格 Markdown 执行规范修复 SKILL.md："
                "蓝图真实规划的每个 scripts/ 文件必须有一个标准、独立、无缩进的 ```bash fenced code block；"
                "每个 block 内只放一条命令；命令必须直接调用 scripts/ 路径；"
                "脚本路径后必须传入 json.loads 可解析的 JSON object argv；"
                "所有动态占位符必须作为 JSON 字符串值出现。"
                "不要使用 '''bash；不要只写行内 scripts/*.py；不要把示例/反例路径当成真实脚本。"
            )

        if "frontmatter" in error_text:
            return (
                "修复 SKILL.md YAML frontmatter：文件开头必须是 --- / name / description / ---；"
                "不要求文件末尾追加 ---。只修文件开头 frontmatter。"
            )

        if "蓝图意图不一致" in error_text or "intent" in error_text or "workflow" in error_text or "file_plan" in error_text:
            return (
                "按模型审查意见最小修复 SKILL.md：必须覆盖蓝图真实规划任务、真实 scripts、真实 references、真实 assets、workflow 顺序、输入输出数据流和最终产物；"
                "真实脚本必须使用 ```bash fenced code block；"
                "JSON 配置或 stdout 示例必须使用 ```json fenced code block；"
                "不要把示例/反例路径当成真实文件。"
            )

        return (
            "修复 SKILL.md：保持其作为最终 Skill 使用说明；覆盖蓝图真实任务和 workflow；"
            "删除 Creator 创建流程；确保真实脚本命令块使用 ```bash fence，JSON 示例使用 ```json fence。"
        )

    if file_path.startswith("scripts/"):
        lower_error = error_text.lower()

        if (
            "unicodeencodeerror" in lower_error
            and "latin-1" in lower_error
            and (
                "fpdf" in lower_error
                or "pdf.output" in lower_error
                or "build_pdf" in lower_error
                or file_path.endswith("build_pdf.py")
            )
        ):
            return (
                "这是 PDF 中文/UTF-8 编码根因错误，不是 stdout/ensure_ascii 问题。"
                "当前失败来自 fpdf 默认核心字体（Helvetica/Arial/Times/Courier）只能写 latin-1，不能写中文。\n"
                "必须修复真实 PDF 生成逻辑：\n"
                "1. 禁止继续使用 FPDF 默认 Helvetica/Arial/Times/Courier 直接写 payload 文本；\n"
                "2. 优先改用 reportlab.pdfgen.canvas，并注册 reportlab.pdfbase.cidfonts.UnicodeCIDFont('STSong-Light') 后 setFont('STSong-Light', size)；\n"
                "3. 或使用 reportlab.pdfbase.ttfonts.TTFont / fpdf2 add_font 加载可用 TTF 字体后再写文本；\n"
                "4. 保留 sys.argv[1] JSON 输入协议，继续读取现有 SkillPlan inputs，不要改 SKILL.md 或蓝图；\n"
                "5. stdout 必须输出真实存在的 pdf_path/file_paths，且文件必须是合法 PDF；\n"
                "6. 不允许通过 try/except 输出 {'error': ...}、{}、{'pdf_path': ''} 或空 file_paths 来绕过试运行；\n"
                "7. 修复目标是让包含中文的 article_text/story_text/text/content 能成功写入 PDF。"
            )

        if (
            "script.pdf_builder.unicode_text_supported" in error_text
            or "latin-1 UnicodeEncodeError" in error_text
            or "fpdf 默认核心字体" in error_text
            or "PDF 构建脚本必须能处理中文" in error_text
        ):
            return (
                "按 pdf_builder Unicode 合同修复："
                "把 fpdf 默认字体方案替换为支持中文/UTF-8 的 PDF 方案。"
                "推荐使用 reportlab + UnicodeCIDFont('STSong-Light')；"
                "或 reportlab + TTFont；"
                "或 fpdf2 + add_font 加载 TTF。"
                "禁止只改 ensure_ascii、禁止吞异常输出 error/{}、禁止返回空 pdf_path。"
            )

        if (
            "stdout JSON 不得包含 error 字段" in error_text
            or "stdout JSON 至少需要一个非空字段" in error_text
            or "stdout={}" in error_text
            or '"pdf_path": ""' in error_text
            or "'pdf_path': ''" in error_text
        ):
            return (
                "不要通过 try/except 输出 error、{}、空 pdf_path 或空 file_paths 来绕过试运行。"
                "必须修复导致异常的真实代码路径，并输出真实存在的文件路径。"
                "如果当前脚本是 PDF 构建脚本，重点检查是否仍在用 fpdf 默认 Helvetica/Arial/Times/Courier 写中文；"
                "需要改为支持中文/UTF-8 的 PDF 生成方案。"
            )

        if "Markdown 代码块或多文件包" in error_text or "script.raw_source.single_file" in error_text:
            return (
                "本轮必须把上一次内容改成单个裸脚本源码：删除所有 ``` fence、```python/```text 标签、"
                "文件路径标题、写入文件标签、解释性文字和多文件包内容；最终响应第一个字符应是脚本源码字符。"
            )

        if "forbidden_image_generation" in error_text or "调用了图片生成 helper" in error_text:
            return (
                "当前脚本的 SkillPlan forbidden_capabilities 禁止 image_generation，因此 validator 禁止调用 generate_stable_diffusion_image。"
                "蓝图和 SKILL.md 确定后不能由后台修复流程修改；只能修当前脚本，使其与既有 role、required_capabilities 和 SKILL.md 数据流一致。"
            )

        if (
            "script.required_capabilities.called" in error_text
            or "未调用这些 required_capabilities" in error_text
            or "没有调用这些 required_capabilities" in error_text
        ):
            return (
                "按当前脚本的 SkillPlan role + 有效 required_capabilities 补齐真实能力调用：包含 image_generation 必须调用 generate_stable_diffusion_image；"
                "包含 text_generation 必须调用 generate_text_with_llm 或平台 LLM；pdf_builder/exporter 默认只需真实创建文件，并在 stdout JSON 任意业务字段中返回路径，不要因 SKILL.md 全局模型说明而补模型调用。"
                "PDF 构建脚本必须支持中文/UTF-8 文本，禁止用 fpdf 默认核心字体写 payload 文本。"
                "禁止返回固定 f-string/template-only 文本或 placeholder；蓝图和 SKILL.md 确定后只能修当前脚本。"
            )

        if "试运行" in error_text or "JSON 参数" in error_text or "合法 Python" in error_text:
            return (
                "按脚本合同修复：保持单文件源码，修正语法/参数解析/运行错误；"
                "如果 SKILL.md 命令传 JSON，脚本必须读取 sys.argv[1] 并 json.loads，stdout 输出结构化 JSON，至少包含一个非空字段；"
                "字段名由现有 SKILL.md 变量消费关系决定，不要修改蓝图或 SKILL.md。"
                "不要通过 try/except 输出 error、{}、空路径来掩盖真实异常；必须修复导致试运行失败的代码。"
            )

    if file_path.startswith("assets/"):
        if "contract 未通过" in error_text or "asset" in error_text or "JSON" in error_text:
            return (
                "按 asset 合同修复：只输出当前资源文件内容；确保非空、JSON 可解析，"
                "删除 Creator 流程、多文件包和运行时代码。"
            )

    if file_path.startswith("references/"):
        if "contract 未通过" in error_text or "多文件包" in error_text or "Creator" in error_text:
            return (
                "按 reference 合同修复：只输出当前参考资料 Markdown 正文；"
                "删除 Creator 流程、写入文件标签、多文件包和其它文件路径标题。"
            )

    return ""

async def _run_generated_file_validator_round(
    *,
    file_path: str,
    content: str,
    deterministic_error: str,
    requested_model: str,
    targeted_repair: str = "",
    contract_text: str = "",
    passed_checks_text: str = "",
    failed_checks_text: str = "",
    repair_mode: str = "minimal_edit",
) -> dict:
    """Ask the validator model for actionable repair feedback for the coder."""
    route = route_model(
        VALIDATOR_TASK,
        requested_model=requested_model,
        reason=f"creator generated file validation: {file_path}",
    )
    _log_creator_model_usage(
        phase="validator.route",
        file_path=file_path,
        route=route,
        extra=f"requested_generation_model={requested_model} repair_mode={repair_mode} content_chars={len(content)}",
    )
    messages = [
        {
            "role": "system",
            "content": (
                "你是 Creator 生成文件校验模型，只输出严格 JSON object。"
                "你不改代码，只给 coder 可执行的局部修复意见。"
                "重要规则：Markdown 执行命令必须使用标准 ```bash fenced code block；"
                "机器可读 JSON 示例必须使用标准 ```json fenced code block；"
                "不要使用 '''bash 或 '''json；不要把行内 scripts/*.py 当成执行命令。"
                "SKILL.md YAML frontmatter 只需要在文件开头用 --- 开启，并在 metadata 后用 --- 关闭；"
                "不要求整个文件末尾再出现 ---。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"目标文件：{file_path}\n"
                "后端确定性校验/试运行错误：\n"
                f"{deterministic_error}\n\n"
                + (f"完整 contract：\n{contract_text}\n\n" if contract_text else "")
                + (f"已通过检查（repair 时必须保留）：\n{passed_checks_text}\n\n" if passed_checks_text else "")
                + (f"未通过检查（repair 只修这些项）：\n{failed_checks_text}\n\n" if failed_checks_text else "")
                + (f"本轮修复模式：{repair_mode}\n\n" if repair_mode else "")
                + (f"后端根据该错误生成的必做修复步骤：\n{targeted_repair}\n\n" if targeted_repair else "")
                + "候选内容：\n"
                "```text\n"
                f"{content[-12000:]}\n"
                "```\n\n"
                "请输出 JSON："
                "{\"passed\": false, \"issues\": [\"...\"], "
                "\"failed_checks\": [{\"id\": \"...\", \"target\": \"...\", \"expected\": \"...\", \"minimal_edit\": \"...\"}], "
                "\"preserve\": [\"已通过检查对应内容\"], "
                "\"repair_instructions\": \"给 coder 的局部修改指令；如果上方有必做修复步骤，必须复述并细化这些步骤，不得改用其它占位符或省略必需的 fenced block。若有 Markdown fence/多文件包，明确要求只返回目标脚本源码本身，不要 fence/说明/写入文件标签。\"}"
            ),
        },
    ]
    try:
        text = await complete_chat_once(messages, route.model)
        logger.info(
            "[Creator][model] phase=validator.response file=%s model=%s chars=%d repair_mode=%s",
            file_path,
            route.model,
            len(text or ""),
            repair_mode,
        )
    except Exception as exc:  # pragma: no cover - defensive fallback
        logger.warning("Creator file validator failed; using deterministic feedback: %s", exc)
        return {
            "passed": False,
            "issues": [deterministic_error],
            "repair_instructions": deterministic_error,
            "model": route.model,
        }

    data = _parse_validator_json_object(text)
    if not data:
        return {
            "passed": False,
            "issues": [deterministic_error],
            "repair_instructions": deterministic_error,
            "model": route.model,
        }

    issues = data.get("issues") if isinstance(data.get("issues"), list) else []
    raw_failed_checks = data.get("failed_checks") if isinstance(data.get("failed_checks"), list) else []
    failed_checks = _filter_validator_failed_checks(raw_failed_checks, failed_checks_text)
    preserve = data.get("preserve") if isinstance(data.get("preserve"), list) else []
    instructions = str(data.get("repair_instructions") or data.get("feedback") or deterministic_error)
    filtered_issues, instructions = _filter_validator_model_call_misjudgements(
        file_path=file_path,
        deterministic_error=deterministic_error,
        failed_checks_text=failed_checks_text,
        issues=issues,
        instructions=instructions,
    )
    return {
        "passed": bool(data.get("passed", data.get("valid", False))) and not filtered_issues and not failed_checks,
        "issues": filtered_issues,
        "failed_checks": failed_checks,
        "preserve": [str(item) for item in preserve],
        "repair_instructions": instructions,
        "model": route.model,
    }



def _filter_validator_model_call_misjudgements(
    *,
    file_path: str,
    deterministic_error: str,
    failed_checks_text: str,
    issues: list[Any],
    instructions: str,
) -> tuple[list[str], str]:
    """Remove validator feedback that invents model-call requirements.

    The validator model may over-generalize from SKILL.md prose. Only the
    deterministic contract for this script may introduce required model calls.
    """
    issue_strings = [str(item) for item in issues]
    if not file_path.startswith("scripts/"):
        return issue_strings, instructions
    deterministic_scope = f"{deterministic_error}\n{failed_checks_text}"
    deterministic_mentions_model_requirement = bool(re.search(
        r"text_generation|image_generation|generate_text_with_llm|generate_stable_diffusion_image|LLM|TEXT_MODEL|IMAGE_MODEL|模型",
        deterministic_scope,
        re.IGNORECASE,
    ))
    if deterministic_mentions_model_requirement:
        return issue_strings, instructions
    model_error_re = re.compile(r"必须.*(?:模型|LLM|TEXT_MODEL|IMAGE_MODEL|generate_text_with_llm|generate_stable_diffusion_image)|(?:未|没有)调用.*(?:模型|LLM|TEXT_MODEL|IMAGE_MODEL)", re.IGNORECASE)
    filtered_issues = [item for item in issue_strings if not model_error_re.search(item)]
    if model_error_re.search(instructions or ""):
        instructions = deterministic_error
    return filtered_issues, instructions

def _format_file_validator_feedback(deterministic_error: str, validator_report: dict, targeted_repair: str = "") -> str:
    issues = validator_report.get("issues") or []
    issue_text = "\n".join(f"- {issue}" for issue in issues) if issues else "- （校验模型未返回额外问题）"
    failed_checks = validator_report.get("failed_checks") or []
    failed_check_text = (
        "\n".join(f"- {json.dumps(check, ensure_ascii=False)}" for check in failed_checks)
        if failed_checks else "- （校验模型未返回结构化 failed_checks）"
    )
    return (
        "后端确定性校验/试运行错误：\n"
        f"{deterministic_error}\n\n"
        + (f"后端确定性修复指令：\n{targeted_repair}\n\n" if targeted_repair else "")
        + f"校验模型：{validator_report.get('model', '')}\n"
        "校验模型问题列表：\n"
        f"{issue_text}\n\n"
        "校验模型结构化 failed_checks：\n"
        f"{failed_check_text}\n\n"
        "校验模型给 coder 的修复意见：\n"
        f"{validator_report.get('repair_instructions') or deterministic_error}"
    )


def _is_valid_normalized_script_source(file_path: str, content: str) -> bool:
    """Return whether content is safe to accept as the requested raw script.

    This helper is intentionally narrower than the full script validator: it only
    checks that deterministic Markdown/bundle cleanup produced one raw source
    file.  Full fake-implementation, contract, dependency and trial-run checks
    still happen in the normal validation pipeline.
    """
    stripped = content.strip()
    if not stripped or "```" in stripped or "~~~" in stripped or _MULTI_FILE_MARKER_RE.search(stripped):
        return False

    if Path(file_path).suffix.lower() == ".py":
        try:
            ast.parse(stripped)
        except SyntaxError:
            return False

    return True


def _extract_single_wrapping_fence(content: str) -> str | None:
    """Extract a code block only when it wraps the entire model response.

    Models often wrap repaired scripts in ```text fences, sometimes with CRLF
    line endings or a closing fence that is longer than the opener.  This parser
    intentionally accepts only a whole-response fence: any prose before/after the
    block, or any non-fence trailing line, returns None and lets validation reject
    the ambiguous output.
    """
    stripped = content.strip().lstrip("\ufeff")
    lines = stripped.splitlines()
    if len(lines) < 2:
        return None

    opening = lines[0].strip()
    opening_match = re.match(r"^(`{3,}|~{3,})[^`~]*$", opening)
    if not opening_match:
        return None

    fence = opening_match.group(1)
    fence_char = fence[0]
    min_fence_len = len(fence)
    closing = lines[-1].strip()
    if not re.fullmatch(rf"{re.escape(fence_char)}{{{min_fence_len},}}", closing):
        return None

    return "\n".join(lines[1:-1]).strip()


def _extract_only_fenced_block(content: str) -> str | None:
    """Extract the body only when exactly one fenced block appears in content."""
    lines = content.strip().lstrip("\ufeff").splitlines()
    blocks: list[str] = []
    idx = 0
    while idx < len(lines):
        opening = lines[idx].strip()
        opening_match = re.match(r"^(`{3,}|~{3,})[^`~]*$", opening)
        if not opening_match:
            idx += 1
            continue

        fence = opening_match.group(1)
        fence_char = fence[0]
        min_fence_len = len(fence)
        body: list[str] = []
        idx += 1
        while idx < len(lines):
            closing = lines[idx].strip()
            if re.fullmatch(rf"{re.escape(fence_char)}{{{min_fence_len},}}", closing):
                blocks.append("\n".join(body).strip())
                break
            body.append(lines[idx])
            idx += 1
        else:
            return None

        if len(blocks) > 1:
            return None
        idx += 1

    if len(blocks) != 1:
        return None
    return blocks[0]


def _extract_first_fenced_block(content: str) -> str | None:
    """Extract the first fenced block body, or None if no complete block exists."""
    lines = content.strip().lstrip("\ufeff").splitlines()
    idx = 0
    while idx < len(lines):
        opening = lines[idx].strip()
        opening_match = re.match(r"^(`{3,}|~{3,})[^`~]*$", opening)
        if not opening_match:
            idx += 1
            continue
        fence = opening_match.group(1)
        fence_char = fence[0]
        min_fence_len = len(fence)
        body: list[str] = []
        idx += 1
        while idx < len(lines):
            closing = lines[idx].strip()
            if re.fullmatch(rf"{re.escape(fence_char)}{{{min_fence_len},}}", closing):
                return "\n".join(body).strip()
            body.append(lines[idx])
            idx += 1
        return None
    return None


def _drop_common_non_code_lines(text: str) -> str:
    """Remove common chat/file-label prose that models place around scripts."""
    drop_patterns = [
        r"^\s*下面是",
        r"^\s*以下是",
        r"^\s*(?:文件|路径)[:：]",
        r"^\s*写入文件[:：]",
        r"^\s*#+\s*scripts/",
        r"^\s*`?scripts/[^`]+`?\s*$",
    ]
    cleaned: list[str] = []
    for line in text.strip().splitlines():
        if any(re.search(pattern, line, flags=re.IGNORECASE) for pattern in drop_patterns):
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def _looks_like_python_source(text: str) -> bool:
    """Return True for probable Python source without requiring valid syntax yet."""
    stripped = text.strip()
    if not stripped or "```" in stripped or "~~~" in stripped or _MULTI_FILE_MARKER_RE.search(stripped):
        return False
    return bool(re.search(
        r"(?m)^\s*(?:import\s+|from\s+|def\s+|class\s+|if __name__\s*==\s*['\"]__main__['\"]|#!/usr/bin/env python|#)",
        stripped,
    ))


def _extract_probable_python_source(content: str) -> str | None:
    """Extract a raw Python source candidate before syntax validation."""
    stripped = content.strip().lstrip("\ufeff")
    candidates: list[str] = []

    normalized = stripped
    for _ in range(3):
        wrapping = _extract_single_wrapping_fence(normalized)
        if wrapping is None:
            break
        normalized = wrapping.strip()
        candidates.append(normalized)

    only_block = _extract_only_fenced_block(stripped)
    if only_block is not None:
        candidates.append(only_block.strip())

    # Use the first block only when the response does not look like an explicit
    # multi-file bundle.  Multi-file bundles are rejected rather than guessed.
    if not _MULTI_FILE_MARKER_RE.search(stripped) and not re.search(r"(?im)^\s*写入文件[:：]", stripped):
        first_block = _extract_first_fenced_block(stripped)
        if first_block is not None:
            candidates.append(first_block.strip())

    candidates.append(stripped)

    seen: set[str] = set()
    for candidate in candidates:
        cleaned = _drop_common_non_code_lines(candidate)
        if cleaned in seen:
            continue
        seen.add(cleaned)
        if _looks_like_python_source(cleaned):
            return cleaned
    return None


def _normalize_generated_file_content(file_path: str, content: str) -> str:
    """Normalize model output while keeping script extraction conservative."""
    if file_path.startswith("scripts/"):
        stripped = content.strip()
        if Path(file_path).suffix.lower() == ".py":
            extracted = _extract_probable_python_source(stripped)
            if extracted:
                return extracted

        normalized = stripped
        for _ in range(3):
            wrapping_fence = _extract_single_wrapping_fence(normalized)
            if wrapping_fence is None:
                break
            normalized = wrapping_fence.strip()
            if _is_valid_normalized_script_source(file_path, normalized):
                return normalized

        only_block = _extract_only_fenced_block(stripped)
        if only_block is not None:
            normalized = only_block.strip()
            if _is_valid_normalized_script_source(file_path, normalized):
                return normalized

        candidate = _strip_orphan_trailing_fence(stripped)
        if _is_valid_normalized_script_source(file_path, candidate):
            return candidate
        return candidate

    extracted = _extract_target_file_from_bundle(content, file_path)
    return _strip_code_fence(extracted if extracted is not None else content)



def _trim_source_to_runtime_entrypoint(file_path: str, content: str, skill_plan_entry: dict[str, Any] | None = None) -> str:
    """Drop leading/trailing prose around a probable runtime source entrypoint."""
    if not file_path.startswith("scripts/"):
        return content.strip()
    plan_entry = _skill_plan_entry_for_file(file_path=file_path, skill_plan_entry=skill_plan_entry)
    text = _strip_orphan_trailing_fence(content.strip().lstrip("\ufeff"))
    lines = text.splitlines()

    start_patterns: list[str]
    end_patterns: list[str]
    if plan_entry.runtime == "node":
        start_patterns = [r"^\s*(?:const|let|var)\s+", r"^\s*function\s+", r"^\s*#!/usr/bin/env\s+node"]
        end_patterns = [r"console\.log\s*\("]
    elif plan_entry.runtime in {"bash", "shell"}:
        start_patterns = [r"^\s*#!/", r"^\s*set\s+-", r"^\s*payload_json=", r"^\s*[A-Za-z_][A-Za-z0-9_]*="]
        end_patterns = [r"^\s*(?:echo|printf|python\s+-c)\b"]
    else:
        start_patterns = [r"^\s*(?:import\s+|from\s+|def\s+|class\s+|#!/usr/bin/env\s+python)"]
        end_patterns = [r"main\s*\(\s*\)"]

    start_idx = 0
    for idx, line in enumerate(lines):
        if any(re.search(pattern, line) for pattern in start_patterns):
            start_idx = idx
            break
    trimmed = lines[start_idx:]

    end_idx = len(trimmed)
    for idx in range(len(trimmed) - 1, -1, -1):
        line = trimmed[idx]
        if any(re.search(pattern, line) for pattern in end_patterns):
            end_idx = idx + 1
            break
    return "\n".join(trimmed[:end_idx]).strip()

_REFERENCE_EXECUTABLE_SCRIPT_CMD_RE = re.compile(
    r"(?m)^\s*(?:python|python3|node|bash|sh)\s+scripts/[A-Za-z0-9_./-]+\b"
)

_MARKDOWN_FENCED_BLOCK_RE = re.compile(
    r"```(?P<lang>[A-Za-z0-9_-]*)\s*\n(?P<body>.*?)```",
    re.DOTALL,
)


def _sanitize_reference_markdown(content: str) -> str:
    """Keep reference examples non-executable.

    references/*.md may contain examples, including code examples.
    But executable shell blocks that call scripts/** must not be preserved as
    ```bash / ```sh / ```shell, because references are documentation resources,
    not workflow sources.
    """
    def repl(match: re.Match[str]) -> str:
        lang = (match.group("lang") or "").strip().lower()
        body = match.group("body") or ""

        if lang in {"bash", "sh", "shell"} and _REFERENCE_EXECUTABLE_SCRIPT_CMD_RE.search(body):
            return "```text\n" + body.strip() + "\n```"

        return match.group(0)

    return _MARKDOWN_FENCED_BLOCK_RE.sub(repl, content)

def _sanitize_generated_file_content(
    file_path: str,
    content: str,
    role: str | None = None,
    skill_plan_entry: dict[str, Any] | None = None,
) -> str:
    """Normalize model output into exactly the requested file content.

    references/*.md are documentation resources. If a reference contains
    executable-looking bash/sh/shell blocks calling scripts/**, demote those
    blocks to text before validation/writing.
    """
    if file_path.startswith("scripts/") and _MULTI_FILE_MARKER_RE.search(content) and _extract_only_fenced_block(content) is None:
        sanitized = content.strip()
    else:
        sanitized = _normalize_generated_file_content(file_path, content)
        sanitized = _trim_source_to_runtime_entrypoint(
            file_path,
            sanitized,
            skill_plan_entry=skill_plan_entry,
        )

    if file_path.startswith("references/") or role == "reference":
        sanitized = _sanitize_reference_markdown(sanitized)

    _validate_generated_file_content(
        file_path,
        sanitized,
        role=role,
        skill_plan_entry=skill_plan_entry,
    )

    return sanitized


def _strip_code_fence(content: str) -> str:
    """Strip wrapping code-fence markers that a model may output despite instructions.

    Handles:
      ```python\\n<code>\\n```
      ```\\n<code>\\n```
      ~~~~\\n<code>\\n~~~~
    """
    stripped = content.strip()
    # Opening fence with optional language tag
    m_open = re.match(r"^(`{3,}|~{3,})[^\n]*\n", stripped)
    if m_open:
        fence_char = stripped[0]
        rest = stripped[m_open.end():]
        close_pat = re.compile(r"\n" + re.escape(fence_char) + r"{3,}\s*$")
        m_close = close_pat.search(rest)
        if m_close:
            return rest[: m_close.start()].strip()
        return rest.strip()
    return stripped




def _script_generation_skeleton(
    file_path: str,
    purpose: str,
    blueprint_text: str,
    *,
    role: str | None = None,
    skill_plan_entry: dict[str, Any] | None = None,
) -> str:
    """Return a runtime-aware scaffold selected by SkillPlan role/runtime."""
    plan_entry = _skill_plan_entry_for_file(
        file_path=file_path,
        purpose=purpose,
        blueprint_text=blueprint_text,
        role=role,
        skill_plan_entry=skill_plan_entry,
    )
    input_keys = list(plan_entry.inputs or ["payload"])
    effective_required_capabilities = _effective_required_capabilities_for_script(plan_entry)
    py_value_expr = " or ".join(f"payload.get({key!r})" for key in input_keys) + " or ''"
    js_value_expr = " || ".join(f"payload[{json.dumps(key)}]" for key in input_keys) + " || ''"
    bash_py_expr = " or ".join(f"p.get({key!r})" for key in input_keys) + " or ''"

    if plan_entry.runtime == "node":
        if {"text_generation", "image_generation"} <= set(effective_required_capabilities):
            return (
                "必须使用下面的 node composite_generator skeleton；先调用平台 generate_text_with_llm，再调用 generate_stable_diffusion_image，stdout 只能 console.log JSON 字符串：\n"
                "const { spawnSync } = require('child_process');\n"
                "const payload = process.argv[2] ? JSON.parse(process.argv[2]) : {};\n"
                "function pyEval(code, arg) {\n"
                "  const proc = spawnSync(process.env.PYTHON || 'python', ['-c', code, arg], { encoding: 'utf8' });\n"
                "  if (proc.status !== 0) throw new Error(proc.stderr || 'platform helper failed');\n"
                "  return JSON.parse(proc.stdout);\n"
                "}\n"
                "function run(payload) {\n"
                f"  const prompt = String({js_value_expr}).trim();\n"
                "  const textCode = `from backend.services.skill_runtime import generate_text_with_llm\\nimport json,sys\\nprint(json.dumps({'text': generate_text_with_llm(sys.argv[1])}, ensure_ascii=False))`;\n"
                "  const textResult = pyEval(textCode, prompt);\n"
                "  const imagePrompt = textResult.text || prompt;\n"
                "  const imageCode = `from backend.services.skill_runtime import generate_stable_diffusion_image\\nimport json,sys\\nresult = generate_stable_diffusion_image(sys.argv[1], filename_prefix='generated')\\nprint(json.dumps(result, ensure_ascii=False))`;\n"
                "  const imageResult = pyEval(imageCode, imagePrompt);\n"
                "  return { text: textResult.text, text_with_image_prompts: [{ text: textResult.text, image_prompt: imagePrompt }], image_paths: [imageResult.image_path].filter(Boolean), images: [imageResult] };\n"
                "}\n"
                "console.log(JSON.stringify(run(payload)));"
            )
        if plan_entry.role == "image_generator":
            return (
                "必须使用下面的 node image_generator skeleton；通过 Python 平台 helper 生成图片，stdout 只能 console.log JSON 字符串：\n"
                "const { spawnSync } = require('child_process');\n"
                "const payload = process.argv[2] ? JSON.parse(process.argv[2]) : {};\n"
                "function run(payload) {\n"
                f"  const desc = String({js_value_expr}).trim();\n"
                "  const helper = `from backend.services.skill_runtime import generate_stable_diffusion_image\\nimport json,sys\\nresult = generate_stable_diffusion_image(sys.argv[1], filename_prefix='generated')\\nprint(json.dumps(result, ensure_ascii=False))`;\n"
                "  const proc = spawnSync(process.env.PYTHON || 'python', ['-c', helper, desc], { encoding: 'utf8' });\n"
                "  if (proc.status !== 0) throw new Error(proc.stderr || 'generate_stable_diffusion_image failed');\n"
                "  const result = JSON.parse(proc.stdout);\n"
                "  const image_paths = [];\n"
                "  const images = [];\n"
                "  image_paths.push(result.image_path);\n"
                "  images.push(result);\n"
                "  return { image_paths: image_paths.filter(Boolean), images };\n"
                "}\n"
                "console.log(JSON.stringify(run(payload)));"
            )
        if plan_entry.role == "pdf_builder" or "pdf_generation" in set(effective_required_capabilities):
            return (
                "PDF builder 修复提示：\n"
                "- 脚本必须生成真实 PDF 文件，支持中文/UTF-8。\n"
                "- 允许使用 reportlab 或 fpdf2 等库实现，但必须注册/加载可用字体（reportlab: UnicodeCIDFont/TTFont，fpdf2: add_font + TTF/OTF）。\n"
                "- 禁止使用 FPDF 默认核心字体（Helvetica/Arial/Times/Courier）直接写正文；禁止 raw %PDF 字节拼接或输出空 PDF。\n"
                "- 脚本必须通过 sys.argv[1] 接收 JSON object 输入，stdout 仅输出 JSON object，并包含真实存在的 pdf_path/file_paths。\n"
                "- 保留脚本自由结构和函数命名，分页、换行、标题或图片插入由模型自由设计。\n"
                "- 不要通过 try/except 输出 error、{} 或空路径绕过试运行。\n"
                "- 修复关注点是：确保中文 PDF 生成能力、遵守 JSON argv/stdout 协议，不强制模板结构。"
            )
        if plan_entry.role == "text_generator":
            return (
                "必须使用下面的 node text_generator skeleton；调用平台 generate_text_with_llm helper，stdout JSON 包含非空 text：\n"
                "const { spawnSync } = require('child_process');\n"
                "const payload = process.argv[2] ? JSON.parse(process.argv[2]) : {};\n"
                "function run(payload) {\n"
                f"  const prompt = String({js_value_expr}).trim();\n"
                "  const helper = `from backend.services.skill_runtime import generate_text_with_llm\\nimport json,sys\\nprint(json.dumps({'text': generate_text_with_llm(sys.argv[1])}, ensure_ascii=False))`;\n"
                "  const proc = spawnSync(process.env.PYTHON || 'python', ['-c', helper, prompt], { encoding: 'utf8' });\n"
                "  if (proc.status !== 0) throw new Error(proc.stderr || 'generate_text_with_llm failed');\n"
                "  return JSON.parse(proc.stdout);\n"
                "}\n"
                "console.log(JSON.stringify(run(payload)));"
            )
        return (
            "必须使用下面的 node_skeleton；解析 process.argv[2] JSON，stdout 只能 console.log JSON 字符串：\n"
            "const payload = process.argv[2] ? JSON.parse(process.argv[2]) : {};\n"
            "function run(payload) {\n"
            f"  const text = String({js_value_expr}).trim();\n"
            "  return { text, file_paths: [] };\n"
            "}\n"
            "console.log(JSON.stringify(run(payload)));"
        )

    if plan_entry.runtime in {"bash", "shell"}:
        if {"text_generation", "image_generation"} <= set(effective_required_capabilities):
            helper = "from backend.services.skill_runtime import generate_text_with_llm, generate_stable_diffusion_image; import json,sys; p=json.loads(sys.argv[1]); prompt=str(" + bash_py_expr + "); text=generate_text_with_llm(prompt); result=generate_stable_diffusion_image(text or prompt, filename_prefix='generated'); print(json.dumps({'story_text': text, 'text': text, 'text_with_image_prompts': [{'text': text, 'image_prompt': text or prompt}], 'image_paths':[result.get('image_path')], 'images':[result]}, ensure_ascii=False))"
        elif plan_entry.role == "image_generator":
            helper = "from backend.services.skill_runtime import generate_stable_diffusion_image; import json,sys; result=generate_stable_diffusion_image(sys.argv[1], filename_prefix='generated'); print(json.dumps({'image_paths':[result.get('image_path')], 'images':[result]}, ensure_ascii=False))"
        elif plan_entry.role == "pdf_builder" or "pdf_generation" in set(effective_required_capabilities):
            helper = "from backend.services.skill_runtime import create_pdf; import json,sys; p=json.loads(sys.argv[1]); text=str(" + bash_py_expr + " or 'Generated PDF'); print(json.dumps(create_pdf(text, output_dir=p.get('output_dir') or 'assets/generated'), ensure_ascii=False))"
        elif plan_entry.role == "text_generator":
            helper = "from backend.services.skill_runtime import generate_text_with_llm; import json,sys; p=json.loads(sys.argv[1]); prompt=str(" + bash_py_expr + "); print(json.dumps({'text': generate_text_with_llm(prompt)}, ensure_ascii=False))"
        else:
            helper = "import json,sys; p=json.loads(sys.argv[1]); text=str(" + bash_py_expr + "); print(json.dumps({'text': text, 'file_paths': []}, ensure_ascii=False))"
        return (
            "必须使用下面的 shell_skeleton；从 $1 读取 JSON argv，并向 stdout 输出 JSON（文件生成脚本必须包含 pdf_path/docx_path/pptx_path 与 file_paths/file_outputs）：\n"
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "payload_json=${1:-'{}'}\n"
            f"python -c {shlex.quote(helper)} \"$payload_json\""
        )


    if plan_entry.role == "docx_builder" or "docx_generation" in set(effective_required_capabilities):
        return (
            "必须使用下面的 docx_builder 脚本骨架；默认只消费已有 stdout JSON/text/image_paths 并通过平台 create_docx helper 生成 Word；仅当当前脚本 capabilities 显式声明 text/image generation 时才调用模型：\n"
            "import json\n"
            "import sys\n"
            "from backend.services.skill_runtime import create_docx, print_json\n\n"
            "def parse_args() -> dict:\n"
            "    if len(sys.argv) < 2:\n"
            "        return {}\n"
            "    return json.loads(sys.argv[1])\n\n"
            "def previous_payload(payload: dict) -> dict:\n"
            "    raw = payload.get('previous_stdout') or payload.get('stdout_json') or '{}'\n"
            "    if isinstance(raw, dict):\n"
            "        return raw\n"
            "    try:\n"
            "        data = json.loads(str(raw))\n"
            "        return data if isinstance(data, dict) else {}\n"
            "    except json.JSONDecodeError:\n"
            "        return {}\n\n"
            "def run(payload: dict) -> dict:\n"
            "    prev = previous_payload(payload)\n"
            f"    text = str(payload.get('story_text') or payload.get('text') or prev.get('story_text') or prev.get('text') or {py_value_expr} or 'Generated document').strip()\n"
            "    return create_docx(text, filename='output.docx')\n\n"
            "def main() -> None:\n"
            "    print_json(run(parse_args()))\n\n"
            "if __name__ == '__main__':\n"
            "    main()"
        )

    if plan_entry.role == "pptx_builder" or "pptx_generation" in set(effective_required_capabilities):
        return (
            "必须使用下面的 pptx_builder 脚本骨架；默认只消费已有 stdout JSON/text/image_paths 并通过平台 create_pptx helper 生成 PPT；仅当当前脚本 capabilities 显式声明 text/image generation 时才调用模型：\n"
            "import json\n"
            "import sys\n"
            "from backend.services.skill_runtime import create_pptx, print_json\n\n"
            "def parse_args() -> dict:\n"
            "    if len(sys.argv) < 2:\n"
            "        return {}\n"
            "    return json.loads(sys.argv[1])\n\n"
            "def run(payload: dict) -> dict:\n"
            f"    text = str(payload.get('story_text') or payload.get('text') or {py_value_expr} or 'Generated presentation').strip()\n"
            "    return create_pptx(text, filename='output.pptx')\n\n"
            "def main() -> None:\n"
            "    print_json(run(parse_args()))\n\n"
            "if __name__ == '__main__':\n"
            "    main()"
        )

    if plan_entry.role in {"html_asset_builder", "asset_builder"} or ({"html_generation", "html_asset_generation"} & set(effective_required_capabilities)):
        return (
            "必须使用下面的 html_asset_builder Python 脚本骨架；只能在当前 Skill 的 assets/generated/ 下写入 HTML，并在 stdout JSON 返回 html_path 与 asset_paths：\n"
            "import html\n"
            "import json\n"
            "import re\n"
            "import sys\n"
            "from pathlib import Path\n\n"
            "def parse_args() -> dict:\n"
            "    if len(sys.argv) < 2:\n"
            "        return {}\n"
            "    return json.loads(sys.argv[1])\n\n"
            "def slugify(value: str) -> str:\n"
            "    slug = re.sub(r'[^A-Za-z0-9_-]+', '-', value).strip('-').lower()\n"
            "    return slug or 'generated'\n\n"
            "def build_html(payload: dict) -> str:\n"
            f"    text = str({py_value_expr} or 'Generated HTML').strip()\n"
            "    safe = html.escape(text)\n"
            "    return '<!doctype html><html><head><meta charset=\"utf-8\"><title>Generated</title></head><body><main><h1>Generated Asset</h1><p>' + safe + '</p></main></body></html>'\n\n"
            "def run(payload: dict) -> dict:\n"
            f"    title = str({py_value_expr} or 'generated').strip()\n"
            "    skill_root = Path(__file__).resolve().parents[1]\n"
            "    out_dir = (skill_root / 'assets' / 'generated').resolve()\n"
            "    required_root = (skill_root / 'assets' / 'generated').resolve()\n"
            "    out_dir.mkdir(parents=True, exist_ok=True)\n"
            "    html_path = (out_dir / (slugify(title) + '.html')).resolve()\n"
            "    html_path.relative_to(required_root)\n"
            "    html_path.write_text(build_html(payload), encoding='utf-8')\n"
            "    rel = html_path.relative_to(skill_root).as_posix()\n"
            "    return {'html_path': rel, 'asset_paths': [rel]}\n\n"
            "def main() -> None:\n"
            "    payload = parse_args()\n"
            "    print(json.dumps(run(payload), ensure_ascii=False))\n\n"
            "if __name__ == '__main__':\n"
            "    main()"
        )

    if {"text_generation", "image_generation"} <= set(effective_required_capabilities):
        return (
            "必须使用下面的 composite_generator 脚本骨架；先调用平台 generate_text_with_llm，再调用 generate_stable_diffusion_image，stdout JSON 包含 text 与 image_paths/images：\n"
            "import json\n"
            "import sys\n"
            "from backend.services.skill_runtime import generate_text_with_llm, generate_stable_diffusion_image\n\n"
            "def parse_args() -> dict:\n"
            "    if len(sys.argv) < 2:\n"
            "        return {}\n"
            "    return json.loads(sys.argv[1])\n\n"
            "def build_prompt(payload: dict) -> str:\n"
            f"    return str({py_value_expr}).strip()\n\n"
            "def generate_text(prompt: str) -> str:\n"
            "    return generate_text_with_llm(prompt).strip()\n\n"
            "def generate_images(text: str, prompt: str) -> tuple[list[str], list[dict]]:\n"
            "    image_prompt = text or prompt\n"
            "    result = generate_stable_diffusion_image(image_prompt, filename_prefix='generated')\n"
            "    image_paths = [result.get('image_path')]\n"
            "    image_paths = [p for p in image_paths if isinstance(p, str) and p]\n"
            "    return image_paths, [result]\n\n"
            "def run(payload: dict) -> dict:\n"
            "    prompt = build_prompt(payload)\n"
            "    text = generate_text(prompt)\n"
            "    image_paths, images = generate_images(text, prompt)\n"
            "    return {'story_text': text, 'text': text, 'text_with_image_prompts': [{'text': text, 'image_prompt': text or prompt}], 'image_paths': image_paths, 'images': images}\n\n"
            "def main() -> None:\n"
            "    payload = parse_args()\n"
            "    print(json.dumps(run(payload), ensure_ascii=False))\n\n"
            "if __name__ == '__main__':\n"
            "    main()"
        )

    if plan_entry.role == "image_generator":
        return (
            "必须使用下面的 image_generator 脚本骨架；不要改变 import/helper/main/JSON stdout 结构，只填充 build_image_prompt() 中的业务 prompt 组装逻辑，必要时补充返回字段：\n"
            "import json\n"
            "import sys\n"
            "from backend.services.skill_runtime import generate_stable_diffusion_image\n\n"
            "def parse_args() -> dict:\n"
            "    if len(sys.argv) < 2:\n"
            "        return {}\n"
            "    return json.loads(sys.argv[1])\n\n"
            "def build_image_prompt(payload: dict) -> str:\n"
            f"    topic = str({py_value_expr}).strip()\n"
            "    return topic\n\n"
            "def run(payload: dict) -> dict:\n"
            "    desc = build_image_prompt(payload)\n"
            "    image_paths = []\n"
            "    images = []\n"
            "    result = generate_stable_diffusion_image(desc, filename_prefix='generated')\n"
            "    image_paths.append(result.get('image_path'))\n"
            "    images.append(result)\n"
            "    image_paths = [p for p in image_paths if isinstance(p, str) and p]\n"
            "    return {'image_paths': image_paths, 'images': images}\n\n"
            "def main() -> None:\n"
            "    payload = parse_args()\n"
            "    print(json.dumps(run(payload), ensure_ascii=False))\n\n"
            "if __name__ == '__main__':\n"
            "    main()"
        )

    if plan_entry.role == "pdf_builder" or "pdf_generation" in set(effective_required_capabilities):
        return (
            "必须使用下面的 pdf_builder 脚本骨架；默认只负责读取已有内容并通过平台 create_pdf helper 构建 PDF 文件；stdout JSON 由 helper 返回 pdf_path/file_paths/file_outputs；仅当当前脚本 capabilities 显式声明 text/image generation 时才调用模型：\n"
            "import json\n"
            "import sys\n"
            "from backend.services.skill_runtime import create_pdf, print_json\n\n"
            "def parse_args() -> dict:\n"
            "    if len(sys.argv) < 2:\n"
            "        return {}\n"
            "    return json.loads(sys.argv[1])\n\n"
            "def run(payload: dict) -> dict:\n"
            f"    text = str({py_value_expr} or payload.get('story_text') or payload.get('content') or 'Generated PDF').strip()\n"
            "    return create_pdf(text, filename='output.pdf')\n\n"
            "def main() -> None:\n"
            "    print_json(run(parse_args()))\n\n"
            "if __name__ == '__main__':\n"
            "    main()"
        )

    if plan_entry.role == "text_generator":
        return (
            "必须使用下面的 text_generator 脚本骨架；调用平台 generate_text_with_llm，stdout JSON 必须包含非空 text，不要生成图片或 PDF：\n"
            "import json\n"
            "import sys\n"
            "from backend.services.skill_runtime import generate_text_with_llm\n\n"
            "def parse_args() -> dict:\n"
            "    if len(sys.argv) < 2:\n"
            "        return {}\n"
            "    return json.loads(sys.argv[1])\n\n"
            "def generate_text(payload: dict) -> str:\n"
            f"    prompt = str({py_value_expr}).strip()\n"
            "    return generate_text_with_llm(prompt)\n\n"
            "def run(payload: dict) -> dict:\n"
            "    text = generate_text(payload).strip()\n"
            "    return {'story_text': text, 'text': text}\n\n"
            "def main() -> None:\n"
            "    payload = parse_args()\n"
            "    print(json.dumps(run(payload), ensure_ascii=False))\n\n"
            "if __name__ == '__main__':\n"
            "    main()"
        )

    return (
        "必须使用下面的 generic_script 脚本骨架；不要改变 import/parse_args/main/JSON stdout 结构，只填充 run() 中的真实业务逻辑并按 SkillPlan 使用 payload 字段：\n"
        "import json\n"
        "import sys\n\n"
        "def parse_args() -> dict:\n"
        "    if len(sys.argv) < 2:\n"
        "        return {}\n"
        "    return json.loads(sys.argv[1])\n\n"
        "def run(payload: dict) -> dict:\n"
        f"    text = str({py_value_expr}).strip()\n"
        "    return {'text': text, 'file_paths': []}\n\n"
        "def main() -> None:\n"
        "    payload = parse_args()\n"
        "    print(json.dumps(run(payload), ensure_ascii=False))\n\n"
        "if __name__ == '__main__':\n"
        "    main()"
    )


def _creator_kernel_reference_context() -> str:
    """Load small Creator prompt context from kernel references.

    These references are advisory generation context only; SKILL.md protocol and
    generated file contracts remain unchanged.
    """
    kernel_dir = Path(__file__).resolve().parents[2] / "kernel"
    candidates = [
        kernel_dir / "references" / "best-practices.md",
        kernel_dir / "references" / "workflows.md",
        kernel_dir / "references" / "output-patterns.md",
        kernel_dir / "SKILL.md",
    ]
    chunks: list[str] = []
    for path in candidates:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            rel = path.relative_to(kernel_dir.parent)
            chunks.append(f"### INTERNAL-ONLY {rel}\n{text[:1800]}")
    return "\n\n".join(chunks)

def _build_generate_file_prompt(
    file_path: str,
    skill_name: str,
    purpose: str,
    blueprint_text: str,
    conversation_history: list[dict],
    role: str | None = None,
    skill_plan_entry: dict[str, Any] | None = None,
) -> list[dict]:
    """Build a minimal generation prompt for a single Skill file.

    The model is asked to output *only* raw file content — no fences, no JSON,
    no explanations.  This maximises reliability for small or unstable models.
    """
    ext = Path(file_path).suffix.lower()
    lang = _LANG_LABELS.get(ext, "文本")

    clean_blueprint_text = _clean_blueprint_for_file_prompt(blueprint_text)
    declared_paths = _extract_declared_skill_paths(blueprint_text)
    declared_paths_text = "\n".join(f"- {path}" for path in declared_paths) or "- （蓝图未显式列出资源文件）"
    plan_entry = _skill_plan_entry_for_file(
        file_path=file_path, purpose=purpose, blueprint_text=blueprint_text, role=role, skill_plan_entry=skill_plan_entry
    )
    generated_file_contract_text = _build_generated_file_contract_text(
        file_path, blueprint_text, purpose, role=role, skill_plan_entry=skill_plan_entry
    )
    skill_md_contract_text = generated_file_contract_text if file_path == "SKILL.md" else ""
    skill_md_e2e_authoring_guide = (
        _build_skill_md_e2e_authoring_guide(blueprint_text)
        if file_path == "SKILL.md"
        else ""
    )
    script_skeleton_text = _script_generation_skeleton(
        file_path,
        purpose,
        blueprint_text,
        role=plan_entry.role,
        skill_plan_entry=skill_plan_entry,
    ) if file_path.startswith("scripts/") else ""
    kernel_reference_context = _creator_kernel_reference_context()
    plan_summary = (
        f"SkillPlan role：{plan_entry.role}；"
        f"inputs：{', '.join(plan_entry.inputs)}；"
        f"outputs：{', '.join(plan_entry.outputs)}；"
        f"language：{plan_entry.language}；"
        f"runtime：{plan_entry.runtime}；"
        f"command_template：{_script_command_template(file_path, blueprint_text, plan_entry)}；"
        f"forbidden_capabilities：{', '.join(plan_entry.forbidden_capabilities)}"
    )

    if file_path == "SKILL.md":
        instruction = (
            f'你正在为 Skill 包 "{skill_name}" 生成 SKILL.md 文件。\n\n'
            "要求：\n"
            "1. 只输出 SKILL.md 的文件内容，不要任何解释，不要 Markdown 代码块包裹。\n"
            "2. 文件必须以 YAML frontmatter 开始，格式严格如下（冒号后有一个空格）：\n"
            "---\n"
            f"name: {skill_name}\n"
            "description: <一句话说明本 Skill 的用途>\n"
            "---\n"
            "3. frontmatter 闭合后，输出 Skill 的核心执行说明（普通 Markdown 正文）。\n"
            "4. SKILL.md 必须生成成 E2E 可试运行的线性 workflow，不允许依赖后续人工修产物文件。\n"
            "5. 如果蓝图包含 scripts/ 资源，SKILL.md 正文必须为每个 scripts/ 路径提供一个标准、独立、无缩进的 ```bash fenced code block。\n"
            "6. 每个 bash fenced code block 内只能有一条脚本命令；命令必须直接调用 scripts/ 路径，并在脚本路径后传入一个 JSON object argv。\n"
            "7. JSON argv 必须是 json.loads 可解析的模板；所有动态占位符必须是字符串值，例如 \"theme\":\"{{theme}}\"；禁止裸写 \"scene_count\":{{scene_count}}。\n"
            "8. 若需要默认数字，直接写 JSON 数字，例如 \"scene_count\":3；不要把数字 placeholder 裸露在 JSON 中。\n"
            "9. E2E 不理解自然语言循环；禁止写“每幕一次/共3次/对每个场景调用一次”作为执行协议。多场景循环必须由脚本内部完成，SKILL.md 只写一次脚本调用。\n"
            "10. 后续步骤只能引用前序脚本 stdout JSON 中真实输出的字段；禁止发明 image_path_1/image_path_2/image_path_3 等编号字段。\n"
            "11. 列表或对象字段必须通过整值占位符传递，例如 \"image_paths\":\"{{image_paths}}\"，不要写成 [\"{{image_path_1}}\",\"{{image_path_2}}\"]。\n"
            "12. 如果蓝图包含 references/ 资源，SKILL.md 正文必须在“参考资料/资源”小节明确引用每个 references/ 路径，并说明何时读取。\n"
            "13. 不要在输出内容的外侧套 ``` 代码块，但 SKILL.md 正文内部必须按需包含标准 ```bash fenced code block。\n"
            "14. 禁止只写‘立即调用 `scripts/...`’这种隐式执行描述；必须写明 assistant 应输出可执行 fenced block。\n"
            "15. 禁止复制 Creator 界面流程、确认清单、‘点击开始创建/开始生成’、系统将自动创建文件等平台创建流程文案。\n"
            "16. 以下宿主 Markdown 执行说明是内部写作约束，只能转化为面向使用者的 Skill 说明，不要逐字复制这些约束或标题。\n"
            "17. 后续脚本命令中，JSON key 是当前脚本读取的 argv 字段；{{placeholder}} 是上游 stdout 字段。脚本源码只需要读取 JSON key，不需要出现 placeholder 名称。\n"
            "18. 禁止为下游脚本添加没有上游来源的额外 placeholder，例如禁止 custom_character:\"{{character}}\"，除非前序 stdout 明确输出 character。\n"
            "19. 对图片生成脚本，若上一步输出 script_content，则推荐命令为：python scripts/generate_images.py '{\"scene_text\":\"{{script_content}}\"}'，不要额外添加 custom_character。\n"
            "20. SKILL.md 必须覆盖蓝图真实规划的任务、workflow、脚本顺序、输入输出数据流、资源使用和最终产物。\n"
            "21. 真实文件计划需要结合蓝图语境判断：目录结构、SkillPlan path、dependencies、references 字段通常是真实文件计划。\n"
            "22. 如果蓝图在“禁止隐式执行/示例/反例/例如/比如”语境中提到某个 scripts/*.py、references/*.md 或 assets/*，它只是解释性示例，不应进入最终 SKILL.md，除非它同时出现在目录结构或 SkillPlan path 中。\n"
            "23. 不要为了满足格式而新增蓝图外脚本；只为蓝图真实规划脚本提供命令块。\n"
            f"{_SKILL_MD_MARKDOWN_EXECUTION_GUIDE}\n\n"
            "以下 E2E workflow authoring guide 是强制生成策略，最终 SKILL.md 必须按它组织命令和数据流：\n"
            f"{skill_md_e2e_authoring_guide}\n\n"
            "生成前请先隐式检查以下合同，最终输出必须逐项满足；如果合同要求内部 ```bash block，必须在 SKILL.md 正文中写出该 block：\n"
            f"{skill_md_contract_text}\n\n"
            f"蓝图声明的文件路径（必须覆盖对应 scripts/references 要求）：\n{declared_paths_text}\n\n"
            f"以下是已确认的蓝图（已移除 Creator UI 确认文案），你的内容必须与此一致：\n\n{clean_blueprint_text}"
        )
    elif file_path.startswith("scripts/"):
        instruction = (
            f'你正在为 Skill 包 "{skill_name}" 生成 {file_path} 文件。\n\n'
            f"职责说明：{purpose}\n"
            f"{plan_summary}\n\n"
            "你是文件内容生成器，不是聊天助手；当前输出会被直接写入目标文件。\n"
            "要求：\n"
            f"1. 只输出完整可运行的 {lang} 文件字节内容本身，不要任何说明文字。\n"
            "2. 禁止 Markdown，禁止 ```，禁止‘下面是代码’，禁止文件名标题，禁止多文件输出；如果输出包含 ```，系统会判定失败。\n"
            "3. 脚本的命令行参数、stdin/stdout 接口必须与蓝图和 SKILL.md 里的 Markdown 命令示例一致。\n"
            "4. 如果命令示例传入 JSON 字符串参数，脚本必须按 SkillPlan.runtime 解析；Python 默认读取 sys.argv[1] 并 json.loads 解析，Node 使用 process.argv[2]+JSON.parse，Bash 使用 $1 JSON。\n"
            "5. 必须实际使用用户可变参数生成结果；禁止把示例结果、示例标题、示例图片路径硬编码成固定输出。\n"
            "6. 文本/代码/视觉理解与图片生成的模型来源必须区分：text_generation 使用 generate_text_with_llm 或 LLM_BASE_URL + TEXT_MODEL；看图/OCR/多模态理解使用 LLM_BASE_URL + VISION_MODEL；image_generation 使用平台 Stable Diffusion 图片运行时（IMAGE_BASE_URL + IMAGE_MODEL），不要把 VISION_MODEL 用于图片生成。\n"
            "7. 是否必须调用文本/图片模型只由当前脚本 SkillPlan.required_capabilities 决定：包含 image_generation 时必须调用 `from backend.services.skill_runtime import generate_stable_diffusion_image`；builder/exporter 默认是确定性文件构建脚本，不要因为整个 Skill.md 提到模型就强制 builder 调模型；若 builder 需要模型辅助，用 optional_capabilities/allowed_capabilities 或显式 required_capabilities 表达。\n"
            "8. image_generation stdout 输出结构化 JSON，并返回 helper 结果里的 image_paths/images；必须使用 result = generate_stable_diffusion_image(desc)、image_paths.append(result.get(\"image_path\"))、images.append(result) 的骨架，禁止 image_path = generate_stable_diffusion_image(...)；禁止输出 base64 data URI，禁止假设接口只返回 url；可按需读取平台注入的 IMAGE_MODEL / IMAGE_BASE_URL / IMAGE_SIZE / IMAGE_API_KEY 等环境变量，但不要硬编码，也不需要额外校验它们是否存在。\n"
            "9. 如果脚本只做确定性计算、转换、文件处理或格式化，必须实现真实算法并使用用户输入；禁止假 API、placeholder 文件、纯色/空白图片或 ASCII 图冒充输出。\n"
            "10. stdout 输出结构化 JSON，字段必须以 SkillPlan.outputs 为准；允许极少数 legacy alias 仅用于兼容旧 Skill，新生成 Skill 必须统一字段命名，不要混用 text/text_content/story_text 等别名；composite_generator 只表示多能力组合，具体输出由 outputs/required_capabilities 决定。\n"
            "11. 所有导入的第三方库必须真实存在且常见；Creator 保存前会先扫描 Python import 并安装缺失依赖，再按“生成→测试→修复生成→再测试”的闭环试运行；脚本仍必须包含必要的错误处理逻辑（如参数校验、文件不存在提示等）。\n"
            "12. 必须基于下方固定骨架生成：默认优先 Python；若 SkillPlan.runtime 为 node/bash，则使用对应骨架并保留入口、参数解析和 JSON stdout。\n"
            f"13. 最终响应必须是单个 {plan_entry.language} 源码文件；去掉 Markdown fence、说明文字、文件路径标题和多文件包。\n"
            "生成前请先隐式检查以下脚本合同，最终输出必须逐项满足：\n"
            f"{generated_file_contract_text}\n\n"
            f"固定脚本骨架（仅用于约束生成结构；输出时应是补全后的源码，不要保留空实现）：\n{script_skeleton_text}\n\n"
            f"Creator internal-only kernel guidance（只可指导生成，禁止把以下 kernel 路径、文件名或组织结构复制到最终业务 SKILL.md / references / assets）：\n{kernel_reference_context}\n\n"
            f"蓝图声明的文件路径：\n{declared_paths_text}\n\n"
            f"以下是已确认的蓝图（scripts/ 生成不会追加聊天历史，只使用本蓝图）：\n\n{clean_blueprint_text}"
        )
    elif file_path.startswith("references/"):
        instruction = (
            f'你正在为 Skill 包 "{skill_name}" 生成 {file_path} 参考资料文件。\n\n'
            f"职责说明：{purpose}\n\n"
            "要求：\n"
            "1. 只输出 Markdown 文档内容，不要额外的说明文字。\n"
            "2. 不要在文档外套 ``` 代码块。\n"
            "3. 内容应是有实际指导价值的参考资料，不是对参考资料的再描述。\n"
            "生成前请先隐式检查以下 reference 合同，最终输出必须逐项满足：\n"
            f"{generated_file_contract_text}\n\n"
            f"蓝图声明的文件路径：\n{declared_paths_text}\n\n"
            f"以下是已确认的蓝图（参考资料职责说明见 references/ 部分）：\n\n{clean_blueprint_text}"
        )
    elif file_path.startswith("assets/"):
        instruction = (
            f'你正在为 Skill 包 "{skill_name}" 生成 {file_path} 资源文件。\n\n'
            f"职责说明：{purpose}\n\n"
            "要求：\n"
            f"1. 只输出 {lang} 格式的文件内容，不要任何说明文字。\n"
            "2. 不要用 ``` 代码块包裹输出。\n"
            "生成前请先隐式检查以下 asset 合同，最终输出必须逐项满足：\n"
            f"{generated_file_contract_text}\n\n"
            f"蓝图声明的文件路径：\n{declared_paths_text}\n\n"
            f"以下是已确认的蓝图：\n\n{clean_blueprint_text}"
        )
    else:
        instruction = (
            f'你正在为 Skill 包 "{skill_name}" 生成 {file_path} 文件。\n\n'
            f"职责说明：{purpose}\n\n"
            "要求：直接输出文件内容，不要任何解释，不要 Markdown 代码块包裹。\n\n"
            f"蓝图声明的文件路径：\n{declared_paths_text}\n\n"
            f"蓝图：\n\n{clean_blueprint_text}"
        )

    messages: list[dict] = [{"role": "system", "content": instruction}]

    if file_path.startswith("scripts/"):
        # Scripts are generated from the system instruction plus the confirmed
        # blueprint only.  Do not append conversation history: recent Creator UI
        # copy (file-list previews, confirmation instructions, panel messages)
        # has repeatedly polluted first-pass script output.
        return messages

    # Include recent user context but skip Creator UI confirmation text. Assistant
    # blueprint confirmations often contain "click Start" operational prose that
    # must never be copied into generated files.
    for msg in conversation_history[-_MAX_HISTORY_TURNS:]:
        if not isinstance(msg, dict) or msg.get("role") not in {"user", "assistant"}:
            continue
        content = str(msg.get("content") or "")
        if msg.get("role") == "assistant" and _CREATOR_FLOW_LEAK_RE.search(content):
            continue
        messages.append({**msg, "content": _clean_blueprint_for_file_prompt(content)})

    return messages



def _local_blueprint_text_for_path(path: str, blueprint_text: str, *, window: int = 600) -> str:
    text = blueprint_text or ""
    idx = text.find(path)
    if idx < 0:
        return ""
    return text[max(0, idx - window): min(len(text), idx + len(path) + window)]


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/analyze-blueprint", response_model=AnalyzeBlueprintResponse)
async def analyze_blueprint(request: AnalyzeBlueprintRequest):
    plan: BlueprintPlan = parse_blueprint(request.messages)
    entries_by_path = {entry.path: entry for entry in (plan.skill_plan.files if plan.skill_plan else [])}

    blueprint_text = "\n\n".join(
        str(message.get("content") or "")
        for message in request.messages
        if isinstance(message, dict)
    )

    base_paths = {f.path for f in plan.files}

    candidate_paths: set[str] = set(_extract_declared_skill_paths(blueprint_text))
    candidate_paths.update(entries_by_path.keys())

    extra_paths = []
    extra_path_warnings: list[str] = []
    for path in sorted(candidate_paths):
        if path in base_paths or not (path.startswith("references/") or path.startswith("assets/")):
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

    files_out: list[FileSpecOut] = []

    for f in plan.files:
        entry = entries_by_path.get(f.path)
        role = entry.role if entry else fallback_role(f.path)
        file_type = entry.file_type if entry else fallback_file_type(f.path)
        language = entry.language if entry else language_for_path(f.path)
        runtime = entry.runtime if entry else runtime_for_language(language, file_type or "")

        files_out.append(
            FileSpecOut(
                path=f.path,
                purpose=f.purpose,
                required=f.required,
                can_skip=f.can_skip,
                file_type=file_type,
                role=role,
                inputs=entry.inputs if entry else [],
                outputs=entry.outputs if entry else [],
                dependencies=entry.dependencies if entry else [],
                required_capabilities=entry.required_capabilities if entry else [],
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
            )
        )

    for path in extra_paths:
        role = fallback_role(path)
        file_type = fallback_file_type(path)
        language = language_for_path(path)
        runtime = runtime_for_language(language, file_type or "")
        is_asset = path.startswith("assets/")

        required_capabilities, forbidden_capabilities = capabilities_for_role(role or "generic_script")
        required_capabilities = normalize_required_capabilities(
            role=role or "generic_script",
            path=path,
            required_capabilities=list(required_capabilities or []),
            user_blueprint_text=blueprint_text,
        )
        inputs, outputs = default_io_for_role(role or "generic_script")

        files_out.append(
            FileSpecOut(
                path=path,
                purpose=(
                    f"用户上传的静态素材：{path}"
                    if is_asset
                    else f"参考说明文件：{path}"
                ),
                required=True,
                can_skip=False,
                file_type=file_type,
                role=role,
                inputs=list(inputs or []),
                outputs=list(outputs or []),
                dependencies=[],
                required_capabilities=list(required_capabilities or []),
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
            )
        )

    available_tools = [tool_status(cap) for cap in list_tool_capabilities()]
    required_tool_names = {
        capability
        for file_spec in files_out
        for capability in file_spec.required_capabilities
    }
    missing_tool_configs = []
    warnings = [*list(plan.warnings), *extra_path_warnings]
    for capability_name in sorted(required_tool_names):
        cap = get_tool_capability(capability_name)
        if not cap or cap.category == "resource":
            continue
        status = tool_status(cap)
        missing_runtime_helpers = status.get("missing_runtime_helpers") or []
        missing_dependencies = status.get("missing_dependencies") or []
        if not status["creator_available"]:
            warnings.append(
                f"工具能力 {capability_name} 已被禁用或不允许 Creator 使用，相关脚本不会默认获得该能力。"
            )
        if missing_runtime_helpers:
            warnings.append(
                f"工具能力 {capability_name} 缺少 runtime helper: {', '.join(missing_runtime_helpers)}。"
            )
        if missing_dependencies:
            warnings.append(
                f"工具能力 {capability_name} 缺少 runtime dependency: {', '.join(missing_dependencies)}。"
            )
        if not status["configured"] or missing_runtime_helpers or missing_dependencies or not status["creator_available"]:
            missing_tool_configs.append(status)

    return AnalyzeBlueprintResponse(
        skill_name=plan.skill_name,
        files=files_out,
        warnings=warnings,
        available_tools=available_tools,
        missing_tool_configs=missing_tool_configs,
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

@router.post("/generate-file")
async def generate_file(request: GenerateFileRequest):
    """Generate one Creator file and stream it back as SSE.

    Important:
    - This endpoint must not write files to disk.
    - The frontend expects streamed content and then calls /write-file.
    - assets/** are upload-only and must never be generated by model.
    - SKILL.md must pass blueprint-intent alignment before returned.
    - references/*.md must contain YAML metadata frontmatter.
    """
    skill_name = _validate_skill_name(request.skill_name)
    _validate_file_path(request.file_path)

    if request.file_path.startswith("assets/"):
        raise HTTPException(
            status_code=400,
            detail=f"{request.file_path} 属于 assets 静态素材目录，必须上传，不能生成。",
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

            prompt_messages = _build_generate_file_prompt(
                request.file_path,
                skill_name,
                request.purpose,
                request.blueprint_text,
                request.conversation_history,
                role=request.role,
                skill_plan_entry=request.skill_plan_entry,
            )
        except Exception as exc:
            logger.exception("Creator generate_file prepare failed: %s", exc)
            yield _sse({
                "error": f"生成前准备失败：{exc}",
                "done": True,
            })
            return

        candidate = ""
        try:
            candidate = await complete_chat_once(prompt_messages, route.model)
        except Exception as exc:
            logger.exception("Creator generate_file initial model call failed: %s", exc)
            yield _sse({
                "error": f"模型调用失败：{exc}",
                "done": True,
            })
            return

        for attempt in range(1, _MAX_FILE_REPAIR_ATTEMPTS + 1):
            try:
                content = _sanitize_generated_file_content(
                    request.file_path,
                    candidate,
                    role=request.role,
                    skill_plan_entry=request.skill_plan_entry,
                )

                if request.file_path.startswith("references/") and Path(request.file_path).suffix.lower() == ".md":
                    content = _ensure_reference_metadata_frontmatter(
                        file_path=request.file_path,
                        content=content,
                        purpose=request.purpose,
                        skill_plan_entry=request.skill_plan_entry,
                    )

                if request.file_path == "SKILL.md":
                    # SKILL.md is generated before scripts/references are materialized.
                    # Validate against blueprint-declared plan, not disk existence.
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
                        skill_plan_entry=request.skill_plan_entry,
                        model=request.model or route.model,
                    )

                elif request.file_path.startswith("references/"):
                    _validate_reference_file_contract(
                        request.file_path,
                        content,
                        request.purpose or request.blueprint_text,
                    )

                elif request.file_path.startswith("scripts/"):
                    _validate_script_against_existing_skill_contract(
                        skill_name,
                        request.file_path,
                        content,
                    )
                    _trial_run_generated_script_with_plan(
                        skill_name,
                        request.file_path,
                        content,
                        role=request.role,
                        skill_plan_entry=request.skill_plan_entry,
                    )

                if not content.strip():
                    raise ValueError(f"{request.file_path} 生成内容为空。")

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
                })

                yield _sse({
                    "type": "file_done",
                    "status": "success",
                    "success": True,
                    "file_path": request.file_path,
                    "role": request.role,
                    "done": True,
                })

                return

            except Exception as exc:
                deterministic_error = str(exc)
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
                        "\n\n额外修复目标：reference Markdown 必须以 YAML frontmatter metadata 开始，"
                        "metadata 必须包含 name/description/role/type/path/scope/loading/when_to_use，"
                        "其中 role/type=reference，path 等于当前文件路径，"
                        "loading=metadata-first-body-on-demand。"
                    )

                contract_text = _build_generated_file_contract_text(
                    request.file_path,
                    request.blueprint_text,
                    request.purpose,
                    role=request.role,
                    skill_plan_entry=request.skill_plan_entry,
                )

                passed_checks_text = ""
                failed_checks_text = ""
                if isinstance(exc, ContractValidationError):
                    passed_checks_text = _format_contract_checks(exc.results, passed=True)
                    failed_checks_text = _format_contract_checks(exc.results, passed=False)

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

                    yield _sse({
                        "type": "file_done",
                        "status": "error",
                        "success": False,
                        "file_path": request.file_path,
                        "role": request.role,
                        "error": error_message,
                        "done": True,
                    })
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
                        "error": deterministic_error,
                    }
                })

                validator_report = await _run_generated_file_validator_round(
                    file_path=request.file_path,
                    content=candidate,
                    deterministic_error=deterministic_error,
                    requested_model=route.model,
                    targeted_repair=targeted_repair,
                    contract_text=contract_text,
                    passed_checks_text=passed_checks_text,
                    failed_checks_text=failed_checks_text,
                    repair_mode=(
                        "strict_contract_rewrite"
                        if attempt >= 2 and request.file_path.startswith("scripts/")
                        else "minimal_edit"
                    ),
                )

                feedback = _format_file_validator_feedback(
                    deterministic_error,
                    validator_report,
                    targeted_repair=targeted_repair,
                )

                candidate = await _repair_generated_file_with_feedback(
                    prompt_messages=prompt_messages,
                    model=route.model,
                    file_path=request.file_path,
                    previous_content=candidate,
                    validation_error=feedback,
                    targeted_repair=targeted_repair,
                    contract_text=contract_text,
                    passed_checks_text=passed_checks_text,
                    failed_checks_text=failed_checks_text,
                    repair_mode=(
                        "strict_contract_rewrite"
                        if attempt >= 2 and request.file_path.startswith("scripts/")
                        else "minimal_edit"
                    ),
                    skill_plan_entry=request.skill_plan_entry,
                )

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
    """Write generated file content to disk after deterministic validation."""
    skill_name = _validate_skill_name(request.skill_name)
    _validate_file_path(request.file_path)

    if request.file_path.startswith("assets/"):
        raise HTTPException(
            status_code=400,
            detail=f"{request.file_path} 属于 assets 静态素材目录，必须通过 /api/creator/upload-asset 上传，不能由模型写入。",
        )

    skill_dir = settings.skills_path / skill_name
    if not skill_dir.exists():
        raise HTTPException(status_code=404, detail=f"Skill 不存在：{skill_name}")

    try:
        content = _sanitize_generated_file_content(
            request.file_path,
            request.content,
            role=request.role,
            skill_plan_entry=request.skill_plan_entry,
        )

        if request.file_path.startswith("references/") and Path(request.file_path).suffix.lower() == ".md":
            content = _ensure_reference_metadata_frontmatter(
                file_path=request.file_path,
                content=content,
                purpose=getattr(request, "purpose", "") or "",
                skill_plan_entry=request.skill_plan_entry,
            )

        if request.file_path == "SKILL.md":
            # write-file is still part of file-by-file creation.
            # scripts/references may not exist yet, so do not require disk existence.
            _validate_skill_md_against_existing_files(
                skill_name,
                content,
                blueprint_text=request.blueprint_text or "",
                require_existing=False,
            )

            if request.blueprint_text:
                await _validate_skill_md_blueprint_alignment(
                    skill_name=skill_name,
                    content=content,
                    blueprint_text=request.blueprint_text,
                    skill_plan_entry=request.skill_plan_entry,
                    model=None,
                )

        elif request.file_path.startswith("references/"):
            _validate_reference_file_contract(
                request.file_path,
                content,
                str(request.skill_plan_entry.get("purpose", "")) if request.skill_plan_entry else "",
            )

        elif request.file_path.startswith("scripts/"):
            _validate_script_against_existing_skill_contract(
                skill_name,
                request.file_path,
                content,
            )
            _trial_run_generated_script_with_plan(
                skill_name,
                request.file_path,
                content,
                role=request.role,
                skill_plan_entry=request.skill_plan_entry,
            )

    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{request.file_path} 写入前校验失败：{exc}",
        ) from exc

    target_path = skill_dir / request.file_path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(content, encoding="utf-8")

    return WriteFileResponse(
        success=True,
        path=str(target_path),
        bytes=len(content.encode("utf-8")),
        message=f"已写入：{request.file_path}",
    )

@dataclass(frozen=True)
class E2EWorkflowCommand:
    ordinal: int
    source_path: str
    script_path: str
    raw_command: str
    runner: str
    argv_template: dict[str, Any]

@dataclass(frozen=True)
class E2EStepTrace:
    """Creator E2E workflow boundary trace.

    中间步骤只记录边界，不要求平台协议字段：
    - command JSON argv
    - command placeholders
    - real stdout JSON
    - payload.update(stdout_json) 后新增字段

    最后一步才要求 stdout JSON 能被 sandbox 平台消费。
    """

    ordinal: int
    script_path: str
    raw_command: str
    placeholders: list[str] = field(default_factory=list)
    argv_keys: list[str] = field(default_factory=list)
    stdout_keys: list[str] = field(default_factory=list)
    new_keys: list[str] = field(default_factory=list)
    argv_shape: dict[str, str] = field(default_factory=dict)
    stdout_shape: dict[str, str] = field(default_factory=dict)


def _iter_markdown_shell_blocks_with_source(content: str, *, source_path: str) -> list[tuple[str, str]]:
    """Return shell/bash fenced blocks in document order.

    Keep this parser aligned with _extract_script_command_templates(), otherwise
    file-level validation and final E2E validation can disagree.
    """
    blocks: list[tuple[str, str]] = []

    for info, body in _iter_markdown_fenced_blocks(content):
        if not _is_shell_fence_info(info):
            continue

        command = body.strip()
        if not command:
            continue

        if "scripts/" in command.replace("\\", "/"):
            blocks.append((source_path, command))

    return blocks

def _validate_skill_md_final_resource_existence(skill_name: str) -> None:
    """Final package-time check: SKILL.md references must exist on disk.

    This is not used during file generation. It should run only after all files
    have been generated/uploaded and before packaging.
    """
    skill_name = _validate_skill_name(skill_name)
    skill_dir = settings.skills_path / skill_name
    skill_md_path = skill_dir / "SKILL.md"

    if not skill_md_path.exists():
        raise ValueError("缺少 SKILL.md，无法进行最终资源存在性校验。")

    content = skill_md_path.read_text(encoding="utf-8")

    _validate_skill_md_against_existing_files(
        skill_name,
        content,
        blueprint_text="",
        require_existing=True,
    )

def _ordered_reference_paths_in_skill_md(skill_md: str) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for match in _SKILL_FILE_PATH_RE.finditer(skill_md or ""):
        path = match.group(1).strip()
        if path.startswith("references/") and path not in seen:
            seen.add(path)
            ordered.append(path)
    return ordered

_E2E_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def _json_shape(value: Any) -> str:
    """Compact runtime shape for E2E trace."""
    if isinstance(value, dict):
        keys = ", ".join(sorted(str(k) for k in value.keys())[:12])
        return f"object({keys})"
    if isinstance(value, list):
        if not value:
            return "list[0]"
        return f"list[{len(value)}]<{_json_shape(value[0])}>"
    if isinstance(value, str):
        return "string(non_empty)" if value else "string(empty)"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if value is None:
        return "null"
    return type(value).__name__


def _json_object_shape(obj: dict[str, Any]) -> dict[str, str]:
    return {str(k): _json_shape(v) for k, v in obj.items()}


def _format_json_shape(obj: dict[str, Any]) -> str:
    if not obj:
        return "{}"
    shape = _json_object_shape(obj)
    return json.dumps(shape, ensure_ascii=False, sort_keys=True)

_SANDBOX_TERMINAL_OUTPUT_KEYS = {
    "text",
    "story_text",
    "markdown",
    "image_path",
    "image_paths",
    "images",
    "pdf_path",
    "docx_path",
    "pptx_path",
    "html_path",
    "asset_paths",
    "file_paths",
}


def _e2e_trace_line(trace: E2EStepTrace) -> str:
    return (
        f"step={trace.ordinal} "
        f"script={trace.script_path} "
        f"placeholders={trace.placeholders} "
        f"argv_keys={trace.argv_keys} "
        f"stdout_keys={trace.stdout_keys} "
        f"new_keys={trace.new_keys} "
        f"argv_shape={json.dumps(trace.argv_shape, ensure_ascii=False, sort_keys=True)} "
        f"stdout_shape={json.dumps(trace.stdout_shape, ensure_ascii=False, sort_keys=True)}"
    )


def _format_e2e_trace(traces: list[E2EStepTrace]) -> str:
    if not traces:
        return "（暂无成功步骤）"
    return "\n".join(_e2e_trace_line(trace) for trace in traces)


def _has_sandbox_terminal_output(payload: dict[str, Any]) -> bool:
    """Return whether final stdout JSON is consumable by sandbox runtime.

    这里校验的是平台与 Skill 交互的最终输出协议，不校验中间步骤。
    """
    for key in ("text", "story_text", "markdown"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return True

    for key in ("image_path", "pdf_path", "docx_path", "pptx_path", "html_path"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return True

    for key in ("image_paths", "asset_paths", "file_paths"):
        value = payload.get(key)
        if (
            isinstance(value, list)
            and value
            and all(isinstance(item, str) and item.strip() for item in value)
        ):
            return True

    images = payload.get("images")
    if isinstance(images, list) and images:
        for item in images:
            if isinstance(item, dict) and isinstance(item.get("image_path"), str) and item["image_path"].strip():
                return True

    return False


def _validate_final_platform_output_contract(
    *,
    command: E2EWorkflowCommand,
    stdout_json: dict[str, Any],
    traces: list[E2EStepTrace],
) -> None:
    """Validate only the final workflow output against sandbox platform protocol.

    中间步骤 stdout 可以是任意 JSON object；
    最后一步必须输出 sandbox 能展示/下载的标准字段。
    """
    if _has_sandbox_terminal_output(stdout_json):
        return

    raise ValueError(
        _e2e_error(
            target=command.script_path,
            layer="final_platform_output_contract",
            message=(
                f"第 {command.ordinal} 步 {command.script_path} 是 workflow 最后一步，"
                "但 stdout JSON 没有包含 sandbox 可消费的最终输出字段。\n"
                f"当前 stdout 字段：{sorted(stdout_json.keys())}\n"
                f"平台允许的最终输出字段：{sorted(_SANDBOX_TERMINAL_OUTPUT_KEYS)}\n\n"
                "注意：中间步骤可以使用任意内部字段名，不需要对齐平台协议；"
                "但最后一步必须输出平台字段，例如 text、markdown、image_paths、images、"
                "pdf_path、docx_path、pptx_path、html_path、asset_paths 或 file_paths。\n\n"
                "已成功执行的前序边界 trace：\n"
                f"{_format_e2e_trace(traces)}"
            ),
        )
    )

def _placeholder_exprs_from_value(value: Any) -> list[str]:
    exprs: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for item in v.values():
                walk(item)
        elif isinstance(v, list):
            for item in v:
                walk(item)
        elif isinstance(v, str):
            exprs.extend(match.group(1).strip() for match in _E2E_PLACEHOLDER_RE.finditer(v))

    walk(value)
    return exprs


def _placeholder_root(expr: str) -> str:
    expr = str(expr or "").strip()
    if not expr:
        return ""
    return re.split(r"[.\[]", expr, maxsplit=1)[0].strip()


def _collect_placeholders_from_payload_template(template: dict[str, Any]) -> set[str]:
    placeholders: set[str] = set()
    for value in template.values():
        placeholders.update(_collect_placeholders_from_value(value))
    return placeholders


def _resolve_e2e_payload_expr(
    expr: str,
    *,
    payload: dict[str, Any],
    missing: list[str],
) -> Any:
    """Resolve placeholder expression against current runtime payload.

    Supports:
    - {{text_content}}
    - {{image_paths.0}}
    - {{foo.bar.0}}
    """
    expr = str(expr or "").strip()
    if not expr:
        missing.append(expr)
        return ""

    parts = expr.split(".")
    root = parts[0].strip()

    if root not in payload:
        missing.append(expr)
        return ""

    value: Any = payload[root]

    for part in parts[1:]:
        part = part.strip()
        if isinstance(value, list):
            try:
                index = int(part)
            except ValueError:
                missing.append(expr)
                return ""
            if index < 0 or index >= len(value):
                missing.append(expr)
                return ""
            value = value[index]
            continue

        if isinstance(value, dict):
            if part not in value:
                missing.append(expr)
                return ""
            value = value[part]
            continue

        missing.append(expr)
        return ""

    return value


def _e2e_command_placeholders(command: E2EWorkflowCommand) -> list[str]:
    return _placeholder_exprs_from_value(command.argv_template)

def _parse_e2e_workflow_command(
    *,
    command: str,
    ordinal: int,
    source_path: str,
) -> E2EWorkflowCommand | None:
    """Parse one SKILL.md shell command into executable E2E workflow step.

    Strict rule:
    - command must invoke scripts/*
    - scripts path must be followed by exactly one JSON object argv
    """
    command = (command or "").strip()
    if not command:
        return None

    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise ValueError(
            _e2e_error(
                target=source_path,
                layer="command_parse",
                message=(
                    f"{source_path} 第 {ordinal} 个命令无法被 shell 解析：{exc}\n"
                    f"原始命令：{command}"
                ),
            )
        ) from exc

    for idx, part in enumerate(parts):
        normalized = part.replace("\\", "/")

        if normalized.startswith("scripts/"):
            script_path = normalized
        elif "/scripts/" in normalized:
            script_path = "scripts/" + normalized.rsplit("/scripts/", 1)[1]
        else:
            continue

        runner = Path(parts[idx - 1]).name if idx > 0 else ""

        if idx + 1 >= len(parts):
            raise ValueError(
                _e2e_error(
                    target=source_path,
                    layer="command_argv_missing",
                    message=(
                        f"{source_path} 第 {ordinal} 步 {script_path} 缺少 JSON argv。\n"
                        f"命令必须形如：python {script_path} '{{\"key\":\"{{{{user_input}}}}\"}}'\n"
                        f"原始命令：{command}"
                    ),
                )
            )

        if idx + 2 < len(parts):
            raise ValueError(
                _e2e_error(
                    target=source_path,
                    layer="command_argv_extra",
                    message=(
                        f"{source_path} 第 {ordinal} 步 {script_path} 的 JSON argv 后存在额外参数：{parts[idx + 2:]!r}。\n"
                        "二次 E2E 校验要求脚本路径后只跟一个 JSON object argv。\n"
                        f"原始命令：{command}"
                    ),
                )
            )

        try:
            argv_template = json.loads(parts[idx + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(
                _e2e_error(
                    target=source_path,
                    layer="command_json_parse",
                    message=(
                        f"{source_path} 第 {ordinal} 步 {script_path} 的 JSON argv 不可解析：{exc.msg}\n"
                        f"argv={parts[idx + 1]!r}\n"
                        f"原始命令：{command}"
                    ),
                )
            ) from exc

        if not isinstance(argv_template, dict):
            raise ValueError(
                _e2e_error(
                    target=source_path,
                    layer="command_json_type",
                    message=(
                        f"{source_path} 第 {ordinal} 步 {script_path} 的 argv 必须是 JSON object。\n"
                        f"原始命令：{command}"
                    ),
                )
            )

        return E2EWorkflowCommand(
            ordinal=ordinal,
            source_path=source_path,
            script_path=script_path,
            raw_command=command,
            runner=runner,
            argv_template=argv_template,
        )

    return None

def _extract_e2e_workflow_commands(skill_dir: Path, skill_md: str) -> list[E2EWorkflowCommand]:
    """Extract executable E2E workflow commands from SKILL.md only.

    references/*.md are reference resources only and must never become E2E steps.
    """
    raw_blocks = _iter_markdown_shell_blocks_with_source(skill_md, source_path="SKILL.md")

    commands: list[E2EWorkflowCommand] = []
    seen: set[tuple[str, str]] = set()
    ordinal = 0

    for source_path, raw_command in raw_blocks:
        parsed = _parse_e2e_workflow_command(
            command=raw_command,
            ordinal=ordinal + 1,
            source_path="SKILL.md",
        )
        if parsed is None:
            continue

        key = (parsed.script_path, parsed.raw_command)
        if key in seen:
            continue

        seen.add(key)
        ordinal += 1
        commands.append(replace(parsed, ordinal=ordinal, source_path="SKILL.md"))

    return commands


def _collect_placeholders_from_value(value: Any) -> set[str]:
    """Collect root placeholder names from command argv template.

    支持：
    - {{topic}}
    - {{image_paths.0}}
    - {{result.pdf_path}}

    seed 初始输入时只取 root key。
    """
    placeholders: set[str] = set()

    if isinstance(value, str):
        for match in _E2E_PLACEHOLDER_RE.finditer(value):
            root = _placeholder_root(match.group(1))
            if root:
                placeholders.add(root)

    elif isinstance(value, dict):
        for item in value.values():
            placeholders.update(_collect_placeholders_from_value(item))

    elif isinstance(value, list):
        for item in value:
            placeholders.update(_collect_placeholders_from_value(item))

    return placeholders


def _collect_placeholders_from_payload_template(template: dict[str, Any]) -> set[str]:
    placeholders: set[str] = set()
    for value in template.values():
        placeholders.update(_collect_placeholders_from_value(value))
    return placeholders


def _render_e2e_template_value(
    value: Any,
    *,
    payload: dict[str, Any],
    missing: list[str],
) -> Any:
    if isinstance(value, str):
        whole = re.fullmatch(r"\{\{\s*([^{}]+?)\s*\}\}", value.strip())
        if whole:
            return _resolve_e2e_payload_expr(
                whole.group(1),
                payload=payload,
                missing=missing,
            )

        def replace_match(match: re.Match[str]) -> str:
            rendered = _resolve_e2e_payload_expr(
                match.group(1),
                payload=payload,
                missing=missing,
            )
            if isinstance(rendered, (dict, list)):
                return json.dumps(rendered, ensure_ascii=False)
            return str(rendered)

        return _E2E_PLACEHOLDER_RE.sub(replace_match, value)

    if isinstance(value, dict):
        return {
            str(key): _render_e2e_template_value(item, payload=payload, missing=missing)
            for key, item in value.items()
        }

    if isinstance(value, list):
        return [
            _render_e2e_template_value(item, payload=payload, missing=missing)
            for item in value
        ]

    return value


def _render_e2e_command_payload(
    command: E2EWorkflowCommand,
    *,
    payload: dict[str, Any],
    traces: list[E2EStepTrace] | None = None,
) -> dict[str, Any]:
    missing: list[str] = []

    rendered = {
        str(key): _render_e2e_template_value(value, payload=payload, missing=missing)
        for key, value in command.argv_template.items()
    }

    if missing:
        unique_missing = sorted(set(missing))
        available = sorted(payload.keys())

        raise ValueError(
            _e2e_error(
                target=command.source_path,
                layer="dataflow_missing_input",
                message=(
                    f"第 {command.ordinal} 步 {command.script_path} 的命令模板引用了当前 payload 中不存在的字段："
                    f"{', '.join(unique_missing)}。\n"
                    f"当前可用字段：{', '.join(available) or '(无)'}。\n"
                    f"命令来源：{command.source_path}\n"
                    f"原始命令：{command.raw_command}\n\n"
                    "已成功执行的前序边界 trace：\n"
                    f"{_format_e2e_trace(traces or [])}\n\n"
                    "这只表示 SKILL.md 当前失败步骤的命令占位符，"
                    "无法从用户初始输入或前序 stdout JSON 中解析。"
                    "优先局部修复当前失败步骤的 SKILL.md 命令块；"
                    "不要修改已成功 trace 对应的前序步骤。"
                ),
            )
        )

    return rendered


def _seed_initial_e2e_payload(commands: list[E2EWorkflowCommand]) -> dict[str, Any]:
    """Seed only the first command's user-facing placeholders.

    This simulates runtime user input extraction, but does not seed downstream
    placeholders. Therefore if later steps require fields not produced by upstream
    stdout, E2E validation will fail strictly.
    """
    if not commands:
        return {}

    seed: dict[str, Any] = {}
    for key in sorted(_collect_placeholders_from_payload_template(commands[0].argv_template)):
        seed[key] = _sample_value_for_placeholder(key)

    # Also expose common aliases only if the first command actually uses them.
    return seed


def _e2e_error(*, target: str, layer: str, message: str) -> str:
    return f"E2E_REPAIR_TARGET={target}\nE2E_LAYER={layer}\n{message}"


def _e2e_repair_target_from_errors(errors: list[str]) -> str:
    for error in errors:
        match = re.search(r"^E2E_REPAIR_TARGET=([^\n]+)", error)
        if match:
            target = match.group(1).strip()
            if target:
                return target
    return "SKILL.md"


def _copy_skill_dir_for_e2e(skill_name: str) -> tuple[tempfile.TemporaryDirectory, Path]:
    source_dir = settings.skills_path / skill_name
    tmp = tempfile.TemporaryDirectory(prefix="creator-e2e-skill-")
    tmp_root = Path(tmp.name)
    trial_skill_dir = tmp_root / skill_name
    shutil.copytree(
        source_dir,
        trial_skill_dir,
        ignore=shutil.ignore_patterns(
            ".venv",
            "__pycache__",
            "*.pyc",
            ".pytest_cache",
        ),
    )
    return tmp, trial_skill_dir


def _runner_matches_command_runtime(command: E2EWorkflowCommand, entry: SkillPlanEntry) -> bool:
    runner = Path(command.runner or "").name
    if entry.runtime == "python":
        return runner.startswith("python")
    if entry.runtime == "node":
        return runner == "node"
    if entry.runtime in {"bash", "shell"}:
        return runner in {"bash", "sh"}
    return True


def _execute_e2e_python_command(
    *,
    command: E2EWorkflowCommand,
    trial_skill_dir: Path,
    rendered_payload: dict[str, Any],
    venv_python: Path,
) -> subprocess.CompletedProcess[str]:
    script_abs = trial_skill_dir / command.script_path
    return subprocess.run(
        [
            str(venv_python),
            str(script_abs),
            json.dumps(rendered_payload, ensure_ascii=False),
        ],
        cwd=str(trial_skill_dir / "scripts"),
        capture_output=True,
        text=True,
        timeout=_SCRIPT_TRIAL_TIMEOUT_SECONDS,
        env={**_build_script_runtime_env(trial_skill_dir), "SKILL_TRIAL_RUN": "1"},
    )


def _execute_e2e_node_command(
    *,
    command: E2EWorkflowCommand,
    trial_skill_dir: Path,
    rendered_payload: dict[str, Any],
) -> subprocess.CompletedProcess[str]:
    script_abs = trial_skill_dir / command.script_path
    return subprocess.run(
        [
            "node",
            str(script_abs),
            json.dumps(rendered_payload, ensure_ascii=False),
        ],
        cwd=str(trial_skill_dir / "scripts"),
        capture_output=True,
        text=True,
        timeout=_SCRIPT_TRIAL_TIMEOUT_SECONDS,
        env={**_build_script_runtime_env(trial_skill_dir), "SKILL_TRIAL_RUN": "1"},
    )


def _execute_e2e_shell_command(
    *,
    command: E2EWorkflowCommand,
    trial_skill_dir: Path,
    rendered_payload: dict[str, Any],
) -> subprocess.CompletedProcess[str]:
    script_abs = trial_skill_dir / command.script_path
    runner = "sh" if Path(command.runner or "").name == "sh" else "bash"
    return subprocess.run(
        [
            runner,
            str(script_abs),
            json.dumps(rendered_payload, ensure_ascii=False),
        ],
        cwd=str(trial_skill_dir / "scripts"),
        capture_output=True,
        text=True,
        timeout=_SCRIPT_TRIAL_TIMEOUT_SECONDS,
        env={**_build_script_runtime_env(trial_skill_dir), "SKILL_TRIAL_RUN": "1"},
    )


def _parse_e2e_stdout_json(
    *,
    command: E2EWorkflowCommand,
    proc: subprocess.CompletedProcess[str],
    trial_skill_dir: Path,
    content: str,
    entry: SkillPlanEntry,
    rendered_payload: dict[str, Any],
) -> dict[str, Any]:
    if proc.returncode != 0:
        raise ValueError(
            _e2e_error(
                target=command.script_path,
                layer="script_exit",
                message=(
                    f"第 {command.ordinal} 步 {command.script_path} 执行失败。\n"
                    f"exit_code={proc.returncode}\n"
                    f"argv={json.dumps(rendered_payload, ensure_ascii=False)}\n"
                    f"stdout={proc.stdout[-4000:]}\n"
                    f"stderr={proc.stderr[-4000:]}"
                ),
            )
        )

    try:
        _validate_trial_stdout_json(
            stdout=proc.stdout,
            content=content,
            args=[json.dumps(rendered_payload, ensure_ascii=False)],
            role=entry.role,
            skill_dir=trial_skill_dir,
            skill_plan_entry=entry.__dict__,
        )
    except ValueError as exc:
        raise ValueError(
            _e2e_error(
                target=command.script_path,
                layer="stdout_contract",
                message=(
                    f"第 {command.ordinal} 步 {command.script_path} stdout JSON 不符合运行时合同。\n"
                    f"argv={json.dumps(rendered_payload, ensure_ascii=False)}\n"
                    f"stdout={proc.stdout[-4000:]}\n"
                    f"stderr={proc.stderr[-4000:]}\n"
                    f"错误={exc}"
                ),
            )
        ) from exc

    try:
        parsed = json.loads((proc.stdout or "").strip())
    except json.JSONDecodeError as exc:
        raise ValueError(
            _e2e_error(
                target=command.script_path,
                layer="stdout_json_parse",
                message=(
                    f"第 {command.ordinal} 步 {command.script_path} stdout 不是合法 JSON。\n"
                    f"stdout={proc.stdout[-4000:]}"
                ),
            )
        ) from exc

    if not isinstance(parsed, dict):
        raise ValueError(
            _e2e_error(
                target=command.script_path,
                layer="stdout_json_type",
                message=f"第 {command.ordinal} 步 {command.script_path} stdout 必须是 JSON object。",
            )
        )

    return parsed


def _validate_e2e_command_static(
    *,
    command: E2EWorkflowCommand,
    trial_skill_dir: Path,
    skill_md: str,
) -> SkillPlanEntry:
    source_path = trial_skill_dir / command.script_path
    if not source_path.is_file():
        raise ValueError(
            _e2e_error(
                target=command.source_path,
                layer="script_missing",
                message=f"第 {command.ordinal} 步引用的脚本不存在：{command.script_path}",
            )
        )

    entry = _skill_plan_entry_for_file(file_path=command.script_path, blueprint_text=skill_md)
    if not _runner_matches_command_runtime(command, entry):
        raise ValueError(
            _e2e_error(
                target=command.source_path,
                layer="runtime_mismatch",
                message=(
                    f"第 {command.ordinal} 步 {command.script_path} 的命令 runner={command.runner!r} "
                    f"与 SkillPlan.runtime={entry.runtime!r} 不一致。\n"
                    f"原始命令：{command.raw_command}"
                ),
            )
        )

    expected_keys = set(entry.inputs or [])
    actual_keys = {str(key) for key in command.argv_template.keys()}
    if expected_keys and actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)
        extra = sorted(actual_keys - expected_keys)
        details = {
            "code": "command_block.skillplan_inputs.exact",
            "target_script": command.script_path,
            "expected_keys": sorted(expected_keys),
            "actual_keys": sorted(actual_keys),
            "missing_keys": missing,
            "extra_keys": extra,
            "expected_payload_shape": {key: f"{{{{{key}}}}}" for key in sorted(expected_keys)},
            "upstream_available_outputs": [],
            "minimal_edit": "只修改该命令 JSON object；删除 extra_keys；补齐 missing_keys；保留脚本路径和 runner。",
        }
        raise ValueError(
            _e2e_error(
                target=command.source_path,
                layer="skill_md_contract",
                message=(
                    "command_block.skillplan_inputs.exact\n"
                    "命令 JSON keys 必须与当前脚本在 SkillPlan 中声明的 inputs 一致。"
                    "平台不要求固定字段名；字段名由该 Skill 的 SkillPlan 决定。\n"
                    f"structured_error={json.dumps(details, ensure_ascii=False, sort_keys=True)}\n"
                    f"原始命令：{command.raw_command}"
                ),
            )
        )

    content = source_path.read_text(encoding="utf-8")
    try:
        _validate_script_contract_static(
            file_path=command.script_path,
            content=content,
            skill_md=skill_md,
        )
    except ValueError as exc:
        raise ValueError(
            _e2e_error(
                target=command.script_path,
                layer="script_static_contract",
                message=f"第 {command.ordinal} 步 {command.script_path} 静态合同失败：{exc}",
            )
        ) from exc

    return entry


def _run_skill_workflow_e2e_once(skill_name: str) -> list[str]:
    """Run SKILL.md workflow once with soft internal dataflow validation.

    规则：
    - 只执行 SKILL.md 中的 bash/sh/shell fenced command block。
    - references/*.md 只作为参考资料，不作为执行源。
    - 中间步骤 stdout 只要求是合法非空 JSON object，不要求平台字段。
    - 中间步骤字段名由 Skill 自己流转，不强制对齐 sandbox 平台协议。
    - 最后一步 stdout 必须包含 sandbox 可消费的最终输出字段。
    """
    source_skill_dir = settings.skills_path / skill_name
    skill_md_path = source_skill_dir / "SKILL.md"

    if not skill_md_path.is_file():
        return [
            _e2e_error(
                target="SKILL.md",
                layer="missing_skill_md",
                message="无法加载 SKILL.md。",
            )
        ]

    skill_md = skill_md_path.read_text(encoding="utf-8")
    errors: list[str] = []

    try:
        _validate_skill_md_contract(skill_md, skill_md)
    except ValueError as exc:
        errors.append(
            _e2e_error(
                target="SKILL.md",
                layer="skill_md_contract",
                message=f"SKILL.md 合同错误：{exc}",
            )
        )
        return errors

    try:
        commands = _extract_e2e_workflow_commands(source_skill_dir, skill_md)
    except ValueError as exc:
        return [str(exc)]

    script_files = (
        sorted((source_skill_dir / "scripts").glob("*.py"))
        if (source_skill_dir / "scripts").is_dir()
        else []
    )

    if script_files and not commands:
        shell_like_blocks = [
            body
            for info, body in _iter_markdown_fenced_blocks(skill_md)
            if _is_shell_fence_info(info) and "scripts/" in body.replace("\\", "/")
        ]

        hint = ""
        if shell_like_blocks:
            hint = (
                "\n检测到 SKILL.md 中存在疑似 scripts/ 命令块，但未能解析为 E2E workflow。"
                "请检查 fenced code block 是否是标准 Markdown 形态，"
                "以及命令是否形如：python scripts/name.py '{\"key\":\"{{key}}\"}'。"
            )

        return [
            _e2e_error(
                target="SKILL.md",
                layer="workflow_missing",
                message=(
                    "Skill 包含 scripts/*.py，但 SKILL.md 中没有可执行 bash/sh/shell 命令块。\n"
                    "必须在 SKILL.md 中按真实工作流顺序写出脚本调用命令。\n"
                    "references/*.md 只能作为参考资料，不会被 E2E 解析为执行步骤。"
                    f"{hint}"
                ),
            )
        ]

    tmp_handle: tempfile.TemporaryDirectory | None = None

    try:
        tmp_handle, trial_skill_dir = _copy_skill_dir_for_e2e(skill_name)
        trial_skill_md = (trial_skill_dir / "SKILL.md").read_text(encoding="utf-8")

        payload: dict[str, Any] = _seed_initial_e2e_payload(commands)
        traces: list[E2EStepTrace] = []

        venv_python: Path | None = None
        if any(command.script_path.endswith(".py") for command in commands):
            try:
                venv_python = _get_skill_venv_python(trial_skill_dir)
                for command in commands:
                    if command.script_path.endswith(".py"):
                        entry = _skill_plan_entry_for_file(
                            file_path=command.script_path,
                            blueprint_text=trial_skill_md,
                        )
                        _install_capability_dependencies(venv_python, entry.required_capabilities)
                        _scan_and_install_python_deps(
                            trial_skill_dir / command.script_path,
                            venv_python,
                        )
            except RuntimeError as exc:
                return [
                    _e2e_error(
                        target="scripts",
                        layer="venv_prepare",
                        message=f"端到端试运行环境准备失败：{exc}",
                    )
                ]

        for index, command in enumerate(commands):
            try:
                entry = _validate_e2e_command_static(
                    command=command,
                    trial_skill_dir=trial_skill_dir,
                    skill_md=trial_skill_md,
                )

                content = (trial_skill_dir / command.script_path).read_text(encoding="utf-8")

                rendered_payload = _render_e2e_command_payload(
                    command,
                    payload=payload,
                    traces=traces,
                )

                if entry.runtime == "python":
                    if venv_python is None:
                        raise ValueError("python venv 未初始化。")
                    proc = _execute_e2e_python_command(
                        command=command,
                        trial_skill_dir=trial_skill_dir,
                        rendered_payload=rendered_payload,
                        venv_python=venv_python,
                    )

                elif entry.runtime == "node":
                    proc = _execute_e2e_node_command(
                        command=command,
                        trial_skill_dir=trial_skill_dir,
                        rendered_payload=rendered_payload,
                    )

                elif entry.runtime in {"bash", "shell"}:
                    proc = _execute_e2e_shell_command(
                        command=command,
                        trial_skill_dir=trial_skill_dir,
                        rendered_payload=rendered_payload,
                    )

                else:
                    raise ValueError(
                        _e2e_error(
                            target=command.script_path,
                            layer="unsupported_runtime",
                            message=(
                                f"第 {command.ordinal} 步 {command.script_path} "
                                f"runtime={entry.runtime} 暂不支持端到端执行。"
                            ),
                        )
                    )

                stdout_json = _parse_e2e_stdout_json(
                    command=command,
                    proc=proc,
                    trial_skill_dir=trial_skill_dir,
                    content=content,
                    entry=entry,
                    rendered_payload=rendered_payload,
                )

                is_final_step = index == len(commands) - 1
                if is_final_step:
                    _validate_final_platform_output_contract(
                        command=command,
                        stdout_json=stdout_json,
                        traces=traces,
                    )

                before_keys = set(payload.keys())
                payload.update(stdout_json)
                new_keys = sorted(set(payload.keys()) - before_keys)

                trace = E2EStepTrace(
                    ordinal=command.ordinal,
                    script_path=command.script_path,
                    raw_command=command.raw_command,
                    placeholders=sorted(_e2e_command_placeholders(command)),
                    argv_keys=sorted(str(key) for key in rendered_payload.keys()),
                    stdout_keys=sorted(str(key) for key in stdout_json.keys()),
                    new_keys=new_keys,
                    argv_shape=_json_object_shape(rendered_payload),
                    stdout_shape=_json_object_shape(stdout_json),
                )
                traces.append(trace)

                logger.info("[Creator][E2E] %s", _e2e_trace_line(trace))

            except subprocess.TimeoutExpired as exc:
                errors.append(
                    _e2e_error(
                        target=command.script_path,
                        layer="timeout",
                        message=(
                            f"第 {command.ordinal} 步 {command.script_path} 端到端执行超时：{exc}\n\n"
                            "已成功执行的前序边界 trace：\n"
                            f"{_format_e2e_trace(traces)}"
                        ),
                    )
                )
                break

            except ValueError as exc:
                message = str(exc)
                if "已成功执行的前序边界 trace" not in message and "已成功执行的前序步骤" not in message:
                    message += "\n\n已成功执行的前序边界 trace：\n" + _format_e2e_trace(traces)
                errors.append(message)
                break

    finally:
        if tmp_handle is not None:
            tmp_handle.cleanup()

    return errors



def _placeholder_root_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\{\{\s*([A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)*)\s*\}\}", value.strip())
    if not match:
        return None
    return match.group(1).split(".", 1)[0]


def _values_for_skill_plan_command(
    *,
    entry: SkillPlanEntry,
    existing_payload: dict[str, Any] | None,
    available_values: set[str],
) -> dict[str, str]:
    values: dict[str, str] = {}
    existing_payload = existing_payload or {}
    reusable_by_root: dict[str, str] = {}
    for value in existing_payload.values():
        root = _placeholder_root_name(value)
        if root:
            reusable_by_root[root] = value

    for input_name in entry.inputs or ["payload"]:
        current = existing_payload.get(input_name)
        if isinstance(current, str) and _placeholder_root_name(current) in available_values:
            values[input_name] = current
        elif input_name in reusable_by_root and input_name in available_values:
            values[input_name] = reusable_by_root[input_name]
        else:
            values[input_name] = f"{{{{{input_name}}}}}"
    return values


def _patch_skill_md_command_payloads_from_skill_plan(content: str, blueprint_text: str) -> tuple[str, list[dict[str, Any]]]:
    """Deterministically align SKILL.md command JSON argv with SkillPlan inputs.

    This patcher is deliberately generic: it never maps business field names or
    branches on script names.  The only source of truth for keys is each
    SkillPlanEntry.inputs; outputs only update the placeholder dataflow context.
    """
    parsed = parse_blueprint([{"role": "assistant", "content": blueprint_text}])
    entries = [entry for entry in (parsed.skill_plan.files if parsed.skill_plan else []) if entry.file_type == "script"]
    if not entries:
        return content, []

    entry_by_path = {entry.path: entry for entry in entries}
    available_values: set[str] = set(entries[0].inputs or [])
    patched: list[dict[str, Any]] = []
    lines = (content or "").splitlines(keepends=True)
    output: list[str] = []
    in_shell_fence = False
    fence_char = "`"
    fence_len = 3

    for raw_line in lines:
        stripped = raw_line.lstrip()
        fence_open = re.match(r"(`{3,}|~{3,})([^\n`]*)\n?$", stripped.rstrip("\n"))
        if fence_open:
            fence = fence_open.group(1)
            info = (fence_open.group(2) or "").strip()
            if not in_shell_fence:
                in_shell_fence = _is_shell_fence_info(info)
                fence_char = fence[0]
                fence_len = len(fence)
            else:
                close_match = re.match(rf"{re.escape(fence_char)}{{{fence_len},}}\s*$", stripped.rstrip("\n"))
                if close_match:
                    in_shell_fence = False
            output.append(raw_line)
            continue

        line_body = raw_line.rstrip("\r\n")
        line_ending = raw_line[len(line_body):]
        replacement = line_body
        if in_shell_fence and line_body.strip():
            for script_path, entry in entry_by_path.items():
                payload = _command_payload_object(line_body.strip(), script_path)
                if payload is None:
                    continue
                actual_keys = set(payload.keys())
                expected_keys = set(entry.inputs or [])
                if expected_keys and actual_keys != expected_keys:
                    values = _values_for_skill_plan_command(
                        entry=entry,
                        existing_payload=payload,
                        available_values=available_values | set(entry.inputs or []),
                    )
                    replacement = render_script_command_from_skill_plan(entry, values)
                    patched.append({
                        "target_script": script_path,
                        "expected_keys": sorted(expected_keys),
                        "actual_keys": sorted(actual_keys),
                        "missing_keys": sorted(expected_keys - actual_keys),
                        "extra_keys": sorted(actual_keys - expected_keys),
                        "expected_payload_shape": {key: values[key] for key in sorted(expected_keys)},
                        "upstream_available_outputs": sorted(available_values),
                    })
                available_values.update(entry.outputs or [])
                break
        output.append(replacement + line_ending)

    return "".join(output), patched

async def _repair_existing_file_for_e2e_failure(
    *,
    skill_name: str,
    target_path: str,
    e2e_errors: list[str],
    requested_model: str | None = None,
) -> str:
    """Repair an existing SKILL.md/script file using E2E feedback.

    E2E workflow is defined only by SKILL.md. references/*.md are context
    resources and should not be selected as executable workflow repair targets.
    """
    _validate_file_path(target_path)

    if target_path.startswith("references/"):
        logger.info(
            "[Creator][E2E] remap reference repair target to SKILL.md target=%s",
            target_path,
        )
        target_path = "SKILL.md"

    skill_dir = settings.skills_path / skill_name
    target_file = skill_dir / target_path

    if not target_file.is_file():
        raise ValueError(f"端到端修复目标不存在：{target_path}")

    current_content = target_file.read_text(encoding="utf-8")
    skill_md = (
        (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        if (skill_dir / "SKILL.md").is_file()
        else ""
    )

    all_file_summaries: list[str] = []
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(skill_dir).as_posix()
        if rel.startswith(".venv/") or "__pycache__" in rel:
            continue
        if rel == target_path:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        all_file_summaries.append(f"\n--- FILE {rel} ---\n{text[-6000:]}")

    route = route_creator_file_model(
        file_path=target_path,
        purpose=(
            "修复最终端到端工作流校验失败；"
            "E2E 只以 SKILL.md 命令块为执行源，references/*.md 只是参考资料；"
            "中间步骤允许 Skill 自己使用内部 JSON 字段流转，最终步骤必须对齐 sandbox 平台输出协议；"
            "只修 E2E_REPAIR_TARGET 指向的局部文件，不要修改已成功 trace 对应部分。"
        ),
        requested_model=requested_model,
    )
    model = route.model

    _log_creator_model_usage(
        phase="e2e_repair.route",
        skill_name=skill_name,
        file_path=target_path,
        route=route,
        extra=f"errors={len(e2e_errors)}",
    )

    deterministic_error = "\n\n".join(e2e_errors)[-12000:]

    if target_path == "SKILL.md" and "command_block.skillplan_inputs.exact" in deterministic_error:
        patched_content, patch_details = _patch_skill_md_command_payloads_from_skill_plan(
            current_content,
            skill_md or current_content,
        )
        if patch_details and patched_content != current_content:
            _validate_skill_md_against_existing_files(skill_name, patched_content)
            target_file.write_text(patched_content, encoding="utf-8")
            logger.info(
                "[Creator][E2E] deterministic SKILL.md command payload patch skill=%s details=%s",
                skill_name,
                json.dumps(patch_details, ensure_ascii=False),
            )
            return target_path

    contract_text = _build_generated_file_contract_text(
        target_path,
        skill_md + "\n".join(all_file_summaries)[-16000:],
        "最终端到端数据流修复",
        role=None,
        skill_plan_entry=None,
    )

    if target_path == "SKILL.md":
        target_rule = (
            "你正在修复 SKILL.md 的 workflow 执行块。"
            "E2E 只执行 SKILL.md 中的 bash/sh/shell fenced command block，"
            "references/*.md 只是参考资料，不会作为执行步骤。"
            "不要重新设计协议，不要加入 Runtime Contract JSON。"
            "不要修改平台与 Skill 交互的最终 stdout 字段协议。"
            "中间步骤允许使用任意内部 JSON 字段名流转；"
            "只需要让当前失败步骤的命令块 JSON argv 占位符能从用户初始输入或前序 stdout JSON 中解析。"
            "错误信息中的“已成功执行的前序边界 trace”代表已经跑通的步骤，"
            "这些步骤的命令块、字段名和脚本调用方式不要改。"
        )
    elif target_path.startswith("scripts/"):
        target_rule = (
            "你正在修复脚本源码。"
            "不要重新设计 SKILL.md，不要改其它脚本。"
            "脚本只需要满足当前 SKILL.md 命令块传入的 JSON argv，"
            "并在 stdout 输出合法 JSON object。"
            "中间脚本 stdout 可以使用内部字段名；"
            "如果这是最后一步，stdout 必须包含 sandbox 平台可消费的最终字段："
            "text、story_text、markdown、image_path、image_paths、images、"
            "pdf_path、docx_path、pptx_path、html_path、asset_paths 或 file_paths。"
            "如果后续步骤需要某个字段，当前脚本 stdout JSON 必须真实输出该字段。"
            "错误信息中的“已成功执行的前序边界 trace”代表前序步骤已通过，不要改变前序字段名。"
            "不要输出 Markdown，不要输出 error 字段，不要 mock/placeholder。"
        )
    else:
        target_rule = (
            "只修复 E2E_REPAIR_TARGET 指向的文件。"
            "不要重新设计流程，不要修改已成功 trace 对应的步骤。"
        )

    prompt_messages = [
        {
            "role": "system",
            "content": (
                "你是 superskills Creator 的最终端到端修复模型。"
                "你只能输出目标文件的完整新内容，不能输出解释、Markdown 外壳或多文件 bundle。"
                "E2E 工作流只由 SKILL.md 定义；references/*.md 是参考资料，不是执行步骤。"
                "修复目标是让 SKILL.md workflow 从头到尾真实执行通过。"
                "中间步骤只需要 JSON 边界能流转，不要求使用平台字段；"
                "最终步骤必须输出与 sandbox 运行时一致的平台字段。"
                "错误信息中的已成功前序边界 trace 是冻结区，不要重复修改已经通过的部分。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"Skill 名称：{skill_name}\n"
                f"当前需要修复的文件：{target_path}\n\n"
                f"{target_rule}\n\n"
                "端到端失败信息：\n"
                f"{deterministic_error}\n\n"
                "当前 SKILL.md：\n"
                f"{skill_md[-12000:]}\n\n"
                "其它相关文件摘要：\n"
                f"{''.join(all_file_summaries)[-20000:]}\n\n"
                "请只输出修复后的目标文件完整内容。"
            ),
        },
    ]

    repaired = await _repair_generated_file_with_feedback(
        prompt_messages=prompt_messages,
        model=model,
        file_path=target_path,
        previous_content=current_content,
        validation_error=deterministic_error,
        targeted_repair=target_rule,
        contract_text=contract_text,
        repair_mode="minimal_edit",
        skill_plan_entry=None,
    )

    sanitized = _sanitize_generated_file_content(target_path, repaired)

    if target_path == "SKILL.md":
        _validate_skill_md_against_existing_files(skill_name, sanitized)
    elif target_path.startswith("references/"):
        _validate_reference_file_contract(target_path, sanitized, skill_md)
    elif target_path.startswith("assets/"):
        _validate_asset_file_contract(target_path, sanitized)
    elif target_path.startswith("scripts/"):
        _validate_script_against_existing_skill_contract(skill_name, target_path, sanitized)
        _trial_run_generated_script(skill_name, target_path, sanitized)

    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text(sanitized, encoding="utf-8")

    return target_path

def _iter_markdown_fenced_blocks(content: str) -> list[tuple[str, str]]:
    """Return fenced code blocks as (info_string, body).

    This parser is intentionally line-based instead of one regex so it accepts
    normal Markdown shapes generated by LLMs, including fences indented under
    list items:

        1. step
           ```bash
           python scripts/foo.py '{"topic":"{{topic}}"}'
           ```

    It also accepts CRLF, trailing spaces after fences, and ~~~ fences.
    """
    lines = (content or "").splitlines()
    blocks: list[tuple[str, str]] = []

    in_block = False
    fence_char = ""
    fence_len = 0
    info = ""
    body_lines: list[str] = []

    open_re = re.compile(r"^\s*(`{3,}|~{3,})([^\n`]*)\s*$")

    for line in lines:
        if not in_block:
            match = open_re.match(line)
            if not match:
                continue

            fence = match.group(1)
            fence_char = fence[0]
            fence_len = len(fence)
            info = (match.group(2) or "").strip().lower()
            body_lines = []
            in_block = True
            continue

        close_re = re.compile(rf"^\s*{re.escape(fence_char)}{{{fence_len},}}\s*$")
        if close_re.match(line):
            blocks.append((info, "\n".join(body_lines).strip()))
            in_block = False
            fence_char = ""
            fence_len = 0
            info = ""
            body_lines = []
            continue

        body_lines.append(line)

    return blocks


def _is_shell_fence_info(info: str) -> bool:
    """Return whether a fenced block should be treated as shell commands.

    Empty info is accepted only when the block contains scripts/ later, matching
    the previous permissive behavior.
    """
    normalized = (info or "").strip().lower()
    if not normalized:
        return True
    first = normalized.split()[0]
    return first in {"bash", "sh", "shell", "zsh"}

def _validate_skill_package_smoke(skill_name: str, *, mode: str = "trial") -> list[str]:
    """Strict end-to-end workflow validation.

    This replaces the old per-script smoke test with a real SKILL.md workflow run:
    upstream stdout JSON is parsed and merged into payload, then used to render
    downstream JSON argv placeholders.
    """
    return _run_skill_workflow_e2e_once(skill_name)

@router.post("/validate-skill", response_model=SkillActionResponse)
async def validate_skill(request: SkillActionRequest):
    """Validate and strictly E2E-run a Skill package.

    Flow:
    1. basic SKILL.md validation
    2. strict SKILL.md workflow E2E execution
    3. if failed, route feedback to MD/code model and rewrite the failing file
    4. retry until success or max attempts exhausted
    """
    skill_name = _validate_skill_name(request.skill_name)

    result = run_action({"action": "validate", "name": skill_name})
    if not result["success"]:
        return SkillActionResponse(
            success=False,
            path=result.get("path"),
            message=result["message"],
        )

    max_attempts = max(0, min(int(request.max_e2e_repair_attempts or 0), 10))
    attempt = 0
    repair_logs: list[str] = []

    while True:
        e2e_errors = _validate_skill_package_smoke(skill_name, mode="trial")
        if not e2e_errors:
            suffix = ""
            if repair_logs:
                suffix = "\n\n端到端自动修复记录：\n" + "\n".join(repair_logs)
            return SkillActionResponse(
                success=True,
                path=result.get("path"),
                message=result["message"] + "\n严格端到端工作流校验通过：SKILL.md 命令已按顺序真实执行，中间 JSON 边界已流转，最终 stdout 已对齐 sandbox 平台输出协议。" + suffix,
            )

        if not request.auto_repair or attempt >= max_attempts:
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
            )

        target_path = _e2e_repair_target_from_errors(e2e_errors)
        try:
            repaired_target = await _repair_existing_file_for_e2e_failure(
                skill_name=skill_name,
                target_path=target_path,
                e2e_errors=e2e_errors,
                requested_model=request.model,
            )
            attempt += 1
            repair_logs.append(
                f"第 {attempt} 轮：根据端到端失败反馈修复 {repaired_target}"
            )
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
        e2e_errors = _validate_skill_package_smoke(skill_name, mode="trial")
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
    
    Creates empty files based on the blueprint analysis result.
    This ensures the file structure matches exactly what the user confirmed.
    
    Workflow:
    1. Create main skill directory
    2. Create required subdirectories (scripts/, references/, assets/)
    3. Create empty files based on the blueprint file list
    """
    skill_name = _validate_skill_name(request.skill_name)
    skill_root = settings.skill_public_dir / skill_name
    
    try:
        # Create main skill directory
        skill_root.mkdir(parents=True, exist_ok=True)
        
        files_created = 0
        
        for file_spec in request.files:
            file_path = skill_root / file_spec.path
            
            # Ensure parent directory exists
            file_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Create empty file if it doesn't exist
            if not file_path.exists():
                file_path.touch()
                files_created += 1
        
        return InitFromBlueprintResponse(
            success=True,
            path=str(skill_root),
            files_created=files_created,
            message=f"已创建 {files_created} 个文件",
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
