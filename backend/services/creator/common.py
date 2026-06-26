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
import hashlib
import difflib
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
from pydantic import BaseModel, Field, field_validator

from ...config import settings
from ..blueprint_parser import BlueprintPlan, BlueprintShapeError, clean_blueprint_body_text, parse_blueprint
from ..skill_plan import SkillPlanEntry, ScriptRuntimeSpec, build_skill_plan_entry, capabilities_for_role, command_template_for_entry, default_io_for_file_kind, file_role_classifier, file_type_for_path, file_kind_for_path, language_for_path, runtime_for_language, normalize_required_capabilities, is_runtime_artifact_semantic, command_payload_placeholders, render_script_command_from_skill_plan
from ..creator_tool_registry import get_tool_capability, list_tool_capabilities, tool_status, resolve_tools_for_skill_plan_entry, function_cards_for_tool, resolve_tool_snippets_for_context, tool_snippet_prompt
from ..llm_proxy import complete_chat_once, stream_chat
from ..model_router import VALIDATOR_TASK, route_creator_file_model, route_model
from ..skill_executor import _build_script_runtime_env, run_action
from ..skill_creator_dry_run import build_creator_external_input_context
from ..artifact_validator import validate_stdout_file_outputs, FileOutputValidationError
from ..markdown_metadata import (
    parse_frontmatter,
    validate_skill_frontmatter,
    validate_reference_frontmatter,
    canonicalize_skill_frontmatter,
    canonicalize_reference_frontmatter,
    apply_frontmatter_patch,
)
from ..creator_contracts import (
    compile_canonical_file_contract,
    contract_payload,
    refine_contract_with_resolution,
    resolve_implementation,
    call_template_for_tool,
    validate_script_functional_evidence,
)
from ...routers.chat_utils import _get_skill_venv_python

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
- 如果确实需要运行 scripts/ 下的脚本，必须使用标准 Markdown fenced code block，且 info string 必须是 bash。
- 每个 ```bash block 内只能有一条真实 shell 命令。
- 脚本命令必须直接调用 scripts/ 下的真实脚本，例如 `python scripts/example.py ...`。
- 命令参数形态必须由脚本真实接口决定：如果脚本读取 JSON argv，则传入一个 json.loads 可解析的 JSON object 字符串；如果脚本使用 argparse，则使用对应 flags；如果脚本无需参数，可以不传参数。
- 不得固定套用 payload/user_request/fields/options/input_files 等模板字段。
- 禁止在 ```bash block 内直接写 JSON 配置对象、runner/script/argv 伪命令对象、说明文字、列表、多条命令或 `<真实参数>` 这类占位说明。
- 机器可读 JSON 示例、配置、stdout 示例如果需要展示，必须使用 ```json fenced code block，不得伪装成 ```bash。
- 命令示例必须与脚本真实接口一致：脚本读 JSON argv 时，示例就传 JSON；脚本读 stdin 时，正文就说明 stdin 内容。禁止让运行时主模型根据脚本名临时猜 CLI flags。
- 参数映射用普通 Markdown 列表说明通用来源：命令示例应从用户输入、显式字段、默认值、上传文件、前序 stdout 中选择当前脚本真正需要的值。第一轮 SKILL.md 只约束可解析命令形态，不要求证明后续 placeholder 来自前序 stdout。
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

    # True：用于 Phase2 展示前，执行蓝图合同多轮检查与 patch 修复。
    # False：用于用户确认后，只 parse 已确认蓝图拿文件清单，不再修蓝图。
    refine_contract: bool = True

    # Phase2 展示前最多修几轮。确认后 refine_contract=False 时不会使用。
    refine_rounds: int = 3

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

class RequirementGraphValidationError(ValueError):
    """Requirement graph parser/schema failure that must not be treated as a business-file error."""

    def __init__(self, message: str, *, code: str = "validator_error", details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


class RequirementConstraint(BaseModel):
    name: str = ""
    kind: str = "constraint"
    value: Any = None
    comparator: str = "equals"
    unit: str = ""
    source: str = "blueprint"
    required: bool = True
    evidence_policy: dict[str, Any] = Field(default_factory=dict)


class RequirementItem(BaseModel):
    id: str
    target_file: str
    owner_step: Optional[str] = None
    kind: str = "component"
    required: bool = True
    source: str = "blueprint"
    description: str = ""
    semantic_inputs: list[str] = Field(default_factory=list)
    semantic_outputs: list[str] = Field(default_factory=list)
    required_components: list[str] = Field(default_factory=list)
    constraints: list[RequirementConstraint] = Field(default_factory=list)
    evidence_policy: dict[str, Any] = Field(default_factory=dict)
    non_requirements: list[str] = Field(default_factory=list)

    @field_validator("constraints", mode="before")
    @classmethod
    def _coerce_constraints(cls, value: Any) -> list[Any]:
        if value in (None, ""):
            return []
        raw_items = value if isinstance(value, list) else [value]
        items: list[Any] = []
        for raw in raw_items:
            if isinstance(raw, RequirementConstraint):
                items.append(raw)
            elif isinstance(raw, dict):
                items.append(raw)
            else:
                text = str(raw or "").strip()
                if text:
                    items.append({
                        "name": text,
                        "kind": "constraint",
                        "value": text,
                        "comparator": "describes",
                        "source": "blueprint",
                        "required": True,
                    })
        return items


class RequirementGraph(BaseModel):
    requirements: list[RequirementItem] = Field(default_factory=list)
    requirement_graph_source: str = "validator"
    requirement_graph_quality: str = "full"


def _requirement_id_for_file(path: str, suffix: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9]+", "_", str(path or "file")).strip("_").lower() or "file"
    tail = re.sub(r"[^a-zA-Z0-9]+", "_", str(suffix or "requirement")).strip("_").lower() or "requirement"
    return f"req_{base}_{tail}"[:120]


def _file_spec_has_substantive_responsibility(file_spec: Any) -> bool:
    return bool(
        getattr(file_spec, "outputs", None)
        or getattr(file_spec, "artifact_contract", None)
        or getattr(file_spec, "required_capabilities", None)
        or getattr(file_spec, "runtime_contract", None)
        or str(getattr(file_spec, "purpose", "") or "").strip()
    )


def build_default_requirement_graph(files: list[Any]) -> RequirementGraph:
    items: list[RequirementItem] = []
    for file_spec in files or []:
        path = str(getattr(file_spec, "path", "") or "")
        if not path or path == "SKILL.md" or path.startswith("assets/"):
            continue
        required = bool(getattr(file_spec, "required", True))
        purpose = str(getattr(file_spec, "purpose", "") or "").strip()
        inputs = [str(x).strip() for x in (getattr(file_spec, "inputs", []) or []) if str(x).strip()]
        outputs = [str(x).strip() for x in (getattr(file_spec, "outputs", []) or []) if str(x).strip()]
        components = [purpose] if purpose else []
        constraints: list[str] = []
        artifact_contract = getattr(file_spec, "artifact_contract", None) or {}
        runtime_contract = getattr(file_spec, "runtime_contract", None) or {}
        for source_obj in (artifact_contract, runtime_contract):
            if isinstance(source_obj, dict):
                for key, value in source_obj.items():
                    if value not in (None, "", [], {}):
                        constraints.append(RequirementConstraint(
                            name=str(key),
                            kind="contract",
                            value=value,
                            comparator="declared",
                            source="default_contract",
                            required=True,
                            evidence_policy={"first_round": "look for semantic use in options/styles/config/parameters/builders", "e2e": "look for structured runtime metadata evidence"},
                        ))
        if purpose or outputs or components or constraints:
            items.append(RequirementItem(
                id=_requirement_id_for_file(path, "core"),
                target_file=path,
                owner_step=path,
                kind="component",
                required=required,
                source="inferred",
                description=purpose or f"Implement the declared responsibility for {path}.",
                semantic_inputs=inputs,
                semantic_outputs=outputs,
                required_components=components,
                constraints=constraints,
                evidence_policy={
                    "first_round": "Review semantic responsibility evidence in the target file without hard-gating field, variable, function, or tool names.",
                    "e2e": "Verify required semantic inputs are mapped, received, consumed, and reflected in stdout/runtime metadata when applicable.",
                    "graph_quality": "fallback_coarse",
                },
                non_requirements=[
                    "exact field names", "exact variable names", "fixed function names",
                    "fixed helper/tool invocation style", "subjective quality wording",
                ],
            ))
    return RequirementGraph(
        requirements=items,
        requirement_graph_source="fallback",
        requirement_graph_quality="fallback_coarse",
    )


def parse_requirement_graph_result(text: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(text, dict):
        return text
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S).strip()
    try:
        data = json.loads(raw)
    except Exception as exc:
        raise RequirementGraphValidationError("Requirement graph validator did not return valid JSON.", code="validator_error", details={"error": str(exc), "raw": raw[:1000]}) from exc
    if not isinstance(data, dict):
        raise RequirementGraphValidationError("Requirement graph JSON must be an object.", code="validator_incomplete", details={"type": type(data).__name__})
    return data


def normalize_requirement_graph(data: dict[str, Any] | RequirementGraph) -> RequirementGraph:
    if isinstance(data, RequirementGraph):
        return data
    raw_items = data.get("requirements", data.get("items", [])) if isinstance(data, dict) else []
    if not isinstance(raw_items, list):
        raise RequirementGraphValidationError("Requirement graph requirements must be a list.", code="validator_incomplete")
    items: list[RequirementItem] = []
    for idx, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise RequirementGraphValidationError("Requirement item must be an object.", code="validator_incomplete", details={"index": idx})
        merged = dict(raw)
        merged.setdefault("id", f"req_{idx+1}")
        merged.setdefault("kind", "component")
        merged.setdefault("required", True)
        merged.setdefault("source", "blueprint")
        for key in ("semantic_inputs", "semantic_outputs", "required_components", "non_requirements"):
            value = merged.get(key, [])
            if isinstance(value, str):
                value = [value]
            merged[key] = [str(v).strip() for v in (value or []) if str(v).strip()] if isinstance(value, list) else []
        if not isinstance(merged.get("constraints"), list):
            merged["constraints"] = [merged.get("constraints")] if merged.get("constraints") else []
        if not isinstance(merged.get("evidence_policy"), dict):
            merged["evidence_policy"] = {}
        try:
            item = RequirementItem(**merged)
        except Exception as exc:
            raise RequirementGraphValidationError("Requirement item schema is incomplete.", code="validator_incomplete", details={"index": idx, "error": str(exc)}) from exc
        if not item.id.strip() or not item.target_file.strip() or not item.description.strip():
            raise RequirementGraphValidationError("Requirement item misses id, target_file, or description.", code="validator_incomplete", details={"index": idx, "item": merged})
        items.append(item)
    source = str(data.get("requirement_graph_source") or data.get("source") or "validator") if isinstance(data, dict) else "validator"
    quality = str(data.get("requirement_graph_quality") or data.get("quality") or "full") if isinstance(data, dict) else "full"
    return RequirementGraph(requirements=items, requirement_graph_source=source, requirement_graph_quality=quality)


def validate_requirement_graph_schema(graph: RequirementGraph, files: list[Any]) -> RequirementGraph:
    required_by_file: dict[str, list[RequirementItem]] = {}
    for item in graph.requirements:
        if item.required:
            required_by_file.setdefault(item.target_file, []).append(item)
    for file_spec in files or []:
        path = str(getattr(file_spec, "path", "") or "")
        if not path.startswith("scripts/"):
            continue
        if _file_spec_has_substantive_responsibility(file_spec) and not required_by_file.get(path):
            raise RequirementGraphValidationError(
                f"Required script {path} has substantive responsibilities but no required requirement.",
                code="validator_incomplete",
                details={"path": path},
            )
    return graph


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
    requirements: list[RequirementItem] = Field(default_factory=list)




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
    requirement_graph: RequirementGraph = Field(default_factory=RequirementGraph)

    # 这是展示给用户确认的最终蓝图文本。
    # 注意：前端应该展示这个字段，而不是展示 LLM 第一次生成的原始蓝图。
    blueprint_text: str = ""

    # true 表示后台在展示前对蓝图做过合同修正。
    blueprint_refined: bool = False


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


class FinalizeSkillMdRequest(BaseModel):
    skill_name: str
    description: str = ""
    blueprint_text: str = ""
    model: Optional[str] = None
    references: list[str] = Field(default_factory=list)
    assets: list[str] = Field(default_factory=list)
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
    max_e2e_repair_attempts: int = 8
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
    repair_events: list[dict[str, Any]] = Field(default_factory=list)


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



@dataclass(frozen=True)
class E2EWorkflowCommand:
    ordinal: int
    source_path: str
    script_path: str
    raw_command: str
    runner: str
    argv_template: dict[str, Any]

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



@dataclass
class FileGenerationStageError(Exception):
    """Structured failure source for first-round generation validation."""

    source: str
    layer: str
    detail: str
    original: Exception | None = None

    def __str__(self) -> str:
        return self.detail

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
        from ..creator_tool_registry import tool_layer_prompt_for_context
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




def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"



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


def _script_stdout_schema_for_entry(plan_entry: SkillPlanEntry) -> dict[str, Any]:
    """Build a deterministic stdout schema from the per-file output contract.

    注意：
    - 只有 SkillPlanEntry.outputs 明确声明的字段才作为 required；
    - outputs 为空时，不要伪造 required=["text"]；
    - stdout 至少一个非空字段由 _validate_trial_stdout_json 的通用规则保证。
    """
    properties = {
        key: {"description": f"Non-empty value for declared output field {key}."}
        for key in (plan_entry.outputs or [])
        if isinstance(key, str) and key.strip()
    }

    return {
        "type": "object",
        "required": list(properties.keys()),
        "properties": properties,
        "additionalProperties": True,
        "forbidden": ["error"],
    }



def _numbered_source(content: str) -> str:
    return "\n".join(f"{idx:04d}: {line}" for idx, line in enumerate((content or "").splitlines(), start=1))



def _complete_chat_once_sync_for_e2e(messages: list[dict[str, str]], model: str) -> str:
    """Run async complete_chat_once from synchronous E2E code.

    validate_workflow_e2e / _run_skill_workflow_e2e_once 是同步链路。
    如果当前线程没有 event loop，直接 asyncio.run；
    如果当前线程已经在 event loop 中，则开一个短生命周期线程执行 asyncio.run，
    避免 RuntimeError: asyncio.run() cannot be called from a running event loop。
    """

    import asyncio
    import concurrent.futures

    async def _call() -> str:
        return await complete_chat_once(messages, model)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_call())

    def _runner() -> str:
        return asyncio.run(_call())

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_runner)
        return future.result()


__all__ = [name for name in globals() if not name.startswith("__")]


def coerce_requirement_items(requirements: Any) -> list[RequirementItem]:
    """Best-effort requirement coercion shared by validator layers."""
    items: list[RequirementItem] = []
    for raw in requirements or []:
        try:
            if isinstance(raw, RequirementItem):
                items.append(raw)
            elif isinstance(raw, dict):
                items.append(RequirementItem(**raw))
        except Exception:
            continue
    return items

_coerce_requirement_items = coerce_requirement_items
try:
    __all__.extend(["coerce_requirement_items", "_coerce_requirement_items"])
except Exception:
    pass
