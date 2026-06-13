"""Creator tool capability registry.

This module is the single source of truth for Creator-facing tool metadata
and default role mappings.  The sandbox/runtime execution path is kept separate:
this registry describes what Creator may plan, prompt and expose through
management APIs.  Runtime helpers and deep source validators are intentionally
implemented in follow-up modules; tool status reports whether registered helper
names are exported by ``backend.services.skill_runtime``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields as dataclasses_fields, replace
from datetime import datetime, timezone
import asyncio
import ast
import importlib
import importlib.util
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Literal


UsagePolicy = Literal["helper_required", "helper_preferred", "self_implementation_allowed"]



_TOOL_AUTHORING_CONFIG_STORE: dict[str, dict[str, Any]] = {}
_SENSITIVE_CONFIG_KEYS = ("api_key", "apikey", "token", "password", "secret", "authorization", "credential")


def _tool_config_session_id(payload: dict[str, Any] | None = None) -> str:
    payload = payload or {}
    seed = str(payload.get("session_id") or payload.get("tool_name") or payload.get("operation") or payload.get("description") or "default")
    slug = _slug(seed) if "_slug" in globals() else re.sub(r"[^a-z0-9]+", "_", seed.lower()).strip("_")
    return slug or "default"


def _env_name_for_tool_config(tool_name: str, key: str, *, secret: bool = False) -> str:
    prefix = re.sub(r"[^A-Za-z0-9]+", "_", (tool_name or "TOOL")).strip("_").upper() or "TOOL"
    suffix = re.sub(r"[^A-Za-z0-9]+", "_", key).strip("_").upper() or ("SECRET" if secret else "CONFIG")
    if secret and not any(token in suffix for token in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
        suffix = f"{suffix}_SECRET"
    return f"{prefix}_{suffix}"


def _is_sensitive_config_key(key: str) -> bool:
    lowered = (key or "").lower()
    return any(token in lowered for token in _SENSITIVE_CONFIG_KEYS)


def save_tool_authoring_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Save authoring configuration as env/secret references without returning plaintext values."""
    data = dict(payload or {})
    session_id = _tool_config_session_id(data)
    tool_name = str(data.get("tool_name") or data.get("operation") or session_id or "tool")
    raw_config = data.get("config") if isinstance(data.get("config"), dict) else {}
    config_refs: dict[str, str] = {}
    configured_env: list[str] = []
    configured_secrets: list[str] = []

    def store_value(key: str, value: Any, *, secret: bool = False, env_name: str | None = None) -> None:
        if value in (None, "", {}, []):
            return
        name = env_name or _env_name_for_tool_config(tool_name, key, secret=secret)
        os.environ[name] = str(value)
        config_refs[key] = f"${{ENV:{name}}}"
        (configured_secrets if secret else configured_env).append(name)

    # Normalized form fields from the authorization dialog.
    store_value("base_url", data.get("base_url") or raw_config.get("base_url") or raw_config.get("endpoint") or raw_config.get("url"), secret=False, env_name=data.get("base_url_env") or raw_config.get("base_url_env"))
    auth_type = str(data.get("auth_type") or raw_config.get("auth_type") or raw_config.get("authentication") or "none").strip().lower() or "none"
    if auth_type != "none":
        secret_key = "api_key" if auth_type in {"api_key", "key"} else "token" if auth_type in {"token", "bearer"} else "password" if auth_type == "basic" else "secret"
        store_value(secret_key, data.get("secret_value") or raw_config.get(secret_key) or raw_config.get("api_key") or raw_config.get("token") or raw_config.get("password"), secret=True, env_name=data.get("secret_env") or raw_config.get("secret_env") or raw_config.get(f"{secret_key}_env"))

    extra = data.get("extra") if isinstance(data.get("extra"), dict) else raw_config.get("extra") if isinstance(raw_config.get("extra"), dict) else {}
    for key, value in extra.items():
        store_value(str(key), value, secret=_is_sensitive_config_key(str(key)))
    additional_fields = data.get("additional_fields") if isinstance(data.get("additional_fields"), list) else []
    for field in additional_fields:
        if not isinstance(field, dict):
            continue
        key = str(field.get("key") or "").strip()
        if not key:
            continue
        store_value(key, field.get("value"), secret=bool(field.get("sensitive")) or _is_sensitive_config_key(key))

    sanitized_config = dict(raw_config)
    if "base_url" in config_refs:
        sanitized_config["base_url"] = config_refs["base_url"]
    sanitized_config["auth_type"] = auth_type
    for key, ref in config_refs.items():
        if key != "base_url":
            sanitized_config[key] = ref
    sanitized_config, inferred_refs = _sanitize_authoring_config(sanitized_config)
    for ref in inferred_refs:
        if ref not in configured_secrets and ref not in configured_env:
            configured_secrets.append(ref)

    _TOOL_AUTHORING_CONFIG_STORE[session_id] = {
        "tool_name": tool_name,
        "config": sanitized_config,
        "sample_input": data.get("sample_input") if isinstance(data.get("sample_input"), dict) else {},
        "configured_env": sorted(set(configured_env)),
        "configured_secrets": sorted(set(configured_secrets)),
        "config_refs": config_refs,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    return {"success": True, "session_id": session_id, "configured_env": sorted(set(configured_env)), "configured_secrets": sorted(set(configured_secrets)), "config_refs": config_refs, "config": sanitized_config}


def tool_authoring_config_status(session_id: str = "default") -> dict[str, Any]:
    key = _tool_config_session_id({"session_id": session_id})
    stored = _TOOL_AUTHORING_CONFIG_STORE.get(key)
    if not stored:
        return {"success": True, "configured": False, "session_id": key, "configured_env": [], "configured_secrets": [], "config_refs": {}}
    configured_env = [name for name in stored.get("configured_env", []) if os.environ.get(name) is not None]
    configured_secrets = [name for name in stored.get("configured_secrets", []) if os.environ.get(name) is not None]
    return {"success": True, "configured": bool(configured_env or configured_secrets), "session_id": key, "configured_env": configured_env, "configured_secrets": configured_secrets, "config_refs": stored.get("config_refs") or {}, "config": stored.get("config") or {}, "sample_input": stored.get("sample_input") or {}, "updated_at": stored.get("updated_at")}

SnippetKind = Literal[
    "minimal_usage",
    "multi_input_usage",
    "file_output_usage",
    "batch_usage",
    "error_repair_usage",
    "anti_pattern",
    "trial_run_usage",
]


@dataclass(frozen=True)
class ToolSnippet:
    id: str
    title: str
    kind: SnippetKind = "minimal_usage"
    applies_to: dict[str, list[str]] = field(default_factory=dict)
    description: str = ""
    code: str = ""
    expected_input_shape: dict[str, Any] = field(default_factory=dict)
    expected_output_shape: dict[str, Any] = field(default_factory=dict)
    return_rule: str = ""
    anti_patterns: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    usage_policy: UsagePolicy = "self_implementation_allowed"
    priority: int = 0

@dataclass(frozen=True)
class ToolFunctionManifest:
    function_name: str
    import_path: str
    short_description: str
    when_to_use: str
    signature: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    return_contract: str = "Returns a dict that conforms to output_schema."
    example_call: str = ""
    example_return: str = ""
    example_stdout: str = ""
    common_mistakes: list[str] = field(default_factory=list)
    trial_mode_behavior: str = ""
    safety_notes: list[str] = field(default_factory=list)
    required_env: list[str] = field(default_factory=list)
    required_secrets: list[str] = field(default_factory=list)
    usage_policy: UsagePolicy = "self_implementation_allowed"
    allowed_roles: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    forbidden_imports: list[str] = field(default_factory=list)
    forbidden_side_effects: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ToolCapability:
    name: str
    display_name: str
    category: str
    roles: list[str] = field(default_factory=list)

    enabled_by_default: bool = True
    allow_creator_use: bool = True
    allow_external_side_effect: bool = False

    helper_imports: list[str] = field(default_factory=list)
    allowed_roles: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    optional_capabilities: list[str] = field(default_factory=list)
    forbidden_capabilities: list[str] = field(default_factory=list)
    usage_policy: UsagePolicy = "self_implementation_allowed"
    required_env: list[str] = field(default_factory=list)
    required_secrets: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    helper_module: str = "backend.services.skill_runtime"
    forbidden_direct_imports: list[str] = field(default_factory=list)
    safety_level: str = "standard"

    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)

    trial_mode: Literal["none", "mock", "minimal_file"] = "mock"
    validator_kind: str = "generic"
    prompt_guidance: str = ""
    tool_type: str = "python_helper"
    functions: list[ToolFunctionManifest] = field(default_factory=list)
    snippets: list[ToolSnippet] = field(default_factory=list)
    adapter_path: str = ""
    version: str = "1.0.0"
    approval_status: str = "approved"
    test_status: str = "unknown"
    last_validation_result: dict[str, Any] = field(default_factory=dict)
    created_by: str = "system"
    created_at: str = ""
    updated_at: str = ""


_ROLE_FORBIDDEN_CAPABILITIES: dict[str, list[str]] = {
    "text_generator": ["image_generation", "pdf_generation"],
    "image_generator": ["text_generation", "pdf_generation"],
    "composite_generator": ["pdf_generation"],
    "generic_script": ["text_generation", "image_generation", "pdf_generation"],
    "reference": ["runtime_execution", "image_generation"],
    "asset": ["runtime_execution", "image_generation"],
    "skill_overview": ["hidden_runtime_protocol"],
}


BUILTIN_TOOL_CAPABILITIES: dict[str, ToolCapability] = {
    "text_generation": ToolCapability(
        name="text_generation",
        display_name="文本生成",
        category="generation",
        roles=["text_generator", "composite_generator"],
        helper_imports=["generate_text_with_llm"],
        input_schema={"type": "object", "properties": {"prompt": {"type": "string"}}},
        output_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        validator_kind="helper_import",
        usage_policy="helper_preferred",
        prompt_guidance="需要文本生成时，可优先使用 backend.services.skill_runtime.generate_text_with_llm；也可在声明能力边界内自实现。",
    ),
    "image_generation": ToolCapability(
        name="image_generation",
        display_name="图片生成",
        category="generation",
        roles=["image_generator", "composite_generator"],
        helper_imports=["generate_stable_diffusion_image"],
        output_schema={"type": "object", "properties": {"image_path": {"type": "string"}}},
        trial_mode="minimal_file",
        validator_kind="helper_import",
        usage_policy="helper_preferred",
        prompt_guidance="需要图片生成时，可优先使用 generate_stable_diffusion_image；也可自实现，但最终 stdout/artifact 必须通过 E2E 校验。",
    ),
    "pdf_generation": ToolCapability(
        name="pdf_generation",
        display_name="PDF 生成",
        category="document",
        roles=["pdf_builder"],
        helper_imports=["create_pdf", "build_pdf_report", "images_to_pdf", "merge_pdfs"],
        dependencies=["reportlab"],
        output_schema={"type": "object", "properties": {"pdf_path": {"type": "string"}}},
        trial_mode="minimal_file",
        validator_kind="file_output",
        usage_policy="helper_preferred",
        prompt_guidance=(
            "PDF 生成可优先使用平台 helper create_pdf/build_pdf_report/images_to_pdf/merge_pdfs；"
            "也可自实现，但最终 PDF 文件、路径和 stdout 字段必须通过 E2E 校验。"
        ),
    ),
    "docx_generation": ToolCapability(
        name="docx_generation",
        display_name="Word 生成",
        category="document",
        roles=["docx_builder"],
        helper_imports=["create_docx"],
        dependencies=["python-docx"],
        output_schema={"type": "object", "properties": {"docx_path": {"type": "string"}}},
        trial_mode="minimal_file",
        validator_kind="file_output",
        usage_policy="helper_preferred",
        prompt_guidance="Word 生成可优先使用平台 helper create_docx；也可自实现，并返回合法 docx_path/file_outputs。",
    ),
    "pptx_generation": ToolCapability(
        name="pptx_generation",
        display_name="PPT 生成",
        category="document",
        roles=["pptx_builder"],
        helper_imports=["create_pptx"],
        dependencies=["python-pptx"],
        output_schema={"type": "object", "properties": {"pptx_path": {"type": "string"}}},
        trial_mode="minimal_file",
        validator_kind="file_output",
        usage_policy="helper_preferred",
        prompt_guidance="PPT 生成可优先使用平台 helper create_pptx；也可自实现，并返回合法 pptx_path/file_outputs。",
    ),
    "html_asset_generation": ToolCapability(
        name="html_asset_generation",
        display_name="HTML 素材生成",
        category="document",
        roles=["html_asset_builder"],
        output_schema={"type": "object", "properties": {"html_path": {"type": "string"}}},
        validator_kind="file_output",
        prompt_guidance="HTML 资产生成必须输出确定性 HTML 文件路径，并在 stdout JSON 中声明文件输出。",
    ),
    "asset_generation": ToolCapability(
        name="asset_generation",
        display_name="静态素材生成",
        category="asset",
        roles=["asset_builder"],
        output_schema={"type": "object", "properties": {"asset_path": {"type": "string"}}},
        validator_kind="file_output",
        prompt_guidance="静态素材生成只能创建本地文件，不得调用外部服务。",
    ),
    "file_output": ToolCapability(
        name="file_output",
        display_name="文件输出",
        category="common",
        roles=["pdf_builder", "docx_builder", "pptx_builder", "html_asset_builder", "asset_builder"],
        trial_mode="minimal_file",
        validator_kind="file_output",
        prompt_guidance="写文件时必须输出可校验的本地路径，并在 stdout JSON 中包含 path 或 file_outputs。",
    ),
    "pdf_parsing": ToolCapability(
        name="pdf_parsing",
        display_name="PDF 解析",
        category="parsing",
        roles=["pdf_parser"],
        helper_imports=["extract_pdf_text"],
        dependencies=["pypdf"],
        validator_kind="helper_import",
        usage_policy="helper_preferred",
        prompt_guidance="PDF 解析可优先使用 extract_pdf_text；也可自实现。",
    ),
    "docx_parsing": ToolCapability(
        name="docx_parsing",
        display_name="Word 解析",
        category="parsing",
        roles=["docx_parser"],
        helper_imports=["read_docx_text"],
        dependencies=["python-docx"],
        validator_kind="helper_import",
        usage_policy="helper_preferred",
        prompt_guidance="Word 解析可优先使用 read_docx_text；也可自实现。",
    ),
    "pptx_parsing": ToolCapability(
        name="pptx_parsing",
        display_name="PPT 解析",
        category="parsing",
        roles=["pptx_parser"],
        helper_imports=["read_pptx_text"],
        dependencies=["python-pptx"],
        validator_kind="helper_import",
        usage_policy="helper_preferred",
        prompt_guidance="PPT 解析可优先使用 read_pptx_text；也可自实现。",
    ),
    "spreadsheet_read": ToolCapability(
        name="spreadsheet_read",
        display_name="表格读取",
        category="parsing",
        roles=["spreadsheet_reader"],
        helper_imports=["read_spreadsheet"],
        dependencies=["openpyxl"],
        validator_kind="helper_import",
        prompt_guidance="表格读取只能读取本地文件，避免写入或修改原始表格。",
    ),
    "vision_understanding": ToolCapability(
        name="vision_understanding",
        display_name="视觉理解",
        category="ai",
        roles=["vision_analyzer"],
        helper_imports=["analyze_image_with_vision", "ocr_image"],
        required_env=["VISION_MODEL"],
        validator_kind="helper_import",
        usage_policy="helper_preferred",
        prompt_guidance="视觉理解可优先使用 analyze_image_with_vision 或 ocr_image；试运行时可返回 mock 结果。",
    ),
    "web_search": ToolCapability(
        name="web_search",
        display_name="网页搜索",
        category="retrieval",
        roles=["search_reader"],
        helper_imports=["web_search", "fetch_url_text"],
        required_env=["SEARCHXNG_BASE_URL"],
        validator_kind="helper_import",
        usage_policy="helper_preferred",
        prompt_guidance="网页搜索可优先使用 web_search/fetch_url_text；也可在能力声明边界内自实现。",
    ),
    "database_read": ToolCapability(
        name="database_read",
        display_name="数据库只读查询",
        category="retrieval",
        roles=["database_reader"],
        helper_imports=["query_database_readonly", "list_database_tables", "describe_database_table"],
        usage_policy="helper_required",
        required_secrets=["DATABASE_URL"],
        validator_kind="database_readonly",
        prompt_guidance="数据库能力只允许 SELECT/WITH 只读查询，必须通过 query_database_readonly，禁止 INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE/CREATE。",
    ),
    "wechat_draft": ToolCapability(
        name="wechat_draft",
        display_name="微信公众号草稿",
        category="publisher",
        roles=["wechat_draft_creator"],
        helper_imports=["create_wechat_draft", "upload_wechat_media"],
        required_secrets=["WECHAT_APP_ID", "WECHAT_APP_SECRET"],
        validator_kind="helper_import",
        usage_policy="helper_required",
        prompt_guidance="默认只能创建微信公众号草稿，不得自动发布。使用 create_wechat_draft 并返回 draft_id。",
    ),
    "wechat_publish": ToolCapability(
        name="wechat_publish",
        display_name="微信公众号发布",
        category="publisher",
        roles=["wechat_publisher"],
        enabled_by_default=False,
        allow_external_side_effect=True,
        helper_imports=["publish_wechat_draft"],
        required_secrets=["WECHAT_APP_ID", "WECHAT_APP_SECRET"],
        validator_kind="external_side_effect",
        usage_policy="helper_required",
        prompt_guidance="除非用户明确要求直接发布，否则只能创建草稿；发布必须通过 publish_wechat_draft。",
    ),
    "deterministic_execution": ToolCapability(
        name="deterministic_execution",
        display_name="确定性脚本执行",
        category="common",
        roles=["generic_script"],
        trial_mode="none",
        validator_kind="generic",
        prompt_guidance="通用脚本不得调用模型、搜索、数据库或外部发布能力，除非 SkillPlan 显式声明对应 capability。",
    ),
    "reference_guidance": ToolCapability(
        name="reference_guidance",
        display_name="参考文档指导",
        category="resource",
        roles=["reference"],
        allow_creator_use=False,
        trial_mode="none",
        validator_kind="resource",
    ),
    "static_resource": ToolCapability(
        name="static_resource",
        display_name="静态资源",
        category="resource",
        roles=["asset"],
        allow_creator_use=False,
        trial_mode="none",
        validator_kind="resource",
    ),
    "workflow_overview": ToolCapability(
        name="workflow_overview",
        display_name="工作流概览",
        category="resource",
        roles=["skill_overview"],
        allow_creator_use=False,
        trial_mode="none",
        validator_kind="resource",
    ),
    "authoring_config_collector": ToolCapability(
        name="authoring_config_collector",
        display_name="Authoring 通用配置收集",
        category="authoring",
        roles=["tool_authoring"],
        allow_creator_use=False,
        tool_type="internal_authoring_tool",
        validator_kind="internal_authoring_tool",
        usage_policy="helper_required",
        prompt_guidance="仅 Tool Authoring 流程可调用；收集 endpoint/base_url、认证、env/secret 引用、headers/query/body 模板和 sample input，不保存明文 secret。",
    ),
    "authoring_schema_infer": ToolCapability(
        name="authoring_schema_infer",
        display_name="Authoring Schema 推断",
        category="authoring",
        roles=["tool_authoring"],
        allow_creator_use=False,
        tool_type="internal_authoring_tool",
        validator_kind="internal_authoring_tool",
        usage_policy="helper_required",
        prompt_guidance="仅 Tool Authoring 流程可调用；根据需求、配置和 sample input 推断输入/输出 schema 草案。",
    ),
    "authoring_live_test": ToolCapability(
        name="authoring_live_test",
        display_name="Authoring Live Test",
        category="authoring",
        roles=["tool_authoring"],
        allow_creator_use=False,
        allow_external_side_effect=True,
        tool_type="internal_authoring_tool",
        validator_kind="internal_authoring_tool",
        usage_policy="helper_required",
        prompt_guidance="仅 Tool Authoring 流程可调用；在用户确认外部网络后执行一次通用 live_test。",
    ),
    "authoring_dependency_check": ToolCapability(
        name="authoring_dependency_check",
        display_name="Authoring 依赖检测",
        category="authoring",
        roles=["tool_authoring"],
        allow_creator_use=False,
        tool_type="internal_authoring_tool",
        validator_kind="internal_authoring_tool",
        usage_policy="helper_required",
        prompt_guidance="仅 Tool Authoring 流程可调用；检测 adapter 计划所需 Python 依赖是否可 import。",
    ),
    "authoring_code_protocol_check": ToolCapability(
        name="authoring_code_protocol_check",
        display_name="Authoring 代码协议检查",
        category="authoring",
        roles=["tool_authoring"],
        allow_creator_use=False,
        tool_type="internal_authoring_tool",
        validator_kind="internal_authoring_tool",
        usage_policy="helper_required",
        prompt_guidance="仅 Tool Authoring 流程可调用；静态检查 adapter 是否暴露 run/manifest function、env 读取和危险调用。",
    ),
    "authoring_file_output_check": ToolCapability(
        name="authoring_file_output_check",
        display_name="Authoring 文件输出协议检查",
        category="authoring",
        roles=["tool_authoring"],
        allow_creator_use=False,
        tool_type="internal_authoring_tool",
        validator_kind="internal_authoring_tool",
        usage_policy="helper_required",
        prompt_guidance="仅 Tool Authoring 流程可调用；检查文件输出 schema 是否声明 file_paths/file_outputs 或 OUTPUT_DIR 约束。",
    ),
}

RESOURCE_ROLES: frozenset[str] = frozenset({"skill_overview", "reference", "asset", "tool_authoring"})
TOOL_OVERRIDE_PERSISTENCE = "process_memory"
CUSTOM_TOOL_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "config" / "tool_registry.custom.json"
CUSTOM_TOOL_ADAPTER_DIR = Path(__file__).resolve().parent / "runtime_tools" / "custom_tools"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
_REGISTERED_TOOL_CAPABILITIES: dict[str, ToolCapability] = {}
_TOOL_OVERRIDES: dict[str, dict[str, bool]] = {}
_RUNTIME_HELPERS_CACHE: set[str] | None = None

_ALLOWED_USAGE_POLICIES = {"helper_required", "helper_preferred", "self_implementation_allowed"}
_ALLOWED_SNIPPET_KINDS = {"minimal_usage", "multi_input_usage", "file_output_usage", "batch_usage", "error_repair_usage", "anti_pattern", "trial_run_usage"}
_ALLOWED_TOOL_TYPES = {
    "python_helper", "http_api", "local_command", "database_query",
    "file_converter", "document_generator", "image_generator", "custom_adapter",
    "internal_authoring_tool",
}
_DANGEROUS_IMPORTS = {"subprocess", "shutil", "socket", "paramiko", "ftplib", "telnetlib"}
_DANGEROUS_CALLS = {"eval", "exec", "compile", "open"}
_HIGH_RISK_CAPABILITIES = {
    "database_write", "external_http", "wechat_publish", "file_delete",
    "shell_command", "network_access", "secret_access",
}

_DEPENDENCY_IMPORT_NAMES = {
    "python-docx": "docx",
    "python-pptx": "pptx",
}


def _dependency_available(dependency: str) -> bool:
    module_name = _DEPENDENCY_IMPORT_NAMES.get(dependency, dependency).replace("-", "_")
    return importlib.util.find_spec(module_name) is not None


def _with_overrides(capability: ToolCapability) -> ToolCapability:
    override = _TOOL_OVERRIDES.get(capability.name, {})
    return replace(
        capability,
        enabled_by_default=override.get("enabled", capability.enabled_by_default),
        allow_creator_use=override.get("allow_creator_use", capability.allow_creator_use),
    )



def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: str, fallback: str = "custom_tool") -> str:
    text = re.sub(r"[^a-zA-Z0-9_]+", "_", (value or "").strip().lower()).strip("_")
    if not text:
        text = fallback
    if text[0].isdigit():
        text = f"tool_{text}"
    return text[:64]


def _function_from_dict(data: dict[str, Any]) -> ToolFunctionManifest:
    known = {field.name for field in dataclasses_fields(ToolFunctionManifest)}
    payload = {key: value for key, value in (data or {}).items() if key in known}
    return ToolFunctionManifest(**payload)


def _snippet_from_dict(data: dict[str, Any]) -> ToolSnippet:
    known = {field.name for field in dataclasses_fields(ToolSnippet)}
    payload = {key: value for key, value in (data or {}).items() if key in known}
    if not payload.get("id"):
        payload["id"] = _slug(str(payload.get("title") or "snippet"), fallback="snippet")
    if not payload.get("title"):
        payload["title"] = str(payload["id"]).replace("_", " ").title()
    if payload.get("kind") not in _ALLOWED_SNIPPET_KINDS:
        payload["kind"] = "minimal_usage"
    if not isinstance(payload.get("applies_to"), dict):
        payload["applies_to"] = {}
    for key in ("roles", "capabilities", "failure_layers"):
        values = payload["applies_to"].get(key, [])
        payload["applies_to"][key] = [str(item) for item in values if item] if isinstance(values, list) else []
    for key in ("expected_input_shape", "expected_output_shape"):
        if not isinstance(payload.get(key), dict):
            payload[key] = {}
    for key in ("anti_patterns", "requires"):
        payload[key] = [str(item) for item in payload.get(key, []) if item] if isinstance(payload.get(key), list) else []
    if payload.get("usage_policy") not in _ALLOWED_USAGE_POLICIES:
        payload["usage_policy"] = "self_implementation_allowed"
    try:
        payload["priority"] = int(payload.get("priority") or 0)
    except (TypeError, ValueError):
        payload["priority"] = 0
    return ToolSnippet(**payload)


def _capability_from_dict(data: dict[str, Any]) -> ToolCapability:
    payload = dict(data or {})
    functions = payload.pop("functions", []) or []
    snippets = payload.pop("snippets", []) or []
    if "enabled" in payload and "enabled_by_default" not in payload:
        payload["enabled_by_default"] = bool(payload.pop("enabled"))
    if "allowed_roles" in payload and "roles" not in payload:
        payload["roles"] = list(payload.get("allowed_roles") or [])
    known = {field.name for field in dataclasses_fields(ToolCapability)}
    payload = {key: value for key, value in payload.items() if key in known}
    payload["functions"] = [_function_from_dict(item) for item in functions if isinstance(item, dict)]
    payload["snippets"] = [_snippet_from_dict(item) for item in snippets if isinstance(item, dict)]
    return ToolCapability(**payload)


def _capability_to_registry_record(capability: ToolCapability) -> dict[str, Any]:
    data = asdict(capability)
    data["enabled"] = data.pop("enabled_by_default")
    data["allowed_roles"] = capability.allowed_roles or capability.roles
    return data


def _load_registered_tools_from_disk() -> None:
    if not CUSTOM_TOOL_REGISTRY_PATH.exists():
        return
    try:
        payload = json.loads(CUSTOM_TOOL_REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return
    records = payload.get("tools") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        return
    for record in records:
        if not isinstance(record, dict):
            continue
        try:
            cap = _capability_from_dict(record)
        except Exception:
            continue
        if cap.name:
            _REGISTERED_TOOL_CAPABILITIES[cap.name] = cap


def persist_registered_tools() -> None:
    CUSTOM_TOOL_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    records = [_capability_to_registry_record(cap) for cap in _REGISTERED_TOOL_CAPABILITIES.values()]
    CUSTOM_TOOL_REGISTRY_PATH.write_text(
        json.dumps({"tools": records}, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _runtime_helper_names() -> set[str]:
    global _RUNTIME_HELPERS_CACHE
    if _RUNTIME_HELPERS_CACHE is not None:
        return set(_RUNTIME_HELPERS_CACHE)

    try:
        runtime_module = importlib.import_module("backend.services.skill_runtime")
    except Exception:
        runtime_module = None

    if runtime_module is not None:
        _RUNTIME_HELPERS_CACHE = {
            helper
            for capability in [*BUILTIN_TOOL_CAPABILITIES.values(), *_REGISTERED_TOOL_CAPABILITIES.values()]
            for helper in capability.helper_imports
            if hasattr(runtime_module, helper)
        }
        return set(_RUNTIME_HELPERS_CACHE)

    # Fallback for damaged import environments: keep the old static scan, but
    # include imported/re-exported helper aliases as well as local definitions.
    runtime_path = Path(__file__).with_name("skill_runtime.py")
    try:
        tree = ast.parse(runtime_path.read_text(encoding="utf-8"))
    except OSError:
        _RUNTIME_HELPERS_CACHE = set()
        return set()

    helper_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            helper_names.add(node.name)
        elif isinstance(node, ast.ImportFrom):
            helper_names.update(alias.asname or alias.name for alias in node.names)
    _RUNTIME_HELPERS_CACHE = helper_names
    return set(_RUNTIME_HELPERS_CACHE)



@dataclass(frozen=True)
class ToolResolveResult:
    allowed_tools: list[str] = field(default_factory=list)
    allowed_helper_imports: list[str] = field(default_factory=list)
    required_dependencies: list[str] = field(default_factory=list)
    forbidden_imports: list[str] = field(default_factory=list)
    tool_function_cards: list[str] = field(default_factory=list)
    tool_snippets: list[dict[str, Any]] = field(default_factory=list)
    tool_usage_prompt: str = ""
    warnings: list[str] = field(default_factory=list)



def _schema_summary(schema: dict[str, Any]) -> str:
    if not schema:
        return "{}"
    return json.dumps(schema, ensure_ascii=False, sort_keys=True)



def _default_snippet_for_function(capability: ToolCapability, fn: ToolFunctionManifest) -> ToolSnippet:
    import_stmt = f"from {fn.import_path} import {fn.function_name}" if fn.import_path else f"import {fn.function_name}"
    return ToolSnippet(
        id=f"{fn.function_name}.minimal_usage",
        title=f"Use {fn.function_name}",
        kind="minimal_usage",
        applies_to={
            "roles": fn.allowed_roles or capability.allowed_roles or capability.roles,
            "capabilities": fn.required_capabilities or capability.required_capabilities or [capability.name],
            "failure_layers": ["helper_call_failed", "final_platform_output_value_invalid", "artifact_missing"],
        },
        description=fn.when_to_use or fn.short_description,
        code=f"{import_stmt}\n\nresult = {fn.function_name}(... )\nreturn result",
        expected_input_shape=fn.input_schema or capability.input_schema,
        expected_output_shape=fn.output_schema or capability.output_schema,
        return_rule=fn.return_contract or "Return the helper result directly if it is already a platform stdout dict.",
        anti_patterns=fn.common_mistakes or ["Do not guess parameters.", "Do not wrap a platform stdout dict inside the wrong field."],
        requires=fn.required_capabilities or capability.required_capabilities or [capability.name],
        usage_policy=fn.usage_policy or capability.usage_policy,
        priority=10,
    )


def snippets_for_tool(capability: ToolCapability) -> list[ToolSnippet]:
    snippets = list(capability.snippets)
    if snippets:
        return snippets
    functions = list(capability.functions)
    if not functions:
        functions = [
            ToolFunctionManifest(
                function_name=helper,
                import_path=capability.helper_module,
                short_description=capability.prompt_guidance or capability.display_name,
                when_to_use=f"Use for capability {capability.name} when role/capability resolution allows it.",
                signature=f"{helper}(...) -> dict",
                input_schema=capability.input_schema,
                output_schema=capability.output_schema,
                return_contract="Return the helper result directly when it already contains platform stdout fields; do not wrap helper result in the wrong key.",
                usage_policy=capability.usage_policy,
                allowed_roles=capability.allowed_roles or capability.roles,
                required_capabilities=capability.required_capabilities or [capability.name],
            )
            for helper in capability.helper_imports
        ]
    return [_default_snippet_for_function(capability, fn) for fn in functions]


def format_tool_snippet(capability: ToolCapability, snippet: ToolSnippet) -> str:
    anti = "\n".join(f"- {item}" for item in snippet.anti_patterns) or "- Follow the helper contract; do not guess parameters or return shape."
    return "\n".join([
        "[Tool Snippet]",
        f"Tool: {capability.name}",
        f"Snippet: {snippet.id} ({snippet.kind}, priority={snippet.priority})",
        f"Use when: {snippet.description or snippet.title}",
        "Correct usage:",
        snippet.code.strip(),
        "Expected input shape:",
        _schema_summary(snippet.expected_input_shape),
        "Expected return:",
        _schema_summary(snippet.expected_output_shape),
        f"Return rule: {snippet.return_rule or 'Return a JSON-serializable dict that matches the expected return.'}",
        f"Usage policy: {snippet.usage_policy}",
        "Do not:",
        anti,
    ])


def validate_tool_snippet(capability: ToolCapability, snippet: ToolSnippet) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", snippet.id or ""):
        errors.append("snippet id must be 1-128 chars of letters, numbers, '_', '.', ':', or '-'")
    if snippet.kind not in _ALLOWED_SNIPPET_KINDS:
        errors.append(f"snippet kind must be one of {sorted(_ALLOWED_SNIPPET_KINDS)}")
    if not (snippet.code or "").strip():
        errors.append("snippet code is required")
    if snippet.usage_policy not in _ALLOWED_USAGE_POLICIES:
        errors.append("snippet usage_policy is invalid")
    code = snippet.code or ""
    imported_names: set[str] = set()
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported_names.update(alias.asname or alias.name for alias in node.names)
                root = (node.module or "").split(".")[0]
                if root in _DANGEROUS_IMPORTS:
                    errors.append(f"dangerous import is forbidden in snippet: {root}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    imported_names.add(alias.asname or alias.name.split(".")[-1])
                    if root in _DANGEROUS_IMPORTS:
                        errors.append(f"dangerous import is forbidden in snippet: {root}")
    except SyntaxError as exc:
        warnings.append(f"snippet is not a complete executable Python block: {exc}")
    helpers = set(capability.helper_imports) | {fn.function_name for fn in capability.functions}
    if helpers and not any(helper in code for helper in helpers):
        errors.append("snippet code must reference at least one manifest helper/function name")
    if imported_names and helpers and not (imported_names & helpers):
        warnings.append("snippet imports do not include a declared manifest helper/function")
    if re.search(r"(?:/tmp|/var|/etc|~[/\\]|[A-Za-z]:\\\\)", code):
        errors.append("snippet must not write or direct outputs to dangerous absolute paths")
    if re.search(r"(?:sk-|AKIA|-----BEGIN [A-Z ]*PRIVATE KEY-----)[A-Za-z0-9_\-+/=]{8,}", code):
        errors.append("snippet appears to contain a hard-coded secret")
    declared_outputs = set((capability.output_schema or {}).get("properties", {}).keys())
    for fn in capability.functions:
        declared_outputs.update((fn.output_schema or {}).keys())
    snippet_outputs = set(snippet.expected_output_shape.keys())
    if declared_outputs and snippet_outputs and not (declared_outputs & snippet_outputs):
        warnings.append("snippet expected_output_shape has no overlap with manifest output_schema")
    return {"success": not errors, "errors": sorted(set(errors)), "warnings": sorted(set(warnings))}


def _snippet_score(*, capability: ToolCapability, snippet: ToolSnippet, role: str, capabilities: list[str], tool_names: list[str], failure_layer: str | None, error_text: str | None) -> tuple[int, int, int, int, int, int]:
    applies = snippet.applies_to or {}
    snippet_caps = set(applies.get("capabilities") or snippet.requires or [])
    snippet_roles = set(applies.get("roles") or [])
    snippet_failures = set(applies.get("failure_layers") or [])
    haystack = (error_text or "").lower()
    helper_names = set(capability.helper_imports) | {fn.function_name for fn in capability.functions} | {capability.name}
    capability_match = len(snippet_caps & set(capabilities))
    role_match = 1 if role and role in snippet_roles else 0
    failure_match = 1 if failure_layer and failure_layer in snippet_failures else 0
    error_match = 1 if any(name.lower() in haystack for name in helper_names) else 0
    minimal = 1 if snippet.kind in {"minimal_usage", "error_repair_usage"} else 0
    tool_match = 1 if capability.name in tool_names or any(name in tool_names for name in helper_names) else 0
    return (capability_match + tool_match, role_match, failure_match, error_match, int(snippet.priority or 0), minimal)


def resolve_tool_snippets_for_context(
    *,
    role: str,
    capabilities: list[str],
    tool_names: list[str],
    file_path: str,
    failure_layer: str | None = None,
    error_text: str | None = None,
    max_snippets: int = 5,
) -> list[dict[str, Any]]:
    max_snippets = max(1, min(int(max_snippets or 5), 10))
    candidates: list[tuple[tuple[int, int, int, int, int, int], ToolCapability, ToolSnippet]] = []
    requested_tools = set(tool_names or [])
    requested_caps = set(capabilities or [])
    for cap in list_tool_capabilities():
        status = tool_status(cap)
        if not status.get("creator_available"):
            continue
        helper_names = set(cap.helper_imports) | {fn.function_name for fn in cap.functions}
        if requested_tools and cap.name not in requested_tools and not (helper_names & requested_tools):
            continue
        if not requested_tools and requested_caps and cap.name not in requested_caps and not (set(cap.required_capabilities or [cap.name]) & requested_caps):
            continue
        if cap.roles and role and role not in cap.roles and cap.name not in {"file_output", "deterministic_execution"}:
            # Still allow explicit error-text matches during repair.
            haystack = (error_text or "").lower()
            if not any(name.lower() in haystack for name in helper_names | {cap.name}):
                continue
        for snippet in snippets_for_tool(cap):
            score = _snippet_score(
                capability=cap,
                snippet=snippet,
                role=role,
                capabilities=capabilities or [],
                tool_names=tool_names or [],
                failure_layer=failure_layer,
                error_text=error_text,
            )
            if any(score[:4]) or not requested_tools:
                candidates.append((score, cap, snippet))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [
        {"tool": cap.name, **asdict(snippet), "formatted": format_tool_snippet(cap, snippet)}
        for _, cap, snippet in candidates[:max_snippets]
    ]


def tool_snippet_prompt(snippets: list[dict[str, Any]]) -> str:
    if not snippets:
        return "当前脚本可用工具 Snippets: 无"
    return "当前脚本可用工具 Snippets（调用任何工具前必须优先参考；不要根据函数名猜参数或返回值；若 snippet 与猜测冲突，以 snippet 为准）：\n\n" + "\n\n---\n\n".join(str(item.get("formatted") or "") for item in snippets)


def set_tool_snippets(name: str, snippets: list[ToolSnippet]) -> ToolCapability | None:
    global BUILTIN_TOOL_CAPABILITIES
    current = BUILTIN_TOOL_CAPABILITIES.get(name) or _REGISTERED_TOOL_CAPABILITIES.get(name)
    if current is None:
        return None
    updated = replace(current, snippets=snippets, updated_at=_utc_now())
    if name in BUILTIN_TOOL_CAPABILITIES:
        BUILTIN_TOOL_CAPABILITIES[name] = updated
    else:
        _REGISTERED_TOOL_CAPABILITIES[name] = updated
    return get_tool_capability(name)


def function_cards_for_tool(capability: ToolCapability) -> list[str]:
    """Return Creator prompt cards with function-level I/O and call contracts."""
    functions = list(capability.functions)
    if not functions:
        functions = [
            ToolFunctionManifest(
                function_name=helper,
                import_path=capability.helper_module,
                short_description=capability.prompt_guidance or capability.display_name,
                when_to_use=f"Use for capability {capability.name} when role/capability resolution allows it.",
                signature=f"{helper}(...) -> dict",
                input_schema=capability.input_schema,
                output_schema=capability.output_schema,
                return_contract="Return the helper result directly when it already contains platform stdout fields; do not wrap helper result in the wrong key.",
                example_call=f"from {capability.helper_module} import {helper}\nresult = {helper}(...)\nreturn result",
                example_stdout="return result",
                common_mistakes=[
                    "Do not guess parameters; inspect the signature or follow the generated adapter contract.",
                    "Do not wrap a platform stdout dict inside another unrelated field.",
                ],
                trial_mode_behavior=str(capability.trial_mode),
                safety_notes=[capability.prompt_guidance] if capability.prompt_guidance else [],
                required_env=capability.required_env,
                required_secrets=capability.required_secrets,
                usage_policy=capability.usage_policy,
                allowed_roles=capability.allowed_roles or capability.roles,
                required_capabilities=capability.required_capabilities or [capability.name],
                forbidden_imports=capability.forbidden_direct_imports,
            )
            for helper in capability.helper_imports
        ]
    cards: list[str] = []
    for fn in functions:
        mistakes = "\n".join(f"  - {item}" for item in fn.common_mistakes) or "  - None declared"
        safety = "\n".join(f"  - {item}" for item in fn.safety_notes) or "  - Follow platform sandbox and OUTPUT_DIR rules."
        import_stmt = f"from {fn.import_path} import {fn.function_name}" if fn.import_path else f"import {fn.function_name}"
        cards.append(
            "\n".join([
                f"Tool: {capability.name}.{fn.function_name}",
                f"Purpose: {fn.short_description}",
                f"When to use: {fn.when_to_use}",
                f"Import: {import_stmt}",
                f"Signature: {fn.signature}",
                f"Input schema: {_schema_summary(fn.input_schema)}",
                f"Output schema: {_schema_summary(fn.output_schema)}",
                f"Return contract: {fn.return_contract}",
                f"Example call: {fn.example_call}",
                f"Example stdout/return: {fn.example_stdout or fn.example_return}",
                "Common mistakes:",
                mistakes,
                f"Trial mode behavior: {fn.trial_mode_behavior}",
                "Safety notes:",
                safety,
                f"Usage policy: {fn.usage_policy or capability.usage_policy}",
                f"Allowed roles: {', '.join(fn.allowed_roles or capability.allowed_roles or capability.roles) if (fn.allowed_roles or capability.allowed_roles or capability.roles) else 'all'}",
                f"Required capabilities: {', '.join(fn.required_capabilities or capability.required_capabilities or [capability.name])}",
                f"Required env: {', '.join(fn.required_env or capability.required_env) if (fn.required_env or capability.required_env) else 'none'}",
                f"Required secrets: {', '.join(fn.required_secrets or capability.required_secrets) if (fn.required_secrets or capability.required_secrets) else 'none'}",
                f"Forbidden imports: {', '.join(fn.forbidden_imports or capability.forbidden_direct_imports) if (fn.forbidden_imports or capability.forbidden_direct_imports) else 'none'}",
                f"Forbidden side effects: {', '.join(fn.forbidden_side_effects) if fn.forbidden_side_effects else 'none'}",
            ])
        )
    return cards


def _entry_capabilities(entry: Any) -> list[str]:
    values: list[str] = []
    for attr in ("required_capabilities", "optional_capabilities", "allowed_capabilities"):
        raw = getattr(entry, attr, None) if not isinstance(entry, dict) else entry.get(attr)
        if isinstance(raw, list):
            values.extend(str(item) for item in raw if item)
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _entry_role(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("role") or "")
    return str(getattr(entry, "role", "") or "")


def resolve_tools_for_skill_plan_entry(entry: Any) -> ToolResolveResult:
    """Resolve Creator-usable helpers for a SkillPlan entry.

    This is the pre-generation Tool Resolve step.  It is the only place that
    converts role/capability declarations into helper names exposed to the
    model.  Disabled tools, disallowed Creator tools, missing env/secret, and
    missing runtime helpers are excluded before prompt construction.
    """
    role = _entry_role(entry)
    capabilities = _entry_capabilities(entry)
    allowed_tools: list[str] = []
    allowed_helper_imports: list[str] = []
    required_dependencies: list[str] = []
    forbidden_imports: list[str] = []
    guidance: list[str] = []
    tool_function_cards: list[str] = []
    warnings: list[str] = []

    for capability in capabilities:
        cap = get_tool_capability(capability)
        if not cap:
            warnings.append(f"unknown capability {capability!r} has no registered tool")
            continue
        status = tool_status(cap)
        if not status["enabled"]:
            warnings.append(f"tool {cap.name} is disabled")
            continue
        if not status["creator_available"]:
            warnings.append(f"tool {cap.name} is not allowed for Creator use")
            continue
        if cap.roles and role and role not in cap.roles and capability not in {"file_output", "deterministic_execution"}:
            warnings.append(f"tool {cap.name} is not allowed for role {role}")
            continue
        if status["missing_env"] or status["missing_secrets"]:
            warnings.append(f"tool {cap.name} is not configured: missing env/secret")
            continue
        if cap.helper_imports and status["missing_runtime_helpers"] and cap.usage_policy == "helper_required":
            warnings.append(f"tool {cap.name} missing required runtime helpers: {', '.join(status['missing_runtime_helpers'])}")
            continue
        allowed_tools.append(cap.name)
        allowed_helper_imports.extend(status["runtime_helpers_available"] or cap.helper_imports)
        required_dependencies.extend(cap.dependencies)
        if cap.usage_policy == "helper_required":
            forbidden_imports.extend(cap.forbidden_direct_imports)
        tool_function_cards.extend(function_cards_for_tool(cap))
        if cap.prompt_guidance:
            guidance.append(f"- {cap.name}: {cap.prompt_guidance}")

    # de-duplicate preserving order
    def dedupe(items: list[str]) -> list[str]:
        seen: set[str] = set(); out: list[str] = []
        for item in items:
            if item and item not in seen:
                seen.add(item); out.append(item)
        return out

    allowed_helper_imports = dedupe(allowed_helper_imports)
    allowed_tools = dedupe(allowed_tools)
    required_dependencies = dedupe(required_dependencies)
    forbidden_imports = dedupe(forbidden_imports)
    helper_line = (
        "可优先使用的 backend.services.skill_runtime helper: "
        + (", ".join(allowed_helper_imports) if allowed_helper_imports else "无")
        + "。"
    )
    policy_lines = [
        f"- {cap.name}: usage_policy={cap.usage_policy}; allowed_roles={', '.join(cap.roles) if cap.roles else 'all'}; "
        f"required_env={', '.join(cap.required_env) if cap.required_env else '无'}; "
        f"required_secrets={', '.join(cap.required_secrets) if cap.required_secrets else '无'}; "
        f"dependencies={', '.join(cap.dependencies) if cap.dependencies else '无'}; "
        f"required_capabilities={', '.join(cap.required_capabilities or [cap.name])}; "
        f"optional_capabilities={', '.join(cap.optional_capabilities) if cap.optional_capabilities else '无'}; "
        f"forbidden_capabilities={', '.join(cap.forbidden_capabilities or _ROLE_FORBIDDEN_CAPABILITIES.get(role, [])) if (cap.forbidden_capabilities or _ROLE_FORBIDDEN_CAPABILITIES.get(role, [])) else '无'}"
        for cap_name in allowed_tools
        for cap in [get_tool_capability(cap_name)]
        if cap is not None
    ]
    forbid_line = (
        "helper_required 工具禁止直接 import/调用底层库或绕过 helper: " + ", ".join(forbidden_imports) + "。"
        if forbidden_imports else
        "除 usage_policy=helper_required 的能力外，helper 是可用/推荐工具，不强制实现方式；最终以 E2E stdout/artifact 合同为准。"
    )
    resolved_snippets = resolve_tool_snippets_for_context(
        role=role,
        capabilities=capabilities,
        tool_names=allowed_tools + allowed_helper_imports,
        file_path=str(getattr(entry, "path", "") if not isinstance(entry, dict) else entry.get("path", "")),
        max_snippets=5,
    ) if allowed_tools or allowed_helper_imports else []
    snippet_prompt = tool_snippet_prompt(resolved_snippets)
    cards_text = "\n\n".join(tool_function_cards)
    card_header = "当前脚本可用工具 Function Cards（作为 schema 补充；真实调用优先模仿 Tool Snippets，不要只凭函数名猜参数/返回值）:" if tool_function_cards else "当前脚本可用工具 Function Cards: 无"
    tool_usage_prompt = "\n".join([helper_line, forbid_line, *policy_lines, *guidance, snippet_prompt, card_header, cards_text])
    return ToolResolveResult(
        allowed_tools=allowed_tools,
        allowed_helper_imports=allowed_helper_imports,
        required_dependencies=required_dependencies,
        forbidden_imports=forbidden_imports,
        tool_function_cards=tool_function_cards,
        tool_snippets=resolved_snippets,
        tool_usage_prompt=tool_usage_prompt,
        warnings=warnings,
    )

def list_tool_capabilities() -> list[ToolCapability]:
    return [_with_overrides(cap) for cap in [*BUILTIN_TOOL_CAPABILITIES.values(), *_REGISTERED_TOOL_CAPABILITIES.values()]]


def get_tool_capability(name: str) -> ToolCapability | None:
    key = (name or "").strip()
    cap = BUILTIN_TOOL_CAPABILITIES.get(key) or _REGISTERED_TOOL_CAPABILITIES.get(key)
    return _with_overrides(cap) if cap else None


def register_tool_capability(capability: ToolCapability) -> ToolCapability:
    """Register a user/admin-provided Creator tool capability in process memory."""
    if not capability.name:
        raise ValueError("registered tool capability name is required")
    _REGISTERED_TOOL_CAPABILITIES[capability.name] = capability
    global _RUNTIME_HELPERS_CACHE
    _RUNTIME_HELPERS_CACHE = None
    return capability


def clear_registered_tool_capabilities() -> None:
    _REGISTERED_TOOL_CAPABILITIES.clear()
    global _RUNTIME_HELPERS_CACHE
    _RUNTIME_HELPERS_CACHE = None


def set_tool_capability_override(name: str, *, enabled: bool | None = None, allow_creator_use: bool | None = None) -> ToolCapability | None:
    if name not in BUILTIN_TOOL_CAPABILITIES and name not in _REGISTERED_TOOL_CAPABILITIES:
        return None
    current = dict(_TOOL_OVERRIDES.get(name, {}))
    if enabled is not None:
        current["enabled"] = bool(enabled)
    if allow_creator_use is not None:
        current["allow_creator_use"] = bool(allow_creator_use)
    _TOOL_OVERRIDES[name] = current
    return get_tool_capability(name)


def capabilities_for_role(role: str, *, only_creator_enabled: bool = True) -> tuple[list[str], list[str]]:
    role = (role or "").strip()
    capabilities = list_tool_capabilities()
    if only_creator_enabled:
        capabilities = [
            cap
            for cap in capabilities
            if cap.enabled_by_default and cap.allow_creator_use
        ]
    required = [cap.name for cap in capabilities if role in cap.roles]
    return required, list(_ROLE_FORBIDDEN_CAPABILITIES.get(role, []))


def roles() -> list[str]:
    values = {role for cap in [*BUILTIN_TOOL_CAPABILITIES.values(), *_REGISTERED_TOOL_CAPABILITIES.values()] for role in cap.roles}
    return sorted(values)


def get_script_roles() -> list[str]:
    return [role for role in roles() if role not in RESOURCE_ROLES]


def is_resource_role(role: str) -> bool:
    return (role or "").strip() in RESOURCE_ROLES


def is_script_role(role: str) -> bool:
    return (role or "").strip() in set(get_script_roles())


def role_regex() -> str:
    return "|".join(re.escape(role) for role in sorted(roles(), key=len, reverse=True))


def get_role_pattern() -> str:
    return role_regex()


def validate_capability_names(names: list[str]) -> list[str]:
    known = set(BUILTIN_TOOL_CAPABILITIES) | set(_REGISTERED_TOOL_CAPABILITIES)
    return [name for name in names if name not in known]



def build_tool_manifest_draft(description: dict[str, Any]) -> dict[str, Any]:
    '''Deterministically draft a complete function-level manifest from NL form fields.'''
    name = _slug(str(description.get("tool_name") or description.get("name") or description.get("display_name") or "custom_tool"))
    display_name = str(description.get("display_name") or description.get("tool_name") or name.replace("_", " ").title())
    tool_type = str(description.get("tool_type") or "python_helper")
    output_generates_file = bool(description.get("generates_file"))
    safety_level = "high" if description.get("high_risk") else ("medium" if description.get("needs_external_network") or description.get("needs_secret") else "low")
    usage_policy = "helper_required" if safety_level == "high" else "helper_preferred"
    roles = [str(item) for item in description.get("allowed_roles") or [] if item] or ["generic_script", "composite_generator"]
    capability = _slug(str(description.get("capability") or name))
    input_schema = description.get("input_schema") if isinstance(description.get("input_schema"), dict) else {
        "payload": {"type": "object", "required": True, "description": str(description.get("input_description") or "Tool input payload.")}
    }
    output_schema = description.get("output_schema") if isinstance(description.get("output_schema"), dict) else {
        "result": {"type": "object", "description": str(description.get("output_description") or "Tool result.")}
    }
    if output_generates_file:
        output_schema.setdefault("file_paths", {"type": "array[string]", "description": "Generated files under OUTPUT_DIR."})
        output_schema.setdefault("file_outputs", {"type": "array[object]", "description": "Platform downloadable file metadata."})
    adapter_import = f"backend.services.runtime_tools.custom_tools.{name}"
    return {
        "name": name, "display_name": display_name, "category": str(description.get("category") or ("document" if output_generates_file else "custom")),
        "capability": capability, "tool_type": tool_type if tool_type in _ALLOWED_TOOL_TYPES else "custom_adapter", "usage_policy": usage_policy,
        "allowed_roles": roles, "roles": roles, "required_capabilities": [capability],
        "required_env": [str(item) for item in description.get("required_env") or [] if item],
        "required_secrets": [str(item) for item in description.get("required_secrets") or [] if item] + (["TOOL_API_KEY"] if description.get("needs_secret") else []),
        "dependencies": [str(item) for item in description.get("dependencies") or [] if item], "safety_level": safety_level,
        "enabled": False, "approval_status": "draft", "test_status": "untested", "adapter_path": f"backend/services/runtime_tools/custom_tools/{name}.py",
        "version": "0.1.0",
        "functions": [{
            "function_name": name, "import_path": adapter_import,
            "short_description": str(description.get("short_description") or description.get("purpose") or description.get("description") or display_name),
            "when_to_use": str(description.get("when_to_use") or f"Use when a Creator script needs {display_name}."),
            "signature": f"{name}(payload: dict) -> dict", "input_schema": input_schema, "output_schema": output_schema,
            "return_contract": "Returns a dict conforming to output_schema. If file_outputs/file_paths are returned, paths must exist under OUTPUT_DIR.",
            "example_call": f"from {adapter_import} import {name}\nresult = {name}(payload)\nreturn result",
            "example_stdout": "return result", "example_return": "{...output_schema fields...}",
            "common_mistakes": ["Do not guess parameter names; follow the signature and input_schema.", "Do not print or return secret values.", "Do not write files outside OUTPUT_DIR."],
            "trial_mode_behavior": str(description.get("trial_mode_behavior") or "When SKILL_TRIAL_RUN=1, return a minimal deterministic mock that still satisfies output_schema."),
            "safety_notes": ["Validate paths before reading or writing.", "Declare every env var, secret, network host, and side effect in the manifest."],
            "required_env": [str(item) for item in description.get("required_env") or [] if item], "required_secrets": [str(item) for item in description.get("required_secrets") or [] if item],
            "usage_policy": usage_policy, "allowed_roles": roles, "required_capabilities": [capability],
            "forbidden_imports": sorted(_DANGEROUS_IMPORTS), "forbidden_side_effects": ["write outside OUTPUT_DIR", "leak secrets", "undeclared network access"],
        }],
        "snippets": [{
            "id": f"{name}.minimal_usage", "title": f"Use {display_name}", "kind": "minimal_usage",
            "applies_to": {"roles": roles, "capabilities": [capability], "failure_layers": ["helper_call_failed", "final_platform_output_value_invalid", "artifact_missing"]},
            "description": str(description.get("when_to_use") or f"Use when a Creator script needs {display_name}."),
            "code": f"from {adapter_import} import {name}\n\npayload = {{...}}\nresult = {name}(payload)\nreturn result",
            "expected_input_shape": input_schema, "expected_output_shape": output_schema,
            "return_rule": "Return the adapter result directly when it already matches output_schema; do not wrap it in another field.",
            "anti_patterns": ["Do not guess parameter names; pass a payload dict unless the signature says otherwise.", "Do not print or return secret values.", "Do not write files outside OUTPUT_DIR."],
            "requires": [capability], "usage_policy": usage_policy, "priority": 80,
        }],
    }


def generate_adapter_code(manifest: dict[str, Any]) -> str:
    cap = _capability_from_dict(manifest)
    fn = cap.functions[0] if cap.functions else _function_from_dict(build_tool_manifest_draft(manifest)["functions"][0])
    output_keys = list((fn.output_schema or {}).keys()) or ["result"]
    input_keys = list((fn.input_schema or {}).keys())
    return f"""# Generated adapter for registered Creator tool: {cap.name}.

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


OUTPUT_KEYS = {output_keys!r}
INPUT_KEYS = {input_keys!r}


def _output_dir() -> Path:
    root = Path(os.environ.get(\"OUTPUT_DIR\", \"outputs\")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_filename(value: str, default: str = \"result\") -> str:
    cleaned = \"\".join(ch if ch.isalnum() or ch in (\"-\", \"_\", \".\") else \"_\" for ch in value.strip())
    return (cleaned or default)[:120]


def _build_file(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    output_dir = _output_dir()
    title = str(payload.get(\"title\") or payload.get(\"name\") or \"result\")
    suffix = str(payload.get(\"extension\") or payload.get(\"suffix\") or \"txt\").lstrip(\".\") or \"txt\"
    if suffix not in {{\"txt\", \"md\", \"json\", \"csv\", \"html\"}}:
        suffix = \"txt\"
    path = (output_dir / f\"{{_safe_filename(title)}}.{{suffix}}\").resolve()
    if output_dir not in path.parents and path != output_dir:
        raise ValueError(\"generated file path must stay under OUTPUT_DIR\")
    content = payload.get(\"content\") or payload.get(\"markdown\") or payload.get(\"text\") or json.dumps(payload, ensure_ascii=False, indent=2)
    path.write_text(str(content), encoding=\"utf-8\")
    return str(path), {{\"path\": str(path), \"mime_type\": \"text/plain\", \"label\": title}}


def run(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    \"\"\"{fn.short_description}\"\"\"
    payload = dict(payload or {{}})
    if os.getenv(\"SKILL_TRIAL_RUN\") == \"1\":
        mock_result: dict[str, Any] = {{\"result\": {{\"ok\": True, \"trial_run\": True, \"payload_keys\": sorted(payload.keys())}}}}
        for key in OUTPUT_KEYS:
            mock_result.setdefault(key, [] if key.endswith(\"s\") else {{\"ok\": True, \"trial_run\": True}})
        return mock_result
    result: dict[str, Any] = {{\"result\": {{\"ok\": True, \"payload_keys\": sorted(payload.keys())}}}}
    wants_file = any(key in OUTPUT_KEYS for key in (\"file_paths\", \"file_outputs\", \"path\", \"output_path\"))
    if wants_file:
        path, file_output = _build_file(payload)
        result.update({{\"path\": path, \"output_path\": path, \"file_paths\": [path], \"file_outputs\": [file_output]}})
    for key in OUTPUT_KEYS:
        if key in result:
            continue
        if \"path\" in key:
            path, _file_output = _build_file(payload)
            result[key] = path
        elif key.endswith(\"s\"):
            result[key] = []
        else:
            result[key] = {{\"ok\": True}}
    return result


def {fn.function_name}(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return run(payload)


def main() -> None:
    raw = sys.stdin.read().strip() or \"{{}}\"
    payload = json.loads(raw)
    print(json.dumps(run(payload), ensure_ascii=False))


if __name__ == \"__main__\":
    main()
"""

def _manifest_errors(manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    try:
        cap = _capability_from_dict(manifest)
    except Exception as exc:
        return [f"manifest cannot be parsed: {exc}"]
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", cap.name or ""):
        errors.append("name must be a valid Python identifier-like slug")
    if cap.tool_type not in _ALLOWED_TOOL_TYPES:
        errors.append(f"tool_type must be one of {sorted(_ALLOWED_TOOL_TYPES)}")
    if cap.usage_policy not in _ALLOWED_USAGE_POLICIES:
        errors.append("usage_policy is invalid")
    if cap.safety_level not in {"low", "medium", "high", "standard"}:
        errors.append("safety_level must be low/medium/high")
    if not cap.functions:
        errors.append("at least one function manifest is required")
    for fn in cap.functions:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", fn.function_name or ""):
            errors.append(f"invalid function_name: {fn.function_name!r}")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", fn.import_path or ""):
            errors.append(f"invalid import_path for {fn.function_name}")
        if not fn.signature or "(" not in fn.signature or ")" not in fn.signature:
            errors.append(f"signature is not parseable for {fn.function_name}")
        if not isinstance(fn.input_schema, dict) or not fn.input_schema:
            errors.append(f"input_schema is required for {fn.function_name}")
        if not isinstance(fn.output_schema, dict) or not fn.output_schema:
            errors.append(f"output_schema is required for {fn.function_name}")
    for snippet in snippets_for_tool(cap):
        result = validate_tool_snippet(cap, snippet)
        errors.extend(f"snippet {snippet.id}: {err}" for err in result["errors"])
    if set(cap.required_capabilities) & _HIGH_RISK_CAPABILITIES and cap.approval_status not in {"approved", "validated"}:
        errors.append("high-risk tools must be validated and admin-approved before enabling")
    return errors


def _code_security_errors(code: str) -> list[str]:
    errors: list[str] = []
    try:
        tree = ast.parse(code or "")
    except SyntaxError as exc:
        return [f"adapter code syntax error: {exc}"]
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name.split(".")[0] for alias in node.names]
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module.split(".")[0])
            for name in names:
                if name in _DANGEROUS_IMPORTS:
                    errors.append(f"dangerous import is forbidden: {name}")
        if isinstance(node, ast.Call):
            func = node.func
            called = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else "")
            if called in _DANGEROUS_CALLS:
                errors.append(f"dangerous call requires a controlled helper: {called}")
    if re.search(r"(?:sk-|AKIA|-----BEGIN [A-Z ]*PRIVATE KEY-----)[A-Za-z0-9_\-+/=]{8,}", code or ""):
        errors.append("adapter appears to contain a hard-coded secret")
    return sorted(set(errors))


def _safe_adapter_module_path(cap: ToolCapability) -> Path:
    """Return the only allowed persistent adapter path for a custom tool."""
    return (CUSTOM_TOOL_ADAPTER_DIR / f"{_slug(cap.name)}.py").resolve()


def _adapter_module_path(cap: ToolCapability) -> Path:
    """Resolve persisted adapters without trusting arbitrary manifest paths."""
    return _safe_adapter_module_path(cap)


def safe_adapter_path_for_manifest(manifest: dict[str, Any]) -> str:
    """Return the normalized repository-relative adapter path for a manifest."""
    cap = _capability_from_dict(manifest)
    path = _safe_adapter_module_path(cap)
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def normalize_tool_manifest_adapter_path(manifest: dict[str, Any]) -> dict[str, Any]:
    """Force registered custom adapters into CUSTOM_TOOL_ADAPTER_DIR."""
    payload = dict(manifest or {})
    cap = _capability_from_dict(payload)
    payload["adapter_path"] = safe_adapter_path_for_manifest(payload)
    adapter_import = f"backend.services.runtime_tools.custom_tools.{_slug(cap.name)}"
    functions = []
    for item in payload.get("functions") or []:
        if isinstance(item, dict):
            fn = dict(item)
            fn["import_path"] = adapter_import
            functions.append(fn)
    if functions:
        payload["functions"] = functions
    return payload


def write_registered_adapter(manifest: dict[str, Any], adapter_code: str | None) -> dict[str, Any]:
    """Persist confirmed adapter code and return a path-normalized manifest."""
    payload = normalize_tool_manifest_adapter_path(manifest)
    if adapter_code:
        cap = _capability_from_dict(payload)
        path = _safe_adapter_module_path(cap)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(adapter_code, encoding="utf-8")
    return payload


def validate_tool_manifest(manifest: dict[str, Any], *, adapter_code: str | None = None, sample_input: dict[str, Any] | None = None, dynamic: bool = True) -> dict[str, Any]:
    errors = _manifest_errors(manifest)
    warnings: list[str] = []
    cap = _capability_from_dict(manifest) if not errors else None
    if adapter_code:
        errors.extend(_code_security_errors(adapter_code))
    dynamic_result: dict[str, Any] = {"skipped": not dynamic}
    if cap and dynamic and not errors:
        temp_code_dir: tempfile.TemporaryDirectory[str] | None = None
        if adapter_code:
            temp_code_dir = tempfile.TemporaryDirectory(prefix="creator_tool_validate_")
            path = Path(temp_code_dir.name) / f"{_slug(cap.name)}.py"
            path.write_text(adapter_code, encoding="utf-8")
        else:
            path = _adapter_module_path(cap)
        if not path.exists():
            errors.append(f"adapter file does not exist: {path}")
        else:
            try:
                spec = importlib.util.spec_from_file_location(f"_custom_tool_validation_{cap.name}", path)
                if spec is None or spec.loader is None:
                    raise ImportError("could not create import spec")
                module = importlib.util.module_from_spec(spec)
                old_trial = os.environ.get("SKILL_TRIAL_RUN")
                old_output_dir = os.environ.get("OUTPUT_DIR")
                os.environ["SKILL_TRIAL_RUN"] = "1"
                with tempfile.TemporaryDirectory(prefix="creator_tool_trial_") as trial_dir:
                    os.environ["OUTPUT_DIR"] = trial_dir
                    trial_root = Path(trial_dir).resolve()
                    try:
                        spec.loader.exec_module(module)
                        fn = cap.functions[0]
                        target = getattr(module, fn.function_name)
                        if not callable(target):
                            raise TypeError(f"{fn.function_name} is not callable")
                        payload = sample_input or {}
                        try:
                            value = target(payload)
                        except TypeError:
                            value = target(**payload)
                    finally:
                        if old_trial is None:
                            os.environ.pop("SKILL_TRIAL_RUN", None)
                        else:
                            os.environ["SKILL_TRIAL_RUN"] = old_trial
                        if old_output_dir is None:
                            os.environ.pop("OUTPUT_DIR", None)
                        else:
                            os.environ["OUTPUT_DIR"] = old_output_dir
                    if not isinstance(value, dict):
                        errors.append("dynamic trial must return a dict")
                        value = {}
                    file_values: list[str] = []
                    for key in ("path", "output_path"):
                        if isinstance(value.get(key), str):
                            file_values.append(value[key])
                    for key in ("file_paths", "paths"):
                        if isinstance(value.get(key), list):
                            file_values.extend(str(item) for item in value[key] if isinstance(item, str))
                    for item in value.get("file_outputs", []) if isinstance(value.get("file_outputs"), list) else []:
                        if isinstance(item, dict) and isinstance(item.get("path"), str):
                            file_values.append(item["path"])
                    for file_path in file_values:
                        resolved = Path(file_path).resolve()
                        if trial_root not in resolved.parents and resolved != trial_root:
                            errors.append(f"dynamic trial returned file outside OUTPUT_DIR: {file_path}")
                        elif not resolved.exists():
                            errors.append(f"dynamic trial returned missing file path: {file_path}")
                expected = set((cap.functions[0].output_schema or {}).keys())
                missing = [key for key in expected if key not in value]
                if missing:
                    warnings.append(f"dynamic trial did not return declared optional/expected fields: {', '.join(missing)}")
                dynamic_result = {"skipped": False, "return_keys": sorted(value.keys())}
            except Exception as exc:
                errors.append(f"dynamic trial failed: {exc}")
            finally:
                if temp_code_dir is not None:
                    temp_code_dir.cleanup()
    snippet_validations = [validate_tool_snippet(cap, snippet) for snippet in snippets_for_tool(cap)] if cap else []
    success = not errors
    return {
        "success": success,
        "status": "validated" if success else "failed",
        "errors": errors,
        "warnings": warnings,
        "dynamic_trial": dynamic_result,
        "tool_card_preview": function_cards_for_tool(cap) if cap else [],
        "snippet_preview": [format_tool_snippet(cap, snippet) for snippet in snippets_for_tool(cap)] if cap else [],
        "snippet_validations": snippet_validations,
    }


def _json_from_model_text(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    match = re.search(r"\{.*\}", raw, re.S)
    if match:
        raw = match.group(0)
    try:
        value = json.loads(raw)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


async def _complete_author_model(task: str, messages: list[dict[str, str]], *, reason: str) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
    """Call the routed model with a short authoring timeout and JSON parsing."""
    try:
        from .llm_proxy import complete_chat_once
        from .model_router import route_model

        route = route_model(task, reason=reason)
        text = await asyncio.wait_for(complete_chat_once(messages, route.model), timeout=float(os.environ.get(f"TOOL_AUTHOR_{task.upper()}_TIMEOUT_SECONDS", os.environ.get("TOOL_AUTHOR_LLM_TIMEOUT_SECONDS", "120"))))
        parsed = _json_from_model_text(text)
        if task == "code" and not parsed and (text or "").strip():
            parsed = {"code": _strip_code_fence(text)}
        return parsed, route.ack(), None
    except Exception as exc:
        return {}, None, str(exc)


def _infer_schema_from_code(code: str) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    input_keys: set[str] = set()
    output_keys: set[str] = set()
    notes: list[str] = []
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return {}, {}, ["code_block has syntax errors; code_model should repair before trial run"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            target = node.func.value
            if isinstance(target, ast.Name) and target.id in {"payload", "data", "input"} and node.args and isinstance(node.args[0], ast.Constant):
                if isinstance(node.args[0].value, str):
                    input_keys.add(node.args[0].value)
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id in {"payload", "data", "input"}:
            key = node.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                input_keys.add(key.value)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for key in node.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    output_keys.add(key.value)
    input_schema = {key: {"type": "string", "required": False, "description": f"Inferred from code_block field {key}."} for key in sorted(input_keys)}
    output_schema = {key: {"type": "object", "description": f"Inferred from code_block return field {key}."} for key in sorted(output_keys)}
    if input_keys:
        notes.append(f"inferred input fields from code_block: {', '.join(sorted(input_keys))}")
    if output_keys:
        notes.append(f"inferred output fields from code_block: {', '.join(sorted(output_keys))}")
    return input_schema, output_schema, notes


def _first_function_name(code: str) -> str:
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return "run"
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_") and node.name not in {"main"}:
            return node.name
    return "run"


def _has_config_value(config: dict[str, Any], *keys: str) -> bool:
    return any(config.get(key) not in (None, "", {}, []) for key in keys)


def _plan_defaults() -> dict[str, Any]:
    return {
        "needs_clarification": False,
        "questions": [],
        "clarification_questions": [],
        "requires_config": False,
        "config_required_fields": [],
        "config_form_schema": {},
        "suggested_entrypoint": {},
        "additional_fields_schema": [],
        "requires_authorization": False,
        "tool_kind": "unknown",
        "operation": "",
        "resolved_clarifications": [],
        "requires_secret": False,
        "secret_env_suggestions": [],
        "requires_external_network": False,
        "requires_live_test": False,
        "ready_for_live_test": False,
        "ready_for_code_generation": False,
        "missing_fields": [],
        "suggested_config_schema": {},
        "sample_input_schema": {},
        "manifest": {},
        "implementation_plan": "",
        "sample_input": {},
        "risk_notes": [],
        "model_notes": [],
        "requires_authoring_tools": False,
        "authoring_tool_plan": [],
        "authoring_context": {},
    }


def _external_api_missing_fields(config: dict[str, Any], sample_input: dict[str, Any], request: dict[str, Any]) -> list[str]:
    """Return only connection/config panel requirements, not schema/template questions."""
    missing: list[str] = []
    if not _has_config_value(config, "url", "endpoint", "base_url"):
        missing.append("base_url")
    auth_type = str(config.get("auth_type") or config.get("authentication") or "").strip().lower()
    has_secret_ref = bool(_extract_env_refs(config)) or _has_config_value(config, "secret_env", "secret_env_name", "api_key_env", "token_env", "password_env")
    if not auth_type:
        missing.append("auth_type")
    elif auth_type not in {"none", "no_auth", "anonymous"} and not has_secret_ref:
        missing.append("secret_env")
    return missing


def _slug_env_prefix(value: str) -> str:
    prefix = re.sub(r"[^A-Za-z0-9]+", "_", (value or "TOOL")).strip("_").upper()
    return prefix or "TOOL"


def _suggest_external_api_entrypoint(request: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Build a best-effort prefill for the authorization modal without asking the user."""
    text = " ".join(str(request.get(key) or "") for key in ("description", "operation", "tool_name"))
    url_match = re.search(r"https?://[^\s，。；,;]+", text)
    base_url = str(config.get("base_url") or config.get("endpoint") or config.get("url") or (url_match.group(0) if url_match else "")).strip()
    method = str(config.get("method") or "").strip().upper()
    lowered = text.lower()
    if not method:
        method = "POST" if any(token in lowered for token in ("创建", "新增", "提交", "发送", "发布", "上传", "create", "post", "send", "submit")) else "GET"
    auth_type = str(config.get("auth_type") or config.get("authentication") or "").strip().lower()
    has_secret_hint = bool(request.get("needs_secret") or any(token in lowered for token in ("key", "token", "密钥", "令牌", "认证", "鉴权", "bearer")))
    if not auth_type:
        auth_type = "token" if "token" in lowered or "令牌" in lowered or "bearer" in lowered else "api_key" if has_secret_hint else "none"
    env_prefix = _slug_env_prefix(str(request.get("tool_name") or request.get("operation") or request.get("description") or "TOOL"))
    existing_secret = str(config.get("secret_env") or config.get("secret_env_name") or config.get("api_key_env") or config.get("token_env") or "").strip()
    secret_env = existing_secret or (f"{env_prefix}_{'TOKEN' if auth_type == 'token' else 'API_KEY'}" if auth_type not in {"none", "no_auth", "anonymous"} else "")
    confidence = "high" if base_url and (config.get("auth_type") or config.get("authentication") or existing_secret) else "medium" if base_url else "low"
    return {"base_url": base_url, "method": method, "auth_type": auth_type, "secret_env": secret_env, "confidence": confidence}


def _clarification_answer_texts(request_or_answers: Any) -> list[str]:
    answers = request_or_answers.get("clarification_answers") if isinstance(request_or_answers, dict) else request_or_answers
    values: list[str] = []
    for item in answers or []:
        if isinstance(item, dict):
            answer = str(item.get("answer_label") or item.get("answer") or "").strip()
        else:
            answer = str(item or "").strip()
        if answer:
            values.append(answer)
    return values


def _apply_clarification_answers(request: dict[str, Any]) -> dict[str, Any]:
    updated = dict(request or {})
    answers = [item for item in (updated.get("clarification_answers") or []) if isinstance(item, dict) and str(item.get("answer_label") or item.get("answer") or "").strip()]
    capability_answers = _clarification_answer_texts(answers)
    if capability_answers:
        updated["operation"] = capability_answers[-1]
    if answers:
        updated["resolved_clarifications"] = answers
    return updated


def _question_text(question: Any) -> str:
    if isinstance(question, dict):
        return str(question.get("question") or question.get("text") or "").strip()
    return str(question or "").strip()


def _normalize_clarification_question(question: Any) -> dict[str, Any] | None:
    text = _question_text(question)
    if not text:
        return None
    if isinstance(question, dict):
        normalized = {
            "id": str(question.get("id") or _slug(text) or "capability_detail"),
            "type": str(question.get("type") or ("single_choice" if question.get("options") else "short_text")),
            "question": text,
            "required": bool(question.get("required", True)),
        }
        options: list[dict[str, str]] = []
        for idx, option in enumerate(question.get("options") or []):
            if isinstance(option, dict):
                label = str(option.get("label") or option.get("text") or option.get("value") or "").strip()
                value = str(option.get("value") or label or idx).strip()
            else:
                label = str(option or "").strip()
                value = label
            if label:
                options.append({"label": label, "value": value})
        if options:
            normalized["options"] = options
        elif normalized["type"] in {"single_choice", "multi_choice"}:
            normalized["type"] = "short_text"
        return normalized
    return {"id": _slug(text) or "capability_detail", "type": "short_text", "question": text, "required": True}


def _filter_answered_clarification_questions(questions: list[Any], answers: list[Any]) -> list[dict[str, Any]]:
    answered_questions = {
        str(item.get("question") or "").strip()
        for item in answers or []
        if isinstance(item, dict) and str(item.get("answer_label") or item.get("answer") or "").strip()
    }
    answered_ids = {
        str(item.get("id") or item.get("question_id") or "").strip()
        for item in answers or []
        if isinstance(item, dict) and str(item.get("answer_label") or item.get("answer") or "").strip()
    }
    filtered: list[dict[str, Any]] = []
    for question in questions or []:
        normalized = _normalize_clarification_question(question)
        if not normalized:
            continue
        if normalized.get("id") in answered_ids or normalized.get("question") in answered_questions:
            continue
        filtered.append(normalized)
    return filtered


def _needs_capability_clarification(request: dict[str, Any]) -> bool:
    if _clarification_answer_texts(request):
        return False
    text = " ".join(str(request.get(key) or "") for key in ("description", "operation", "input_description", "output_description")).strip().lower()
    if not text:
        return True
    vague_phrases = ("调用接口", "连接接口", "外部 api", "某个api", "某个 api", "api tool", "http tool", "小工具")
    has_vague_api_goal = any(phrase in text for phrase in vague_phrases)
    action_tokens = ("查询", "创建", "更新", "删除", "同步", "发送", "发布", "下载", "上传", "检索", "搜索", "分析", "转换", "生成", "通知", "query", "create", "update", "delete", "sync", "send", "search", "fetch")
    object_tokens = ("数据", "订单", "用户", "消息", "文件", "报告", "记录", "天气", "价格", "库存", "邮件", "短信", "result", "record", "message", "file")
    has_action = any(token in text for token in action_tokens)
    has_object = any(token in text for token in object_tokens)
    return has_vague_api_goal and not (has_action and has_object)


def _external_api_clarification_questions(request: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    """Ask only about real capability ambiguity; connection/key fields belong to config_form_schema."""
    questions: list[dict[str, Any]] = []
    if _needs_capability_clarification(request):
        questions.append({"id": "operation_detail", "type": "short_text", "question": "请用一句话补充这个工具要完成的具体能力。", "required": True})
    return questions[:3]


def _infer_tool_kind(request: dict[str, Any]) -> str:
    explicit = str(request.get("tool_kind") or "").strip()
    if explicit:
        return explicit
    text = " ".join(str(request.get(key) or "") for key in ("description", "tool_type", "operation")).lower()
    if any(token in text for token in ("api", "http", "https://", "接口", "endpoint", "base_url")) or request.get("needs_external_network"):
        return "external_api"
    if request.get("generates_file"):
        return "file_generator"
    if any(token in text for token in ("transform", "转换", "normalize", "清洗")):
        return "data_transform"
    if request.get("code_block"):
        return "local_helper"
    return "unknown"


def _author_fallback_plan(request: dict[str, Any]) -> dict[str, Any]:
    code_block = str(request.get("code_block") or "")
    code_input_schema, code_output_schema, notes = _infer_schema_from_code(code_block) if code_block.strip() else ({}, {}, [])
    merged = dict(request)
    config = request.get("config") if isinstance(request.get("config"), dict) else {}
    sample = request.get("sample_input") if isinstance(request.get("sample_input"), dict) and request.get("sample_input") else {}
    if code_input_schema and not merged.get("input_schema"):
        merged["input_schema"] = code_input_schema
    if code_output_schema and not merged.get("output_schema"):
        merged["output_schema"] = code_output_schema
    tool_kind = _infer_tool_kind(request)
    requires_network = tool_kind == "external_api" or bool(request.get("needs_external_network"))
    requires_secret = bool(request.get("needs_secret") or _extract_env_refs(config) or config.get("secret_env") or config.get("secret_env_name"))
    missing_fields: list[str] = []
    questions: list[Any] = []
    if tool_kind == "external_api":
        missing_fields = _external_api_missing_fields(config, sample, request)
        questions = _external_api_clarification_questions(request, config)
    elif tool_kind == "unknown" and not (request.get("tool_name") and (request.get("description") or code_block.strip())):
        missing_fields = ["tool_name", "description or code_block"]
        questions = [{"id": "operation_detail", "type": "short_text", "question": "请用一句话补充这个工具要完成的具体能力。", "required": True}]

    if request.get("manifest"):
        manifest = dict(request["manifest"])
    elif tool_kind == "external_api" and missing_fields:
        manifest = {}
    else:
        manifest_seed = dict(merged)
        if tool_kind == "external_api":
            manifest_seed["needs_external_network"] = True
            if requires_secret:
                refs = _extract_env_refs(config)
                manifest_seed["required_secrets"] = sorted(refs) or [str(config.get("secret_env") or config.get("secret_env_name") or "TOOL_API_KEY")]
            manifest_seed["tool_type"] = "custom_adapter"
            manifest_seed["input_schema"] = request.get("input_schema") or {"payload": {"type": "object", "required": True, "description": "Fields used to render the confirmed request templates."}}
            manifest_seed["output_schema"] = request.get("output_schema") or {"result": {"type": "object", "description": str(request.get("output_description") or "Normalized external API response.")}}
        manifest = build_tool_manifest_draft(manifest_seed)
    if not sample and tool_kind != "external_api":
        sample = {key: "demo" for key in (code_input_schema or {"payload": {}}).keys()} or {"payload": {}}
    ready_live = tool_kind == "external_api" and not _external_api_missing_fields(config, sample, {**request, "allow_external_network": True})
    live_success = bool((request.get("live_test_result") or {}).get("success"))
    authoring_tool_plan: list[dict[str, Any]] = []
    if tool_kind == "external_api" and missing_fields:
        authoring_tool_plan.append({"tool_name": "authoring_config_collector", "reason": "需要通过授权弹窗保存连接地址、认证方式和 env/secret 引用", "input": {"config_required_fields": missing_fields, "config": config, "sample_input": sample}})
    elif tool_kind == "external_api" and ready_live and not live_success and not request.get("skip_live_test"):
        authoring_tool_plan.append({"tool_name": "authoring_live_test", "reason": "需要在生成 adapter 前确认配置、凭据和 sample input 可用", "input": {"config": config, "sample_input": sample}})
    if code_block.strip():
        authoring_tool_plan.append({"tool_name": "authoring_schema_infer", "reason": "从现有代码推断输入输出 schema", "input": {"code_block": code_block}})
    ready_code = bool(manifest) and not missing_fields and not authoring_tool_plan and (tool_kind != "external_api" or live_success or request.get("skip_live_test") is True)
    return {
        **_plan_defaults(),
        "needs_clarification": bool(questions),
        "questions": questions[:3],
        "clarification_questions": questions[:3],
        "requires_config": tool_kind == "external_api" and bool(missing_fields),
        "config_required_fields": missing_fields,
        "config_form_schema": _external_api_config_schema() if tool_kind == "external_api" else {},
        "suggested_entrypoint": _suggest_external_api_entrypoint(request, config) if tool_kind == "external_api" else {},
        "additional_fields_schema": _external_api_additional_fields_schema() if tool_kind == "external_api" else [],
        "requires_authorization": tool_kind == "external_api" and requires_secret,
        "tool_kind": tool_kind,
        "operation": str(request.get("operation") or request.get("description") or ""),
        "requires_secret": requires_secret,
        "secret_env_suggestions": sorted(_extract_env_refs(config)) or ([str(config.get("secret_env") or config.get("secret_env_name"))] if config.get("secret_env") or config.get("secret_env_name") else []),
        "requires_external_network": requires_network,
        "requires_live_test": tool_kind == "external_api",
        "ready_for_live_test": ready_live,
        "ready_for_code_generation": ready_code,
        "requires_authoring_tools": bool(authoring_tool_plan),
        "authoring_tool_plan": authoring_tool_plan,
        "missing_fields": [],
        "suggested_config_schema": _external_api_config_schema() if tool_kind == "external_api" else {},
        "sample_input_schema": {"type": "object", "description": "Sample payload used for live_test and adapter dynamic validation."},
        "manifest": manifest,
        "implementation_plan": "Use confirmed configuration only. For external APIs, read secrets from environment variables, return a deterministic mock when SKILL_TRIAL_RUN=1, and never put secrets in payload, logs, manifest, adapter, or snippet." if tool_kind == "external_api" else ("Normalize existing code_block while preserving business logic." if code_block.strip() else "Generate a complete Python adapter from the manifest."),
        "sample_input": sample,
        "risk_notes": notes,
        "model_notes": ["deterministic fallback planner used", *notes],
    }


def _safe_clarification_questions(questions: list[Any], fallback: list[Any]) -> list[dict[str, Any]]:
    banned = ("headers", "body", "query", "schema", "expected output", "输出字段", "输入输出", "method", "模板", "sample input", "服务地址", "连接地址", "endpoint", "密钥", "token", "认证", "auth", "外部网络", "连接测试")
    safe: list[dict[str, Any]] = []
    for item in questions or []:
        normalized = _normalize_clarification_question(item)
        if not normalized:
            continue
        text = normalized.get("question", "")
        if any(token.lower() in text.lower() for token in banned):
            continue
        safe.append(normalized)
    if not safe:
        for item in fallback or []:
            normalized = _normalize_clarification_question(item)
            if not normalized:
                continue
            text = normalized.get("question", "")
            if not any(token.lower() in text.lower() for token in banned):
                safe.append(normalized)
    return safe[:3]


def _normalize_author_plan(plan: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    normalized = {**_plan_defaults(), **(plan if isinstance(plan, dict) else {})}
    fallback = _author_fallback_plan(request)
    if not isinstance(normalized.get("questions"), list):
        normalized["questions"] = []
    if not isinstance(normalized.get("clarification_questions"), list):
        normalized["clarification_questions"] = normalized.get("questions") or []
    if not isinstance(normalized.get("config_required_fields"), list):
        normalized["config_required_fields"] = []
    if not isinstance(normalized.get("config_form_schema"), dict):
        normalized["config_form_schema"] = {}
    if not isinstance(normalized.get("suggested_entrypoint"), dict):
        normalized["suggested_entrypoint"] = {}
    if not isinstance(normalized.get("additional_fields_schema"), list):
        normalized["additional_fields_schema"] = []
    if not isinstance(normalized.get("missing_fields"), list):
        normalized["missing_fields"] = []
    if not isinstance(normalized.get("manifest"), dict):
        normalized["manifest"] = {}
    if not isinstance(normalized.get("authoring_tool_plan"), list):
        normalized["authoring_tool_plan"] = []
    if not normalized.get("tool_kind") or normalized.get("tool_kind") == "unknown":
        normalized["tool_kind"] = fallback.get("tool_kind", "unknown")
    if normalized["tool_kind"] == "external_api":
        config = request.get("config") if isinstance(request.get("config"), dict) else {}
        sample = request.get("sample_input") if isinstance(request.get("sample_input"), dict) else {}
        missing = _external_api_missing_fields(config, sample, request)
        normalized["requires_external_network"] = True
        normalized["requires_live_test"] = True
        normalized["requires_config"] = bool(missing)
        normalized["config_required_fields"] = missing
        normalized["config_form_schema"] = _external_api_config_schema()
        normalized["suggested_entrypoint"] = {**_suggest_external_api_entrypoint(request, config), **(normalized.get("suggested_entrypoint") or {})}
        normalized["additional_fields_schema"] = normalized.get("additional_fields_schema") or _external_api_additional_fields_schema()
        normalized["requires_authorization"] = bool(normalized.get("requires_secret") or any(field == "secret_env" for field in missing))
        normalized["ready_for_live_test"] = not _external_api_missing_fields(config, sample, {**request, "allow_external_network": True})
        normalized["missing_fields"] = []
        safe_questions = _safe_clarification_questions((normalized.get("clarification_questions") or normalized.get("questions") or []), fallback.get("clarification_questions") or fallback.get("questions") or [])
        safe_questions = _filter_answered_clarification_questions(safe_questions, request.get("clarification_answers") or [])
        normalized["clarification_questions"] = safe_questions
        normalized["questions"] = safe_questions
        if missing:
            normalized["ready_for_code_generation"] = False
            if not normalized.get("authoring_tool_plan"):
                normalized["authoring_tool_plan"] = fallback.get("authoring_tool_plan") or []
        elif not normalized.get("manifest"):
            normalized["manifest"] = fallback.get("manifest") or {}
        live_success = bool((request.get("live_test_result") or {}).get("success"))
        if not normalized.get("authoring_tool_plan") and not live_success and normalized.get("ready_for_live_test") and not request.get("skip_live_test"):
            normalized["authoring_tool_plan"] = [{"tool_name": "authoring_live_test", "reason": "需要在生成 adapter 前确认配置、凭据和 sample input 可用", "input": {"config": config, "sample_input": sample}}]
        normalized["ready_for_code_generation"] = bool(normalized.get("ready_for_code_generation")) and (live_success or request.get("skip_live_test") is True)
    elif not normalized.get("manifest"):
        normalized["manifest"] = fallback.get("manifest") or {}
    if request.get("operation"):
        normalized["operation"] = request.get("operation")
    if request.get("resolved_clarifications"):
        normalized["resolved_clarifications"] = request.get("resolved_clarifications")
    normalized["requires_authoring_tools"] = bool(normalized.get("authoring_tool_plan")) or bool(normalized.get("requires_authoring_tools"))
    if normalized["requires_authoring_tools"]:
        normalized["ready_for_code_generation"] = False
    normalized["clarification_questions"] = _safe_clarification_questions(normalized.get("clarification_questions") or normalized.get("questions") or [], fallback.get("clarification_questions") or fallback.get("questions") or [])
    normalized["clarification_questions"] = _filter_answered_clarification_questions(normalized["clarification_questions"], request.get("clarification_answers") or [])
    normalized["questions"] = normalized["clarification_questions"]
    normalized["needs_clarification"] = bool(normalized["clarification_questions"])
    if (
        request.get("stage") == "draft"
        and not normalized.get("needs_clarification")
        and normalized.get("manifest")
        and normalized.get("tool_kind") != "external_api"
        and not normalized.get("ready_for_code_generation")
        and "ready_for_code_generation" not in (plan or {})
    ):
        normalized["ready_for_code_generation"] = True
    return normalized


def _external_api_config_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "ui": "authorization_modal",
        "required": ["base_url", "auth_type"],
        "properties": {
            "base_url": {"type": "string", "title": "连接地址 / endpoint / IP", "placeholder": "https://api.example.com"},
            "auth_type": {"type": "string", "title": "认证方式", "enum": ["none", "api_key", "token", "basic", "custom"], "default": "none"},
            "secret_env": {"type": "string", "title": "密钥名称", "description": "自动生成 env 名，可修改；代码只使用 os.getenv 引用。"},
            "secret_value": {"type": "string", "title": "密钥值", "format": "password", "writeOnly": True},
            "extra": {"type": "object", "title": "其他字段", "additionalProperties": {"type": "string"}},
            "sample_input": {"type": "object", "title": "sample input（可选）"},
        },
    }



def _external_api_additional_fields_schema() -> list[dict[str, Any]]:
    return [
        {"key": "", "value": "", "sensitive": False, "description": "Optional extra connection field added by the user."}
    ]


def _strip_code_fence(text: str) -> str:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:python)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    return raw


def _normalize_existing_code_fallback(code: str, manifest: dict[str, Any]) -> str:
    cap = _capability_from_dict(manifest)
    fn = cap.functions[0] if cap.functions else _function_from_dict(build_tool_manifest_draft(manifest)["functions"][0])
    code = _strip_code_fence(code)
    if not code.strip():
        return generate_adapter_code(manifest)
    try:
        tree = ast.parse(code)
        has_run = any(isinstance(node, ast.FunctionDef) and node.name == "run" for node in tree.body)
        has_main = any(isinstance(node, ast.FunctionDef) and node.name == "main" for node in tree.body)
        first_fn = _first_function_name(code)
    except SyntaxError:
        return generate_adapter_code(manifest)
    suffix = ""
    if not has_run:
        suffix += f'''


def run(payload: dict | None = None) -> dict:
    payload = dict(payload or {{}})
    value = {first_fn}(payload)
    if isinstance(value, dict):
        return value
    return {{"result": value}}
'''
    if fn.function_name not in {"run", first_fn}:
        suffix += f'''


def {fn.function_name}(payload: dict | None = None) -> dict:
    return run(payload)
'''
    if not has_main:
        suffix += '''


def main() -> None:
    import json
    import sys
    raw = sys.stdin.read().strip() or "{}"
    payload = json.loads(raw)
    print(json.dumps(run(payload), ensure_ascii=False))


if __name__ == "__main__":
    main()
'''
    return code.rstrip() + suffix


def _author_adapter_static_errors(code: str, manifest: dict[str, Any]) -> list[str]:
    errors = _code_security_errors(code)
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return errors
    function_names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    if "run" not in function_names:
        errors.append("adapter must define run(payload: dict) -> dict")
    if "main" not in function_names:
        errors.append("adapter must define a JSON stdin/stdout main() entrypoint")
    try:
        cap = _capability_from_dict(manifest)
        if cap.functions and cap.functions[0].function_name not in function_names:
            errors.append(f"adapter must expose manifest function {cap.functions[0].function_name}")
        tree = ast.parse(code or "")
        imports: set[str] = set()
        env_keys: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "getenv" and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    env_keys.add(node.args[0].value)
                elif node.func.attr == "get" and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    owner = ast.unparse(node.func.value) if hasattr(ast, "unparse") else ""
                    if owner.endswith("environ"):
                        env_keys.add(node.args[0].value)
            elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "environ":
                key = node.slice
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    env_keys.add(key.value)
                else:
                    env_keys.add("<dynamic>")
        undeclared_env_keys = env_keys - {"OUTPUT_DIR", "SKILL_TRIAL_RUN"} - set(cap.required_env) - set(cap.required_secrets)
        if imports & {"requests", "httpx", "urllib"} and cap.safety_level not in {"medium", "high"}:
            errors.append("adapter imports network libraries but manifest does not declare external network access")
        if undeclared_env_keys:
            errors.append("adapter reads undeclared environment variables or secrets: " + ", ".join(sorted(undeclared_env_keys)))
    except Exception:
        pass
    return sorted(set(errors))


def _validate_author_snippet(snippet: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    required = ["id", "title", "description", "code", "return_rule", "usage_policy", "priority"]
    for key in required:
        if snippet.get(key) in (None, ""):
            errors.append(f"snippet {key} is required")
    if not isinstance(snippet.get("anti_patterns", []), list) or not all(isinstance(item, str) for item in snippet.get("anti_patterns", [])):
        errors.append("snippet anti_patterns must be a string array")
    if snippet.get("usage_policy") not in _ALLOWED_USAGE_POLICIES:
        errors.append("snippet usage_policy is invalid")
    try:
        priority = int(snippet.get("priority", 0))
        if priority < 0 or priority > 100:
            warnings.append("snippet priority should be between 0 and 100")
    except Exception:
        errors.append("snippet priority must be numeric")
    try:
        cap = _capability_from_dict({**manifest, "snippets": [snippet]})
        if cap.snippets:
            result = validate_tool_snippet(cap, cap.snippets[0])
            errors.extend(result["errors"])
            warnings.extend(result["warnings"])
    except Exception as exc:
        errors.append(f"snippet cannot be parsed: {exc}")
    return {"success": not errors, "errors": sorted(set(errors)), "warnings": sorted(set(warnings))}


def _fallback_snippet(manifest: dict[str, Any], sample_input: dict[str, Any]) -> dict[str, Any]:
    cap = _capability_from_dict(manifest)
    fn = cap.functions[0]
    return {
        "id": f"{cap.name}.minimal_usage",
        "title": f"Use {cap.display_name}",
        "kind": "minimal_usage",
        "applies_to": {"roles": cap.allowed_roles or cap.roles, "capabilities": cap.required_capabilities or [cap.name], "failure_layers": ["helper_call_failed", "artifact_missing"]},
        "description": fn.when_to_use or fn.short_description,
        "code": f"from {fn.import_path} import {fn.function_name}\n\npayload = {json.dumps(sample_input or {}, ensure_ascii=False, indent=2)}\nresult = {fn.function_name}(payload)\nreturn result",
        "expected_input_shape": fn.input_schema or cap.input_schema,
        "expected_output_shape": fn.output_schema or cap.output_schema,
        "return_rule": "Return the adapter result directly. If it contains generated file paths, pass those exact OUTPUT_DIR paths to downstream skill steps.",
        "anti_patterns": ["Do not call this tool before human-confirming the adapter code.", "Do not write or expect files outside OUTPUT_DIR.", "Do not pass secrets unless the manifest explicitly declares them."],
        "requires": cap.required_capabilities or [cap.name],
        "usage_policy": cap.usage_policy,
        "priority": 80,
    }


_ENV_REF_RE = re.compile(r"\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)\}")
_INPUT_REF_RE = re.compile(r"\$\{(?:input|payload)\.([A-Za-z0-9_.-]+)\}")


def _extract_env_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, str):
        refs.update(_ENV_REF_RE.findall(value))
    elif isinstance(value, dict):
        for item in value.values():
            refs.update(_extract_env_refs(item))
    elif isinstance(value, list):
        for item in value:
            refs.update(_extract_env_refs(item))
    return refs


def _redact_secrets(value: Any, secret_values: set[str] | None = None) -> Any:
    secret_values = {item for item in (secret_values or set()) if item}
    if isinstance(value, str):
        redacted = value
        for secret in secret_values:
            redacted = redacted.replace(secret, "***")
        if _ENV_REF_RE.search(redacted):
            return _ENV_REF_RE.sub("${ENV:***}", redacted)
        return redacted
    if isinstance(value, dict):
        return {key: _redact_secrets(item, secret_values) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_secrets(item, secret_values) for item in value]
    return value


def _get_by_path(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return ""
    return current


def _render_template(value: Any, sample_input: dict[str, Any], secret_values: dict[str, str]) -> Any:
    if isinstance(value, str):
        def env_replace(match: re.Match[str]) -> str:
            name = match.group(1)
            return secret_values.get(name, "")

        def input_replace(match: re.Match[str]) -> str:
            resolved = _get_by_path(sample_input, match.group(1))
            return str(resolved if resolved is not None else "")

        return _INPUT_REF_RE.sub(input_replace, _ENV_REF_RE.sub(env_replace, value))
    if isinstance(value, dict):
        return {key: _render_template(item, sample_input, secret_values) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_render_template(item, sample_input, secret_values) for item in value]
    return value


def _normalized_preview(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        keys = sorted(str(key) for key in data.keys())
        return {"type": "object", "keys": keys[:50], "preview": {key: data[key] for key in list(data.keys())[:10]}}
    if isinstance(data, list):
        return {"type": "array", "length": len(data), "first_item": data[0] if data else None}
    return {"type": type(data).__name__, "value": data}


def live_test_tool(request: dict[str, Any]) -> dict[str, Any]:
    """Perform one generic external API probe without generating or registering code."""
    if request.get("allow_external_network") is not True:
        return {"success": False, "status": "blocked", "errors": ["allow_external_network must be true for live_test"], "preview": None, "normalized_preview": None}
    config = request.get("config") if isinstance(request.get("config"), dict) else {}
    sample_input = request.get("sample_input") if isinstance(request.get("sample_input"), dict) else {}
    env_refs = _extract_env_refs(config)
    missing_env = sorted(name for name in env_refs if not os.environ.get(name))
    if missing_env:
        return {"success": False, "status": "missing_secret", "errors": ["missing required env secret(s): " + ", ".join(missing_env)], "missing_env": missing_env, "preview": None, "normalized_preview": None}
    secret_values = {name: os.environ.get(name, "") for name in env_refs}
    method = str(config.get("method") or "GET").upper()
    url = str(config.get("url") or config.get("endpoint") or "")
    if not url:
        base = str(config.get("base_url") or "").rstrip("/")
        path = str(config.get("path") or "").lstrip("/")
        url = f"{base}/{path}" if base and path else base
    url = _render_template(url, sample_input, secret_values)
    if not url.startswith(("http://", "https://")):
        return {"success": False, "status": "invalid_config", "errors": ["live_test url must start with http:// or https://"], "preview": None, "normalized_preview": None}
    headers = _render_template(config.get("headers_template") or config.get("headers") or {}, sample_input, secret_values)
    query = _render_template(config.get("query_template") or config.get("params_template") or {}, sample_input, secret_values)
    body_template = config.get("json_body_template") if "json_body_template" in config else config.get("body_template", {})
    json_body = _render_template(body_template or {}, sample_input, secret_values)
    if isinstance(query, dict) and query:
        parsed = urllib.parse.urlsplit(url)
        merged_query = urllib.parse.urlencode({**dict(urllib.parse.parse_qsl(parsed.query)), **query}, doseq=True)
        url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, merged_query, parsed.fragment))
    data: bytes | None = None
    if method not in {"GET", "HEAD"}:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers = {**headers, "Content-Type": headers.get("Content-Type") or headers.get("content-type") or "application/json"}
    timeout = float(config.get("timeout_seconds") or os.environ.get("TOOL_AUTHOR_LIVE_TEST_TIMEOUT_SECONDS", "20"))
    req = urllib.request.Request(url=url, data=data, method=method, headers={str(k): str(v) for k, v in (headers or {}).items()})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(int(config.get("max_preview_bytes") or 65536))
            status_code = int(resp.status)
            content_type = resp.headers.get("content-type", "")
    except urllib.error.HTTPError as exc:
        raw = exc.read(65536)
        status_code = int(exc.code)
        content_type = exc.headers.get("content-type", "") if exc.headers else ""
    except Exception as exc:
        return {"success": False, "status": "request_failed", "errors": [str(exc)], "preview": None, "normalized_preview": None}
    text = raw.decode("utf-8", errors="replace")
    try:
        preview: Any = json.loads(text)
    except Exception:
        preview = text[:4000]
    redacted_preview = _redact_secrets(preview, set(secret_values.values()))
    success = 200 <= status_code < 400
    return {
        "success": success,
        "status": "ok" if success else "http_error",
        "status_code": status_code,
        "content_type": content_type,
        "preview": redacted_preview,
        "normalized_preview": _normalized_preview(redacted_preview),
        "request_preview": {"method": method, "url": _redact_secrets(url, set(secret_values.values())), "header_keys": sorted((headers or {}).keys()), "has_body": data is not None},
        "errors": [] if success else [f"HTTP {status_code}"],
    }

AUTHORING_HELPER_NAMES = {
    "authoring_config_collector",
    "authoring_schema_infer",
    "authoring_live_test",
    "authoring_dependency_check",
    "authoring_code_protocol_check",
    "authoring_file_output_check",
}


def _secret_env_name_from_key(key: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", key or "TOOL_SECRET").strip("_").upper()
    if not cleaned:
        cleaned = "TOOL_SECRET"
    if not any(token in cleaned for token in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
        cleaned += "_SECRET"
    return cleaned[:80]


def _sanitize_authoring_config(value: Any, *, parent_key: str = "") -> tuple[Any, set[str]]:
    env_refs: set[str] = set()
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            child, child_refs = _sanitize_authoring_config(item, parent_key=str(key))
            env_refs.update(child_refs)
            sanitized[key] = child
        return sanitized, env_refs
    if isinstance(value, list):
        items = []
        for item in value:
            child, child_refs = _sanitize_authoring_config(item, parent_key=parent_key)
            env_refs.update(child_refs)
            items.append(child)
        return items, env_refs
    if isinstance(value, str):
        refs = _extract_env_refs(value)
        env_refs.update(refs)
        lowered_key = (parent_key or "").lower()
        is_env_reference_field = lowered_key.endswith("_env") or lowered_key in {"secret_env", "secret_env_name", "api_key_env", "token_env", "username_env", "password_env"}
        looks_secret_key = any(token in lowered_key for token in ("api_key", "apikey", "token", "password", "secret", "authorization"))
        if is_env_reference_field:
            return value, env_refs | ({value} if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) else set())
        if looks_secret_key and value and not refs:
            env_name = _secret_env_name_from_key(parent_key)
            env_refs.add(env_name)
            if "authorization" in lowered_key and value.lower().startswith("bearer "):
                return f"Bearer ${{ENV:{env_name}}}", env_refs
            return f"${{ENV:{env_name}}}", env_refs
        return value, env_refs
    return value, env_refs


def _authoring_helper_result_context(context: dict[str, Any], tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    updated = dict(context or {})
    results = dict(updated.get("tool_results") or {})
    # Store only sanitized helper outputs. Secret values are redacted/replaced before this point.
    results[tool_name] = result
    updated["tool_results"] = results
    if result.get("config"):
        updated["config"] = result["config"]
    if result.get("sample_input"):
        updated["sample_input"] = result["sample_input"]
    if result.get("schemas"):
        updated["schemas"] = result["schemas"]
    if result.get("live_test_result"):
        updated["live_test_result"] = result["live_test_result"]
    return updated


def _config_collector_schema(missing_fields: list[str] | None = None) -> dict[str, Any]:
    schema = _external_api_config_schema()
    schema["properties"] = {
        **schema.get("properties", {}),
        "ip": {"type": "string"},
        "port": {"type": "string"},
        "api_key_env": {"type": "string", "description": "Environment variable name only; do not enter the secret value."},
        "token_env": {"type": "string", "description": "Environment variable name only; do not enter the token value."},
        "username_env": {"type": "string", "description": "Optional username env var name."},
        "password_env": {"type": "string", "description": "Optional password env var name."},
        "sample_input": {"type": "object"},
    }
    if missing_fields:
        schema["missing_fields"] = missing_fields
    return schema


def run_authoring_helper(tool_name: str, input: dict[str, Any] | None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one internal Tool Authoring helper and write its output into authoring context."""
    normalized_name = _slug(tool_name)
    cap = get_tool_capability(normalized_name)
    if cap is None or cap.tool_type != "internal_authoring_tool" or normalized_name not in AUTHORING_HELPER_NAMES:
        raise ValueError(f"authoring helper is not allowed: {tool_name}")
    payload = dict(input or {})
    ctx = dict(context or {})
    result: dict[str, Any]
    if normalized_name == "authoring_config_collector":
        config = payload.get("config") if isinstance(payload.get("config"), dict) else ctx.get("config") if isinstance(ctx.get("config"), dict) else {}
        sample_input = payload.get("sample_input") if isinstance(payload.get("sample_input"), dict) else ctx.get("sample_input") if isinstance(ctx.get("sample_input"), dict) else {}
        required_fields = [str(item) for item in payload.get("config_required_fields") or payload.get("missing_fields") or []]
        if required_fields:
            result = {"success": False, "requires_input": True, "schema": _config_collector_schema(required_fields), "config_required_fields": required_fields, "message": "等待用户在授权弹窗保存连接配置。"}
        else:
            saved = save_tool_authoring_config({**ctx, **payload, "config": config, "sample_input": sample_input})
            result = {"success": True, "requires_input": False, "config": saved.get("config") or {}, "sample_input": sample_input, "configured_env": saved.get("configured_env") or [], "configured_secrets": saved.get("configured_secrets") or [], "config_refs": saved.get("config_refs") or {}, "secret_env_suggestions": saved.get("configured_secrets") or [], "message": "配置已保存为 env/secret 引用。"}
    elif normalized_name == "authoring_schema_infer":
        code = str(payload.get("code_block") or ctx.get("code_block") or "")
        input_schema, output_schema, notes = _infer_schema_from_code(code) if code.strip() else ({}, {}, [])
        sample_input = payload.get("sample_input") if isinstance(payload.get("sample_input"), dict) else ctx.get("sample_input") if isinstance(ctx.get("sample_input"), dict) else {}
        if sample_input and not input_schema:
            input_schema = {key: {"type": type(value).__name__, "required": False, "description": "Inferred from sample_input."} for key, value in sample_input.items()}
        expected = payload.get("expected_output_fields") or ((ctx.get("config") or {}).get("expected_output_fields") if isinstance(ctx.get("config"), dict) else [])
        if expected and not output_schema:
            output_schema = {str(key): {"type": "object", "description": "Expected output field confirmed during authoring."} for key in expected}
        result = {"success": True, "schemas": {"input_schema": input_schema, "output_schema": output_schema}, "notes": notes}
    elif normalized_name == "authoring_live_test":
        if payload.get("allow_external_network") is not True and ctx.get("allow_external_network") is not True:
            result = {"success": False, "requires_input": True, "schema": {"type": "object", "required": ["allow_external_network"], "properties": {"allow_external_network": {"type": "boolean", "const": True}}}, "message": "live_test 需要用户确认允许外部网络。"}
        else:
            live_request = {**ctx, **payload, "allow_external_network": True}
            result = {"success": True, "requires_input": False, "live_test_result": live_test_tool(live_request)}
            result["success"] = bool(result["live_test_result"].get("success"))
    elif normalized_name == "authoring_dependency_check":
        dependencies = [str(item) for item in payload.get("dependencies") or ctx.get("dependencies") or []]
        missing = [dep for dep in dependencies if not _dependency_available(dep)]
        result = {"success": not missing, "dependencies": dependencies, "missing_dependencies": missing}
    elif normalized_name == "authoring_code_protocol_check":
        manifest = payload.get("manifest") if isinstance(payload.get("manifest"), dict) else ctx.get("manifest") if isinstance(ctx.get("manifest"), dict) else {}
        adapter_code = str(payload.get("adapter_code") or ctx.get("adapter_code") or "")
        errors = _author_adapter_static_errors(adapter_code, manifest) if adapter_code and manifest else ["adapter_code and manifest are required"]
        result = {"success": not errors, "errors": errors}
    elif normalized_name == "authoring_file_output_check":
        manifest = payload.get("manifest") if isinstance(payload.get("manifest"), dict) else ctx.get("manifest") if isinstance(ctx.get("manifest"), dict) else {}
        output_schema = ((manifest.get("functions") or [{}])[0].get("output_schema") if isinstance(manifest, dict) else {}) or {}
        has_file_contract = any(key in output_schema for key in ("file_paths", "file_outputs", "path", "output_path"))
        result = {"success": has_file_contract, "has_file_contract": has_file_contract, "warnings": [] if has_file_contract else ["file output tools should declare file_paths/file_outputs or path/output_path"]}
    else:
        raise ValueError(f"unknown authoring helper: {tool_name}")
    return {"tool_name": normalized_name, **result, "authoring_context": _authoring_helper_result_context(ctx, normalized_name, result)}


def _run_authoring_tool_plan(plan: dict[str, Any], request: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    context = dict(request.get("authoring_context") or plan.get("authoring_context") or {})
    context.update({
        "config": request.get("config") or context.get("config") or {},
        "sample_input": request.get("sample_input") or context.get("sample_input") or {},
        "allow_external_network": request.get("allow_external_network") is True,
        "manifest": request.get("manifest") or plan.get("manifest") or context.get("manifest") or {},
        "code_block": request.get("code_block") or context.get("code_block") or "",
    })
    results: list[dict[str, Any]] = []
    for item in plan.get("authoring_tool_plan") or []:
        if not isinstance(item, dict):
            continue
        tool_name = str(item.get("tool_name") or "")
        helper_input = item.get("input") if isinstance(item.get("input"), dict) else {}
        result = run_authoring_helper(tool_name, helper_input, context)
        results.append(result)
        context = result.get("authoring_context") or context
        if result.get("requires_input"):
            break
    updated = {**plan, "authoring_context": context, "authoring_tool_results": results, "ready_for_code_generation": False}
    return updated, results


def _author_response_from_plan(plan: dict[str, Any], *, model_notes: list[str], warnings: list[str]) -> dict[str, Any]:
    needs_clarification = bool(plan.get("needs_clarification"))
    status = "needs_clarification" if needs_clarification else "waiting_for_user_input"
    return {
        "needs_clarification": needs_clarification,
        "questions": plan.get("clarification_questions") or plan.get("questions") or [],
        "clarification_questions": plan.get("clarification_questions") or plan.get("questions") or [],
        "requires_config": bool(plan.get("requires_config")),
        "config_required_fields": plan.get("config_required_fields") or [],
        "config_form_schema": plan.get("config_form_schema") or plan.get("suggested_config_schema") or {},
        "suggested_entrypoint": plan.get("suggested_entrypoint") or {},
        "additional_fields_schema": plan.get("additional_fields_schema") or [],
        "requires_authorization": bool(plan.get("requires_authorization")),
        "tool_kind": plan.get("tool_kind") or "unknown",
        "operation": plan.get("operation") or "",
        "resolved_clarifications": plan.get("resolved_clarifications") or [],
        "requires_secret": bool(plan.get("requires_secret")),
        "secret_env_suggestions": plan.get("secret_env_suggestions") or [],
        "requires_external_network": bool(plan.get("requires_external_network")),
        "requires_live_test": bool(plan.get("requires_live_test")),
        "ready_for_live_test": bool(plan.get("ready_for_live_test")),
        "ready_for_code_generation": bool(plan.get("ready_for_code_generation")),
        "requires_authoring_tools": bool(plan.get("requires_authoring_tools")),
        "authoring_tool_plan": plan.get("authoring_tool_plan") or [],
        "authoring_context": plan.get("authoring_context") or {},
        "authoring_tool_results": plan.get("authoring_tool_results") or [],
        "missing_fields": plan.get("missing_fields") or [],
        "suggested_config_schema": plan.get("suggested_config_schema") or {},
        "sample_input_schema": plan.get("sample_input_schema") or {},
        "manifest": plan.get("manifest") or {},
        "adapter_code": "",
        "sample_input": plan.get("sample_input") or {},
        "validation": {"success": False, "status": status, "errors": [], "warnings": plan.get("risk_notes") or []},
        "snippet": None,
        "model_notes": model_notes,
        "warnings": warnings,
        "requires_human_confirmation": True,
    }



async def _run_capability_ambiguity_judge(request: dict[str, Any], model_notes: list[str], warnings: list[str]) -> list[str]:
    """Ask planner_model to catch capability ambiguity that deterministic heuristics may miss."""
    if _clarification_answer_texts(request):
        return []
    judge_payload = {
        key: request.get(key)
        for key in [
            "description",
            "operation",
            "input_description",
            "output_description",
            "tool_kind",
            "needs_external_network",
            "clarification_answers",
        ]
    }
    judge_messages = [
        {
            "role": "system",
            "content": (
                "You are the planner_model capability ambiguity judge for Tool Authoring. "
                "Return strict JSON: {needs_capability_clarification: boolean, question: {id, type, question, options, required}}. "
                "Set needs_capability_clarification=true only when the user's desired business capability or operation is genuinely unclear. "
                "If clarification_answers already contain a non-empty answer that resolves the requested operation, return needs_capability_clarification=false. Do not ask the same question again. "
                "The question must be Chinese, short, structured, and ask only what the tool should do. If you provide options, generate context-specific options for this user request; do not use generic fixed options. "
                "Do not ask for service address, endpoint, IP, key, token, auth method, connection-test permission, method, headers/body/query templates, schemas, sample input, or expected output fields."
            ),
        },
        {"role": "user", "content": json.dumps(judge_payload, ensure_ascii=False)},
    ]
    judge, ack, err = await _complete_author_model("planner", judge_messages, reason="creator_tool_author_capability_ambiguity")
    if ack:
        model_notes.append(f"capability_ambiguity_judge={ack['model']}")
    if err:
        warnings.append(f"capability ambiguity judge unavailable, used deterministic ambiguity heuristic only: {err}")
        return []
    if not bool(judge.get("needs_capability_clarification") or judge.get("capability_ambiguous")):
        return []
    question = judge.get("question") or judge.get("clarification_question") or {"id": "operation_detail", "type": "short_text", "question": "请用一句话补充这个工具要完成的具体能力。", "required": True}
    return _safe_clarification_questions([question], [])

async def _run_planner(request: dict[str, Any], model_notes: list[str], warnings: list[str]) -> dict[str, Any]:
    request = _apply_clarification_answers(request)
    planner_payload = {
        key: request.get(key)
        for key in [
            "description",
            "tool_name",
            "tool_type",
            "code_block",
            "input_description",
            "output_description",
            "manifest",
            "sample_input",
            "allowed_roles",
            "needs_secret",
            "needs_external_network",
            "generates_file",
            "high_risk",
            "clarification_answers",
            "resolved_clarifications",
            "tool_kind",
            "operation",
            "config",
            "live_test_result",
            "allow_external_network",
            "authoring_context",
        ]
    }
    planner_messages = [
        {
            "role": "system",
            "content": (
                "You are planner_model, a generic Tool Authoring flow controller, not a provider-specific code generator. "
                "Return strict JSON with this layered contract where possible: needs_clarification, clarification_questions, requires_config, "
                "suggested_entrypoint, config_required_fields, config_form_schema, additional_fields_schema, requires_authorization, tool_kind "
                "(local_helper|external_api|file_generator|data_transform|unknown), operation, requires_secret, "
                "secret_env_suggestions, requires_external_network, requires_live_test, ready_for_live_test, "
                "ready_for_code_generation, requires_authoring_tools, authoring_tool_plan, suggested_config_schema, sample_input_schema, manifest, "
                "implementation_plan. clarification_questions must be structured objects with id/type/question/options/required, Chinese, preferably 1-3 questions and never more than 5, and only ask about real capability ambiguity. Generate options dynamically from the user request when a choice is useful; do not use hardcoded generic options. "
                "Do not ask whether the user has a service address, key, token, account, auth method, or connection-test permission as clarification questions; infer and prefill suggested_entrypoint with base_url, method, auth_type, secret_env, and confidence when possible, using empty base_url with low confidence if unknown. Put connection/key/IP/auth/test-permission fields only in config_form_schema/config_required_fields so the UI can show an authorization modal. "
                "Never ask users for method, headers/body/query templates, input/output schema, sample input, or expected output fields as clarification questions; infer those later from the goal, suggested_entrypoint, saved config, and test result. "
                "If helper tools are needed, plan only internal_authoring_tool names such as authoring_config_collector, authoring_schema_infer, authoring_live_test, authoring_dependency_check, authoring_code_protocol_check, or authoring_file_output_check. Do not invent provider details."
            ),
        },
        {"role": "user", "content": json.dumps(planner_payload, ensure_ascii=False)},
    ]
    model_plan, ack, err = await _complete_author_model("planner", planner_messages, reason="creator_tool_author_plan")
    if ack:
        model_notes.append(f"planner_model={ack['model']}")
    if err:
        warnings.append(f"planner_model unavailable, used deterministic fallback: {err}")
    plan = model_plan if isinstance(model_plan, dict) and model_plan else _author_fallback_plan(request)
    if not isinstance(plan.get("manifest"), dict):
        plan = _author_fallback_plan(request)
    normalized = _normalize_author_plan(plan, request)
    if ack and normalized.get("tool_kind") == "external_api" and not normalized.get("clarification_questions"):
        judged_questions = await _run_capability_ambiguity_judge(request, model_notes, warnings)
        if judged_questions:
            normalized["clarification_questions"] = judged_questions
            normalized["questions"] = judged_questions
            normalized["needs_clarification"] = True
            normalized["ready_for_code_generation"] = False
    model_notes.extend(normalized.get("model_notes") or [])
    return normalized


async def author_tool(request: dict[str, Any]) -> dict[str, Any]:
    """Generic Tool Authoring pipeline driven by explicit actions."""
    stage = str(request.get("stage") or "").strip().lower()
    action = str(request.get("action") or "clarify").strip().lower()
    # Backwards compatibility for older clients/tests that only send stage.
    if stage == "finalize":
        action = "finalize"
    elif stage == "draft" and action == "clarify":
        action = "generate"
    model_notes: list[str] = []
    warnings: list[str] = []
    if action not in {"clarify", "configure", "live_test", "generate", "finalize"}:
        raise ValueError("action must be one of clarify/configure/live_test/generate/finalize")

    if action == "live_test":
        result = live_test_tool(request)
        return {
            "needs_clarification": False,
            "questions": [],
            "tool_kind": request.get("tool_kind") or "external_api",
            "operation": request.get("operation") or "",
            "live_test_result": result,
            "preview": result.get("preview"),
            "normalized_preview": result.get("normalized_preview"),
            "manifest": request.get("manifest") or {},
            "adapter_code": "",
            "sample_input": request.get("sample_input") or {},
            "validation": {"success": bool(result.get("success")), "status": "live_test", "errors": result.get("errors") or [], "warnings": []},
            "snippet": None,
            "model_notes": model_notes,
            "warnings": warnings,
            "requires_human_confirmation": True,
        }

    if action == "finalize":
        manifest = request.get("manifest") if isinstance(request.get("manifest"), dict) else {}
        adapter_code = str(request.get("adapter_code") or request.get("code_block") or "")
        validation = validate_tool_manifest(manifest, adapter_code=adapter_code, sample_input=request.get("sample_input") or {}, dynamic=True)
        static_errors = _author_adapter_static_errors(adapter_code, manifest)
        if static_errors:
            validation["errors"] = sorted(set(validation.get("errors", []) + static_errors))
            validation["success"] = False
            validation["status"] = "failed"
        snippet = None
        if validation.get("success"):
            messages = [
                {"role": "system", "content": "You are text_model. Generate one strict JSON ToolSnippet only after adapter dynamic validation and human code confirmation. Do not include secrets."},
                {"role": "user", "content": json.dumps({"final_manifest": manifest, "final_adapter_code": adapter_code[:20000], "live_test_result": request.get("live_test_result") or (request.get("authoring_context") or {}).get("live_test_result"), "authoring_context": request.get("authoring_context") or {}, "dynamic_validation": validation, "confirmed_io": {"sample_input": request.get("sample_input") or {}, "input_description": request.get("input_description") or "", "output_description": request.get("output_description") or ""}}, ensure_ascii=False)},
            ]
            model_json, ack, err = await _complete_author_model("text", messages, reason="creator_tool_author_finalize_snippet")
            if ack:
                model_notes.append(f"text_model={ack['model']}")
            if err:
                warnings.append(f"text_model unavailable, used fallback snippet: {err}")
            snippet = model_json if model_json else _fallback_snippet(manifest, request.get("sample_input") or {})
            snippet_validation = _validate_author_snippet(snippet, manifest)
            if not snippet_validation["success"]:
                warnings.extend(snippet_validation["errors"])
                snippet = _fallback_snippet(manifest, request.get("sample_input") or {})
                snippet_validation = _validate_author_snippet(snippet, manifest)
            validation["snippet_validation"] = snippet_validation
        return {"needs_clarification": False, "questions": [], "manifest": manifest, "adapter_code": adapter_code, "sample_input": request.get("sample_input") or {}, "validation": validation, "snippet": snippet, "model_notes": model_notes, "warnings": warnings, "requires_human_confirmation": True}

    plan = await _run_planner(request, model_notes, warnings)
    if plan.get("requires_authoring_tools") or plan.get("authoring_tool_plan"):
        plan, _helper_results = _run_authoring_tool_plan(plan, request)
        return _author_response_from_plan(plan, model_notes=model_notes, warnings=warnings)
    if action in {"clarify", "configure"} or plan.get("needs_clarification") or not plan.get("ready_for_code_generation"):
        return _author_response_from_plan(plan, model_notes=model_notes, warnings=warnings)

    manifest = plan.get("manifest") or build_tool_manifest_draft(request)
    sample_input = request.get("sample_input") if isinstance(request.get("sample_input"), dict) and request.get("sample_input") else plan.get("sample_input") or {}
    code_block = str(request.get("code_block") or "")
    mode = "normalize_existing_code" if code_block.strip() else "generate_new_adapter"
    code_messages = [
        {
            "role": "system",
            "content": (
                f"You are code_model in mode={mode}. Return Python code only. Consume only confirmed requirements/config/sample input/live_test_result/manifest/implementation_plan/protocol. "
                "Do not guess endpoint, secret name, auth scheme, inputs, or outputs. For network/API adapters, include `if os.getenv(\"SKILL_TRIAL_RUN\") == \"1\": return mock_result`, "
                "read secrets only with os.getenv(DECLARED_ENV_NAME), never payload.get('api_key'), and never print or return secrets. Include run(payload), manifest function wrapper, and JSON main()."
            ),
        },
        {"role": "user", "content": json.dumps({"final_requirement": request.get("description") or "", "confirmed_config": request.get("config") or {}, "sample_input": sample_input, "live_test_result": request.get("live_test_result") or (plan.get("authoring_context") or {}).get("live_test_result"), "authoring_context": plan.get("authoring_context") or {}, "manifest": manifest, "implementation_plan": plan.get("implementation_plan"), "adapter_protocol": "Expose run(payload: dict|None)->dict and the manifest function; dynamic validation runs with SKILL_TRIAL_RUN=1 and must not access real external services.", "code_block": code_block}, ensure_ascii=False)},
    ]
    code_json, code_ack, code_err = await _complete_author_model("code", code_messages, reason=f"creator_tool_author_{mode}")
    if code_ack:
        model_notes.append(f"code_model={code_ack['model']}")
    if code_err:
        warnings.append(f"code_model unavailable, used deterministic fallback: {code_err}")
    adapter_code = _strip_code_fence(str(code_json.get("adapter_code") or code_json.get("code") or "")) if code_json else ""
    if not adapter_code:
        adapter_code = _normalize_existing_code_fallback(code_block, manifest) if code_block.strip() else generate_adapter_code(manifest)

    repair_log: list[dict[str, Any]] = []
    validation: dict[str, Any] = {}
    for attempt in range(3):
        validation = validate_tool_manifest(manifest, adapter_code=adapter_code, sample_input=sample_input, dynamic=True)
        static_errors = _author_adapter_static_errors(adapter_code, manifest)
        if static_errors:
            validation["errors"] = sorted(set(validation.get("errors", []) + static_errors))
            validation["success"] = False
            validation["status"] = "failed"
        if validation.get("success"):
            break
        if attempt >= 2:
            break
        repair_log.append({"attempt": attempt + 1, "errors": validation.get("errors", []), "warnings": validation.get("warnings", [])})
        repair_messages = [{"role": "system", "content": "Repair the Python adapter locally. Preserve business logic. Return code only. Keep SKILL_TRIAL_RUN mock behavior for network/API adapters."}, {"role": "user", "content": json.dumps({"manifest": manifest, "sample_input": sample_input, "validation": validation, "adapter_code": adapter_code}, ensure_ascii=False)}]
        repair_json, _repair_ack, repair_err = await _complete_author_model("code", repair_messages, reason="creator_tool_author_repair")
        repaired = _strip_code_fence(str(repair_json.get("adapter_code") or repair_json.get("code") or "")) if repair_json else ""
        if repair_err or not repaired:
            warnings.append(f"automatic repair stopped; using fallback/local code: {repair_err or 'empty repair'}")
            break
        adapter_code = repaired
    validation["repair_log"] = repair_log
    return {"needs_clarification": False, "questions": [], "tool_kind": plan.get("tool_kind"), "operation": plan.get("operation"), "manifest": manifest, "adapter_code": adapter_code, "sample_input": sample_input, "validation": validation, "snippet": None, "model_notes": model_notes, "warnings": warnings, "requires_human_confirmation": True}


async def stream_author_tool(request: dict[str, Any]):
    """SSE-friendly wrapper that emits coarse progress events for long authoring actions."""
    action = str(request.get("action") or ("finalize" if request.get("stage") == "finalize" else "clarify"))
    try:
        yield {"event": "step_started", "step": "planner", "message": "正在理解需求"}
        if action == "live_test":
            yield {"event": "step_started", "step": "live_test", "message": "正在测试连接"}
        elif action == "generate":
            yield {"event": "step_started", "step": "code", "message": "正在生成 adapter"}
        elif action == "finalize":
            yield {"event": "step_started", "step": "validation", "message": "正在执行动态验证"}
        result = await author_tool(request)
        for item in result.get("authoring_tool_plan") or []:
            if isinstance(item, dict):
                yield {"event": "tool_call_planned", "tool": item.get("tool_name"), "reason": item.get("reason") or ""}
        for item in result.get("authoring_tool_results") or []:
            if not isinstance(item, dict):
                continue
            tool_name = item.get("tool_name")
            yield {"event": "tool_call_started", "tool": tool_name}
            if item.get("requires_input"):
                yield {"event": "tool_call_requires_input", "tool": tool_name, "schema": item.get("schema") or {}}
            yield {"event": "tool_call_result", "tool": tool_name, "success": bool(item.get("success"))}
        if result.get("needs_clarification"):
            yield {"event": "clarification_required", "questions": result.get("questions") or []}
        if result.get("live_test_result"):
            yield {"event": "live_test_result", **result["live_test_result"]}
        if result.get("adapter_code"):
            yield {"event": "model_delta", "step": "code", "delta": result.get("adapter_code")}
        if result.get("validation"):
            yield {"event": "validation", "success": bool(result["validation"].get("success")), "errors": result["validation"].get("errors") or [], "warnings": result["validation"].get("warnings") or []}
        yield {"event": "step_finished", "step": "planner", "summary": result.get("validation", {}).get("status", "done")}
        yield {"event": "final_result", **result}
    except Exception as exc:
        yield {"event": "error", "message": str(exc)}


def tool_status(capability: ToolCapability) -> dict[str, Any]:
    missing_env = [name for name in capability.required_env if not os.environ.get(name)]
    missing_secrets = [name for name in capability.required_secrets if not os.environ.get(name)]
    helper_names = _runtime_helper_names()
    runtime_helpers_available = [name for name in capability.helper_imports if name in helper_names]
    missing_runtime_helpers = [name for name in capability.helper_imports if name not in helper_names]
    missing_dependencies = [name for name in capability.dependencies if not _dependency_available(name)]
    creator_available = capability.enabled_by_default and capability.allow_creator_use
    return {
        **asdict(capability),
        "allowed_roles": capability.allowed_roles or capability.roles,
        "enabled": capability.enabled_by_default,
        "creator_available": creator_available,
        "configured": not missing_env and not missing_secrets,
        "missing_env": missing_env,
        "missing_secrets": missing_secrets,
        # Toggle overrides remain process-local; custom registered manifests are
        # persisted separately in backend/config/tool_registry.custom.json.
        "override_persistence": TOOL_OVERRIDE_PERSISTENCE,
        "runtime_helpers_available": runtime_helpers_available,
        "missing_runtime_helpers": missing_runtime_helpers,
        "missing_dependencies": missing_dependencies,
    }

def _make_snippet(
    tool: str,
    helper: str,
    title: str,
    code: str,
    outputs: dict[str, str],
    *,
    kind: SnippetKind = "minimal_usage",
    roles: list[str] | None = None,
    capabilities: list[str] | None = None,
    failures: list[str] | None = None,
    description: str = "",
    anti_patterns: list[str] | None = None,
    priority: int = 100,
    usage_policy: UsagePolicy = "helper_preferred",
) -> ToolSnippet:
    return ToolSnippet(
        id=f"{helper}.{kind}",
        title=title,
        kind=kind,
        applies_to={"roles": roles or [], "capabilities": capabilities or [tool], "failure_layers": failures or ["helper_call_failed", "final_platform_output_value_invalid", "artifact_missing", "artifact_invalid"]},
        description=description or title,
        code=code.strip(),
        expected_input_shape={},
        expected_output_shape=outputs,
        return_rule="If the helper returns the platform stdout dict with file_paths/file_outputs, return that dict directly; only merge with extra scalar fields using {**result, ...}.",
        anti_patterns=anti_patterns or ["Do not guess parameter names.", "Do not wrap helper result inside an output field.", "Do not write files outside OUTPUT_DIR/outputs."],
        requires=capabilities or [tool],
        usage_policy=usage_policy,
        priority=priority,
    )


def _install_builtin_tool_snippets() -> None:
    specs: dict[str, list[ToolSnippet]] = {
        "pdf_generation": [
            _make_snippet("pdf_generation", "create_pdf", "Create a simple text PDF", """
from backend.services.skill_runtime import create_pdf

content = payload.get("text") or payload.get("content") or "Generated PDF"
result = create_pdf(content, filename="output.pdf")
return result
""", {"pdf_path": "string", "file_paths": "list[string]", "file_outputs": "list[object]"}, roles=["pdf_builder", "document_generator", "composite_generator"], capabilities=["pdf_generation"], anti_patterns=["Do not return {'pdf_path': result}; create_pdf returns a dict, not a string.", "Do not pass a dict as the text argument unless you intentionally want its string/list lines.", "Do not write outside OUTPUT_DIR; use filename or output_dir/output_path under outputs."], priority=120),
            _make_snippet("pdf_generation", "build_pdf_report", "Create a structured PDF report", """
from backend.services.skill_runtime import build_pdf_report

sections = [
    {"heading": "Summary", "content": payload.get("summary") or "No summary provided."},
    {"heading": "Details", "content": payload.get("details") or []},
]
result = build_pdf_report("Report", sections, image_paths=payload.get("image_paths") or [], filename="report.pdf")
return result
""", {"pdf_path": "string", "file_paths": "list[string]", "file_outputs": "list[object]"}, roles=["pdf_builder"], capabilities=["pdf_generation"], priority=100),
            _make_snippet("pdf_generation", "images_to_pdf", "Convert images to one PDF", """
from backend.services.skill_runtime import images_to_pdf

image_paths = payload.get("image_paths") or []
result = images_to_pdf(image_paths, output_path="outputs/images.pdf")
return result
""", {"pdf_path": "string", "file_paths": "list[string]", "file_outputs": "list[object]"}, roles=["pdf_builder"], capabilities=["pdf_generation"], kind="file_output_usage", priority=90),
            _make_snippet("pdf_generation", "merge_pdfs", "Merge several PDFs", """
from backend.services.skill_runtime import merge_pdfs

pdf_paths = payload.get("pdf_paths") or []
result = merge_pdfs(pdf_paths, output_path="outputs/merged.pdf")
return result
""", {"pdf_path": "string", "file_paths": "list[string]", "file_outputs": "list[object]"}, roles=["pdf_builder"], capabilities=["pdf_generation"], kind="batch_usage", priority=90),
        ],
        "image_generation": [
            _make_snippet("image_generation", "generate_stable_diffusion_image", "Generate one image", """
from backend.services.skill_runtime import generate_stable_diffusion_image

prompt = payload.get("prompt") or payload.get("description") or payload.get("text") or "A clean illustration"
result = generate_stable_diffusion_image(prompt, filename_prefix="generated")
return {"image_path": result["image_path"], "image_paths": [result["image_path"]]}
""", {"image_path": "string", "image_paths": "list[string]"}, roles=["image_generator", "composite_generator"], capabilities=["image_generation"], anti_patterns=["Do not call /v1/images/generations directly; use the registered helper.", "Do not use VISION_MODEL for image generation.", "During SKILL_TRIAL_RUN the helper may return a deterministic minimal file; still return image_path/image_paths."], priority=120),
            _make_snippet("image_generation", "generate_stable_diffusion_image", "Generate multiple images in a loop", """
from backend.services.skill_runtime import generate_stable_diffusion_image

prompts = payload.get("prompts") or [payload.get("prompt") or "Generated image"]
image_paths = []
for index, prompt in enumerate(prompts, start=1):
    result = generate_stable_diffusion_image(str(prompt), filename_prefix=f"generated_{index}")
    image_paths.append(result["image_path"])
return {"image_paths": image_paths, "image_path": image_paths[0] if image_paths else ""}
""", {"image_path": "string", "image_paths": "list[string]"}, kind="batch_usage", roles=["image_generator", "composite_generator"], capabilities=["image_generation"], priority=80),
        ],
        "docx_generation": [_make_snippet("docx_generation", "create_docx", "Create a DOCX document", """
from backend.services.skill_runtime import create_docx

content = payload.get("sections") or payload.get("text") or "Generated document"
result = create_docx(content, filename="output.docx", title=payload.get("title") or "Document")
return result
""", {"docx_path": "string", "file_paths": "list[string]", "file_outputs": "list[object]"}, roles=["docx_builder"], capabilities=["docx_generation"], priority=120)],
        "pptx_generation": [_make_snippet("pptx_generation", "create_pptx", "Create a PPTX deck", """
from backend.services.skill_runtime import create_pptx

slides = payload.get("slides") or payload.get("sections") or [payload.get("text") or "Generated slide"]
result = create_pptx(slides, filename="output.pptx", title=payload.get("title") or "Presentation")
return result
""", {"pptx_path": "string", "file_paths": "list[string]", "file_outputs": "list[object]"}, roles=["pptx_builder"], capabilities=["pptx_generation"], priority=120)],
        "pdf_parsing": [_make_snippet("pdf_parsing", "extract_pdf_text", "Extract text from an input PDF", """
from backend.services.skill_runtime import extract_pdf_text

input_files = payload.get("input_files") or payload.get("files") or []
pdf_path = payload.get("pdf_path") or (input_files[0] if input_files else "")
result = extract_pdf_text(pdf_path, max_pages=payload.get("max_pages"))
return {"text": result["text"], "pages": result.get("pages", []), "pdf_path": result.get("pdf_path", pdf_path)}
""", {"text": "string", "pages": "list[string]", "pdf_path": "string"}, roles=["pdf_parser"], capabilities=["pdf_parsing"], priority=110)],
        "docx_parsing": [_make_snippet("docx_parsing", "read_docx_text", "Read text from a DOCX", """
from backend.services.skill_runtime import read_docx_text

input_files = payload.get("input_files") or payload.get("files") or []
docx_path = payload.get("docx_path") or (input_files[0] if input_files else "")
result = read_docx_text(docx_path)
return {"text": result["text"], "paragraphs": result.get("paragraphs", []), "source_path": result.get("source_path", docx_path)}
""", {"text": "string", "paragraphs": "list[string]", "source_path": "string"}, roles=["docx_parser"], capabilities=["docx_parsing"], priority=100)],
        "pptx_parsing": [_make_snippet("pptx_parsing", "read_pptx_text", "Read text from a PPTX", """
from backend.services.skill_runtime import read_pptx_text

input_files = payload.get("input_files") or payload.get("files") or []
pptx_path = payload.get("pptx_path") or (input_files[0] if input_files else "")
result = read_pptx_text(pptx_path)
return {"text": result["text"], "slides": result.get("slides", []), "source_path": result.get("source_path", pptx_path)}
""", {"text": "string", "slides": "list[string]", "source_path": "string"}, roles=["pptx_parser"], capabilities=["pptx_parsing"], priority=100)],
        "web_search": [_make_snippet("web_search", "web_search", "Search the web with the registered helper", """
from backend.services.skill_runtime import web_search

query = payload.get("query") or payload.get("user_request") or payload.get("text") or ""
result = web_search(query, top_k=int(payload.get("top_k") or 5), language=payload.get("language"))
return {"results": result.get("results", []), "query": query}
""", {"results": "list[object]", "query": "string"}, roles=["search_reader"], capabilities=["web_search"], anti_patterns=["Do not use requests against undeclared search APIs when web_search is registered.", "Do not output secrets or raw provider credentials.", "If SEARCHXNG_BASE_URL is missing, fail clearly or use trial-run behavior."], priority=100)],
        "database_read": [_make_snippet("database_read", "query_database_readonly", "Run a bounded readonly SQL query", """
from backend.services.skill_runtime import query_database_readonly

sql = payload.get("sql") or "SELECT 1 AS value"
result = query_database_readonly(sql, params=payload.get("params") or {}, limit=int(payload.get("limit") or 100))
return result
""", {"columns": "list[string]", "rows": "list[object]", "row_count": "integer", "truncated": "boolean"}, roles=["database_reader"], capabilities=["database_read"], anti_patterns=["Only SELECT/WITH is allowed; never INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE/CREATE.", "Do not read or print DATABASE_URL.", "Do not bypass query_database_readonly with a direct database driver."], priority=120, usage_policy="helper_required")],
        "wechat_draft": [_make_snippet("wechat_draft", "create_wechat_draft", "Create a WeChat draft only", """
from backend.services.skill_runtime import create_wechat_draft

result = create_wechat_draft(
    title=payload.get("title") or "Untitled",
    content_html=payload.get("content_html") or payload.get("html") or "<p>Draft</p>",
    author=payload.get("author") or "",
    digest=payload.get("digest") or "",
    cover_image_path=payload.get("cover_image_path"),
)
return result
""", {"draft_id": "string", "media_id": "string", "url": "string|null", "status": "string"}, roles=["wechat_draft_creator"], capabilities=["wechat_draft"], anti_patterns=["Do not publish automatically from a draft creator script.", "Do not output WECHAT_APP_ID or WECHAT_APP_SECRET.", "Use upload_wechat_media only for declared local cover images."], priority=120, usage_policy="helper_required")],
        "wechat_publish": [_make_snippet("wechat_publish", "publish_wechat_draft", "Publish an explicitly requested WeChat draft", """
from backend.services.skill_runtime import publish_wechat_draft

draft_id = payload.get("draft_id") or ""
result = publish_wechat_draft(draft_id)
return result
""", {"draft_id": "string", "publish_id": "string", "status": "string"}, roles=["wechat_publisher"], capabilities=["wechat_publish"], anti_patterns=["Do not publish unless the user explicitly requested publishing and the tool is enabled.", "Do not output WeChat secrets.", "Do not create a draft and publish as a hidden side effect unless the plan says so."], priority=120, usage_policy="helper_required")],
    }
    for name, snippets in specs.items():
        cap = BUILTIN_TOOL_CAPABILITIES.get(name)
        if cap is not None and not cap.snippets:
            BUILTIN_TOOL_CAPABILITIES[name] = replace(cap, snippets=snippets)


_install_builtin_tool_snippets()
_load_registered_tools_from_disk()
