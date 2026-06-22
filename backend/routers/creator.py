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
from ..services.blueprint_parser import BlueprintPlan, BlueprintShapeError, clean_blueprint_body_text, parse_blueprint
from ..services.skill_plan import SkillPlanEntry, ScriptRuntimeSpec, build_skill_plan_entry, capabilities_for_role, command_template_for_entry, default_io_for_file_kind, file_role_classifier, file_type_for_path, file_kind_for_path, language_for_path, runtime_for_language, normalize_required_capabilities, is_runtime_artifact_semantic, command_payload_placeholders, render_script_command_from_skill_plan
from ..services.creator_tool_registry import get_tool_capability, list_tool_capabilities, tool_status, resolve_tools_for_skill_plan_entry, function_cards_for_tool, resolve_tool_snippets_for_context, tool_snippet_prompt
from ..services.llm_proxy import complete_chat_once, stream_chat
from ..services.model_router import VALIDATOR_TASK, route_creator_file_model, route_model
from ..services.skill_executor import _build_script_runtime_env, run_action
from ..services.skill_creator_dry_run import build_creator_external_input_context
from ..services.artifact_validator import validate_stdout_file_outputs, FileOutputValidationError
from ..services.markdown_metadata import (
    parse_frontmatter,
    validate_skill_frontmatter,
    validate_reference_frontmatter,
)
from ..services.creator_contracts import (
    compile_canonical_file_contract,
    contract_payload,
    refine_contract_with_resolution,
    resolve_implementation,
    call_template_for_tool,
    validate_script_functional_evidence,
)
from .chat_utils import _get_skill_venv_python

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
- 如果确实需要运行 scripts/ 下的脚本，必须使用标准 Markdown fenced code block，且 info string 必须是 bash。第一条脚本命令示例应只引用 external envelope 或显式结构化来源，例如：
  ```bash
  python scripts/<script-name> '{"payload":{"user_request":"{{user_request}}","input_files":"{{input_files}}","fields":"{{fields}}","options":"{{options}}"}}'
  ```
- 命令示例必须与脚本真实接口一致：脚本读 JSON argv 时，示例就传 JSON；脚本读 stdin 时，正文就说明 stdin 内容。禁止让运行时主模型根据脚本名临时猜 CLI flags。
- 参数映射用普通 Markdown 列表说明通用来源：命令示例优先引用 external envelope（user_request/input/text/input_files/files/fields/options）或显式 fields/defaults/input binding。第一轮 SKILL.md 只约束可解析命令形态，不要求证明后续 placeholder 来自前序 stdout。
- 只有 assistant 在 Sandbox 当轮回复中输出的 fenced code block 才会被宿主解析和执行；SKILL.md 中的 block 是运行说明/示例，不会在加载时自动执行。
- 如果需要写文件，用普通 Markdown 说明 assistant 应输出 `写入文件：<path>` 或 `保存到：<path>`，并把完整文件内容放在紧随其后的 fenced code block。
- assistant 不得假装脚本已经执行；必须等待宿主返回 stdout/stderr/observation 后，再基于 observation 生成最终回答。
- 禁止在 SKILL.md 中只写“立即调用 `scripts/...`”这种隐式执行描述；应写成“运行时 assistant 输出以下命令块交由宿主执行”，并给出具体命令示例。
- 如果用户要求使用平台内置模型、图像模型或多模态模型，不要写外部 API key、关键词数据库或假 API；应说明由宿主配置的模型完成相关步骤。任何脚本都必须是有实际功能的实现：要么执行确定性的真实计算/转换/文件处理，要么在需要开放式生成、语义理解、视觉/图像能力时使用宿主已配置的模型能力；模型与认证相关参数由平台运行时注入；生成脚本可按需读取 `IMAGE_MODEL`、`IMAGE_BASE_URL`、`IMAGE_SIZE`、`IMAGE_API_KEY` / `LLM_API_KEY` / `OPENAI_API_KEY` 等环境变量，但不要硬编码这些值，也不需要额外校验它们是否存在。
- 如果需要生成图片，SKILL.md 只描述“使用平台稳定扩散图片生成能力”即可；不要把输入文本翻译、TEXT_MODEL 调用、接口字段解析等平台细节写入创建出来的 Skill 正文。平台运行时会静默处理图片生成所需的通用输入转换。
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
_EMPTY_GENERATION_PROMPT_VARIANTS: tuple[str, ...] = ("standard", "simplified", "minimal")
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
    strict: bool = False

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
    generation_order: int = 0
    purpose: str
    required: bool
    can_skip: bool
    file_type: Optional[str] = None
    file_kind: str = "config"
    role: Optional[str] = None
    component_hint: str = ""
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    side_effects: list[str] = Field(default_factory=list)
    required_tool_slots: list[dict[str, Any]] = Field(default_factory=list)
    implementation_strategy: list[dict[str, Any]] = Field(default_factory=list)
    selected_tools: list[str] = Field(default_factory=list)
    runtime_contract: dict[str, Any] = Field(default_factory=dict)
    artifact_contract: dict[str, Any] = Field(default_factory=dict)
    required_capabilities: list[str] = Field(default_factory=list)
    raw_capability_hints: list[str] = Field(default_factory=list)
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
    asset_source: str = ""




def _generation_order_for_file(path: str, asset_source: str = "") -> int:
    normalized = _normalize_skill_path(path)
    if normalized.startswith("references/"):
        return 0
    if normalized.startswith("scripts/"):
        return 1
    if normalized.startswith("assets/") and asset_source != "user_upload":
        return 2
    if normalized.startswith("assets/") and asset_source == "user_upload":
        return 3
    if normalized == "SKILL.md":
        return 4
    return 2


def _final_outputs_from_plan_entries(entries: list[SkillPlanEntry]) -> list[str]:
    for entry in entries or []:
        contract = entry.artifact_contract or {}
        explicit = contract.get("final_output") or contract.get("final_outputs")
        if isinstance(explicit, str) and explicit.strip():
            return [explicit.strip()]
        if isinstance(explicit, list):
            values = [str(item).strip() for item in explicit if str(item).strip()]
            if values:
                return values
    final_entries = [entry for entry in entries or [] if bool((entry.artifact_contract or {}).get("final"))]
    for entry in reversed(final_entries or entries or []):
        contract = entry.artifact_contract or {}
        for key in ("stdout_fields", "artifact_fields", "file_fields", "file_outputs"):
            raw = contract.get(key)
            if isinstance(raw, list):
                values = [str(item).strip() for item in raw if str(item).strip()]
                if values:
                    return values
        if entry.outputs:
            return list(entry.outputs)
    return []


class AssetRequirementOut(BaseModel):
    path: str
    generation_order: int = 3
    source: str
    required: bool = True
    description: str = ""


class AnalyzeBlueprintResponse(BaseModel):
    skill_name: str
    files: list[FileSpecOut]
    warnings: list[Any]
    asset_requirements: list[AssetRequirementOut] = Field(default_factory=list)
    final_outputs: list[str] = Field(default_factory=list)
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
    runtime_spec: Optional[dict[str, Any]] = None


class FinalizeSkillMdRequest(BaseModel):
    skill_name: str
    description: str = ""
    blueprint_text: str = ""
    model: Optional[str] = None
    references: list[str] = Field(default_factory=list)
    assets: list[str] = Field(default_factory=list)
    script_runtime_specs: list[dict[str, Any]] = Field(default_factory=list)
    final_outputs: list[str] = Field(default_factory=list)

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
    messages: list[dict[str, Any]] = Field(default_factory=list)
    input_files: list[dict[str, Any]] = Field(default_factory=list)
    fields: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)

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

    required_capabilities, forbidden_capabilities = [], []
    file_kind = file_kind_for_path(file_path)
    inputs, outputs = default_io_for_file_kind(file_kind)

    return {
        "path": file_path,
        "purpose": purpose or f"{file_path} 的职责说明",
        "file_type": file_type,
        "file_kind": file_kind,
        "role": resolved_role,
        "inputs": list(inputs or []),
        "outputs": list(outputs or []),
        "dependencies": [],
        "required_capabilities": required_capabilities,
        "raw_capability_hints": list(required_capabilities or []),
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

    if file_path.startswith("assets/") and (skill_plan_entry or {}).get("asset_source") != "bundled":
        raise HTTPException(
            status_code=400,
            detail=f"{file_path} 属于 assets 静态素材目录；source=user_upload 必须上传，只有 source=bundled 可作为预置静态资源写入",
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
    data["raw_capability_hints"] = list(data.get("raw_capability_hints") or required or [])
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
        default_inputs, default_outputs = default_io_for_file_kind(file_kind_for_path(file_path))
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
    layer: str = ""


class ContractValidationError(ValueError):
    """Validation error carrying structured contract check results."""

    def __init__(self, message: str, results: list[ContractCheckResult]):
        super().__init__(message)
        self.results = results


def _infer_script_input_keys_from_blueprint(script_path: str, blueprint_text: str) -> list[str]:
    """Compatibility shim: Creator no longer guesses business argv keys.

    Input fields must come from SkillPlan.inputs, SKILL.md command JSON argv, or
    E2E trace payload/stdout. The generic fallback is a single payload object.
    """
    return ["payload"]


def _script_command_template(script_path: str, blueprint_text: str, entry: SkillPlanEntry | None = None) -> str:
    """Render the command template from SkillPlanEntry, the sole execution contract."""
    if entry is None:
        entry = _skill_plan_entry_for_file(file_path=script_path, blueprint_text=blueprint_text)
    return render_script_command_from_skill_plan(entry)


def _creator_tool_context_for_script(
    *,
    file_path: str,
    skill_plan_entry: SkillPlanEntry | dict[str, Any] | None,
    blueprint_text: str = "",
    failure_layer: str | None = None,
    error_text: str | None = None,
    include_snippets: bool = True,
) -> str:
    """Build tool context from explicit SkillPlan contract and registry metadata only."""
    if not file_path.startswith("scripts/"):
        return ""
    entry = skill_plan_entry or _skill_plan_entry_for_file(file_path=file_path, blueprint_text=blueprint_text)
    tool_resolve = resolve_tools_for_skill_plan_entry(entry)
    parts = [tool_resolve.tool_usage_prompt]
    if failure_layer or error_text:
        role = str(entry.get("role") if isinstance(entry, dict) else getattr(entry, "role", "") or "")
        required = list(entry.get("required_capabilities", []) if isinstance(entry, dict) else getattr(entry, "required_capabilities", []) or [])
        optional = list(entry.get("optional_capabilities", []) if isinstance(entry, dict) else getattr(entry, "optional_capabilities", []) or [])
        allowed = list(entry.get("allowed_capabilities", []) if isinstance(entry, dict) else getattr(entry, "allowed_capabilities", []) or [])
        forbidden = list(entry.get("forbidden_capabilities", []) if isinstance(entry, dict) else getattr(entry, "forbidden_capabilities", []) or [])
        from ..services.creator_tool_registry import tool_layer_prompt_for_context
        parts.append(tool_layer_prompt_for_context(
            role=role,
            required_capabilities=required,
            optional_capabilities=optional,
            allowed_capabilities=allowed,
            forbidden_capabilities=forbidden,
            failure_layer=failure_layer,
            error_text=error_text,
        ))
        if include_snippets:
            snippets = resolve_tool_snippets_for_context(
                role=role,
                capabilities=[*required, *optional, *allowed],
                tool_names=[*required, *optional, *allowed],
                file_path=file_path,
                failure_layer=failure_layer,
                error_text=error_text,
                max_snippets=6,
            )
            if snippets:
                parts.append(tool_snippet_prompt(snippets))
    return "\n\n".join(part for part in parts if part)



def _command_signature(command: str, script_path: str) -> dict[str, Any] | None:
    """Normalize the single Creator SKILL.md command protocol.

    First-round command validation intentionally allows exactly one protocol:
    ``python scripts/<file>.py '<JSON object>'``.  It does not infer business
    field correctness; E2E owns placeholder/dataflow validation.
    """
    try:
        parts = shlex.split((command or "").strip())
    except ValueError:
        return None

    expected_script = script_path.replace("\\", "/")
    if len(parts) != 3:
        return None
    if Path(parts[0]).name not in {"python", "python3"}:
        return None

    normalized_script = parts[1].replace("\\", "/")
    if normalized_script != expected_script:
        return None
    if not expected_script.startswith("scripts/") or Path(expected_script).suffix.lower() != ".py":
        return None

    try:
        payload = json.loads(parts[2])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    placeholders: dict[str, str] = {}
    for key, value in payload.items():
        if isinstance(value, str):
            match = re.fullmatch(r"\{\{\s*([A-Za-z_][\w.-]*)\s*\}\}", value.strip())
            placeholders[str(key)] = match.group(1) if match else value.strip()
        else:
            placeholders[str(key)] = ""
    return {
        "runner": Path(parts[0]).name,
        "script_path": expected_script,
        "keys": set(str(key) for key in payload.keys()),
        "placeholders": placeholders,
    }


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
    """Return the JSON argv object for the strict Creator command protocol."""
    if _command_signature(command, script_path) is None:
        return None
    try:
        parts = shlex.split(command or "")
        payload = json.loads(parts[2])
    except (ValueError, json.JSONDecodeError, IndexError):
        return None
    if not isinstance(payload, dict):
        return None
    return {str(key): value for key, value in payload.items()}


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
    - Do not enforce cross-step dataflow or exact SkillPlan input-key matching;
      E2E workflow validation owns argv/stdout handoff checks.
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
            expected="Python 用 python，Node 用 node，Bash/Shell 用 bash/sh；JSON keys 只需保持 workflow/脚本自洽。",
            minimal_edit="修正 runner 或脚本路径；不要仅因 SkillPlan.inputs 改写可运行 payload。",
        ))

        keys = _command_payload_keys(command, script_path)
        json_ok = keys is not None
        results.append(ContractCheckResult(
            id="command_block.json_argv.parseable",
            passed=json_ok,
            target=target,
            message="命令块使用可解析 JSON argv。" if json_ok else f"{script_path} 命令块必须在脚本路径后传入 JSON object argv。",
            expected="脚本路径后跟一个 JSON object argv；JSON keys 可由 workflow envelope 自由定义。",
            minimal_edit="确保 JSON 可解析；字段对齐由第二轮 E2E trace 定位。",
        ))

        # First-round file contracts stop at command syntax/runtime/JSON shape.
        # Exact argv key alignment with SkillPlan inputs is a workflow dataflow
        # concern and is validated during second-round E2E execution.

    return results

def _build_skill_md_contract_text(blueprint_text: str) -> str:
    """Hard-format authoring contract for SKILL.md.

    Semantic coverage is checked by model review. This contract only defines
    document format and fenced block norms.
    """
    return "\n".join([
        "必须满足以下 SKILL.md 合同（硬格式与平台边界）：",
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
        "- 若需要数值默认值，直接写固定 JSON 数字；不要把动态数值 placeholder 裸露在 JSON 中。",
        "",
        "D. workflow / 平台边界:",
        "- SKILL.md 应说明 Skill 用途、真实脚本调用顺序（如有）和最终产物类型，但第一轮不要求证明内部 stdout/placeholder 闭环。",
        "- 命令 placeholder 优先使用 external envelope 中确定存在的字段：user_request、input、text、input_files、files、fields、options，或显式 fields/default_values/input_binding 提供的字段。",
        "- 不要固定特定中间字段名；内部脚本流转只在第二轮 E2E 真实执行时验证。",
        "- 多场景、多图片、多页 PDF 等循环应由脚本实现；SKILL.md 第一轮只需保持命令块静态可解析。",
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
    """Build first-round static authoring guidance for SKILL.md.

    Despite the historical function name, this guide intentionally does not
    impose internal workflow dataflow.  First-round SKILL.md generation owns
    static Markdown/platform boundaries only; second-round E2E owns placeholder
    provenance, stdout field closure, and downstream parser alignment.
    """
    script_paths = _paths_requiring_skill_md_mentions(blueprint_text, prefix="scripts/")
    reference_paths = _paths_requiring_skill_md_mentions(blueprint_text, prefix="references/")

    if not script_paths:
        return (
            "SKILL.md first-round static authoring guide:\n"
            "- 当前蓝图没有 scripts/ 文件；SKILL.md 不要编造脚本命令块。\n"
            "- 若任务可直接回答，明确写“直接回答用户问题”，不要生成伪脚本流程。"
        )

    lines: list[str] = [
        "SKILL.md first-round static authoring guide（只约束静态格式和平台边界，不验证内部 dataflow）:",
        "A. 命令块静态形态:",
        "- 对蓝图真实规划的 scripts/ 文件，使用标准 Markdown 独立 ```bash fenced code block。",
        "- 每个 fence 内只放一条命令；命令必须直接调用 scripts/ 路径。",
        "- 脚本路径后传入 json.loads 可解析的 JSON object argv；所有动态 {{placeholder}} 必须作为 JSON 字符串值出现。",
        "- 命令 placeholder 优先引用 external envelope 字段：user_request、input、text、input_files、files、fields、options，或显式 fields/default_values/input_binding。",
        "- 第一轮不要证明后续 placeholder 来自前序 stdout；不要固定特定中间字段名；内部流转交给第二轮 E2E 执行验证。",
        "",
        "B. 资源边界:",
        "- references/ 只在参考资料/资源小节说明用途和按需读取时机，不替代主流程命令块。",
        "- assets/ 只能作为上传素材/静态资源引用，不能描述为模型生成。",
        "",
        "C. 可用脚本路径与静态命令示例:",
    ]

    for idx, script_path in enumerate(script_paths, start=1):
        entry = _skill_plan_entry_for_file(file_path=script_path, blueprint_text=blueprint_text)
        runner = {"python": "python", "node": "node", "bash": "bash", "shell": "sh"}.get(entry.runtime, "")
        payload = json.dumps(
            {
                "payload": "{{user_request}}",
                "input_files": "{{input_files}}",
                "fields": "{{fields}}",
                "options": "{{options}}",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        command = f"{runner} {script_path} '{payload}'" if runner else f"{script_path} '{payload}'"
        lines.extend([
            f"{idx}. {script_path}",
            f"   role: {entry.role}",
            "   static command shape example（可按脚本实际 argv key 调整，但不要引入无来源平台外 API 或 Creator 内部路径）:",
            "```bash",
            command,
            "```",
        ])

    if reference_paths:
        lines.extend([
            "",
            "D. references:",
            "- SKILL.md 应在参考资料/资源小节逐字引用以下本地 reference，并说明何时读取：",
        ])
        for path in reference_paths:
            lines.append(f"- {path}")

    lines.extend([
        "",
        "E. 第二轮 E2E 责任边界:",
        "- placeholder 来源、前后脚本 stdout 字段闭环、最终 stdout 平台输出字段，不在第一轮 SKILL.md prompt 中证明。",
        "- 如果这些内容不一致，第二轮 E2E 真实执行会基于实际 stdout/文件产物反馈修复 SKILL.md 或脚本。",
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

def _skill_md_frontmatter_errors(content: str) -> list[str]:
    meta, _body, _had = parse_frontmatter(content)
    return validate_skill_frontmatter(meta)


def _has_valid_skill_md_frontmatter(content: str) -> bool:
    """Validate SKILL.md frontmatter using the Creator top-level schema."""
    return not _skill_md_frontmatter_errors(content)


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

    final_outputs = set(_final_outputs_from_plan_entries(entries))
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

    frontmatter_errors = _skill_md_frontmatter_errors(stripped)
    has_frontmatter = not frontmatter_errors
    results.append(ContractCheckResult(
        id="skill_md.frontmatter",
        passed=has_frontmatter,
        target="SKILL.md",
        message=(
            "SKILL.md frontmatter 合格。"
            if has_frontmatter
            else "SKILL.md frontmatter 必须只包含允许的顶层 YAML 字段，并包含 name/description。错误：" + "; ".join(frontmatter_errors)
        ),
        expected="顶层 YAML 只允许 name、description、license、allowed-tools、metadata；Creator 规划字段只能放正文或 metadata.creator。",
        minimal_edit="只修正文件开头 YAML frontmatter；不改正文、workflow block、脚本路径或 scripts。",
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

    for reference_path in _paths_requiring_skill_md_mentions(blueprint_text, prefix="references/"):
        mentioned = reference_path in content
        results.append(ContractCheckResult(
            id="skill_md.reference.mentioned",
            passed=mentioned,
            target=reference_path,
            message=(
                f"SKILL.md 已引用参考资料 {reference_path}。"
                if mentioned
                else f"SKILL.md 缺少对参考资料 {reference_path} 的引用。"
            ),
            expected="蓝图真实规划的 references/ 资源必须在 SKILL.md 的参考资料/资源小节中静态引用，并说明用途。",
            minimal_edit=f"添加参考资料小节，引用 `{reference_path}` 并说明何时读取。",
        ))

    for asset_path in _paths_requiring_skill_md_mentions(blueprint_text, prefix="assets/"):
        mentioned = asset_path in content
        results.append(ContractCheckResult(
            id="skill_md.asset.mentioned",
            passed=mentioned,
            target=asset_path,
            message=(
                f"SKILL.md 已引用静态资源 {asset_path}。"
                if mentioned
                else f"SKILL.md 缺少对静态资源 {asset_path} 的引用。"
            ),
            expected="蓝图真实规划的 assets/ 资源必须作为上传素材/静态资源引用，不得描述为模型生成。",
            minimal_edit=f"在资源小节引用 `{asset_path}` 并说明它是静态/上传素材。",
        ))

    results.extend(_check_skill_md_fenced_command_contracts(
        content=content,
        blueprint_text=blueprint_text,
        required_script_paths=None,
    ))
    # Cross-script input/output closure is intentionally excluded from the
    # first-round SKILL.md static contract. Second-round E2E validation owns
    # workflow dataflow checks after all scripts and stdout shapes exist.

    return results



def _contract_layer_for_check_id(check_id: str) -> str:
    """Map deterministic validator check ids to repair layers."""
    cid = check_id or ""
    if cid.startswith("skill_md.frontmatter"):
        return "skill_md_metadata"
    if cid.startswith("skill_md.command_block") or cid.startswith("command_block"):
        return "skill_md_command_block"
    if cid.startswith("skill_md.reference"):
        return "reference_metadata"
    if cid.startswith("skill_md.asset"):
        return "asset_source_contract"
    if cid.startswith("skill_md.forbidden") or cid.startswith("skill_md.resource"):
        return "skill_md_intent_alignment"
    if cid.startswith("reference."):
        return "reference_body"
    if cid.startswith("asset."):
        return "asset_source_contract"
    if cid.startswith("script."):
        return "script_static_contract"
    return ""

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
        layer = result.layer or _contract_layer_for_check_id(result.id)
        layer_text = f" layer={layer}" if layer else ""
        lines.append(
            f"- {result.id} target={result.target}{layer_text}: {result.message}\n"
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
        "raw_capability_hints": list(required_capabilities or []),
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
        "1. SKILL.md 必须覆盖蓝图真实规划的任务、真实脚本/资源引用、脚本调用顺序（如有）和最终产物类型；第一轮不审查内部 stdout/placeholder 闭环。\n"
        "2. 真实文件计划通常来自目录结构、SkillPlan path、dependencies、references 字段。\n"
        "3. 如果蓝图在“禁止隐式执行/示例/反例/例如/比如”语境下提到某个 scripts/*.py、references/*.md 或 assets/*，该路径只是解释性示例，不是实际文件计划。\n"
        "4. 但是，如果某个路径出现在目录结构或 SkillPlan path 字段中，则必须视为真实文件，不能误杀。\n"
        "5. SKILL.md 生成阶段 scripts/references 可能尚未落盘；不要因为文件暂时不存在而判失败。\n"
        "6. 对每个真实规划脚本，SKILL.md 必须有 ```bash fenced code block；只判断是否覆盖真实文件计划，不判断 shell 语法。\n"
        "7. 命令语法、runner、JSON argv、--args/--key/--text_file 等格式问题只由确定性 parser 判断；你不得给出替代命令协议建议。\n"
        "8. 如果 SKILL.md 中给 stdout 示例、配置示例或机器可读数据，必须使用 ```json fenced code block。\n"
        "9. 不要要求文件末尾追加 ---；YAML frontmatter 只需要在文件开头关闭。\n"
        "10. assets/** 只能作为上传素材/静态资源引用，不能描述为模型生成。\n"
        "11. references 应被描述为按需读取，不能把 reference 正文全文塞进 SKILL.md。\n"
        "12. SKILL.md 必须面向 Skill 使用者，不要包含 Creator UI 创建流程。\n\n"

        "你必须从以下角度审查：\n"
        "- intent_reviewer: 是否覆盖业务意图、输入、输出、触发方式、最终产物。\n"
        "- file_plan_reviewer: 是否覆盖真实 scripts/references/assets；是否误用了示例/反例路径。\n"
        "- workflow_reviewer: 只看 SKILL.md 是否描述了必要脚本顺序和脚本用途；不得判断命令语法、argv 协议、--args/--key/--text_file，也不要审查前序 stdout 字段闭环。\n"
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
    """Validate SKILL.md with deterministic hard gates.

    Model intent review is advisory only: it may emit warnings for logs/UI, but
    it cannot block first-round generation or trigger repair. Hard failures are
    limited to deterministic parser/contract checks such as frontmatter,
    non-empty markdown, fenced bash command parsing, and scripts/*.py JSON argv.
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

    # 2. 模型蓝图语义审查（warning only; never a hard gate）
    review: dict[str, Any] = {
        "passed": True,
        "issues": [],
        "warning_only": True,
    }
    try:
        model_review = await _review_skill_md_blueprint_intent_with_model(
            skill_name=skill_name,
            content=content,
            blueprint_text=blueprint_text,
            skill_plan_entry=skill_plan_entry,
            model=model,
        )
        if isinstance(model_review, dict):
            review.update(model_review)
            review["model_passed"] = bool(model_review.get("passed", model_review.get("valid", False)))
        else:
            review["model_warning"] = f"SKILL.md intent review returned {type(model_review).__name__}, ignored as warning."
    except Exception as exc:
        logger.exception(
            "[Creator][skill_md] model blueprint intent review crashed; ignored as warning skill=%s",
            skill_name,
        )
        review["model_warning"] = f"SKILL.md intent review crashed and was ignored: {exc}"

    if review.get("model_passed") is False:
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
            "[Creator][skill_md] model blueprint intent review warning skill=%s message=\n%s",
            skill_name,
            message,
        )
        review["model_warning"] = message

    # 3. fenced command 后置校验
    required_script_paths = [
        path
        for path in _extract_declared_skill_paths(blueprint_text)
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
        "B. 参数接口（输入宽松）:",
        "- 脚本必须能读取一个 JSON argv object。",
        "- 可以宽松兼容 payload / fields / options / input_files / 上游 stdout 字段。",
        "- 不要求第一轮读取所有 input_sources 或 SkillPlan inputs；不因可选输入未使用而失败。",
        "- 内部 workflow 字段名可用 payload/context 或上游 stdout 字段。",
        "C. 角色输出合同:",
    ]

    if entry.role == "text_generator":
        lines.append("- stdout JSON 至少有一个非空字段；字段名由 workflow 决定；可优先调用 text_generation helper，但不强制实现方式。")
    elif entry.role == "image_generator":
        lines.append("- stdout JSON 至少有一个非空字段；字段名由 workflow 决定；可优先调用 generate_stable_diffusion_image helper，但不强制实现方式。")
    elif entry.role == "pdf_builder":
        lines.append(
            "- stdout JSON 必须返回真实存在的文件产物路径；字段名由 workflow 决定。"
            "工具/helper 如何组合不作为第一轮 hard gate；最终以运行、stdout 合同和 artifact 真实存在为准。"
        )
    else:
        lines.append("- stdout JSON 至少有一个非空字段；字段名由 workflow 决定。")

    try:
        workflow_commands = _extract_e2e_workflow_commands(Path("."), blueprint_text or "")
        script_commands = [cmd for cmd in workflow_commands if cmd.script_path == file_path]
        is_last_workflow_step = bool(script_commands and workflow_commands and script_commands[-1].ordinal == workflow_commands[-1].ordinal)
    except Exception:
        is_last_workflow_step = False

    if is_last_workflow_step:
        lines.append(
            "- 这是 SKILL.md workflow 的最后一步：stdout JSON 必须至少包含一个 sandbox 平台最终字段："
            "text、markdown、image_path、image_paths、pdf_path、docx_path、pptx_path、html_path、file_paths 或 file_outputs。"
        )
        lines.append(
            "- 可以同时保留业务内部字段，例如 {\"time_output\": \"12:00:00\", \"text\": \"当前时间：12:00:00\"}；"
            "中间步骤不强制平台字段，只要下一步 placeholder 可解析。"
        )

    lines.append("D. 第一轮验收边界:")
    lines.append("- 第一轮只验协议 + 运行 + 产物：argv JSON、入口、运行成功、stdout JSON object、required outputs、真实 artifact、import/dependency 和危险系统操作。")
    lines.append("- 工具/helper 如何组合、required/optional/allowed_capabilities、placeholder/mock/template 关键词不作为第一轮 hard gate。")
    lines.append("- 不要通过 print {'error':...}、{}、空路径等绕过运行和产物校验。")

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
    description = (purpose or entry.get("purpose") or f"{file_path} reference").strip()
    return {
        "title": title,
        "description": description,
        "metadata": {
            "creator": {
                "path": file_path,
                "purpose": (purpose or entry.get("purpose") or "按 SKILL.md 工作流需要读取正文").strip(),
            }
        },
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

    body = body.strip()

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
    """Validate references/*.md frontmatter without requiring it.

    references may omit frontmatter. If present, only ordinary document metadata
    is allowed; Creator internal planning fields must not leak here. Body is not
    rewritten by metadata repair.
    """
    meta, body, had_frontmatter = parse_frontmatter(content)
    metadata_errors = validate_reference_frontmatter(meta)
    results: list[ContractCheckResult] = []

    results.append(ContractCheckResult(
        id="reference.metadata.frontmatter_schema",
        passed=not metadata_errors,
        target=file_path,
        message=(
            "reference frontmatter schema 合格（或未提供 frontmatter）。"
            if not metadata_errors
            else "reference frontmatter 只能包含普通文档元数据。错误：" + "; ".join(metadata_errors)
        ),
        expected="references/*.md 可无 frontmatter；如有，只允许 title、description、source、license、metadata。",
        minimal_edit="只修当前 reference md 的 YAML frontmatter；不改正文内容，不改其它文件。",
        details={"had_frontmatter": had_frontmatter},
    ))

    results.append(ContractCheckResult(
        id="reference.metadata.body_exists",
        passed=bool(body.strip() if had_frontmatter else (content or "").strip()),
        target=file_path,
        message=("reference 存在 Markdown 正文。" if (body.strip() if had_frontmatter else (content or "").strip()) else "reference 正文为空。"),
        expected="reference 作为辅助上下文必须有非空 Markdown 正文，但正文结构不强制固定章节。",
        minimal_edit="补充非空 reference 正文。",
    ))
    return results

def _build_reference_file_contract_text(file_path: str, purpose: str, blueprint_text: str) -> str:
    script_paths = _paths_requiring_skill_md_mentions(blueprint_text, prefix="scripts/")
    script_lines: list[str] = []

    for script_path in script_paths:
        entry = _skill_plan_entry_for_file(file_path=script_path, blueprint_text=blueprint_text)
        if file_path in entry.reference_files or not entry.reference_files:
            script_lines.extend([
                f"- 本 reference 是 {script_path} 的辅助资料；不要重新定义该脚本的 role/inputs/outputs/capabilities/command_template。",
                f"- 如果 SKILL.md 已包含 {script_path} 的可执行命令块，本 reference 不要再写命令块。",
                "- reference 正文可以自由组织，只要提供有用参考信息且不成为第二套 SkillPlan。",
            ])

    if not script_lines:
        script_lines.append("- 本 reference 对应一个独立子任务/模块；正文必须非空并提供有用参考信息，但不强制固定章节结构。")

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
        "- 正文是辅助参考资料；不要重新定义 SkillPlan 的 role/capability/input/output 合同；如果 SKILL.md 已有可执行命令块，reference 不要重复写命令块。",
        "- 不要重新定义 role / inputs / outputs / capabilities / command_template。",
        *script_lines,
        "- 内容必须是有实际指导价值的参考资料，不是对‘将要生成参考资料’的再描述。",
        "- 正文必须非空并提供有用参考信息；不要求固定章节、示例、反例、步骤清单或输入输出段落。",
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

    declares_runtime_protocol = bool(re.search(r"(?im)^\s*(?:runtime_contract|artifact_contract|required_tool_slots|implementation_strategy|command_template)\s*[:=]", stripped))
    results.append(ContractCheckResult(
        id="reference.no_runtime_protocol",
        passed=not declares_runtime_protocol,
        target=file_path,
        message=("reference 未声明运行时协议。" if not declares_runtime_protocol else f"{file_path} 不应声明 runtime/tool/artifact 执行协议。"),
        expected="reference 只提供文档上下文；运行时协议属于 normalized plan / scripts。",
        minimal_edit="删除 runtime_contract、artifact_contract、required_tool_slots、implementation_strategy 或 command_template 等运行时协议字段。",
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

    # references/*.md are auxiliary context, not executable contracts.
    # Keep validation intentionally light: readable non-empty Markdown plus
    # safety checks below, but no fixed section/metadata richness requirements.

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



_FORBIDDEN_GUESSED_HELPER_IMPORTS = {
    "pdf_generation",
    "file_output",
    "platform_helpers",
    "helpers.pdf_builder",
    "tool_registry.pdf_builder",
}


def _registry_function_import_paths() -> set[str]:
    paths: set[str] = set()
    for capability in list_tool_capabilities():
        for fn in getattr(capability, "functions", []) or []:
            import_path = str(getattr(fn, "import_path", "") or "").strip()
            signature = str(getattr(fn, "signature", "") or "").strip()
            if import_path and signature:
                paths.add(import_path)
    return paths


def _python_imported_modules(content: str) -> set[str]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name:
                    modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _forbidden_guessed_helper_imports(content: str) -> list[str]:
    allowed_cards = _registry_function_import_paths()
    hits: list[str] = []
    for module in _python_imported_modules(content):
        for forbidden in _FORBIDDEN_GUESSED_HELPER_IMPORTS:
            if module == forbidden or module.startswith(forbidden + "."):
                if module not in allowed_cards:
                    hits.append(module)
    return sorted(set(hits))


def _script_satisfies_required_capability(content: str, capability: str) -> bool:
    """Statically enforce only helper_required capabilities.

    helper_preferred/self_implementation_allowed capabilities are validated by
    trial run, E2E stdout, and artifact existence checks instead of source-code
    implementation regexes.
    """
    capability = capability.lower()
    cap = get_tool_capability(capability)
    if cap and cap.usage_policy == "helper_required":
        return _script_uses_registry_helpers(content, capability)
    return True


_ARTIFACT_OUTPUT_KEYS = {"pdf_path", "docx_path", "pptx_path", "html_path", "file_paths"}
_ARTIFACT_CAPABILITIES = {"pdf_generation", "docx_generation", "pptx_generation", "html_generation", "html_asset_generation", "file_output"}


def _script_has_real_file_creation_logic(content: str, *, outputs: list[str], capabilities: list[str]) -> bool:
    """Do not infer artifact implementation from source regexes.

    Static validation keeps syntax/interface/capability boundaries; actual file
    creation is verified by trial run/E2E artifact checks.
    """
    return True



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

        # First-round validation only proves the script file itself can run as
        # a standalone argv/stdout program. Whether each declared input is
        # actually produced by an upstream step belongs to second-round E2E.
        missing_inputs = [key for key in (plan_entry.inputs or []) if key and key not in stripped]
        results.append(
            ContractCheckResult(
                id="script.skillplan_inputs.used",
                passed=True,
                target=file_path,
                message=("脚本源码引用了声明输入。" if not missing_inputs else f"warning: 脚本源码未静态引用声明输入：{', '.join(missing_inputs)}；第一轮不阻断，运行闭环/E2E 负责验证。"),
                expected="第一轮只做协议 + 运行 + 产物检查；不使用输入字段关键词静态匹配作为 hard gate。",
                minimal_edit="仅当运行、stdout required outputs 或 E2E 数据流失败时，修当前脚本的参数读取与输出逻辑。",
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

    tool_resolve = resolve_tools_for_skill_plan_entry(plan_entry)

    guessed_helper_imports = _forbidden_guessed_helper_imports(stripped) if plan_entry.language == "python" else []
    results.append(
        ContractCheckResult(
            id="tool_usage_contract.forbidden_helper_import",
            passed=True,
            target=file_path,
            message=(
                "脚本未发现疑似猜测 helper import path。"
                if not guessed_helper_imports
                else f"warning: {file_path} import 了未由 Tool Registry function card 明确提供 import path 和调用签名的 helper：{', '.join(guessed_helper_imports)}；第一轮不因此阻断，实际以 import/trial run 结果为准。"
            ),
            expected="第一轮不按 helper import 路线做 hard validation；仅在 import 失败、调用失败或 stdout/artifact 不满足合同时失败。",
            minimal_edit="如 trial run/import 失败，只修当前脚本的 import/call；不要修改 SkillPlan、capability 声明或 workflow。",
        )
    )

    helper_required_capabilities = [
        capability
        for capability in effective_required_capabilities
        if (get_tool_capability(capability) and get_tool_capability(capability).usage_policy == "helper_required")
    ]
    missing_capabilities = _script_required_capability_failures(
        stripped,
        helper_required_capabilities,
    )
    results.append(
        ContractCheckResult(
            id="script.required_capabilities.called",
            passed=True,
            target=file_path,
            message=(
                "第一轮不强制 helper_required/required_capabilities 的具体工具路线。"
                if not missing_capabilities
                else f"warning: 脚本未调用这些 helper_required 能力对应接口：{', '.join(missing_capabilities)}；第一轮不阻断，实际以 stdout/artifact/trial run 闭环为准。"
            ),
            expected="scripts/** 可按责任模块自主组合已有工具；第一轮不使用 required/optional/allowed_capabilities 卡死实现路线。",
            minimal_edit="repair 阶段只修当前脚本的 argv/run/stdout/artifact/import/输入使用/真实实现问题；不要修 SkillPlan 或 capability 声明。",
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
            passed=True,
            target=file_path,
            message=(
                "第一轮不静态判定文件创建实现；文件产物存在性与格式由第二轮 E2E 校验。"
                if has_real_file_output
                else f"{file_path} 声明文件输出；第一轮仅提示风险，第二轮 E2E 将验证真实产物。"
            ),
            expected="第一轮不检查文件产物存在性；声明 pdf/docx/pptx/html/file_paths 时，第二轮 E2E 必须验证真实文件和格式。",
            minimal_edit="如 E2E 失败，再修脚本确保写入 OUTPUT_DIR/outputs 并返回平台字段。",
        )
    )

    has_fake = bool(_SCRIPT_FAKE_IMPLEMENTATION_RE.search(stripped))
    results.append(
        ContractCheckResult(
            id="script.no_fake_implementation",
            passed=True,
            target=file_path,
            message=(
                "脚本未包含占位/模拟/假 API 实现。"
                if not has_fake
                else f"{file_path} 包含占位/模拟/假 API 实现。Creator 生成的脚本必须具备真实可执行功能。"
            ),
            expected="第一轮不使用 placeholder/mock/template 关键词正则作为 hard gate；真实失败由运行、stdout required outputs 和 artifact 验证决定。",
            minimal_edit="仅当脚本运行闭环、stdout 合同或 artifact 真实生成失败时修当前脚本。",
        )
    )

    capability_contract_enforced = False  # required_capabilities/forbidden_capabilities are hints, not script-contract gates.
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
                passed=True,
                target=file_path,
                message=(
                    "文件构建脚本未调用图片生成 helper。"
                    if not uses_image_helper
                    else f"{file_path} 是文件构建脚本，但调用了图片生成 helper。"
                ),
                expected="第一轮不按 role/capability 审查工具组合；只验证协议、运行和产物。",
                minimal_edit="仅当运行、stdout 或 artifact 失败时修当前脚本。",
            )
        )

    if capability_contract_enforced and uses_image_helper and "image_generation" not in (plan_entry.required_capabilities or []):
        results.append(
            ContractCheckResult(
                id="script.capability.forbidden_image_generation",
                passed=False,
                target=file_path,
                message=f"{file_path} 调用了图片生成 helper，但 SkillPlan.required_capabilities 未显式声明 image_generation。",
                expected="只有 required_capabilities 显式包含 image_generation 且未被 forbidden_capabilities 禁止时，脚本才可调用图片生成 helper。",
                minimal_edit="移除图片生成 helper，或由规划模型在 SkillPlan 中显式声明 image_generation。",
            )
        )

    if capability_contract_enforced and "image_generation" in (plan_entry.forbidden_capabilities or []):
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
    if capability_contract_enforced and uses_text_helper and "text_generation" not in (plan_entry.required_capabilities or []):
        results.append(
            ContractCheckResult(
                id="script.capability.forbidden_text_generation",
                passed=False,
                target=file_path,
                message=f"{file_path} 调用了文本模型 helper/LLM，但 SkillPlan.required_capabilities 未显式声明 text_generation。",
                expected="只有 required_capabilities 显式包含 text_generation 且未被 forbidden_capabilities 禁止时，脚本才可调用文本模型能力。",
                minimal_edit="移除文本模型调用，或由规划模型在 SkillPlan 中显式声明 text_generation。",
            )
        )

    if capability_contract_enforced and "text_generation" in (plan_entry.forbidden_capabilities or []):
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

    uses_pdf_helper = _script_uses_registry_helpers(stripped, "pdf_generation")
    if capability_contract_enforced and "pdf_generation" in (plan_entry.forbidden_capabilities or []):
        results.append(
            ContractCheckResult(
                id="script.capability.forbidden_pdf_generation",
                passed=not uses_pdf_helper,
                target=file_path,
                message=(
                    "脚本未调用 forbidden_capabilities 中禁止的 pdf_generation helper。"
                    if not uses_pdf_helper
                    else f"{file_path} 的 SkillPlan forbidden_capabilities 包含 pdf_generation，但源码调用了 PDF helper。"
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
            passed=True,
            target=file_path,
            message=(
                "脚本未调用 forbidden_capabilities 中禁止的 registry helper。"
                if not registry_forbidden_helper_hits
                else f"{file_path} 调用了这些 forbidden_capabilities 对应的 registry helper：{', '.join(registry_forbidden_helper_hits)}。"
            ),
            expected="第一轮不按 capability/helper 路线阻断；真实失败以 import/dependency、运行、stdout 和 artifact 合同为准。",
            minimal_edit="如该 helper 导致运行失败，只修当前脚本；不要扩大 SkillPlan/capability 声明。",
        )
    )

    forbidden_direct_hits = [
        item for item in tool_resolve.forbidden_imports
        if re.search(rf"\b{re.escape(item)}\b", stripped, re.IGNORECASE)
    ]
    results.append(
        ContractCheckResult(
            id="tool_usage_contract.forbidden_direct_imports",
            passed=True,
            target=file_path,
            message=(
                "脚本未绕过平台 helper 直接调用被禁止的底层工具库。"
                if not forbidden_direct_hits
                else f"{file_path} 直接调用了 Tool Resolve 禁止的底层工具/库：{', '.join(forbidden_direct_hits)}。"
            ),
            expected="第一轮不因工具路线选择阻断；底层库是否可用由 dependency/import/trial run 和 artifact 合同验证。危险系统操作另由 security gate 阻断。",
            minimal_edit="如 dependency/import/trial run 失败，只修当前脚本依赖和调用路径。",
        )
    )

    undeclared_helper_hits: list[str] = []
    declared_caps = set(effective_required_capabilities) | set(plan_entry.optional_capabilities or []) | set(plan_entry.allowed_capabilities or [])
    for capability in [cap.name for cap in list_tool_capabilities() if cap.helper_imports]:
        if capability not in declared_caps and _script_uses_registry_helpers(stripped, capability):
            undeclared_helper_hits.append(capability)
    results.append(
        ContractCheckResult(
            id="tool_usage_contract.undeclared_helper",
            passed=True,
            target=file_path,
            message=(
                "脚本未调用未声明 registry helper，或无需记录 warning。"
                if not undeclared_helper_hits
                else f"warning: {file_path} 调用了未在 required/optional/allowed_capabilities 声明的工具能力：{', '.join(undeclared_helper_hits)}；第一轮不阻断。"
            ),
            expected="undeclared_helper 降级为 warning；scripts/** 可根据自身责任自主组合已有工具。",
            minimal_edit="不要修 SkillPlan、capability 声明或 workflow；仅当调用本身失败或 stdout/artifact 不满足合同时修当前脚本。",
        )
    )


    dangerous_import_hits = sorted(set(re.findall(r"^\s*(?:import|from)\s+(paramiko|ftplib|telnetlib|subprocess)\b", stripped, re.MULTILINE)))
    dangerous_call_hits = sorted(set(re.findall(r"\b(?:os\.system|subprocess\.(?:run|Popen|call|check_call|check_output))\b", stripped)))
    forbidden_path_hits = sorted(set(re.findall(r"[\'\"]((?:/etc/passwd|/etc/shadow|/root/\.ssh/[^\'\"]*|~/.ssh/[^\'\"]*|\.\./[^\'\"]*))[\'\"]", stripped)))
    security_hits = [*dangerous_import_hits, *dangerous_call_hits, *forbidden_path_hits]
    results.append(
        ContractCheckResult(
            id="script.security.dangerous_operations",
            passed=not security_hits,
            target=file_path,
            message=(
                "脚本未包含危险 import、恶意 shell 调用或禁止路径访问。"
                if not security_hits
                else f"{file_path} 包含危险操作或禁止路径：{', '.join(security_hits)}。"
            ),
            expected="第一轮不审查工具路线，但安全风险、危险 import、禁止路径和恶意 shell 必须阻断。",
            minimal_edit="移除危险 import/shell/禁止路径访问；只保留当前脚本职责所需的安全本地逻辑或受控 helper 调用。",
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
                passed=True,
                target=file_path,
                message=(
                    "数据库读取脚本仅包含只读 SQL 形态。"
                    if not write_sql and not multi_statement_sql
                    else f"{file_path} 的 database_read 能力包含写操作或多语句 SQL 风险。"
                ),
                expected="第一轮不按 database_read 能力路线做 hard gate；真实失败以运行和外部系统权限为准。",
                minimal_edit="如运行失败或安全系统拒绝，再修当前脚本 SQL。",
            )
        )

    artifact_required = bool(_ARTIFACT_CAPABILITIES & set(effective_required_capabilities))
    artifact_outputs_declared = bool(_ARTIFACT_OUTPUT_KEYS & set(plan_entry.outputs or []))
    if artifact_required or artifact_outputs_declared:
        results.append(
            ContractCheckResult(
                id="tool_usage_contract.artifact_output_e2e",
                passed=True,
                target=file_path,
                message=f"{file_path} 的文件产物实现方式不由 Creator 后台源码正则判断；最终由试运行/E2E 校验真实产物与 stdout 字段。",
                expected="文件产物脚本必须在运行时创建真实文件，并通过平台可消费字段返回路径。",
                minimal_edit="保持 argv/stdout 协议，修复真实文件创建和路径返回；具体工具用法参考 Tool Registry snippets/function cards。",
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
                passed=True,
                target=file_path,
                message=(
                    "image_generator 未输出或生成 PDF-only 结果。"
                    if not pdf_only
                    else f"{file_path} 是 image_generator，但源码包含 PDF-only 输出。"
                ),
                expected="第一轮不按 role 名称静态限制输出字段；required outputs、真实产物和 E2E 负责验证。",
                minimal_edit="如 stdout required outputs 或 artifact 验证失败，只修当前脚本输出。",
            )
        )

    return results


_SCRIPT_CONTENT_REVIEW_CHECK_IDS = {
    # First-round scripts/** hard gates are intentionally minimal and
    # deterministic: raw source shape, syntax/argv/entrypoint protocol, and
    # dangerous-operation security checks. Tool route, capability, role,
    # placeholder/mock/template, and static artifact implementation opinions are
    # not hard gates; trial run/stdout/artifact validation owns runtime truth.
    "script.raw_source.single_file",
    "script.source.syntax",
    "script.json_argv.runtime",
    "script.runtime.entrypoint",
    "script.security.dangerous_operations",
}


def _check_script_content_review_contract(
    file_path: str,
    content: str,
    role: str | None = None,
    skill_plan_entry: dict[str, Any] | None = None,
) -> list[ContractCheckResult]:
    """First script phase: deterministic protocol/security review only.

    Runtime truth still comes from the single-script smoke phase, but syntax,
    JSON argv parsing shape, entrypoint shape, and dangerous operations are cheap
    deterministic gates before trial execution.
    """
    return [
        result
        for result in _check_script_file_contract(
            file_path,
            content,
            role=role,
            skill_plan_entry=skill_plan_entry,
        )
        if result.id in _SCRIPT_CONTENT_REVIEW_CHECK_IDS
    ]


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


    if _DATA_URI_RE.search(content):
        raise ValueError(
            f"{file_path} 输出 base64 data URI。"
            "图片结果必须由平台运行时写入 OUTPUT_DIR，并在 stdout JSON 中返回 image_paths。"
        )

    if re.search(r"(?m)^\s*image_path\s*=\s*generate_stable_diffusion_image\s*\(", content):
        raise ValueError(
            f"{file_path} 将 helper 返回 dict 直接赋给 image_path。"
            "图片脚本必须先保存 result = generate_stable_diffusion_image(desc)，"
            "再执行 image_paths.append(result.get(\"image_path\"))。"
        )

    effective_required_capabilities = _effective_required_capabilities_for_script(plan_entry) if plan_entry else []
    if plan_entry and plan_entry.role in {"pdf_builder", "docx_builder", "pptx_builder", "html_asset_builder", "asset_builder"} and not ({"text_generation", "image_generation"} & set(effective_required_capabilities)):
        return
    if not _requires_configured_model_call(plan_entry=plan_entry):
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
        results = _check_script_content_review_contract(
            file_path,
            content,
            role=role,
            skill_plan_entry=skill_plan_entry,
        )
        if any(not result.passed for result in results):
            raise ContractValidationError(
                _format_contract_failures(results).replace("SKILL.md contract", f"{file_path} contract"),
                results,
            )
        return

    if file_path.startswith("references/"):
        _validate_reference_file_contract(file_path, content)
        return

    if file_path.startswith("assets/"):
        _validate_asset_file_contract(file_path, content)
        return


def validate_file_contract(
    *,
    file_path: str,
    content: str,
    blueprint_text: str = "",
    role: str | None = None,
    skill_plan_entry: dict[str, Any] | None = None,
) -> list[ContractCheckResult]:
    """First-round Creator validator: only check one file's own contract.

    This layer deliberately excludes cross-file placeholder/dataflow closure and
    final platform-output checks. Those belong to ``validate_workflow_e2e``.
    """
    if file_path == "SKILL.md":
        return _check_skill_md_contract(content, blueprint_text or content)
    if file_path.startswith("scripts/"):
        return _check_script_file_contract(file_path, content, role=role, skill_plan_entry=skill_plan_entry)
    if file_path.startswith("references/"):
        purpose = ""
        if isinstance(skill_plan_entry, dict):
            purpose = str(skill_plan_entry.get("purpose") or "")
        return _check_reference_file_contract(file_path, content, purpose=purpose)
    if file_path.startswith("assets/"):
        try:
            _validate_asset_file_contract(file_path, content)
            return []
        except ContractValidationError as exc:
            return list(exc.results)
    return []


def validate_workflow_e2e(skill_name: str, *, external_context: dict[str, Any] | None = None) -> list[str]:
    """Second-round Creator validator: execute SKILL.md workflow and check interfaces."""
    return _run_skill_workflow_e2e_once(skill_name, external_context=external_context)


def _raise_file_contract_failures(results: list[ContractCheckResult]) -> None:
    failed = [result for result in results if not result.passed]
    if failed:
        raise ContractValidationError(
            "文件级合同校验未通过：\n" + _format_contract_checks(results, passed=False),
            results,
        )

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

    # First-round SKILL.md command validation only checks local command shape.
    # Field-level argv/dataflow alignment belongs to second-round E2E, where
    # upstream stdout and downstream parser behavior are known.

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
                else f"{script_path} 缺少可执行 Markdown 命令块：标准 ```bash fenced code block。"
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


def _validate_script_contract_static(
    *,
    file_path: str,
    content: str,
    skill_md: str,
    skill_plan_entry: dict[str, Any] | SkillPlanEntry | None = None,
) -> None:
    """Validate script source against SKILL.md contract locally.

    Creator 单文件阶段只做“协议 + 运行 + 产物”中的静态协议部分：
    - 如果 SKILL.md 命令传入 JSON argv，脚本必须读取 JSON argv；
    - 不用 fake/mock/template 关键词、工具能力声明、helper 路线或 LLM
      validator 作为 hard gate；
    - 字段级 stdout/artifact 闭环交给单文件 trial run 和最终 E2E。
    """
    explicit_entry = (
        skill_plan_entry.__dict__
        if isinstance(skill_plan_entry, SkillPlanEntry)
        else skill_plan_entry
    )
    plan_entry = (
        _skill_plan_entry_for_file(file_path=file_path, skill_plan_entry=explicit_entry)
        if explicit_entry is not None
        else _skill_plan_entry_for_file(file_path=file_path, blueprint_text=skill_md)
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
            f"{file_path} SKILL.md 命令传入 JSON argv，但脚本未按 runtime 读取 JSON argv（例如 Python json.loads(sys.argv[1])）。"
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
    """Return a stable, generic multilingual trial value without field-name guessing."""
    return "测试输入：包含中文、English words 和标点符号，用于验证 UTF-8 与真实执行。"


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
    """No field-name-specific trial variants; workflow/E2E validates payload flow."""
    return []


def _trial_args_for_script(skill_md: str, file_path: str, content: str, skill_plan_entry: dict[str, Any] | SkillPlanEntry | None = None) -> list[list[str]]:
    commands = _extract_script_command_templates(skill_md, file_path)
    arg_sets: list[list[str]] = []
    for cmd in commands:
        args = _render_trial_command_args(cmd, file_path)
        if args is not None:
            arg_sets.append(args)
    if not arg_sets and _script_reads_json_argv(content, runtime_for_language(language_for_path(file_path), file_type_for_path(file_path))):
        entry = _skill_plan_entry_for_file(file_path=file_path, skill_plan_entry=skill_plan_entry)
        payload = {
            "payload": _sample_value_for_placeholder("payload"),
            "user_request": _sample_value_for_placeholder("user_request"),
            "fields": {},
            "options": {},
            "input_files": [],
        }
        for key in entry.inputs or []:
            if key in payload:
                continue
            if key.endswith("_path") or key in {"image_path", "pdf_path", "docx_path", "pptx_path", "html_path"}:
                payload[key] = "inputs/sample.txt"
            elif key.endswith("_paths") or key in {"image_paths", "file_paths"}:
                payload[key] = ["inputs/sample.txt"]
            elif key.startswith("structured_") or key.endswith("_data") or key.endswith("_json"):
                payload[key] = {"title": "试运行样例", "items": ["示例"]}
            else:
                payload[key] = _sample_value_for_placeholder(key)
        arg_sets = [[json.dumps(payload, ensure_ascii=False)]]
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


def _format_script_functional_issues(issues: list[dict[str, Any]]) -> str:
    lines = ["script_functional 校验未通过："]
    for issue in issues:
        lines.append(
            "- {id}\n"
            "  failed_file: {failed_file}\n"
            "  failed_function: {failed_function}\n"
            "  code_region: {code_region}\n"
            "  reason: {reason}\n"
            "  minimal_edit: {minimal_edit}\n"
            "  allowed_scope: {allowed_scope}\n"
            "  forbidden_scope: {forbidden_scope}".format(
                id=issue.get("id", "script_functional.unknown"),
                failed_file=issue.get("failed_file", ""),
                failed_function=issue.get("failed_function", ""),
                code_region=issue.get("code_region", ""),
                reason=issue.get("reason", ""),
                minimal_edit=issue.get("minimal_edit", ""),
                allowed_scope=issue.get("allowed_scope", ""),
                forbidden_scope=issue.get("forbidden_scope", ""),
            )
        )
    return "\n".join(lines)


class ScriptFunctionalValidationError(ValueError):
    """First-round responsibility/evidence failure after script smoke passed."""

    def __init__(self, issues: list[dict[str, Any]], *, layer: str = "responsibility"):
        super().__init__(_format_script_functional_issues(issues))
        self.issues = issues
        self.layer = layer


def _stage_error_for_script_functional(exc: Exception) -> FileGenerationStageError:
    layer = getattr(exc, "layer", None) or _failure_layer_from_error_text(str(exc)) or "responsibility"
    return FileGenerationStageError(
        source="script_functional",
        layer=layer,
        detail=str(exc),
        original=exc,
    )


def _stdout_artifact_paths(payload: dict[str, Any], entry: SkillPlanEntry, canonical_contract: Any | None = None) -> list[str]:
    fields = _artifact_fields_for_entry(entry, canonical_contract)
    fields.extend([
        "image_path", "pdf_path", "docx_path", "pptx_path", "html_path",
        "image_paths", "file_paths", "file_outputs",
    ])
    paths: list[str] = []
    for field_name in dict.fromkeys(fields):
        value = payload.get(field_name)
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, str) and item.strip():
                paths.append(item.strip())
    return list(dict.fromkeys(paths))


def _resolve_trial_artifact_path(skill_dir: Path, raw_path: str) -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate
    if raw_path.startswith("outputs/"):
        return (skill_dir / candidate).resolve()
    return (skill_dir / "scripts" / candidate).resolve()


def _validate_artifact_content_evidence(
    *,
    stdout_payload: dict[str, Any],
    argv_payload: dict[str, Any],
    entry: SkillPlanEntry,
    skill_dir: Path,
    canonical_contract: Any | None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    artifact_paths = _stdout_artifact_paths(stdout_payload, entry, canonical_contract)
    input_strings = [
        text.strip()
        for text in _flatten_json_strings(argv_payload)
        if len(text.strip()) >= 8
    ]
    for raw_path in artifact_paths:
        path = _resolve_trial_artifact_path(skill_dir, raw_path)
        if not path.is_file():
            issues.append({
                "id": "script_functional.artifact_evidence.missing",
                "failed_file": entry.path,
                "failed_function": "artifact/file output generation",
                "code_region": "file write path and stdout artifact field",
                "reason": f"stdout 声明产物 {raw_path!r}，但试运行目录中不存在该文件。",
                "minimal_edit": "只修当前脚本的产物写入路径和 stdout 路径映射，确保返回的文件真实存在。",
                "allowed_scope": entry.path,
                "forbidden_scope": "不得改 SKILL.md、其它脚本或 SkillPlan；不得返回不存在路径。",
            })
            continue
        size = path.stat().st_size
        if size <= 0:
            issues.append({
                "id": "script_functional.artifact_evidence.empty",
                "failed_file": entry.path,
                "failed_function": "artifact/file output generation",
                "code_region": "file content write",
                "reason": f"产物 {raw_path!r} 已生成但文件为空。",
                "minimal_edit": "只修当前脚本的文件内容生成逻辑，让产物写入真实内容。",
                "allowed_scope": entry.path,
                "forbidden_scope": "不得输出空文件或占位文件骗过路径校验。",
            })
        if path.suffix.lower() == ".pdf":
            try:
                header = path.read_bytes()[:5]
            except OSError:
                header = b""
            if header != b"%PDF-":
                issues.append({
                    "id": "script_functional.artifact_evidence.pdf_header",
                    "failed_file": entry.path,
                    "failed_function": "pdf artifact generation",
                    "code_region": "PDF writer/output file creation",
                    "reason": f"产物 {raw_path!r} 不是有效 PDF 文件头。",
                    "minimal_edit": "只修当前脚本 PDF 写入逻辑，确保生成真实 PDF，而不是普通文本或空占位文件。",
                    "allowed_scope": entry.path,
                    "forbidden_scope": "不得改产物字段协议或返回假 PDF 路径。",
                })
        elif path.suffix.lower() in {".txt", ".md", ".json", ".html", ".htm", ".csv"} and input_strings:
            try:
                artifact_text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                artifact_text = ""
            if artifact_text and not any(text in artifact_text for text in input_strings[:10]):
                issues.append({
                    "id": "script_functional.artifact_evidence.input_dependency",
                    "failed_file": entry.path,
                    "failed_function": "artifact content assembly",
                    "code_region": "input processing through artifact write",
                    "reason": f"文本类产物 {raw_path!r} 没有体现试运行输入内容，可能未消费 argv JSON。",
                    "minimal_edit": "只修当前脚本产物内容组织逻辑，让文本类产物真实依赖输入或工具/模型结果。",
                    "allowed_scope": entry.path,
                    "forbidden_scope": "不得返回固定模板、mock 内容或空壳产物。",
                })
    return issues


def _flatten_json_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for item in value.values():
            out.extend(_flatten_json_strings(item))
        return out
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_flatten_json_strings(item))
        return out
    return []


def _validate_image_payload_shape(payload: dict[str, Any]) -> bool:
    image_path = payload.get("image_path")
    if isinstance(image_path, str) and image_path.strip():
        return True

    image_paths = payload.get("image_paths")
    if isinstance(image_paths, list) and image_paths and all(isinstance(p, str) and p.strip() for p in image_paths):
        return True

    return False



def _html_output_candidates(payload: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    html_path = payload.get("html_path")
    if isinstance(html_path, str) and html_path.strip():
        candidates.append(html_path.strip())
    for key in ("file_paths", "file_outputs"):
        paths = payload.get(key)
        if isinstance(paths, list):
            candidates.extend(path.strip() for path in paths if isinstance(path, str) and path.strip())
    return candidates


def _validate_html_trial_outputs(payload: dict[str, Any], *, skill_dir: Path | None, args: list[str], stdout: str) -> None:
    checked: list[Path] = []
    for candidate in _html_output_candidates(payload):
        candidate_path = Path(candidate)
        if not candidate_path.is_absolute() and skill_dir is not None:
            if candidate.startswith("outputs/"):
                candidate_path = (skill_dir / candidate_path).resolve()
            else:
                candidate_path = (skill_dir / "scripts" / candidate_path).resolve()
        checked.append(candidate_path)
        if skill_dir is not None:
            generated_root = (skill_dir / "outputs").resolve()
            try:
                candidate_path.relative_to(generated_root)
            except ValueError:
                raise ValueError(
                    "html_asset_builder 输出路径必须位于当前 Skill 的 OUTPUT_DIR/outputs 下："
                    f"argv={args!r} stdout={stdout[-4000:]} checked={candidate_path}"
                )
        if candidate_path.is_file() and candidate_path.suffix.lower() in {".html", ".htm"}:
            text = candidate_path.read_text(encoding="utf-8", errors="replace").lower()
            if "<html" in text or "<!doctype html" in text:
                return
    raise ValueError(
        "html_asset_builder 试运行必须输出 html_path 或 file_paths/file_outputs，并生成真实 HTML 文件："
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



_LEGACY_OUTPUT_ALIASES: dict[str, tuple[str, ...]] = {}


def _payload_has_declared_output(payload: dict[str, Any], output_key: str) -> bool:
    """New Creator E2E requires exact declared output fields; no alias guessing."""
    return output_key in payload


def _payload_output_value(payload: dict[str, Any], output_key: str) -> Any:
    """Return exact declared output value only; aliases belong in migration/warnings."""
    return payload.get(output_key) if output_key in payload else None

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


def _validate_trial_stdout_json(*, stdout: str, content: str, args: list[str], role: str | None = None, skill_dir: Path | None = None, skill_plan_entry: dict[str, Any] | None = None, canonical_contract: Any | None = None) -> None:
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
    if canonical_contract is not None:
        stdout_schema = getattr(canonical_contract, "stdout_schema", {}) or {}
        required = stdout_schema.get("required") if isinstance(stdout_schema, dict) else []
        missing = [str(key) for key in required or [] if str(key) not in payload or not _json_value_non_empty(payload.get(str(key)))]
        if missing:
            raise ValueError(
                "stdout_required_outputs_missing: 当前脚本 stdout 缺少 required_outputs。"
                f" missing={missing!r} actual={list(payload.keys())!r} required={list(required or [])!r} "
                f"argv={args!r} stdout={stripped[-4000:]}"
            )
    elif skill_plan_entry is not None:
        entry = _skill_plan_entry_for_file(file_path=str((skill_plan_entry or {}).get("path") or "scripts/main.py"), skill_plan_entry=skill_plan_entry)
        stdout_schema = _script_stdout_schema_for_entry(entry)
        required = stdout_schema.get("required") if isinstance(stdout_schema, dict) else []
        missing = [str(key) for key in required or [] if str(key) not in payload or not _json_value_non_empty(payload.get(str(key)))]
        if missing:
            raise ValueError(
                "stdout_required_outputs_missing: 当前脚本 stdout 缺少 required_outputs。"
                f" missing={missing!r} actual={list(payload.keys())!r} required={list(required or [])!r} "
                f"argv={args!r} stdout={stripped[-4000:]}"
            )

    try:
        if skill_dir is not None:
            validate_stdout_file_outputs(stripped, skill_dir=skill_dir, cwd=skill_dir / "scripts")
    except FileOutputValidationError as exc:
        raise ValueError(str(exc)) from exc



def _install_capability_dependencies(venv_python: Path, required_capabilities: list[str]) -> None:
    """Install platform-owned runtime dependencies for required capabilities.

    Creator trial runs execute generated scripts in a per-skill venv.  Scripts
    commonly import only ``backend.services.skill_runtime`` while the helper
    itself lazy-imports optional runtime dependencies. Static script import
    scanning cannot see those helper internals, so install the
    dependencies declared by the Creator tool registry before execution.
    """
    dependencies: list[str] = []
    seen: set[str] = set()
    for capability_name in required_capabilities or []:
        capability = get_tool_capability(capability_name)
        if capability is None:
            continue
        for dependency in capability.dependencies or []:
            package = str(dependency.get("package") or dependency.get("name") or "") if isinstance(dependency, dict) else str(dependency or "")
            if package and package not in seen:
                seen.add(package)
                dependencies.append(package)

    _install_declared_dependency_packages(venv_python, dependencies, source_label="capability")


def _install_declared_dependency_packages(venv_python: Path, dependencies: list[str], *, source_label: str = "declared") -> None:
    dependencies = [str(item).strip() for item in dependencies or [] if str(item).strip()]
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

    logger.info("skill-env: pip installing %s deps into venv: %s", source_label, missing)
    result = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--quiet", *missing],
        timeout=180,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"安装 {source_label} 依赖失败 ({', '.join(missing)}): {result.stderr[:500]}")


def _contract_resolution_for_trial(file_path: str, skill_md: str, role: str | None, skill_plan_entry: dict[str, Any] | None) -> tuple[Any, Any]:
    entry = _skill_plan_entry_for_file(
        file_path=file_path,
        blueprint_text=skill_md,
        role=role,
        skill_plan_entry=skill_plan_entry,
    )
    stdout_schema = _script_stdout_schema_for_entry(entry)
    contract = compile_canonical_file_contract(entry, stdout_schema)
    resolution = resolve_implementation(entry, contract)
    refined_contract = refine_contract_with_resolution(contract, resolution)
    resolution = resolve_implementation(entry, refined_contract)
    return refined_contract, resolution




def _artifact_fields_from_contract_dict(contract: dict[str, Any] | None) -> list[str]:
    fields: list[str] = []
    contract = contract or {}
    for key in ("artifact_fields", "file_fields", "stdout_fields", "file_outputs", "output_fields"):
        raw = contract.get(key)
        if isinstance(raw, str) and raw.strip():
            fields.append(raw.strip())
        elif isinstance(raw, list):
            fields.extend(str(item).strip() for item in raw if str(item).strip())
    raw_outputs = contract.get("artifact_outputs")
    if isinstance(raw_outputs, list):
        for item in raw_outputs:
            if isinstance(item, dict):
                field = str(item.get("field") or item.get("name") or "").strip()
                if field:
                    fields.append(field)
    return list(dict.fromkeys(fields))


def _artifact_fields_from_tool_manifests(entry: SkillPlanEntry) -> list[str]:
    fields: list[str] = []
    for capability_name in list(entry.required_capabilities or []) + list(entry.selected_tools if hasattr(entry, "selected_tools") else []):
        cap = get_tool_capability(str(capability_name))
        if not cap:
            continue
        for output in getattr(cap, "artifact_outputs", []) or []:
            if isinstance(output, dict):
                field = str(output.get("field") or output.get("name") or "").strip()
                if field:
                    fields.append(field)
        for fn in getattr(cap, "functions", []) or []:
            for output in getattr(fn, "artifact_outputs", []) or []:
                if isinstance(output, dict):
                    field = str(output.get("field") or output.get("name") or "").strip()
                    if field:
                        fields.append(field)
    return list(dict.fromkeys(fields))


def _artifact_fields_for_entry(entry: SkillPlanEntry, canonical_contract: Any | None = None) -> list[str]:
    fields: list[str] = []
    fields.extend(_artifact_fields_from_contract_dict(entry.artifact_contract))
    if canonical_contract is not None:
        fields.extend(_artifact_fields_from_contract_dict(getattr(canonical_contract, "artifact_contract", {}) or {}))
    fields.extend(_artifact_fields_from_tool_manifests(entry))
    return list(dict.fromkeys(field for field in fields if field))


def _script_runtime_spec_to_dict(spec: ScriptRuntimeSpec | None) -> dict[str, Any] | None:
    if spec is None:
        return None
    return {
        "script_path": spec.script_path,
        "runtime": spec.runtime,
        "role": spec.role,
        "responsibility": spec.responsibility,
        "input_policy": spec.input_policy,
        "accepted_sample_argv": spec.accepted_sample_argv,
        "required_outputs": spec.required_outputs,
        "actual_stdout_fields": spec.actual_stdout_fields,
        "artifact_fields": spec.artifact_fields,
        "file_outputs": spec.file_outputs,
        "stdout_json": spec.stdout_json,
        "artifact_paths": spec.artifact_paths,
        "command_template": spec.command_template,
    }


def _build_script_runtime_spec_from_trial(
    *,
    file_path: str,
    entry: SkillPlanEntry,
    args: list[str],
    stdout: str,
    skill_dir: Path,
    canonical_contract: Any | None = None,
) -> ScriptRuntimeSpec:
    accepted: dict[str, Any] = {}
    if args:
        try:
            parsed_arg = json.loads(args[0])
            if isinstance(parsed_arg, dict):
                accepted = parsed_arg
        except Exception:
            accepted = {}

    payload = json.loads((stdout or "{}").strip())
    actual_fields = list(payload.keys()) if isinstance(payload, dict) else []
    file_outputs: list[str] = []
    artifact_fields: list[str] = _artifact_fields_for_entry(entry, canonical_contract)
    if isinstance(payload, dict) and artifact_fields:
        for key in artifact_fields:
            value = payload.get(key)
            values = value if isinstance(value, list) else [value]
            for item in values:
                if isinstance(item, str) and item.strip():
                    file_outputs.append(item.strip())

    command_template = render_script_command_from_skill_plan(
        entry,
        runtime_spec={
            "script_path": file_path,
            "runtime": entry.runtime,
            "accepted_sample_argv": accepted or {"payload": "{{user_request}}", "fields": {}, "options": {}, "input_files": []},
        },
    )
    return ScriptRuntimeSpec(
        script_path=file_path,
        runtime=entry.runtime,
        role=entry.role,
        responsibility=entry.purpose,
        input_policy="宽松 JSON argv object；兼容 payload/fields/options/input_files 和上游 stdout 字段。",
        accepted_sample_argv=accepted or {"payload": "{{user_request}}", "fields": {}, "options": {}, "input_files": []},
        required_outputs=list(entry.outputs or []),
        actual_stdout_fields=actual_fields,
        artifact_fields=sorted(set(artifact_fields)),
        file_outputs=file_outputs,
        stdout_json=payload if isinstance(payload, dict) else {},
        artifact_paths=_stdout_artifact_paths(payload, entry, canonical_contract) if isinstance(payload, dict) else [],
        command_template=command_template,
    )


def _trial_run_generated_script_with_plan(
    skill_name: str,
    file_path: str,
    content: str,
    *,
    role: str | None = None,
    skill_plan_entry: dict[str, Any] | None = None,
) -> ScriptRuntimeSpec | None:
    try:
        return _trial_run_generated_script(skill_name, file_path, content, role, skill_plan_entry)
    except TypeError as exc:
        # Some tests monkeypatch the trial runner with the historical
        # 4-argument signature; keep production role-aware calls while allowing
        # those narrow fakes to exercise the repair loop.
        if "positional" not in str(exc) and "unexpected keyword" not in str(exc):
            raise
        return _trial_run_generated_script(skill_name, file_path, content, role)


def _trial_run_generated_script(
    skill_name: str,
    file_path: str,
    content: str,
    role: str | None = None,
    skill_plan_entry: dict[str, Any] | None = None,
) -> ScriptRuntimeSpec | None:
    """Run a generated Python script before accepting it from Creator.

    Python scripts are executed in a temporary per-skill virtual environment.
    Trial runs install only dependencies declared by selected capabilities /
    canonical contracts; arbitrary third-party imports must fail validation.
    """
    if not file_path.startswith("scripts/") or Path(file_path).suffix.lower() != ".py":
        return None

    skill_md_path = settings.skills_path / skill_name / "SKILL.md"
    skill_md = skill_md_path.read_text(encoding="utf-8") if skill_md_path.is_file() else ""
    _validate_script_contract_static(
        file_path=file_path,
        content=content,
        skill_md=skill_md,
        skill_plan_entry=skill_plan_entry,
    )

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
            refined_contract, resolution = _contract_resolution_for_trial(file_path, skill_md, role, skill_plan_entry)
            _install_declared_dependency_packages(
                venv_python,
                list(refined_contract.declared_dependencies or []) + list(resolution.declared_dependencies or []),
                source_label="implementation_resolution",
            )
        except RuntimeError as exc:
            raise ValueError(f"脚本试运行环境准备失败：{exc}") from exc

        for args in _trial_args_for_script(skill_md, file_path, content, skill_plan_entry=skill_plan_entry):
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
                canonical_contract=refined_contract,
            )
            stdout_payload = json.loads((proc.stdout or "{}").strip())
            argv_payload: dict[str, Any] = {}
            if args:
                try:
                    parsed_argv = json.loads(args[0])
                    if isinstance(parsed_argv, dict):
                        argv_payload = parsed_argv
                except Exception:
                    argv_payload = {}
            functional_issues = validate_script_functional_evidence(
                content=content,
                stdout_payload=stdout_payload,
                argv_payload=argv_payload,
                contract=refined_contract,
                resolution=resolution,
            )
            functional_issues.extend(_validate_artifact_content_evidence(
                stdout_payload=stdout_payload,
                argv_payload=argv_payload,
                entry=inferred_entry,
                skill_dir=skill_dir,
                canonical_contract=refined_contract,
            ))
            if functional_issues:
                layer = str(functional_issues[0].get("id") or "responsibility").split(".", 2)[-1]
                raise ScriptFunctionalValidationError(functional_issues, layer=layer)
            return _build_script_runtime_spec_from_trial(
                file_path=file_path,
                entry=inferred_entry,
                args=args,
                stdout=proc.stdout,
                skill_dir=skill_dir,
                canonical_contract=refined_contract,
            )

    return None


def _e2e_layer_from_errors(errors: list[str]) -> str:
    for error in errors or []:
        match = re.search(r"^E2E_LAYER=([^\n]+)", str(error), re.M)
        if match:
            return match.group(1).strip()
    return ""


def _targeted_e2e_repair_hint(errors: list[str]) -> str:
    layer = _e2e_layer_from_errors(errors)

    if layer == "final_platform_output_contract":
        return (
            "当前失败只属于最终平台输出字段不对齐。"
            "不要修改 SKILL.md，不要新增模型调用，不要改变已有业务字段。"
            "请保留最后一步脚本 stdout JSON 的原有业务字段，并额外映射到合法平台最终输出字段。"
            "如果最后一步已有可展示的主要结果值，保留原字段并额外映射到 text 或 markdown。"
            "如果最后一步产出文件，输出对应平台文件字段。"
        )

    if layer == "final_platform_output_value_invalid":
        return (
            "当前失败属于最终平台字段值类型不合法。"
            "请保持字段名不变，但修正值类型："
            "text/markdown/pdf_path/docx_path/pptx_path/html_path 必须是非空字符串；"
            "image_paths/file_paths/file_outputs 必须是非空字符串列表。"
        )

    if layer in {"external_input_missing", "e2e_dataflow_missing"}:
        return (
            "当前失败属于命令占位符无法从 payload 或前序 stdout 解析。"
            "优先修 SKILL.md 当前失败步骤的 JSON argv placeholder，"
            "不要改已成功 trace 对应步骤。"
        )

    return ""


def _failure_layer_from_error_text(error_text: str) -> str | None:
    text = (error_text or "").lower()
    if "stdout" in text and "json" in text:
        return "stdout_not_json"
    if "wrapped" in text or "value is object" in text or "expected string" in text:
        return "final_platform_output_value_invalid"
    if "artifact_missing" in text or "does not exist" in text or "missing artifact" in text:
        return "artifact_missing"
    if "artifact_invalid" in text or "invalid artifact" in text:
        return "artifact_invalid"
    if "helper" in text and ("failed" in text or "error" in text):
        return "helper_call_failed"
    if "exit" in text or "traceback" in text:
        return "script_exit"
    return None

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
        f"Rewrite as raw {repair_language} source. Remove any fenced code blocks or file labels. Do NOT include Markdown fences, explanations, file headers, or multi-file content. Keep JSON argv parsing broad and compatible with the current workflow envelope; do not change the blueprint or SKILL.md. {runtime_rule}"
        if is_script
        else "最终只返回 SKILL.md 文件正文；不要在文件外层套 Markdown 代码块，不要输出 Creator 创建流程、确认清单或 `点击开始创建` 文案。"
    )
    local_edit_scope = (
        "保留其它已经正确的导入、函数、参数解析、stdout JSON 协议和业务逻辑。"
        if is_script
        else "保留已经正确的 frontmatter、章节结构、脚本命令示例和 reference 引用。"
    )
    if is_script:
        tool_context = _creator_tool_context_for_script(
            file_path=file_path,
            skill_plan_entry=plan_entry,
            failure_layer=_failure_layer_from_error_text(validation_error),
            error_text=validation_error,
            include_snippets=True,
        ) if plan_entry is not None else ""
        extra_rules = (
            "Python / Node / Bash 必须按 SkillPlan.runtime 读取单个 JSON argv；输入解析应宽松兼容 payload / fields / options / input_files / 上游 stdout 字段；"
            "修复只能基于当前确定性验证错误、当前脚本合同与必要 Tool Snippet；"
            "不要根据错误文本、业务词、文件名或输出类型重新判断 role/capabilities；"
            "不要修改 SkillPlan、SKILL.md capability、workflow 或上下游脚本；"
            "不要生成 topicstring / tonehumorous / stylepopular-science 这类把 key、类型或默认值拼接起来的字段；"
            f"\n统一 Tool Registry 上下文：\n{tool_context}"
        )
    else:
        extra_rules = (
            "你只修 SKILL.md；保持主体内容，不重写整个文件；"
            "如果错误是命令格式，只修对应 bash command block；统一使用 python scripts/<file>.py '<JSON object>'；"
            "不得使用 --args、--key value、--text_file、--image_file 或裸 JSON；"
            "不要检查或修复上下游字段是否完全接上，那属于第二轮 E2E；"
            "不要改变 workflow 主体、已通过命令块、未被错误指向的文件、role/capability；"
            "如果蓝图包含 references/，必须在正文中明确引用对应 reference 路径；"
            "不得复制 Creator UI 流程、待确认清单、文件创建面板说明或系统自动创建文件提示。"
        )

    repair_messages = [*prompt_messages]
    repair_snippet_text = ""
    if is_script and plan_entry is not None:
        snippets = resolve_tool_snippets_for_context(
            role=plan_entry.role,
            capabilities=list(plan_entry.required_capabilities or []) + list(plan_entry.optional_capabilities or []),
            tool_names=[],
            file_path=file_path,
            failure_layer=_failure_layer_from_error_text(validation_error),
            error_text=validation_error + "\n" + previous_content[-6000:],
            max_snippets=5,
        )
        repair_snippet_text = tool_snippet_prompt(snippets)
    previous_for_prompt = previous_content[-8000:] if is_script else previous_content[-16000:]
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
            "这是 script_functional 职责失败后的 localized_patch："
            "只能修改校验模型/确定性证据指出的函数、行号或代码区域；"
            "必须保留已通过的 import、parse_args、main 入口、JSON argv 协议、stdout 字段名和文件输出协议。"
            if is_script and repair_mode == "localized_patch"
            else (
                "请优先做局部修改：只修改校验意见指出的最小错误片段，"
                f"{local_edit_scope}"
                "修改完成后可以整合输出。"
            )
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
            + ("\n\n本次失败涉及以下工具；请优先按相关 Tool Snippet 修复，不要继续猜工具调用方式：\n" + repair_snippet_text if repair_snippet_text else "")
            + ("" if is_script else (f"\n\n完整 contract（最终输出必须满足全部条目）：\n{contract_text}" if contract_text else ""))
            + ("" if is_script else (f"\n\n已通过检查（必须保留，不要重写或删除对应内容）：\n{passed_checks_text}" if passed_checks_text else ""))
            + (f"\n\n未通过检查（本轮只修这些项）：\n{failed_checks_text}" if failed_checks_text else "")
            + (f"\n\n本轮修复模式：{repair_mode}" if repair_mode else "")
            + ("\n- minimal_edit：只做最小编辑；localized_patch：只修失败函数/行号/区域；strict_contract_rewrite：仅 content_review 允许重写目标小节且必须保留已通过项。")
            + ("\n- scripts/ 修复示例：Rewrite as raw <language> source, remove any fenced code blocks or file labels, keep JSON argv parsing broad and preserve required stdout outputs." if is_script else "")
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

    if "stdout_required_outputs_missing" in error_text:
        return (
            "当前脚本 stdout 缺少 required_outputs。不要改 SKILL.md；不要为了适配所有输入参数重写逻辑。"
            "只修改当前脚本 run()/main() 的输出组织。必须返回 deterministic error 中 missing 列出的字段，且值非空；"
            "保留已有核心业务逻辑和宽松 JSON argv 读取，不要把修复方向转向输入参数重命名。"
        )

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
                "按模型审查意见最小修复 SKILL.md：必须覆盖蓝图真实规划任务、真实 scripts、真实 references、真实 assets、workflow 顺序和最终产物类型；第一轮不要证明内部 stdout/placeholder 闭环；"
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

        if "script_functional" in error_text or "职责" in error_text or "responsibility" in lower_error:
            return (
                "当前失败属于 script_functional 职责闭环失败，不是 script_smoke 运行失败。"
                "只修当前脚本中校验信息指出的函数、行号或代码区域；"
                "保留已经通过的 import、parse_args/main 入口、JSON argv 协议、stdout 字段名和文件输出协议；"
                "不得改 SKILL.md、其它脚本或 SkillPlan；不得进入全量重写；"
                "不得通过 try/except 吞错后输出假成功、固定模板、空值或 mock 数据。"
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
                "如果当前脚本生成文件产物，请修复真实文件创建路径并返回平台可消费的文件字段。"
            )

        if "Markdown 代码块或多文件包" in error_text or "script.raw_source.single_file" in error_text:
            return (
                "本轮必须把上一次内容改成单个裸脚本源码：删除所有 ``` fence、```python/```text 标签、"
                "文件路径标题、写入文件标签、解释性文字和多文件包内容；最终响应第一个字符应是脚本源码字符。"
            )

        if "forbidden_image_generation" in error_text or "调用了图片生成 helper" in error_text:
            return (
                "第一轮不因 required/optional/allowed_capabilities 工具路线阻断。"
                "如调用导致 import/运行/stdout/artifact 失败，repair 只能修当前脚本，不能修改 SkillPlan、capability 声明或 workflow。"
            )

        if (
            "script.required_capabilities.called" in error_text
            or "未调用这些 required_capabilities" in error_text
            or "没有调用这些 required_capabilities" in error_text
        ):
            return (
                "按当前脚本的轻量 script_composition 上下文修复脚本闭环："
                "根据 script_goal、inputs、outputs、available_tools、resource_refs、output_contract、rules 组合工具与本地逻辑；"
                "available_tools 只是候选，最终由单文件 evidence/stdout/artifact 校验和 E2E 数据流校验共同验证。"
                "禁止返回固定 template-only 文本、placeholder、空对象或空路径；蓝图和 SKILL.md 确定后只能修当前脚本。"
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
    """Ask the validator model for advisory explanations only.

    The validator model must never be a hard gate. This function is only called
    after deterministic backend checks or a trial run have already produced a
    real failure, and its output is filtered to the deterministic failure set.
    """
    if not str(deterministic_error or "").strip():
        return {
            "passed": True,
            "issues": [],
            "failed_checks": [],
            "preserve": [],
            "repair_instructions": "",
            "model": "",
        }
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
                "你不决定 passed/failed；后端确定性检查和 trial run 才是唯一裁决。"
                "你只解释后端已经给出的真实错误，并给 coder 可执行的局部修复意见。"
                "重要规则：Markdown 执行命令必须使用标准 ```bash fenced code block；"
                "机器可读 JSON 示例必须使用标准 ```json fenced code block；"
                "不要使用 '''bash 或 '''json；不要把行内 scripts/*.py 当成执行命令。"
                "SKILL.md YAML frontmatter 只需要在文件开头用 --- 开启，并在 metadata 后用 --- 关闭；"
                "不要求整个文件末尾再出现 ---。"
                "禁止报告任何未经后端结构化检查确认的推测性运行风险；"
                "禁止新增 failed_checks，禁止猜输入字段，禁止要求修改 SkillPlan/capability/workflow；"
                "真实运行错误只以后端确定性检查和单脚本 smoke 阶段结果为准。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"目标文件：{file_path}\n"
                "后端确定性校验/试运行错误：\n"
                f"{deterministic_error}\n\n"
                + (f"当前脚本合同/输入输出/产物摘要：\n{contract_text[-4000:]}\n\n" if contract_text else "")
                + (f"后端确定性 failed_checks（只能解释这些，不能新增）：\n{failed_checks_text}\n\n" if failed_checks_text else "")
                + (f"本轮修复模式：{repair_mode}\n\n" if repair_mode else "")
                + (f"后端根据该错误生成的必做修复步骤：\n{targeted_repair}\n\n" if targeted_repair else "")
                + "当前代码（尾部截断，仅供定位）：\n"
                "```text\n"
                f"{content[-8000:]}\n"
                "```\n\n"
                "请输出 JSON："
                "{\"passed\": false, \"issues\": [\"...\"], "
                "\"failed_checks\": [], "
                "\"localization\": [{\"failed_file\": \"当前文件路径\", \"failed_function\": \"函数名或 main/run/stdout\", "
                "\"line_region\": \"行号或代码区域\", \"reason\": \"为什么未完成职责\", "
                "\"minimal_edit\": \"最小修改建议\", \"allowed_scope\": \"只允许改哪里\", "
                "\"forbidden_scope\": \"不能改哪里\"}], "
                "\"preserve\": [\"已通过检查对应内容\"], "
                "\"repair_instructions\": \"只针对后端真实错误的局部修改指令；必须说明失败文件、失败函数、行号/区域、原因、最小修改建议、允许/禁止修改范围；不得引入 LLM 推测、SkillPlan/capability/workflow 修改或工具路线重规划。\"}"
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
    deterministic_failed = bool(str(deterministic_error or "").strip())
    return {
        # LLM validator is advisory only. It cannot decide pass/fail and cannot
        # create new blocking failures; deterministic backend checks/trial run
        # are the sole source of truth.
        "passed": not deterministic_failed,
        "issues": filtered_issues if deterministic_failed else [],
        "failed_checks": failed_checks if deterministic_failed else [],
        "preserve": [str(item) for item in preserve],
        "repair_instructions": instructions if deterministic_failed else "",
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
    """Ignore model-invented blocking issues.

    The backend has already produced structured checks and filtered model
    failed_checks to deterministic ids. Free-form model issues are kept out of
    blocking/repair decisions so the model can explain existing results without
    inventing new failure items.
    """
    return [], instructions or deterministic_error


def _format_file_validator_feedback(deterministic_error: str, validator_report: dict, targeted_repair: str = "") -> str:
    return (
        "后端确定性校验/试运行错误：\n"
        f"{deterministic_error}\n\n"
        + (f"后端确定性修复指令：\n{targeted_repair}\n\n" if targeted_repair else "")
        + f"校验模型：{validator_report.get('model', '')}\n"
        "说明：校验模型仅作为真实失败后的解释辅助；其 issues、failed_checks 和 repair_instructions "
        "不进入 repair prompt。repair 只允许后端确定性错误和确定性修复指令驱动。"
    )


async def _run_script_responsibility_review(
    *,
    file_path: str,
    script_content: str,
    skill_plan_entry: SkillPlanEntry,
    runtime_spec: ScriptRuntimeSpec,
    stdout_json: dict[str, Any],
    artifact_paths: list[str],
    deterministic_issues: list[dict[str, Any]],
    requested_model: str | None,
) -> dict[str, Any]:
    """Hard-gate model review for one script's own responsibility only."""
    route = route_model(
        VALIDATOR_TASK,
        requested_model=requested_model,
        reason=f"creator script responsibility review: {file_path}",
    )
    _log_creator_model_usage(
        phase="script_functional.responsibility_review.route",
        file_path=file_path,
        route=route,
        model=requested_model,
    )
    messages = [
        {
            "role": "system",
            "content": (
                "你是 Creator 第一轮单脚本职责验收模型，只输出严格 JSON object。"
                "你是 hard gate：必须判断当前脚本单独拿出来是否完成 SkillPlanEntry 的 purpose/role/outputs/capabilities。"
                "禁止判断上下游脚本是否接上；禁止判断整个 SKILL.md workflow；禁止要求修改其它脚本、SKILL.md 或 SkillPlan。"
                "职责失败时必须定位函数、行号或代码区域，并给出局部 minimal_edit。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"目标脚本：{file_path}\n"
                "SkillPlanEntry 摘要：\n"
                f"{json.dumps(skill_plan_entry.__dict__, ensure_ascii=False, default=str)[:6000]}\n\n"
                "runtime_spec：\n"
                f"{json.dumps(_script_runtime_spec_to_dict(runtime_spec), ensure_ascii=False, default=str)}\n\n"
                "stdout_json：\n"
                f"{json.dumps(stdout_json, ensure_ascii=False, default=str)}\n\n"
                "artifact_paths：\n"
                f"{json.dumps(artifact_paths, ensure_ascii=False)}\n\n"
                "deterministic_evidence_result：\n"
                f"{json.dumps(deterministic_issues, ensure_ascii=False, default=str)}\n\n"
                "当前脚本源码（带行号）：\n"
                f"{_numbered_source(script_content)[-12000:]}\n\n"
                "只判断当前脚本：是否完成 purpose、符合 role、消费 inputs、产生 outputs、满足 required capabilities、真实生成 artifact、没有 fake/mock/template-only 行为、工具/模型结果是否进入 stdout 或产物。\n"
                "返回 JSON 格式："
                "{\"passed\": false, \"issues\": [{\"severity\": \"error\", \"function\": \"run\", "
                "\"line_start\": 20, \"line_end\": 38, \"code_pattern\": \"return {...}\", "
                "\"problem\": \"...\", \"expected\": \"...\", "
                "\"minimal_edit\": \"只修改 run() 中组织输出的部分，不要改 parse_args/main/stdout 字段\"}], "
                "\"repair_instructions\": \"只允许局部修改失败区域\"}"
            ),
        },
    ]
    text = await complete_chat_once(messages, route.model)
    data = _parse_validator_json_object(text) or {}
    if not isinstance(data, dict):
        data = {}
    passed = bool(data.get("passed")) and not deterministic_issues
    issues = data.get("issues") if isinstance(data.get("issues"), list) else []
    if deterministic_issues:
        issues = [*deterministic_issues, *issues]
    return {
        "passed": passed,
        "issues": issues,
        "repair_instructions": str(data.get("repair_instructions") or "只允许局部修改失败区域"),
        "model": route.model,
    }


def _numbered_source(content: str) -> str:
    return "\n".join(f"{idx:04d}: {line}" for idx, line in enumerate((content or "").splitlines(), start=1))


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
    """Return protocol-only scaffolds; tool calls come from Tool Registry snippets."""
    plan_entry = _skill_plan_entry_for_file(
        file_path=file_path,
        purpose=purpose,
        blueprint_text=blueprint_text,
        role=role,
        skill_plan_entry=skill_plan_entry,
    )
    input_keys = list(plan_entry.inputs or ["payload"])
    output_keys = [key for key in (plan_entry.outputs or []) if isinstance(key, str) and key.strip()]
    if not output_keys:
        output_keys = ["text"]

    component_hint = getattr(plan_entry, "component_hint", "") or getattr(plan_entry, "role", "")
    helper_hint = f"# component_hint: {component_hint}\n"

    if plan_entry.runtime == "node":
        return (
            "协议骨架（只约束 argv/run/stdout；具体工具调用必须来自 Tool Registry snippets/function cards）：\n"
            + helper_hint +
            "const payload = process.argv[2] ? JSON.parse(process.argv[2]) : {};\n"
            "function run(payload) {\n"
            "  // TODO: implement the canonical contract using selected tools or real local logic.\n"
            "  // Return an object containing every required stdout field.\n"
            "  return {};\n"
            "}\n"
            "console.log(JSON.stringify(run(payload)));"
        )

    if plan_entry.runtime in {"bash", "shell"}:
        helper = "import json,sys; json.loads(sys.argv[1] or '{}'); print(json.dumps({}))"
        return (
            "协议骨架（只约束 $1 JSON argv 与 stdout JSON；具体工具调用必须来自 Tool Registry snippets/function cards）：\n"
            + helper_hint +
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "payload_json=${1:-'{}'}\n"
            f"python -c {shlex.quote(helper)} \"$payload_json\""
        )

    return (
        "协议骨架（只约束 parse_args/run/main/stdout JSON；具体工具调用必须来自 Tool Registry snippets/function cards）：\n"
        + helper_hint +
        "import json\n"
        "import sys\n\n"
        "def parse_args() -> dict:\n"
        "    if len(sys.argv) < 2:\n"
        "        return {}\n"
        "    data = json.loads(sys.argv[1])\n"
        "    if not isinstance(data, dict):\n"
        "        raise ValueError('argv JSON must be an object')\n"
        "    return data\n\n"
        "def run(payload: dict) -> dict:\n"
        "    # TODO: implement the canonical contract using selected tools or real local logic.\n"
        "    # Return an object containing every required stdout field.\n"
        "    return {}\n\n"
        "def main() -> None:\n"
        "    print(json.dumps(run(parse_args()), ensure_ascii=False))\n\n"
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


def _script_local_contract_payload(
    *,
    file_path: str,
    purpose: str,
    plan_entry: SkillPlanEntry,
    stdout_schema: dict[str, Any],
) -> dict[str, Any]:
    """Return the only business contract a script-generation prompt should need."""
    canonical_contract = compile_canonical_file_contract(plan_entry, stdout_schema)
    implementation_resolution = resolve_implementation(plan_entry, canonical_contract)
    available_tools = []
    tool_function_cards: list[str] = []
    selected_tool_names: list[str] = []
    for tool in implementation_resolution.available_tools or implementation_resolution.selected_tools:
        capability_name = str(tool.tool_id).split(".")[0]
        selected_tool_names.append(capability_name)
        cap = get_tool_capability(capability_name)
        if cap is not None:
            tool_function_cards.extend(function_cards_for_tool(cap))
        available_tools.append({
            "tool_id": tool.tool_id,
            "description": tool.description,
            "call_template": call_template_for_tool(tool),
            "signature": tool.signature,
            "input_schema": tool.input_schema,
            "output_schema": tool.output_schema,
            "return_contract": tool.return_contract,
            "example_return": tool.example_return,
            "example_stdout": tool.example_stdout,
            "common_mistakes": tool.common_mistakes,
            "snippets": tool.snippets,
            "usage_policy": tool.usage_policy,
            "required_env": tool.required_env,
            "required_secrets": tool.required_secrets,
            "artifact_outputs": tool.artifact_outputs,
        })
    tool_snippets = resolve_tool_snippets_for_context(
        role=plan_entry.role or "",
        capabilities=list(dict.fromkeys([req.capability_id for req in canonical_contract.capability_requirements if req.capability_id])),
        tool_names=list(dict.fromkeys(selected_tool_names)),
        file_path=file_path,
        max_snippets=8,
    )
    return {
        "file_path": file_path,
        "runtime": plan_entry.runtime,
        "language": plan_entry.language,
        "script_goal": purpose,
        "inputs": canonical_contract.inputs,
        "outputs": canonical_contract.outputs,
        "available_tools": available_tools,
        "tool_function_cards": tool_function_cards,
        "tool_snippets": tool_snippets,
        "tool_snippet_prompt": tool_snippet_prompt(tool_snippets),
        "resource_refs": canonical_contract.resource_refs,
        "output_contract": {
            "stdout_schema": stdout_schema,
            "artifact_contract": canonical_contract.artifact_contract,
        },
        "rules": [
            "Use script_composition: combine argv inputs, local logic, and any available_tools needed for the goal.",
            "available_tools are recalled candidates only; decide which to call based on the script goal and contracts.",
            "Only import a tool using its exact call_template import path; never guess backend imports.",
            "References/assets are resource_refs only, not pip/install/import dependencies.",
            "First round fixes only the current script; end-to-end chain repair happens later.",
        ],
        "implementation_resolution": {
            "mode": implementation_resolution.mode,
            "available_tools": available_tools,
            "tool_function_cards": tool_function_cards,
            "tool_snippets": tool_snippets,
            "tool_snippet_prompt": tool_snippet_prompt(tool_snippets),
            "allowed_imports": implementation_resolution.allowed_imports,
            "declared_dependencies": implementation_resolution.declared_dependencies,
            "required_evidence": implementation_resolution.required_evidence,
            "reason": implementation_resolution.reason,
        },
    }


def _script_stdout_schema_for_entry(plan_entry: SkillPlanEntry) -> dict[str, Any]:
    """Build a deterministic stdout schema from the per-file output contract."""
    properties = {
        key: {"description": f"Non-empty value for declared output field {key}."}
        for key in (plan_entry.outputs or [])
        if isinstance(key, str) and key.strip()
    }
    if not properties:
        properties = {"text": {"type": "string", "description": "Non-empty human-readable result."}}
    return {
        "type": "object",
        "required": list(properties.keys()),
        "properties": properties,
        "additionalProperties": True,
        "forbidden": ["error"],
    }


def _creator_file_generation_messages(task_content: str, *, system_rule: str | None = None) -> list[dict]:
    """Return Creator generation messages with the concrete task in user role."""
    rule = system_rule or "你是 Creator 文件内容生成器。只遵守用户消息中的当前文件生成任务；只输出目标文件内容。"
    return [
        {"role": "system", "content": rule},
        {"role": "user", "content": task_content},
    ]


def _ensure_user_visible_task_message(messages: list[dict]) -> list[dict]:
    """Guarantee same-model retries always include a user-visible task message."""
    if any(isinstance(message, dict) and message.get("role") == "user" and str(message.get("content") or "").strip() for message in messages):
        return messages
    task_content = "\n\n".join(
        str(message.get("content") or "")
        for message in messages
        if isinstance(message, dict) and str(message.get("content") or "").strip()
    )
    return _creator_file_generation_messages(task_content)


def _message_role_counts(messages: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "unknown")
        counts[role] = counts.get(role, 0) + 1
    return counts


def _message_role_chars(messages: list[dict], role: str) -> int:
    return sum(
        len(str(message.get("content") or ""))
        for message in messages
        if isinstance(message, dict) and message.get("role") == role
    )


def _build_script_generate_file_prompt_variant(
    *,
    file_path: str,
    skill_name: str,
    purpose: str,
    blueprint_text: str,
    role: str | None,
    skill_plan_entry: dict[str, Any] | None,
    variant: str,
) -> list[dict]:
    """Build script-only prompts using progressively smaller local contracts.

    Scripts must not receive the full blueprint, creator UI copy, global kernel
    docs, or E2E/platform workflow text. The platform owns invocation/stdout
    parsing; the model only implements this one file's internals.
    """
    plan_entry = _skill_plan_entry_for_file(
        file_path=file_path, purpose=purpose, blueprint_text=blueprint_text, role=role, skill_plan_entry=skill_plan_entry
    )
    stdout_schema = _script_stdout_schema_for_entry(plan_entry)
    local_contract = _script_local_contract_payload(
        file_path=file_path,
        purpose=purpose,
        plan_entry=plan_entry,
        stdout_schema=stdout_schema,
    )
    implementation_mode = str((local_contract.get("implementation_resolution") or {}).get("mode") or "script_composition")
    if implementation_mode == "unresolved":
        reason = ((local_contract.get("implementation_resolution") or {}).get("reason") if isinstance(local_contract.get("implementation_resolution"), dict) else "") or "canonical contract unresolved"
        raise HTTPException(
            status_code=422,
            detail={
                "severity": "user_warning",
                "code": "contract_unresolved",
                "source": "implementation_resolution",
                "path": file_path,
                "field": "implementation_resolution.mode",
                "message": reason,
                "repair": "补齐脚本 inputs/outputs/stdout_schema/artifact_contract 或注册真实 callable tool manifest 后再生成。",
            },
        )
    tool_usage_prompt = ""
    script_skeleton_text = ""
    if variant == "standard":
        script_skeleton_text = _script_generation_skeleton(
            file_path,
            purpose,
            "",
            role=plan_entry.role,
            skill_plan_entry=skill_plan_entry,
        )
    elif variant == "simplified":
        script_skeleton_text = _script_generation_skeleton(
            file_path,
            purpose,
            "",
            role=plan_entry.role,
            skill_plan_entry=skill_plan_entry,
        )
    resolution_payload = local_contract.get("implementation_resolution") if isinstance(local_contract.get("implementation_resolution"), dict) else {}
    canonical_payload = local_contract
    selected_tools_payload = resolution_payload.get("available_tools") if isinstance(resolution_payload, dict) else []
    tool_function_cards = local_contract.get("tool_function_cards") if isinstance(local_contract.get("tool_function_cards"), list) else []
    tool_snippets = local_contract.get("tool_snippets") if isinstance(local_contract.get("tool_snippets"), list) else []
    logger.info(
        "[Creator][script_generation_contract] file_path=%s inputs=%s outputs=%s output_contract=%s resource_refs=%s mode=%s available_tools=%s tool_function_cards_count=%d tool_snippets_count=%d required_evidence=%s allowed_imports=%s declared_dependencies=%s reason=%s",
        file_path,
        json.dumps(canonical_payload.get("inputs") or [], ensure_ascii=False),
        json.dumps(canonical_payload.get("outputs") or [], ensure_ascii=False),
        json.dumps(canonical_payload.get("output_contract") or {}, ensure_ascii=False, sort_keys=True),
        json.dumps(canonical_payload.get("resource_refs") or [], ensure_ascii=False, sort_keys=True),
        implementation_mode,
        json.dumps([tool.get("tool_id") for tool in selected_tools_payload if isinstance(tool, dict)], ensure_ascii=False),
        len(tool_function_cards),
        len(tool_snippets),
        json.dumps(resolution_payload.get("required_evidence") or [], ensure_ascii=False),
        json.dumps(resolution_payload.get("allowed_imports") or [], ensure_ascii=False),
        json.dumps(resolution_payload.get("declared_dependencies") or [], ensure_ascii=False),
        str(resolution_payload.get("reason") or ""),
    )

    instruction = [
        f'你正在为 Skill 包 "{skill_name}" 生成单个脚本文件：{file_path}。',
        "必须满足以下脚本文件合同（局部合同）：",
        "只实现当前文件；不要重新规划整个 Skill；不要输出 Markdown fence、解释、文件名标题或多文件包。",
        "scripts/ 生成不会追加聊天历史，也不会注入完整蓝图。",
        "外层调用、参数传递和 stdout 解析由 Creator 的确定性规则处理；你不要自由改协议，只实现内部逻辑。",
        "脚本必须读取一个 JSON object argv（Python: 读取 sys.argv[1] 并 json.loads 解析；Node: process.argv[2]；Bash: $1），并向 stdout 输出结构化 JSON object。",
        "stdout JSON 不得包含 error 字段；必须至少包含 stdout_schema.required 中的字段且值非空。",
        "必须读取输入并输出符合 stdout_schema.required 的非空字段；不要通过 error 字段、{}、空文件或空路径绕过运行和产物校验。",
        "只根据轻量上下文实现：script_goal、inputs、outputs、available_tools、tool_function_cards、tool_snippets、tool_snippet_prompt、resource_refs、output_contract、rules。raw role/capability 只能作为 hint，不能当硬合同。",
        "统一按 script_composition 生成脚本：代码模型根据功能目标自行决定如何组合 argv 输入、本地逻辑和 available_tools。",
        "available_tools 只是 embedding/结构化召回的候选工具，不是最终裁决；只有确实需要时才调用。",
        "工具/helper 如何组合不作为第一轮 hard gate；如 import/dependency、调用、stdout 或 artifact 失败，再修当前脚本。",
        "第一轮只修当前脚本；不要修改或重规划上下游链路，第二轮 E2E 才修整链路。",
        f"prompt_variant: {variant}",
        "当前文件结构化合同：",
        json.dumps(local_contract, ensure_ascii=False, indent=2),
        "动态工具函数卡片（从 registry/manifest 读取，不硬编码工具名）：",
        "\n\n---\n\n".join(tool_function_cards) if tool_function_cards else "无",
        "动态工具 Snippet 指南（从 registry/manifest 读取，不硬编码工具名）：",
        str(local_contract.get("tool_snippet_prompt") or "当前脚本可用工具 Snippets: 无"),
    ]
    if variant == "standard":
        instruction.append("不注入完整蓝图、kernel 文档或额外工具清单；只使用上面的轻量上下文。")
    if script_skeleton_text:
        instruction.extend(["固定脚本骨架 / 动态协议骨架（根据当前 outputs 生成；输出时应补全为可运行源码）：", script_skeleton_text])
    if variant == "minimal":
        instruction.append("极简要求：返回可运行脚本源码，解析 JSON argv，真实处理输入，打印满足 stdout_schema 的 JSON object。")
    return _creator_file_generation_messages(
        "\n\n".join(instruction),
        system_rule="你是 Creator 脚本文件生成器。只输出单个目标脚本源码；禁止解释、Markdown fence 或多文件包。",
    )

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

    if file_path.startswith("scripts/"):
        return _build_script_generate_file_prompt_variant(
            file_path=file_path,
            skill_name=skill_name,
            purpose=purpose,
            blueprint_text=blueprint_text,
            role=role,
            skill_plan_entry=skill_plan_entry,
            variant="standard",
        )

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
    tool_usage_prompt = ""
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
            "4. SKILL.md 第一轮只需生成静态可解析的使用说明和命令块；内部脚本流转由第二轮 E2E 真实执行验证。\n"
            "5. 如果蓝图包含 scripts/ 资源，SKILL.md 正文必须为每个 scripts/ 路径提供一个标准、独立、无缩进的 ```bash fenced code block。\n"
            "6. 每个 bash fenced code block 内只能有一条脚本命令；命令必须直接调用 scripts/ 路径，并在脚本路径后传入一个 JSON object argv。\n"
            "7. 第一条脚本命令只能引用 external envelope 中确定存在的通用字段：user_request、input、text、input_files、files、fields、options，或显式结构化来源提供的字段。\n"
            "8. 如果 Skill 需要业务字段，命令可把 user_request/input/text 或 fields 传给脚本，由脚本自行解析；第一轮不固定中间 stdout 字段名。\n"
            "9. 第一轮只要求命令 JSON argv 静态可解析，并优先引用 external envelope 或显式结构化来源；不要要求证明后续 placeholder 来自前序 stdout。\n"
            "10. JSON argv 必须是 json.loads 可解析的模板；所有动态 placeholder 必须是字符串值；禁止裸写动态数值 placeholder。\n"
            "11. 若需要数值默认值，直接写固定 JSON 数字；不要把动态数值 placeholder 裸露在 JSON 中。\n"
            "12. 批量处理、列表处理或多文件处理应由对应脚本内部完成，SKILL.md 静态说明中不展开自然语言循环。\n"
            "13. 列表或对象字段必须通过整值占位符传递；不要写成由无来源拆分字段拼接的列表。\n"
            "14. 如果蓝图包含 references/ 资源，SKILL.md 正文必须在“参考资料/资源”小节明确引用每个 references/ 路径，并说明何时读取。\n"
            "15. 不要在输出内容的外侧套 ``` 代码块，但 SKILL.md 正文内部必须按需包含标准 ```bash fenced code block。\n"
            "16. 禁止只写‘立即调用 `scripts/...`’这种隐式执行描述；必须写明 assistant 应输出可执行 fenced block。\n"
            "17. 禁止复制 Creator 界面流程、确认清单、‘点击开始创建/开始生成’、系统将自动创建文件等平台创建流程文案。\n"
            "18. 以下宿主 Markdown 执行说明是内部写作约束，只能转化为面向使用者的 Skill 说明，不要逐字复制这些约束或标题。\n"
            "19. 命令中 JSON key 是当前脚本读取的 argv 字段；{{placeholder}} 优先来自 external envelope 或显式 fields/defaults/input binding。内部上游 stdout 字段闭环只在第二轮 E2E 验证。\n"
            "20. 不要在第一轮为下游脚本固定无来源中间字段名；placeholder 来源和修复交给第二轮 E2E。\n"
            "21. 第一轮不要求声明最终 stdout 字段闭环；脚本 stdout 与平台标准输出字段由第二轮 E2E 真实执行验证。\n"
            "22. SKILL.md 必须覆盖蓝图真实规划的任务、真实脚本路径、资源使用、脚本调用顺序（如有）和最终产物类型；不要固定特定中间字段。\n"
            "23. 真实文件计划需要结合蓝图语境判断：目录结构、SkillPlan path、dependencies、references 字段通常是真实文件计划。\n"
            "24. 如果蓝图在“禁止隐式执行/示例/反例/例如/比如”语境中提到某个 scripts/*.py、references/*.md 或 assets/*，它只是解释性示例，不应进入最终 SKILL.md，除非它同时出现在目录结构或 SkillPlan path 中。\n"
            "25. 不要为了满足格式而新增蓝图外脚本；只为蓝图真实规划脚本提供命令块。\n"
            f"{_SKILL_MD_MARKDOWN_EXECUTION_GUIDE}\n\n"
            "以下 SKILL.md first-round static authoring guide 只约束静态格式和平台边界；内部脚本流转交给第二轮 E2E 验证：\n"
            f"{skill_md_e2e_authoring_guide}\n\n"
            "生成前请先隐式检查以下合同，最终输出必须逐项满足；如果合同要求内部 ```bash block，必须在 SKILL.md 正文中写出该 block：\n"
            f"{skill_md_contract_text}\n\n"
            f"蓝图声明的文件路径（必须覆盖对应 scripts/references 要求）：\n{declared_paths_text}\n\n"
            f"以下是已确认的蓝图（已移除 Creator UI 确认文案），你的内容必须与此一致：\n\n{clean_blueprint_text}"
        )
    elif file_path.startswith("scripts/"):
        local_contract = _script_local_contract_payload(
            file_path=file_path,
            purpose=purpose,
            plan_entry=plan_entry,
            stdout_schema=_script_stdout_schema_for_entry(plan_entry),
        )
        instruction = (
            f'你正在为 Skill 包 "{skill_name}" 生成单个脚本文件：{file_path}。\n\n'
            "只输出完整可运行源码本身；禁止 Markdown fence、解释、文件名标题或多文件输出。\n"
            "第一轮只修当前脚本；不要修改或重规划上下游链路，第二轮 E2E 才修整链路。\n"
            "生成前只使用以下轻量上下文：script_goal、inputs、outputs、available_tools、resource_refs、output_contract、rules。\n"
            "统一按 script_composition 理解：根据功能目标组合 argv 输入、本地逻辑和 available_tools；available_tools 只做候选召回，不是最终裁决。\n"
            "工具/helper 如何组合不作为第一轮 hard gate；如 import/dependency、调用、stdout 或 artifact 失败，再修当前脚本。\n"
            "references/assets 只能作为 resource_refs/asset_refs 读取，不能作为 dependencies、allowed_imports 或 pip install 依赖。\n"
            "生成后第一轮只校验协议 + 运行 + 产物：argv JSON、入口、stdout JSON object、required outputs、artifact_created、import/dependency 和危险系统操作。\n\n"
            "轻量脚本上下文：\n"
            f"{json.dumps(local_contract, ensure_ascii=False, indent=2)}\n\n"
            f"固定脚本骨架（仅约束入口/JSON stdout；输出时补全为可运行源码）：\n{script_skeleton_text}"
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

    messages: list[dict] = _creator_file_generation_messages(instruction)

    if file_path.startswith("scripts/"):
        # Scripts are generated from a short system rule plus a user-visible concrete task.
        # Do not append conversation history: recent Creator UI
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
    try:
        plan: BlueprintPlan = parse_blueprint(request.messages, strict=request.strict)
    except BlueprintShapeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    entries_by_path = {
        entry.path: entry
        for entry in (plan.skill_plan.files if plan.skill_plan else [])
        if not _is_directory_like_skill_path(entry.path)
    }

    blueprint_text = "\n\n".join(
        str(message.get("content") or "")
        for message in request.messages
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
    required_tool_names = {
        capability
        for file_spec in files_out
        for capability in file_spec.required_capabilities
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

    warnings = []
    seen_warning_keys: set[str] = set()
    for raw_warning in [*list(plan.warnings), *extra_path_warnings]:
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


@dataclass
class FileGenerationStageError(Exception):
    """Structured failure source for first-round generation validation."""

    source: str
    layer: str
    detail: str
    original: Exception | None = None

    def __str__(self) -> str:
        return self.detail


def _contract_failure_layer(results: list[ContractCheckResult]) -> str:
    failed = [result.id for result in results if not result.passed]
    return failed[0] if failed else "content_review"


def _stage_error_from_exception(source: str, exc: Exception, *, default_layer: str) -> FileGenerationStageError:
    if isinstance(exc, FileGenerationStageError):
        return exc
    if isinstance(exc, ContractValidationError):
        layer = _contract_failure_layer(exc.results) or default_layer
    else:
        layer = default_layer
    return FileGenerationStageError(source=source, layer=layer, detail=str(exc), original=exc)


def _first_round_repair_limit(source: str) -> int:
    return {
        "content_review": 3,
        "script_smoke": 5,
        "script_functional": 5,
        "model_empty_content": len(_EMPTY_GENERATION_PROMPT_VARIANTS),
    }.get(source, 3)


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




def _runtime_spec_bash_block_references(script_runtime_specs: list[Any] | None) -> str:
    blocks: list[str] = []
    for spec in script_runtime_specs or []:
        getter = (lambda key, default="": getattr(spec, key, default) if not isinstance(spec, dict) else spec.get(key, default))
        script_path = str(getter("script_path", "")).strip()
        if not script_path:
            continue
        command = str(getter("command_template", "")).strip()
        if not command:
            runtime = str(getter("runtime", "python"))
            argv = getter("accepted_sample_argv", {}) or {"payload": "{{user_request}}", "fields": {}, "options": {}, "input_files": []}
            runner = "python" if runtime == "python" else "node" if runtime == "node" else "bash" if runtime in {"bash", "shell"} else ""
            payload = json.dumps(argv, ensure_ascii=False, separators=(",", ":"))
            command = f"{runner + ' ' if runner else ''}{script_path} '{payload}'"
        blocks.append(f"脚本：{script_path}\n```bash\n{command}\n```")
    return "\n\n".join(blocks)


def _build_skill_md_model_finalizer_prompt(
    *,
    skill_name: str,
    description: str,
    blueprint_text: str,
    references: list[str] | None,
    assets: list[str] | None,
    script_runtime_specs: list[dict[str, Any]] | None,
    final_outputs: list[str] | None,
) -> list[dict[str, str]]:
    runtime_reference = _runtime_spec_bash_block_references(script_runtime_specs)
    resource_reference = {"references": references or [], "assets": assets or [], "final_outputs": final_outputs or []}
    return [
        {
            "role": "system",
            "content": (
                "你是 Skill.md 最终说明文档编辑器。请基于蓝图创作完整、自然、用户可读的 SKILL.md。"
                "runtime spec 只能作为脚本 bash block 的参考，不得泄露 runtime_contract、artifact_contract、ToolSlot、implementation_strategy 等内部字段。"
                "不要把本文档退化成合同字段清单；保留说明文档的表达能力。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"Skill 名称：{skill_name}\n"
                f"描述：{description}\n\n"
                "蓝图：\n"
                f"{clean_blueprint_body_text(blueprint_text or '')}\n\n"
                "已验证脚本命令参考（必须用作 bash fenced block 的依据，但不要公开内部 runtime spec 字段）：\n"
                f"{runtime_reference or '无脚本命令参考。'}\n\n"
                "资源和最终输出参考：\n"
                f"{json.dumps(resource_reference, ensure_ascii=False, indent=2)}\n\n"
                "请输出完整 SKILL.md 文件正文：包含 YAML frontmatter、适用场景、用户需提供内容、自动执行流程、每个真实脚本的 bash fenced block、references/assets 使用说明、最终产物和注意事项。"
                "第一轮只需满足格式和蓝图对齐；接口闭环由第二轮 E2E 校验。"
            ),
        },
    ]


def _strict_contract_rewrite_allowed(source: str) -> bool:
    # First-round repairs must stay localized: the validator identifies the
    # failed function/region, and the repair model edits only that region while
    # preserving entrypoints, JSON argv parsing, stdout fields, and artifact
    # protocol.  Do not escalate script failures into full rewrites.
    return source in {"content_review"}


def _repair_mode_for_first_round(*, source: str, file_path: str, attempt: int) -> str:
    if source == "script_functional" and file_path.startswith("scripts/"):
        return "localized_patch"
    if attempt >= 2 and file_path.startswith("scripts/") and _strict_contract_rewrite_allowed(source):
        return "strict_contract_rewrite"
    return "minimal_edit"


def _contract_result_to_failure(result: ContractCheckResult) -> dict[str, Any]:
    return {
        "id": result.id,
        "target": result.target,
        "message": result.message,
        "expected": result.expected,
        "minimal_edit": result.minimal_edit,
        "details": result.details,
        "layer": result.layer or _contract_layer_for_check_id(result.id),
    }


def _exception_to_skill_md_failures(exc: Exception, *, source: str = "skill_md") -> list[dict[str, Any]]:
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


def _non_code_text_near_script(content: str, script_path: str, window: int = 360) -> str:
    normalized = content.replace("\r\n", "\n")
    idx = normalized.find(script_path)
    if idx < 0:
        return ""
    start = max(0, idx - window)
    end = min(len(normalized), idx + len(script_path) + window)
    nearby = normalized[start:end]
    nearby = re.sub(r"```[\s\S]*?```", " ", nearby)
    nearby = re.sub(r"`[^`]*`", " ", nearby)
    nearby = re.sub(r"[#>*_\-\[\]()`]", " ", nearby)
    return re.sub(r"\s+", " ", nearby).strip()


def _is_generic_script_description(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "").lower()
    if len(compact) < 24:
        return True
    generic_patterns = [
        "执行脚本", "运行脚本", "处理任务", "运行该步骤", "执行该步骤",
        "调用脚本", "script", "runthisscript", "processtask",
    ]
    return any(pattern in compact for pattern in generic_patterns) and len(compact) < 60


def _check_skill_md_script_narrative_quality(content: str, script_paths: list[str]) -> list[ContractCheckResult]:
    results: list[ContractCheckResult] = []
    for script_path in dict.fromkeys(path for path in script_paths if path.startswith("scripts/")):
        mentioned = script_path in content
        results.append(ContractCheckResult(
            id="skill_md.script.mentioned",
            passed=mentioned,
            target=script_path,
            message=f"{script_path} 已在 SKILL.md 中出现。" if mentioned else f"{script_path} 未在 SKILL.md 中出现。",
            expected="每个真实 scripts/** 都必须在 SKILL.md 中被提及。",
            minimal_edit=f"添加 {script_path} 的自然语言功能说明和对应 bash fenced block。",
            layer="skill_md_first_round",
        ))
        commands = _extract_script_command_templates(content, script_path) if mentioned else []
        results.append(ContractCheckResult(
            id="skill_md.script.bash_block_nearby",
            passed=bool(commands),
            target=script_path,
            message=f"{script_path} 有对应 bash fenced block。" if commands else f"{script_path} 缺少对应 bash fenced block。",
            expected="每个脚本必须有对应 ```bash fenced block，且 block 调用真实脚本路径。",
            minimal_edit=f"为 {script_path} 添加调用真实脚本路径且 argv 为 JSON object 的 bash fenced block。",
            layer="skill_md_first_round",
        ))
        nearby_text = _non_code_text_near_script(content, script_path)
        has_specific_description = bool(nearby_text) and not _is_generic_script_description(nearby_text)
        results.append(ContractCheckResult(
            id="skill_md.script.narrative_quality",
            passed=has_specific_description,
            target=script_path,
            message=f"{script_path} 附近有非代码块的具体功能说明。" if has_specific_description else f"{script_path} 附近缺少具体自然语言功能说明，或说明过于空泛。",
            expected="每个脚本附近必须说明它在整体流程中的作用，不能只有“执行脚本/处理任务/运行该步骤”。",
            minimal_edit=f"在 {script_path} 的 bash block 前后添加一句具体说明：它读取什么、完成什么流程步骤、产出什么用户可理解结果。",
            details={"nearby_text": nearby_text[:240]},
            layer="skill_md_first_round",
        ))
    return results


def _skill_md_script_paths_from_runtime_specs(specs: list[dict[str, Any]] | None) -> list[str]:
    paths: list[str] = []
    for spec in specs or []:
        path = str((spec or {}).get("script_path") or "").strip()
        if path.startswith("scripts/"):
            paths.append(path)
    return paths


def _skill_md_first_round_failures(
    *,
    skill_name: str,
    content: str,
    blueprint_text: str,
    script_runtime_specs: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    try:
        _raise_file_contract_failures(validate_file_contract(
            file_path="SKILL.md",
            content=content,
            blueprint_text=blueprint_text or "",
            skill_plan_entry=None,
        ))
    except Exception as exc:
        failures.extend(_exception_to_skill_md_failures(exc, source="skill_md_contract"))
    try:
        _validate_skill_md_against_existing_files(
            skill_name,
            content,
            blueprint_text=blueprint_text or "",
            require_existing=False,
        )
    except Exception as exc:
        failures.extend(_exception_to_skill_md_failures(exc, source="skill_md_files"))
    script_paths = _skill_md_script_paths_from_runtime_specs(script_runtime_specs)
    failures.extend(
        _contract_result_to_failure(result)
        for result in _check_skill_md_script_narrative_quality(content, script_paths)
        if not result.passed
    )
    return failures


async def _repair_skill_md_model_finalizer(
    *,
    previous_content: str,
    failures: list[dict[str, Any]],
    prompt_messages: list[dict[str, str]],
    model: str,
    skill_name: str,
    attempt: int,
) -> str:
    repair_messages = [*prompt_messages, {
        "role": "user",
        "content": (
            "上一次模型生成的 SKILL.md 未通过第一轮校验。只修 SKILL.md；不要修改 scripts、assets、SkillPlan 或 runtime specs。\n"
            "修复反馈只涉及 frontmatter、bash block、脚本自然语言说明、蓝图一致性、references/assets 说明、最终产物说明、Creator 流程泄露或内部合同泄露。\n"
            "不要要求 bash argv 与脚本字段完全一致；不要做 E2E 字段闭环；接口闭环由第二轮 E2E 处理。\n\n"
            "失败项（JSON）：\n"
            f"{json.dumps(failures, ensure_ascii=False, indent=2)}\n\n"
            "上一版 SKILL.md：\n"
            f"{previous_content[-16000:]}\n\n"
            "请返回修复后的完整 SKILL.md 正文，不要使用外层 Markdown fence。"
        ),
    }]
    return await _complete_creator_file_generation(
        messages=repair_messages,
        model=model,
        skill_name=skill_name,
        file_path="SKILL.md",
        prompt_variant="model_finalizer_repair",
        retry_index=attempt,
    )


@router.post("/finalize-skill-md")
async def finalize_skill_md(request: FinalizeSkillMdRequest):
    """Finalize SKILL.md with a model, using runtime specs only as bash-block references."""
    skill_name = _validate_skill_name(request.skill_name)
    route = route_creator_file_model(
        file_path="SKILL.md",
        purpose=request.description or "final SKILL.md",
        requested_model=request.model,
    )
    prompt_messages = _build_skill_md_model_finalizer_prompt(
        skill_name=skill_name,
        description=request.description or "",
        blueprint_text=request.blueprint_text or "",
        references=request.references,
        assets=request.assets,
        script_runtime_specs=request.script_runtime_specs,
        final_outputs=request.final_outputs,
    )
    failures: list[dict[str, Any]] = []
    candidate = ""
    content = ""
    for attempt in range(1, _MAX_FILE_REPAIR_ATTEMPTS + 1):
        try:
            if attempt == 1:
                candidate = await _complete_creator_file_generation(
                    messages=prompt_messages,
                    model=route.model,
                    skill_name=skill_name,
                    file_path="SKILL.md",
                    prompt_variant="model_finalizer",
                    retry_index=0,
                )
            content = _sanitize_generated_file_content("SKILL.md", candidate)
            failures = _skill_md_first_round_failures(
                skill_name=skill_name,
                content=content,
                blueprint_text=request.blueprint_text or "",
                script_runtime_specs=request.script_runtime_specs,
            )
            if not failures:
                try:
                    await _validate_skill_md_blueprint_alignment(
                        skill_name=skill_name,
                        content=content,
                        blueprint_text=request.blueprint_text or "",
                        skill_plan_entry=None,
                        model=request.model or route.model,
                    )
                except Exception as exc:
                    failures = _exception_to_skill_md_failures(exc, source="blueprint_alignment")
            if not failures:
                return {"success": True, "content": content, "repair_attempts": attempt - 1}
            if attempt >= _MAX_FILE_REPAIR_ATTEMPTS:
                break
            candidate = await _repair_skill_md_model_finalizer(
                previous_content=content,
                failures=failures,
                prompt_messages=prompt_messages,
                model=route.model,
                skill_name=skill_name,
                attempt=attempt,
            )
        except Exception as exc:
            failures = _exception_to_skill_md_failures(exc, source="skill_md_model_finalize")
            if attempt >= _MAX_FILE_REPAIR_ATTEMPTS:
                break
            candidate = await _repair_skill_md_model_finalizer(
                previous_content=content or candidate,
                failures=failures,
                prompt_messages=prompt_messages,
                model=route.model,
                skill_name=skill_name,
                attempt=attempt,
            )

    raise HTTPException(
        status_code=400,
        detail={
            "code": "skill_md_model_finalize_failed",
            "message": "SKILL.md model finalize 多轮修复后仍未通过第一轮校验。",
            "attempts": _MAX_FILE_REPAIR_ATTEMPTS,
            "failed_checks": failures,
        },
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

    if request.file_path.startswith("assets/") and (request.skill_plan_entry or {}).get("asset_source") != "bundled":
        raise HTTPException(
            status_code=400,
            detail=f"{request.file_path} 属于 assets 静态素材目录；只有 source=bundled 的预置静态资源可由 Creator 生成，source=user_upload 必须上传。",
        )
    script_entry = request.skill_plan_entry if isinstance(request.skill_plan_entry, dict) else {}
    artifact_contract = script_entry.get("artifact_contract") if isinstance(script_entry.get("artifact_contract"), dict) else {}
    has_script_outputs = bool(script_entry.get("outputs") or artifact_contract.get("stdout_fields"))
    if request.file_path.startswith("scripts/") and not (
        script_entry
        and script_entry.get("path") == request.file_path
        and script_entry.get("file_kind", script_entry.get("file_type")) in {"script", None}
        and has_script_outputs
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "contract_unresolved",
                "severity": "user_warning",
                "source": "generator",
                "path": request.file_path,
                "field": "skill_plan_entry",
                "message": f"{request.file_path} 缺少 normalized skill_plan_entry 或 outputs/artifact_contract.stdout_fields；已停止生成，避免退化为 payload->text 泛型脚本。",
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

            prompt_messages = _build_generate_file_prompt(
                request.file_path,
                skill_name,
                request.purpose,
                request.blueprint_text,
                request.conversation_history,
                role=request.role,
                skill_plan_entry=request.skill_plan_entry,
            )
            prompt_variant = "standard"
        except Exception as exc:
            logger.exception("Creator generate_file prepare failed: %s", exc)
            yield _sse({
                "error": f"生成前准备失败：{exc}",
                "done": True,
            })
            return

        candidate = ""
        repair_counts_by_layer: dict[str, int] = {}
        try:
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
            yield _sse({
                "error": f"模型调用失败：{exc}",
                "done": True,
            })
            return

        for attempt in range(1, _MAX_FILE_REPAIR_ATTEMPTS + 1):
            try:
                if len(candidate or "") == 0:
                    raise FileGenerationStageError(
                        source="model_empty_content",
                        layer="file_generation",
                        detail="model_empty_content: 模型生成结果 content_chars=0，跳过 validator/repair；进入 prompt 降级重试。",
                    )

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

                    if not content.strip():
                        raise FileGenerationStageError(
                            source="content_review",
                            layer="content_empty",
                            detail=f"{request.file_path} 生成内容为空。",
                        )

                    if request.file_path == "SKILL.md":
                        # First round: file-level SKILL.md contract only. Cross-file
                        # placeholder/dataflow closure is handled by validate_workflow_e2e().
                        _raise_file_contract_failures(validate_file_contract(
                            file_path=request.file_path,
                            content=content,
                            blueprint_text=request.blueprint_text,
                            skill_plan_entry=request.skill_plan_entry,
                        ))

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
                        _raise_file_contract_failures(validate_file_contract(
                            file_path=request.file_path,
                            content=content,
                            blueprint_text=request.blueprint_text,
                            skill_plan_entry={**(request.skill_plan_entry or {}), "purpose": request.purpose or request.blueprint_text},
                        ))

                    elif request.file_path.startswith("scripts/"):
                        _raise_file_contract_failures(_check_script_content_review_contract(
                            request.file_path,
                            content,
                            role=request.role,
                            skill_plan_entry=request.skill_plan_entry,
                        ))
                except Exception as exc:
                    raise _stage_error_from_exception("content_review", exc, default_layer="content_review") from exc

                runtime_spec: ScriptRuntimeSpec | None = None
                if request.file_path.startswith("scripts/"):
                    try:
                        runtime_spec = _trial_run_generated_script_with_plan(
                            skill_name,
                            request.file_path,
                            content,
                            role=request.role,
                            skill_plan_entry=request.skill_plan_entry,
                        )
                        if runtime_spec is not None:
                            entry = _skill_plan_entry_for_file(
                                file_path=request.file_path,
                                blueprint_text=(settings.skills_path / skill_name / "SKILL.md").read_text(encoding="utf-8")
                                if (settings.skills_path / skill_name / "SKILL.md").is_file()
                                else "",
                                role=request.role,
                                skill_plan_entry=request.skill_plan_entry,
                            )
                            try:
                                review = await _run_script_responsibility_review(
                                    file_path=request.file_path,
                                    script_content=content,
                                    skill_plan_entry=entry,
                                    runtime_spec=runtime_spec,
                                    stdout_json=runtime_spec.stdout_json,
                                    artifact_paths=runtime_spec.artifact_paths,
                                    deterministic_issues=[],
                                    requested_model=request.model or route.model,
                                )
                            except Exception as exc:
                                raise ScriptFunctionalValidationError([{
                                    "id": "script_functional.responsibility_review_unavailable",
                                    "failed_file": request.file_path,
                                    "failed_function": "model responsibility review",
                                    "code_region": "responsibility hard gate",
                                    "reason": f"职责验收模型不可用或返回异常：{type(exc).__name__}: {exc}",
                                    "minimal_edit": "稍后重试职责验收；不要把职责验收异常归类为脚本运行失败。",
                                    "allowed_scope": request.file_path,
                                    "forbidden_scope": "不得改 SKILL.md、其它脚本或 SkillPlan 来绕过职责验收。",
                                }], layer="responsibility") from exc
                            if not review.get("passed"):
                                issues = review.get("issues") if isinstance(review.get("issues"), list) else []
                                if not issues:
                                    issues = [{
                                        "id": "script_functional.responsibility_review",
                                        "failed_file": request.file_path,
                                        "failed_function": "model responsibility review",
                                        "code_region": "purpose/role/output/capability implementation",
                                        "reason": review.get("repair_instructions") or "职责验收模型判定当前脚本未完成自身职责。",
                                        "minimal_edit": "只根据职责验收模型意见局部修复当前脚本失败区域。",
                                        "allowed_scope": request.file_path,
                                        "forbidden_scope": "不得修改其它脚本、SKILL.md 或 SkillPlan。",
                                    }]
                                raise ScriptFunctionalValidationError(issues, layer="responsibility")
                    except ScriptFunctionalValidationError as exc:
                        raise _stage_error_for_script_functional(exc) from exc
                    except Exception as exc:
                        raise _stage_error_from_exception("script_smoke", exc, default_layer="script_smoke") from exc

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
                    "runtime_spec": _script_runtime_spec_to_dict(runtime_spec),
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
                stage_error = exc if isinstance(exc, FileGenerationStageError) else _stage_error_from_exception("content_review", exc, default_layer="content_review")
                deterministic_error = str(stage_error)
                error_source = stage_error.source
                error_layer = f"{stage_error.source}:{stage_error.layer}"
                repair_counts_by_layer[error_layer] = repair_counts_by_layer.get(error_layer, 0) + 1

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
                        yield _sse({
                            "type": "file_done",
                            "status": "error",
                            "success": False,
                            "file_path": request.file_path,
                            "role": request.role,
                            "error_type": "model_empty_content",
                            "error": "文件内容生成失败：same-model prompt degradation 已尝试 standard -> simplified -> minimal 后仍为空。",
                            "diagnostics": {
                                "model": route.model,
                                "prompt_variant": prompt_variant,
                                "retry_index": empty_retry_index,
                                "prompt_chars": _prompt_chars(prompt_messages),
                                "content_len": len(candidate or ""),
                                "message_roles": _message_role_counts(prompt_messages),
                                "system_chars": _message_role_chars(prompt_messages, "system"),
                                "user_chars": _message_role_chars(prompt_messages, "user"),
                                "finish_reason": "unknown",
                                "prompt_variants": list(_EMPTY_GENERATION_PROMPT_VARIANTS),
                            },
                            "done": True,
                        })
                        return
                    next_variant = _EMPTY_GENERATION_PROMPT_VARIANTS[empty_retry_index]
                    next_messages = (
                        _build_script_generate_file_prompt_variant(
                            file_path=request.file_path,
                            skill_name=skill_name,
                            purpose=request.purpose,
                            blueprint_text=request.blueprint_text,
                            role=request.role,
                            skill_plan_entry=request.skill_plan_entry,
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
                        "validation": {"status": "regenerating", "attempt": attempt, "source": error_source, "layer": stage_error.layer, "error": deterministic_error},
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

                layer_limit = _first_round_repair_limit(error_source)
                if repair_counts_by_layer[error_layer] > layer_limit:
                    yield _sse({
                        "type": "file_done",
                        "status": "error",
                        "success": False,
                        "file_path": request.file_path,
                        "role": request.role,
                        "error": f"文件内容生成失败：同一阶段/层 {error_layer} 已修复 {layer_limit} 次仍未通过。最后错误：{deterministic_error}",
                        "done": True,
                    })
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
                        "source": error_source,
                        "layer": stage_error.layer,
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
                    repair_mode=_repair_mode_for_first_round(
                        source=error_source,
                        file_path=request.file_path,
                        attempt=attempt,
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
                    repair_mode=_repair_mode_for_first_round(
                        source=error_source,
                        file_path=request.file_path,
                        attempt=attempt,
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

    if request.file_path.startswith("assets/") and (request.skill_plan_entry or {}).get("asset_source") != "bundled":
        raise HTTPException(
            status_code=400,
            detail=f"{request.file_path} 属于 assets 静态素材目录；只有 source=bundled 的预置静态资源可写入，source=user_upload 必须通过 /api/creator/upload-asset 上传。",
        )

    skill_dir = settings.skills_path / skill_name
    if not skill_dir.exists():
        raise HTTPException(status_code=404, detail=f"Skill 不存在：{skill_name}")

    runtime_spec: ScriptRuntimeSpec | None = None

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
            # First round: file-level SKILL.md contract only. Cross-file
            # placeholder/dataflow closure is handled by validate_workflow_e2e().
            _raise_file_contract_failures(validate_file_contract(
                file_path=request.file_path,
                content=content,
                blueprint_text=request.blueprint_text or "",
                skill_plan_entry=request.skill_plan_entry,
            ))

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
            _raise_file_contract_failures(validate_file_contract(
                file_path=request.file_path,
                content=content,
                blueprint_text=request.blueprint_text or "",
                skill_plan_entry=request.skill_plan_entry,
            ))

        elif request.file_path.startswith("scripts/"):
            _raise_file_contract_failures(_check_script_content_review_contract(
                request.file_path,
                content,
                role=request.role,
                skill_plan_entry=request.skill_plan_entry,
            ))
            _validate_script_against_existing_skill_contract(
                skill_name,
                request.file_path,
                content,
            )
            runtime_spec = _trial_run_generated_script_with_plan(
                skill_name,
                request.file_path,
                content,
                role=request.role,
                skill_plan_entry=request.skill_plan_entry,
            )
            if runtime_spec is not None:
                skill_md = (
                    (skill_dir / "SKILL.md").read_text(encoding="utf-8")
                    if (skill_dir / "SKILL.md").is_file()
                    else ""
                )
                entry = _skill_plan_entry_for_file(
                    file_path=request.file_path,
                    blueprint_text=skill_md,
                    role=request.role,
                    skill_plan_entry=request.skill_plan_entry,
                )
                try:
                    review = await _run_script_responsibility_review(
                        file_path=request.file_path,
                        script_content=content,
                        skill_plan_entry=entry,
                        runtime_spec=runtime_spec,
                        stdout_json=runtime_spec.stdout_json,
                        artifact_paths=runtime_spec.artifact_paths,
                        deterministic_issues=[],
                        requested_model=None,
                    )
                except Exception as exc:
                    raise ScriptFunctionalValidationError([{
                        "id": "script_functional.responsibility_review_unavailable",
                        "failed_file": request.file_path,
                        "failed_function": "model responsibility review",
                        "code_region": "responsibility hard gate",
                        "reason": f"职责验收模型不可用或返回异常：{type(exc).__name__}: {exc}",
                        "minimal_edit": "稍后重试职责验收；不要把职责验收异常归类为脚本运行失败。",
                        "allowed_scope": request.file_path,
                        "forbidden_scope": "不得改 SKILL.md、其它脚本或 SkillPlan 来绕过职责验收。",
                    }], layer="responsibility") from exc
                if not review.get("passed"):
                    issues = review.get("issues") if isinstance(review.get("issues"), list) else []
                    if not issues:
                        issues = [{
                            "id": "script_functional.responsibility_review",
                            "failed_file": request.file_path,
                            "failed_function": "model responsibility review",
                            "code_region": "purpose/role/output/capability implementation",
                            "reason": review.get("repair_instructions") or "职责验收模型判定当前脚本未完成自身职责。",
                            "minimal_edit": "只根据职责验收模型意见局部修复当前脚本失败区域。",
                            "allowed_scope": request.file_path,
                            "forbidden_scope": "不得修改其它脚本、SKILL.md 或 SkillPlan。",
                        }]
                    raise ScriptFunctionalValidationError(issues, layer="responsibility")

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
        runtime_spec=_script_runtime_spec_to_dict(runtime_spec) if request.file_path.startswith("scripts/") else None,
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
    artifact_paths: list[str] = field(default_factory=list)
    argv_shape: dict[str, str] = field(default_factory=dict)
    stdout_shape: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class E2EFailure:
    failed_step_index: int
    target_file: str
    target_region: str
    failed_command: str
    input_payload: dict[str, Any] = field(default_factory=dict)
    rendered_payload: dict[str, Any] = field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""
    return_code: int | None = None
    expected: str = ""
    actual: str = ""
    repair_instruction: str = ""
    layer: str = ""
    artifact_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "failed_step_index": self.failed_step_index,
            "target_file": self.target_file,
            "target_region": self.target_region,
            "failed_command": self.failed_command,
            "input_payload": self.input_payload,
            "rendered_payload": self.rendered_payload,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "return_code": self.return_code,
            "expected": self.expected,
            "actual": self.actual,
            "repair_instruction": self.repair_instruction,
            "layer": self.layer,
            "artifact_paths": self.artifact_paths,
        }


def _format_e2e_failure(failure: E2EFailure) -> str:
    return (
        f"E2E_REPAIR_TARGET={failure.target_file}\n"
        f"E2E_LAYER={failure.layer}\n"
        "E2E_STRUCTURED_FAILURE="
        + json.dumps(failure.to_dict(), ensure_ascii=False, sort_keys=True)
    )


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
    "markdown",
    "image_path",
    "image_paths",
    "pdf_path",
    "docx_path",
    "pptx_path",
    "html_path",
    "file_paths",
    "file_outputs",
}


def _e2e_trace_line(trace: E2EStepTrace) -> str:
    return (
        f"step={trace.ordinal} "
        f"script={trace.script_path} "
        f"placeholders={trace.placeholders} "
        f"argv_keys={trace.argv_keys} "
        f"stdout_keys={trace.stdout_keys} "
        f"new_keys={trace.new_keys} "
        f"artifact_paths={trace.artifact_paths} "
        f"argv_shape={json.dumps(trace.argv_shape, ensure_ascii=False, sort_keys=True)} "
        f"stdout_shape={json.dumps(trace.stdout_shape, ensure_ascii=False, sort_keys=True)}"
    )


def _format_e2e_trace(traces: list[E2EStepTrace]) -> str:
    if not traces:
        return "（暂无成功步骤）"
    return "\n".join(_e2e_trace_line(trace) for trace in traces)


def _terminal_output_expected_type(key: str) -> str:
    if key in {"text", "markdown", "image_path", "pdf_path", "docx_path", "pptx_path", "html_path"}:
        return "non-empty string"
    if key in {"image_paths", "file_paths", "file_outputs"}:
        return "non-empty list[string]"
    return "platform terminal field"


def _valid_terminal_output_value(key: str, value: Any) -> bool:
    if key in {"text", "markdown", "image_path", "pdf_path", "docx_path", "pptx_path", "html_path"}:
        return isinstance(value, str) and bool(value.strip())
    if key in {"image_paths", "file_paths", "file_outputs"}:
        return (
            isinstance(value, list)
            and bool(value)
            and all(isinstance(item, str) and item.strip() for item in value)
        )
    return False


def _invalid_terminal_output_values(payload: dict[str, Any]) -> list[dict[str, str]]:
    invalid: list[dict[str, str]] = []
    for key in sorted(_SANDBOX_TERMINAL_OUTPUT_KEYS):
        if key not in payload:
            continue
        value = payload.get(key)
        if _valid_terminal_output_value(key, value):
            continue
        invalid.append({
            "key": key,
            "expected_type": _terminal_output_expected_type(key),
            "actual_type": _json_shape(value),
        })
    return invalid


def _has_sandbox_terminal_output(payload: dict[str, Any]) -> bool:
    """Return whether final stdout JSON is consumable by sandbox runtime.

    这里校验的是平台与 Skill 交互的最终输出协议，不校验中间步骤。
    """
    return any(
        _valid_terminal_output_value(key, payload.get(key))
        for key in _SANDBOX_TERMINAL_OUTPUT_KEYS
        if key in payload
    )

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

    invalid_terminal_values = _invalid_terminal_output_values(stdout_json)
    if invalid_terminal_values:
        details = {
            "stdout_shape": _json_object_shape(stdout_json),
            "invalid_terminal_keys": [item["key"] for item in invalid_terminal_values],
            "invalid_terminal_values": invalid_terminal_values,
        }
        raise ValueError(
            _e2e_error(
                target=command.script_path,
                layer="final_platform_output_value_invalid",
                message=(
                    f"第 {command.ordinal} 步 {command.script_path} 是 workflow 最后一步，"
                    "stdout JSON 包含 sandbox 平台字段，但字段值类型/内容不合法。\n"
                    f"stdout_shape={json.dumps(details['stdout_shape'], ensure_ascii=False, sort_keys=True)}\n"
                    f"invalid_terminal_keys={json.dumps(details['invalid_terminal_keys'], ensure_ascii=False)}\n"
                    f"invalid_terminal_values={json.dumps(invalid_terminal_values, ensure_ascii=False, sort_keys=True)}\n"
                    "每个 invalid_terminal_values 项均包含 expected_type 和 actual_type。\n\n"
                    "已成功执行的前序边界 trace：\n"
                    f"{_format_e2e_trace(traces)}"
                ),
            )
        )

    raise ValueError(
        _e2e_error(
            target=command.script_path,
            layer="final_platform_output_contract",
            message=(
                f"第 {command.ordinal} 步 {command.script_path} 是 workflow 最后一步，"
                "但 stdout JSON 没有包含 sandbox 可消费的最终输出字段。\n"
                f"当前 stdout 字段：{sorted(stdout_json.keys())}\n"
                f"stdout_shape={json.dumps(_json_object_shape(stdout_json), ensure_ascii=False, sort_keys=True)}\n"
                f"平台允许的最终输出字段：{sorted(_SANDBOX_TERMINAL_OUTPUT_KEYS)}\n\n"
                "注意：中间步骤可以使用任意内部字段名，不需要对齐平台协议；"
                "但最后一步必须输出平台字段，例如 text、markdown、image_paths、"
                "pdf_path、docx_path、pptx_path、html_path、file_paths 或 file_outputs。\n\n"
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

def _looks_like_directory_tree_block(text: str) -> bool:
    """Heuristically detect directory-tree/documentation blocks.

    These blocks are often rendered as plain Markdown fences and may contain
    scripts/ paths, but they are not executable workflow commands.
    """
    text = text or ""
    if any(marker in text for marker in ("├──", "└──", "│", "─")):
        return True

    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False

    # Typical tree blocks contain multiple directory/file-looking lines and no
    # shell runner at the beginning.
    first = lines[0].strip()
    first_word = first.split(maxsplit=1)[0] if first.split() else ""
    if first_word in {"python", "python3", "node", "bash", "sh"}:
        return False

    treeish_count = 0
    for line in lines:
        stripped = line.strip()
        if stripped.endswith("/") or stripped.startswith(("scripts/", "references/", "assets/")):
            treeish_count += 1
        elif re.match(r"^[A-Za-z0-9_.-]+\.(py|md|json|yaml|yml|txt|pdf|docx|pptx)$", stripped):
            treeish_count += 1

    return len(lines) >= 2 and treeish_count >= 2


def _is_valid_e2e_script_path(script_path: str) -> bool:
    """Return whether a token is a concrete executable script path.

    E2E must not accept directories such as scripts/ as workflow steps.
    """
    normalized = (script_path or "").replace("\\", "/").strip()
    if not normalized.startswith("scripts/"):
        return False
    if normalized.endswith("/"):
        return False

    path = Path(normalized)
    if not path.name or path.name in {".", ".."}:
        return False
    if not path.suffix:
        return False

    return path.suffix.lower() in {
        ".py",
    }

def _parse_e2e_workflow_command(
    *,
    command: str,
    ordinal: int,
    source_path: str,
) -> E2EWorkflowCommand | None:
    """Parse one SKILL.md shell command into executable E2E workflow step.

    Strict rule:
    - The fenced block must contain exactly one effective shell command.
    - The command must invoke a concrete scripts/<file> path, not scripts/.
    - The script path must be followed by exactly one JSON object argv.
    - Directory-tree/documentation blocks are ignored.
    """
    raw_command = (command or "").strip()
    if not raw_command:
        return None

    if _looks_like_directory_tree_block(raw_command):
        return None

    effective_lines: list[str] = []
    for line in raw_command.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        effective_lines.append(stripped)

    if not effective_lines:
        return None

    if len(effective_lines) != 1:
        raise ValueError(
            _e2e_error(
                target=source_path,
                layer="command_block_multiple",
                message=(
                    f"{source_path} 第 {ordinal} 个可执行 fenced block 中包含多条有效命令。\n"
                    "二次 E2E 要求每个 bash/sh/shell block 只包含一条脚本调用命令。\n"
                    f"原始块：{raw_command}"
                ),
            )
        )

    command = effective_lines[0]

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

    if not parts:
        return None

    runner = Path(parts[0]).name
    if runner not in {"python", "python3"}:
        # Creator workflow E2E accepts one command protocol only.
        return None

    script_idx: int | None = None
    script_path = ""

    for idx, part in enumerate(parts[1:], start=1):
        normalized = part.replace("\\", "/")

        candidate = ""
        if normalized.startswith("scripts/"):
            candidate = normalized

        if not candidate:
            continue

        if not _is_valid_e2e_script_path(candidate):
            # Example: scripts/ directory in a tree/list. Not a workflow step.
            return None

        script_idx = idx
        script_path = candidate
        break

    if script_idx is None:
        return None

    if script_idx + 1 >= len(parts):
        raise ValueError(
            _e2e_error(
                target=source_path,
                layer="command_argv_missing",
                message=(
                    f"{source_path} 第 {ordinal} 步 {script_path} 缺少 JSON argv。\n"
                    f"命令必须形如：python {script_path} '{{\"payload\":{{\"user_request\":\"{{{{user_request}}}}\"}}}}'\n"
                    f"原始命令：{command}"
                ),
            )
        )

    if script_idx != 1:
        raise ValueError(
            _e2e_error(
                target=source_path,
                layer="command_protocol",
                message=(
                    f"{source_path} 第 {ordinal} 步必须直接调用 scripts/*.py：python {script_path} '<JSON object>'。\n"
                    f"原始命令：{command}"
                ),
            )
        )

    if script_idx + 2 < len(parts):
        raise ValueError(
            _e2e_error(
                target=source_path,
                layer="command_argv_extra",
                message=(
                    f"{source_path} 第 {ordinal} 步 {script_path} 的 JSON argv 后存在额外参数：{parts[script_idx + 2:]!r}。\n"
                    "二次 E2E 校验要求脚本路径后只跟一个 JSON object argv。\n"
                    f"原始命令：{command}"
                ),
            )
        )

    try:
        argv_template = json.loads(parts[script_idx + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(
            _e2e_error(
                target=source_path,
                layer="command_json_parse",
                message=(
                    f"{source_path} 第 {ordinal} 步 {script_path} 的 JSON argv 不可解析：{exc.msg}\n"
                    f"argv={parts[script_idx + 1]!r}\n"
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
                layer="external_input_missing" if command.ordinal == 1 else "e2e_dataflow_missing",
                message=(
                    ("平台外部输入缺失，第一条命令不能引用无确定来源字段。" if command.ordinal == 1 else "Skill 内部 dataflow 缺失，后续命令只能引用已有 context 或前序 stdout 字段。")
                    + "\n"
                    + f"第 {command.ordinal} 步 {command.script_path} 的命令模板引用了当前 payload 中不存在的字段："
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


def _seed_initial_e2e_payload(
    commands: list[E2EWorkflowCommand],
    *,
    external_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Seed Creator E2E with the same generic external input envelope as runtime.

    The platform never invents business fields for the first command.  Free-form
    text is available only through user_request/input/text; structured values are
    available only when supplied through JSON/key-value fields, explicit fields,
    input files, defaults, or options.
    """
    return dict(external_context or build_creator_external_input_context(messages=[]))


def _e2e_error(*, target: str, layer: str, message: str) -> str:
    failure = {
        "failed_step_index": 0,
        "target_file": target,
        "target_region": "frontmatter" if "frontmatter" in layer else ("workflow block" if target == "SKILL.md" else "run()"),
        "failed_command": "",
        "input_payload": {},
        "stdout": "",
        "stderr": message,
        "return_code": None,
        "expected": "Creator E2E step must be executable and produce valid JSON/artifacts.",
        "actual": message,
        "repair_instruction": f"只修改 {target} 中与 {layer} 失败相关的最小区域，不修改其它文件。",
        "layer": layer,
    }
    return f"E2E_REPAIR_TARGET={target}\nE2E_LAYER={layer}\nE2E_STRUCTURED_FAILURE={json.dumps(failure, ensure_ascii=False, sort_keys=True)}\n{message}"


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
    trial_skill_md: str,
    content: str,
    entry: SkillPlanEntry,
    rendered_payload: dict[str, Any],
) -> dict[str, Any]:
    if proc.returncode != 0:
        raise ValueError(_format_e2e_failure(E2EFailure(
            failed_step_index=command.ordinal,
            target_file=command.script_path,
            target_region="run()",
            failed_command=command.raw_command,
            input_payload=rendered_payload,
            rendered_payload=rendered_payload,
            stdout=(proc.stdout or "")[-4000:],
            stderr=(proc.stderr or "")[-4000:],
            return_code=proc.returncode,
            expected="脚本必须成功退出、stdout 输出合法 JSON object，并真实完成该步骤职责。",
            actual=f"return_code={proc.returncode}",
            repair_instruction=f"只修改 {command.script_path} 中 run()/main 执行失败相关区域，不修改其它文件或已通过步骤。",
            layer="script_exit",
        )))

    try:
        refined_contract, _resolution = _contract_resolution_for_trial(command.script_path, trial_skill_md, entry.role, entry.__dict__)
        _validate_trial_stdout_json(
            stdout=proc.stdout,
            content=content,
            args=[json.dumps(rendered_payload, ensure_ascii=False)],
            role=entry.role,
            skill_dir=trial_skill_dir,
            skill_plan_entry=entry.__dict__,
            canonical_contract=refined_contract,
        )
    except ValueError as exc:
        raise ValueError(_format_e2e_failure(E2EFailure(
            failed_step_index=command.ordinal,
            target_file=command.script_path,
            target_region="stdout output logic",
            failed_command=command.raw_command,
            input_payload=rendered_payload,
            rendered_payload=rendered_payload,
            stdout=(proc.stdout or "")[-4000:],
            stderr=(proc.stderr or "")[-4000:],
            return_code=proc.returncode,
            expected="stdout 必须是合法 JSON object，required outputs 存在，文件产物真实存在。",
            actual=f"stdout_contract_error={exc}",
            repair_instruction=f"只修改 {command.script_path} 的 stdout/artifact 输出逻辑，不修改其它文件。",
            layer="stdout_contract",
        ))) from exc

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


def _validate_e2e_script_static_preflight(*, file_path: str, content: str, skill_md: str) -> None:
    """E2E preflight for local safety/entry/JSON argv only.

    This intentionally does not enforce helper_preferred implementation choices
    or SkillPlan input key exactness. The real workflow run validates rendered
    argv, stdout context propagation, and final artifacts.
    """
    entry = _skill_plan_entry_for_file(file_path=file_path, blueprint_text=skill_md)

    if entry.language == "python":
        try:
            ast.parse(content)
        except SyntaxError as exc:
            raise ValueError(f"{file_path} 不是合法 Python 源码: {exc.msg}") from exc

    if not _script_has_main_entry(content, entry.runtime):
        raise ValueError(f"{file_path} 缺少 runtime={entry.runtime} 的入口或 stdout 输出。")

    commands = _extract_script_command_templates(skill_md, file_path)
    json_argv_commands = [command for command in commands if _command_uses_json_argv(command)]
    if json_argv_commands and not _script_reads_json_argv(content, entry.runtime):
        raise ValueError(
            f"{file_path} SKILL.md 命令传入 JSON argv，但脚本未按 runtime 读取 JSON argv（例如 Python json.loads(sys.argv[1])）。"
        )

    helper_required_capabilities = [
        capability
        for capability in _effective_required_capabilities_for_script(entry)
        if (get_tool_capability(capability) and get_tool_capability(capability).usage_policy == "helper_required")
    ]
    missing_required_helpers = _script_required_capability_failures(content, helper_required_capabilities)
    if missing_required_helpers:
        raise ValueError(
            "脚本没有调用这些 helper_required 能力对应接口："
            + ", ".join(missing_required_helpers)
        )


def _validate_e2e_command_static(
    *,
    command: E2EWorkflowCommand,
    trial_skill_dir: Path,
    skill_md: str,
    available_payload_keys: set[str] | None = None,
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


    content = source_path.read_text(encoding="utf-8")
    try:
        _validate_e2e_script_static_preflight(
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


def _run_skill_workflow_e2e_once(skill_name: str, *, external_context: dict[str, Any] | None = None) -> list[str]:
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

        payload: dict[str, Any] = _seed_initial_e2e_payload(commands, external_context=external_context)
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
                        refined_contract, resolution = _contract_resolution_for_trial(command.script_path, trial_skill_md, None, None)
                        _install_declared_dependency_packages(
                            venv_python,
                            list(refined_contract.declared_dependencies or []) + list(resolution.declared_dependencies or []),
                            source_label="implementation_resolution",
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
                    available_payload_keys=set(payload.keys()),
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
                    trial_skill_md=trial_skill_md,
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
                artifact_paths = _stdout_artifact_paths(stdout_json, entry, None)
                if artifact_paths:
                    payload.setdefault("_artifacts", [])
                    if isinstance(payload["_artifacts"], list):
                        payload["_artifacts"].extend(artifact_paths)
                    payload["_last_artifacts"] = artifact_paths
                new_keys = sorted(set(payload.keys()) - before_keys)

                trace = E2EStepTrace(
                    ordinal=command.ordinal,
                    script_path=command.script_path,
                    raw_command=command.raw_command,
                    placeholders=sorted(_e2e_command_placeholders(command)),
                    argv_keys=sorted(str(key) for key in rendered_payload.keys()),
                    stdout_keys=sorted(str(key) for key in stdout_json.keys()),
                    new_keys=new_keys,
                    artifact_paths=artifact_paths,
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
    """Deprecated no-op.

    Command payload repair must be based on real E2E traces (argv_shape,
    stdout_shape, missing placeholders, and script parser behavior), not by
    rewriting JSON argv to match SkillPlan.inputs. Keep this function as a
    compatibility shim for older callers/tests, but never mutate content.
    """
    return content, []


def _structured_failure_from_errors(errors: list[str]) -> dict[str, Any]:
    for error in errors or []:
        match = re.search(r"E2E_STRUCTURED_FAILURE=(\{.*?\})(?:\n|$)", error, re.S)
        if not match:
            continue
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return {}

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
    structured_failure = _structured_failure_from_errors(e2e_errors)
    target_region = str(structured_failure.get("target_region") or ("workflow block" if target_path == "SKILL.md" else "run()"))
    targeted_e2e_hint = _targeted_e2e_repair_hint(e2e_errors)

    e2e_tool_cards = ""
    if target_path.startswith("scripts/"):
        e2e_entry = _skill_plan_entry_for_file(file_path=target_path, blueprint_text=skill_md)
        e2e_tool_cards = _creator_tool_context_for_script(
            file_path=target_path,
            skill_plan_entry=e2e_entry,
            blueprint_text=skill_md,
            failure_layer=_failure_layer_from_error_text(deterministic_error),
            error_text=deterministic_error,
            include_snippets=True,
        )




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
            "text、markdown、image_path、image_paths、"
            "pdf_path、docx_path、pptx_path、html_path、file_paths 或 file_outputs。"
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
                "你只能修改 E2E_STRUCTURED_FAILURE.target_file 指向的文件，且只能修改 target_region 指定区域。"
                "不要重写整个 Skill，不要重写整个 SKILL.md，不要修改已经通过的脚本/frontmatter/stdout 字段名。"
                "不要用固定样例数据代替动态输入，不要通过 try/except 返回假成功。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"Skill 名称：{skill_name}\n"
                f"当前需要修复的文件：{target_path}\n"
                f"只能修改区域：{target_region}\n\n"
                f"结构化失败对象：\n{json.dumps(structured_failure, ensure_ascii=False, sort_keys=True)}\n\n"
                f"{target_rule}\n\n"
                f"定向 E2E 修复提示：{targeted_e2e_hint or '无'}\n\n"
                "端到端失败信息：\n"
                f"{deterministic_error}\n\n"
                "当前 SKILL.md：\n"
                f"{skill_md[-12000:]}\n\n"
                "其它相关文件摘要：\n"
                f"{''.join(all_file_summaries)[-20000:]}\n\n"
                f"{e2e_tool_cards}\n\n"
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
        _validate_e2e_script_static_preflight(
            file_path=target_path,
            content=sanitized,
            skill_md=skill_md,
        )

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

    Strict Creator/E2E rule:
    - Only explicitly marked shell fences are executable candidates.
    - Plain ``` fenced blocks are documentation blocks, not executable blocks.
    """
    normalized = (info or "").strip().lower()
    if not normalized:
        return False

    first = normalized.split()[0]
    return first in {"bash", "sh", "shell", "zsh"}

def _validate_skill_package_smoke(skill_name: str, *, mode: str = "trial", external_context: dict[str, Any] | None = None) -> list[str]:
    """Strict end-to-end workflow validation.

    This replaces the old per-script smoke test with a real SKILL.md workflow run:
    upstream stdout JSON is parsed and merged into payload, then used to render
    downstream JSON argv placeholders.
    """
    return validate_workflow_e2e(skill_name, external_context=external_context)

def _external_context_from_skill_action_request(request: SkillActionRequest) -> dict[str, Any]:
    return build_creator_external_input_context(
        messages=request.messages,
        input_files=request.input_files,
        fields=request.fields,
        options=request.options,
    )


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
        external_context = _external_context_from_skill_action_request(request)
        try:
            e2e_errors = validate_workflow_e2e(skill_name, external_context=external_context)
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
        external_context = _external_context_from_skill_action_request(request)
        e2e_errors = _validate_skill_package_smoke(skill_name, mode="trial", external_context=external_context)
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
