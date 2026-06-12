"""Creator tool capability registry.

This module is the single source of truth for Creator-facing tool metadata
and default role mappings.  The sandbox/runtime execution path is kept separate:
this registry describes what Creator may plan, prompt and expose through
management APIs.  Runtime helpers and deep source validators are intentionally
implemented in follow-up modules; tool status reports whether registered helper
names are exported by ``backend.services.skill_runtime``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import ast
import importlib
import importlib.util
import os
import re
from pathlib import Path
from typing import Any, Literal


UsagePolicy = Literal["helper_required", "helper_preferred", "self_implementation_allowed"]


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
}

RESOURCE_ROLES: frozenset[str] = frozenset({"skill_overview", "reference", "asset"})
TOOL_OVERRIDE_PERSISTENCE = "process_memory"
_REGISTERED_TOOL_CAPABILITIES: dict[str, ToolCapability] = {}
_TOOL_OVERRIDES: dict[str, dict[str, bool]] = {}
_RUNTIME_HELPERS_CACHE: set[str] | None = None

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
    tool_usage_prompt: str = ""
    warnings: list[str] = field(default_factory=list)


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
    tool_usage_prompt = "\n".join([helper_line, forbid_line, *policy_lines, *guidance])
    return ToolResolveResult(
        allowed_tools=allowed_tools,
        allowed_helper_imports=allowed_helper_imports,
        required_dependencies=required_dependencies,
        forbidden_imports=forbidden_imports,
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
        # Toggle overrides are intentionally process-local for this P0 registry
        # layer.  Persisting them to a database/config file belongs to the tools
        # management page follow-up.
        "override_persistence": TOOL_OVERRIDE_PERSISTENCE,
        "runtime_helpers_available": runtime_helpers_available,
        "missing_runtime_helpers": missing_runtime_helpers,
        "missing_dependencies": missing_dependencies,
    }
