"""Single-runtime Creator tool registry.

Complete generic backend replacement for Creator tool authoring.

Design:
- exactly one runtime: python_script
- step 1 receives user requirements and optional reference code
- step 2 planner asks clarifying questions, decides auth, and stores authorization config when provided
- step 3 coder model writes the whole script
- step 4 backend creates a temporary environment, runs the script, and supports human feedback repair
- step 5 model summarizes reusable snippet + direct/indirect usage + IO contract for sandbox/skill creator
- backend never adds tool-specific business rules; it only enforces generic protocol, auth, dependencies, and IO schema
- no http_api/managed_helper/file_io/python_compute/database_query/local_command wrapper branches
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field, fields as dataclasses_fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from .creator_tool_discovery import discover_creator_tool_records


UsagePolicy = Literal["helper_required", "helper_preferred", "self_implementation_allowed"]
SnippetKind = Literal[
    "minimal_usage",
    "multi_input_usage",
    "file_output_usage",
    "batch_usage",
    "error_repair_usage",
    "anti_pattern",
    "trial_run_usage",
]

RESOURCE_ROLES: frozenset[str] = frozenset({"skill_overview", "reference", "asset", "tool_authoring"})
TOOL_OVERRIDE_PERSISTENCE = "process_memory"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
CUSTOM_TOOL_REGISTRY_PATH = CONFIG_DIR / "tool_registry.custom.json"
CUSTOM_TOOL_ADAPTER_DIR = Path(__file__).resolve().parent / "runtime_tools" / "custom_tools"
SAMPLE_INPUT_DIR = PROJECT_ROOT / "backend" / "samples"
PDF_SAMPLE_PATH = SAMPLE_INPUT_DIR / "sample.pdf"
IMAGE_SAMPLE_PATH = SAMPLE_INPUT_DIR / "sample.png"

_TOOL_AUTHORING_CONFIG_STORE: dict[str, dict[str, Any]] = {}
_TOOL_AUTHORING_CONFIG_LOADED = False
_REGISTERED_TOOL_CAPABILITIES: dict[str, "ToolCapability"] = {}
_DISCOVERED_TOOL_CAPABILITIES: dict[str, "ToolCapability"] = {}
_TOOL_OVERRIDES: dict[str, dict[str, bool]] = {}

_ALLOWED_USAGE_POLICIES = {"helper_required", "helper_preferred", "self_implementation_allowed"}
_ALLOWED_SNIPPET_KINDS = {
    "minimal_usage", "multi_input_usage", "file_output_usage", "batch_usage",
    "error_repair_usage", "anti_pattern", "trial_run_usage",
}
_ALLOWED_TOOL_TYPES = {"python_script", "python_helper", "custom_adapter", "internal_authoring_tool"}

# Safety boundaries, not business routing wrappers.
_NETWORK_IMPORT_ROOTS = {"requests", "httpx", "aiohttp", "socket", "urllib"}
_SUBPROCESS_IMPORT_ROOTS = {"subprocess"}
_DANGEROUS_IMPORT_ROOTS = {"paramiko", "ftplib", "telnetlib"}
_FORBIDDEN_BUILTIN_CALLS = {"eval", "exec", "compile", "__import__", "input", "breakpoint"}
_UNSAFE_FS_ATTRS = {"remove", "unlink", "rmdir", "rmtree", "chmod", "chown"}
_RUNTIME_ENV_NAMES = {"TOOL_OUTPUT_DIR", "OUTPUT_DIR", "TOOL_TRIAL_RUN", "SKILL_TRIAL_RUN"}


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
    artifact_outputs: list[dict[str, Any]] = field(default_factory=list)
    side_effects: list[str] = field(default_factory=list)
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
    capability_aliases: list[str] = field(default_factory=list)
    semantic_tags: list[str] = field(default_factory=list)
    accepted_input_extensions: list[str] = field(default_factory=list)
    accepted_input_mime_types: list[str] = field(default_factory=list)
    output_content_types: list[str] = field(default_factory=list)
    output_extensions: list[str] = field(default_factory=list)
    task_verbs: list[str] = field(default_factory=list)
    domain_terms: list[str] = field(default_factory=list)
    negative_tags: list[str] = field(default_factory=list)
    preference_score: float = 0.0
    tool_quality_score: float = 0.0
    structured_output_score: float = 0.0
    allowed_roles: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    optional_capabilities: list[str] = field(default_factory=list)
    forbidden_capabilities: list[str] = field(default_factory=list)
    usage_policy: UsagePolicy = "self_implementation_allowed"
    required_env: list[str] = field(default_factory=list)
    required_secrets: list[str] = field(default_factory=list)
    dependencies: list[Any] = field(default_factory=list)
    helper_module: str = "backend.services.skill_runtime"
    forbidden_direct_imports: list[str] = field(default_factory=list)
    safety_level: str = "standard"
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    artifact_outputs: list[dict[str, Any]] = field(default_factory=list)
    side_effects: list[str] = field(default_factory=list)
    trial_mode: Literal["none", "mock", "minimal_file"] = "mock"
    validator_kind: str = "generic_python_script"
    prompt_guidance: str = ""
    tool_type: str = "python_script"
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


# Compatibility metadata for the read-only /tool-roles catalogue. It is not
# consumed by Creator planning, validation, tool selection, or authorization.
_LEGACY_DISPLAY_ROLE_FORBIDDEN_CAPABILITIES: dict[str, list[str]] = {
    "text_generator": ["image_generation", "pdf_generation"],
    "image_generator": ["text_generation", "pdf_generation"],
    "composite_generator": [],
    "generic_script": [],
    "reference": ["runtime_execution", "image_generation"],
    "asset": ["runtime_execution", "image_generation"],
    "skill_overview": ["runtime_execution"],
}


def _sample_value_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _schema_string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []


def _schema_file_kind(schema: dict[str, Any]) -> str | None:
    """Infer pdf/image file samples from generic JSON Schema metadata only."""
    if not isinstance(schema, dict):
        return None
    values: list[str] = []
    for key in ("contentMediaType", "media_type", "mime_type"):
        values.extend(_schema_string_values(schema.get(key)))
    for key in ("accepted_extensions", "extensions", "file_extensions"):
        values.extend(_schema_string_values(schema.get(key)))
    fmt = str(schema.get("format") or "").strip().lower()
    if fmt in {"file-path", "file", "path"}:
        values.append(fmt)
    strong = " ".join(values).lower()
    if "application/pdf" in strong or re.search(r"(^|[\s,;])\.pdf($|[\s,;])", strong):
        return "pdf"
    if "image/" in strong or re.search(r"(^|[\s,;])\.(png|jpe?g|webp)($|[\s,;])", strong):
        return "image"
    desc = " ".join(str(schema.get(k) or "") for k in ("description", "title")).lower()
    if "application/pdf" in desc or re.search(r"(^|[\s,;])\.pdf($|[\s,;])", desc):
        return "pdf"
    if "image/" in desc or re.search(r"(^|[\s,;])\.(png|jpe?g|webp)($|[\s,;])", desc):
        return "image"
    return None


def _builtin_sample_path(kind: str) -> Path:
    return PDF_SAMPLE_PATH if kind == "pdf" else IMAGE_SAMPLE_PATH


def _sample_value_for_schema(schema: dict[str, Any], field_name: str = "") -> Any:
    """Generate a safe generic sample value from JSON Schema.

    This is not business-specific. It only follows JSON Schema type/default/enum.
    """
    schema = schema if isinstance(schema, dict) else {}

    if "default" in schema:
        return schema.get("default")

    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and enum_values:
        return enum_values[0]

    const_value = schema.get("const")
    if const_value is not None:
        return const_value

    field_type = schema.get("type")
    if isinstance(field_type, list):
        field_type = next((item for item in field_type if item != "null"), field_type[0] if field_type else "string")

    if field_type == "boolean":
        return False
    if field_type == "integer":
        return 1
    if field_type == "number":
        return 1
    if field_type == "array":
        item_schema = schema.get("items") if isinstance(schema.get("items"), dict) else {}
        return [_sample_value_for_schema(item_schema, field_name)]
    if field_type == "object":
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        return {
            str(key): _sample_value_for_schema(props.get(str(key), {}), str(key))
            for key in required
        }

    fmt = str(schema.get("format") or "").lower()
    if fmt == "date":
        return "2026-01-01"
    if fmt in {"date-time", "datetime"}:
        return "2026-01-01T00:00:00Z"
    if fmt == "email":
        return "user@example.com"
    if fmt in {"uri", "url"}:
        return "https://example.com"

    title = str(schema.get("title") or field_name or "value").strip()
    return f"sample_{title}"


def resolve_tool_trial_sample_input(manifest: dict[str, Any], sample_input: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    """Fill missing PDF/image file inputs from backend/samples based on schema metadata."""
    resolved = dict(sample_input or {}) if isinstance(sample_input, dict) else {}
    notes: list[str] = []
    schema = manifest.get("input_schema") if isinstance(manifest, dict) and isinstance(manifest.get("input_schema"), dict) else {}
    if schema.get("type") != "object" or not isinstance(schema.get("properties"), dict):
        return resolved, notes
    for field_name, field_schema in schema["properties"].items():
        if _sample_value_present(resolved.get(field_name)):
            continue
        if not isinstance(field_schema, dict):
            continue
        field_type = field_schema.get("type")
        target_schema = field_schema
        is_array = field_type == "array" or isinstance(field_schema.get("items"), dict)
        if is_array:
            items = field_schema.get("items") if isinstance(field_schema.get("items"), dict) else {}
            target_schema = {**field_schema, **items}
        elif field_type == "object":
            target_schema = field_schema
        elif isinstance(field_type, list) and "object" in field_type:
            target_schema = field_schema
        kind = _schema_file_kind(target_schema)
        if kind:
            sample_path = _builtin_sample_path(kind)
            if not sample_path.exists():
                notes.append(f"sample_input.{field_name}: built-in {kind} sample is missing at {sample_path}; not fabricating a path")
                continue
            absolute = str(sample_path.resolve())
            resolved[field_name] = [absolute] if is_array else absolute
            continue

        required_fields = schema.get("required") if isinstance(schema.get("required"), list) else []
        if field_name in {str(item) for item in required_fields}:
            resolved[field_name] = _sample_value_for_schema(field_schema, field_name)
            notes.append(
                f"sample_input.{field_name}: missing required sample value; auto-filled from input_schema for trial run"
            )
    return resolved, notes

def _simple_cap(name: str, display_name: str, category: str, roles: list[str], **kwargs: Any) -> ToolCapability:
    return ToolCapability(
        name=name,
        display_name=display_name,
        category=category,
        roles=roles,
        dependencies=kwargs.get("dependencies") or [],
        prompt_guidance=kwargs.get("prompt", ""),
        allow_external_side_effect=bool(kwargs.get("allow_external_side_effect", False)),
        enabled_by_default=bool(kwargs.get("enabled", True)),
        allow_creator_use=bool(kwargs.get("allow_creator_use", True)),
        helper_imports=kwargs.get("helper_imports") or [],
        required_env=kwargs.get("required_env") or [],
        required_secrets=kwargs.get("required_secrets") or [],
    )


BUILTIN_TOOL_CAPABILITIES: dict[str, ToolCapability] = {
    "text_generation": _simple_cap("text_generation", "文本生成", "generation", ["text_generator", "composite_generator"]),
    "image_generation": _simple_cap("image_generation", "图片生成", "generation", ["image_generator", "composite_generator"]),
    "pdf_generation": _simple_cap("pdf_generation", "PDF 生成", "document", ["pdf_builder", "composite_generator"], dependencies=[{"package": "reportlab", "imports": ["reportlab"]}]),
    "docx_generation": _simple_cap("docx_generation", "Word 生成", "document", ["docx_builder", "composite_generator"], dependencies=[{"package": "python-docx", "imports": ["docx"]}]),
    "pptx_generation": _simple_cap("pptx_generation", "PPT 生成", "document", ["pptx_builder", "composite_generator"], dependencies=[{"package": "python-pptx", "imports": ["pptx"]}]),
    "xlsx_generation": _simple_cap("xlsx_generation", "Excel 生成", "document", ["spreadsheet_builder", "composite_generator"], dependencies=[{"package": "openpyxl", "imports": ["openpyxl"]}]),
    "csv_generation": _simple_cap("csv_generation", "CSV 生成", "document", ["spreadsheet_builder", "composite_generator"]),
    "html_asset_generation": _simple_cap("html_asset_generation", "HTML 素材生成", "document", ["html_asset_builder"]),
    "asset_generation": _simple_cap("asset_generation", "静态素材生成", "asset", ["asset_builder"]),
    "file_output": _simple_cap("file_output", "文件输出", "common", ["generic_script", "pdf_builder", "docx_builder", "pptx_builder", "html_asset_builder", "asset_builder", "composite_generator"]),
    "pdf_parsing": _simple_cap("pdf_parsing", "PDF 解析", "parsing", ["pdf_parser"], dependencies=[{"package": "pypdf", "imports": ["pypdf"]}]),
    "docx_parsing": _simple_cap("docx_parsing", "Word 解析", "parsing", ["docx_parser"], dependencies=[{"package": "python-docx", "imports": ["docx"]}]),
    "pptx_parsing": _simple_cap("pptx_parsing", "PPT 解析", "parsing", ["pptx_parser"], dependencies=[{"package": "python-pptx", "imports": ["pptx"]}]),
    "spreadsheet_read": _simple_cap("spreadsheet_read", "表格读取", "parsing", ["spreadsheet_reader"], dependencies=[{"package": "openpyxl", "imports": ["openpyxl"]}]),
    "csv_read": _simple_cap("csv_read", "CSV 读取", "parsing", ["spreadsheet_reader"]),
    "unified_file_text_read": _simple_cap("unified_file_text_read", "多格式文本读取", "parsing", ["generic_script", "document_parser", "aggregator"], helper_imports=["read_file_text"]),
    "vision_understanding": _simple_cap("vision_understanding", "视觉理解", "ai", ["vision_analyzer", "generic_script", "composite_generator"]),
    "http_request": _simple_cap("http_request", "HTTP/API 请求", "retrieval", ["search_reader", "generic_script"], prompt="Metadata only. Generated scripts implement HTTP themselves when permissions.network=true."),
    "network_read": _simple_cap("network_read", "网络资源读取", "retrieval", ["search_reader", "generic_script"]),
    "web_search": _simple_cap("web_search", "网页搜索", "retrieval", ["search_reader"], required_env=["SEARCHXNG_BASE_URL"]),
    "database_read": _simple_cap("database_read", "数据库只读查询", "retrieval", ["database_reader"], required_secrets=["DATABASE_URL"], helper_imports=["query_database_readonly"]),
    "wechat_draft": _simple_cap("wechat_draft", "微信公众号草稿", "publisher", ["wechat_draft_creator"], required_secrets=["WECHAT_APP_ID", "WECHAT_APP_SECRET"]),
    "wechat_publish": _simple_cap("wechat_publish", "微信公众号发布", "publisher", ["wechat_publisher"], required_secrets=["WECHAT_APP_ID", "WECHAT_APP_SECRET"], allow_external_side_effect=True, enabled=False),
    "deterministic_execution": _simple_cap("deterministic_execution", "确定性脚本执行", "common", ["generic_script"]),
    "reference_guidance": _simple_cap("reference_guidance", "参考文档指导", "resource", ["reference"], allow_creator_use=False),
    "static_resource": _simple_cap("static_resource", "静态资源", "resource", ["asset"], allow_creator_use=False),
    "workflow_overview": _simple_cap("workflow_overview", "工作流概览", "resource", ["skill_overview"], allow_creator_use=False),
    "authoring_dependency_check": _simple_cap("authoring_dependency_check", "Authoring 依赖检测", "authoring", ["tool_authoring"], allow_creator_use=False),
    "authoring_code_protocol_check": _simple_cap("authoring_code_protocol_check", "Authoring 代码协议检查", "authoring", ["tool_authoring"], allow_creator_use=False),
}


BUILTIN_TOOL_CAPABILITIES["script_argv_guard"] = ToolCapability(
    name="script_argv_guard",
    display_name="Strict JSON argv guard",
    category="script_core",
    roles=[],
    enabled_by_default=True,
    allow_creator_use=True,
    helper_imports=["strict_json_argv_guard"],
    usage_policy="helper_required",
    helper_module="backend.services.runtime_tools",
    safety_level="core",
    tool_type="python_helper",
    functions=[
        ToolFunctionManifest(
            function_name="strict_json_argv_guard",
            import_path="backend.services.runtime_tools",
            short_description="Validate JSON argv before script core logic.",
            when_to_use="Mandatory for every generated Python script before run() or core logic.",
            signature="strict_json_argv_guard(payload: dict, spec: dict) -> dict",
            input_schema={"type": "object", "required": ["payload", "spec"], "properties": {"payload": {"type": "object"}, "spec": {"type": "object"}}},
            output_schema={"type": "object"},
            return_contract="Returns validated args. Raises on unknown, missing, empty, or invalid typed argv.",
            example_call=(
                "from backend.services.runtime_tools import strict_json_argv_guard\n\n"
                "args = strict_json_argv_guard(payload, {\n"
                "    'topic': {'type': str, 'required': True},\n"
                "})"
            ),
            common_mistakes=[
                "Do not skip this helper in generated scripts.",
                "Do not pass a platform-wide field list as spec.",
                "Do not leave input_text/example/TODO placeholders.",
                "Do not mark core inputs as optional/defaulted.",
                "Do not use unvalidated payload in run().",
            ],
            usage_policy="helper_required",
            required_capabilities=["script_argv_guard"],
        )
    ],
    snippets=[
        ToolSnippet(
            id="script_argv_guard.strict_json_argv_guard",
            title="Mandatory argv validation before core logic",
            kind="minimal_usage",
            description="Import and call strict_json_argv_guard immediately after parsing sys.argv[1].",
            code=(
                "import json\n"
                "import sys\n"
                "from backend.services.runtime_tools import strict_json_argv_guard\n\n"
                "def parse_args() -> dict:\n"
                "    if len(sys.argv) < 2:\n"
                "        raise ValueError('missing JSON argv')\n"
                "    payload = json.loads(sys.argv[1])\n"
                "    return strict_json_argv_guard(payload, {\n"
                "        # Replace with parameters actually used by run(args).\n"
                "        'topic': {'type': str, 'required': True},\n"
                "    })\n"
            ),
            expected_input_shape={"payload": "dict parsed from sys.argv[1]", "spec": "dict describing current script args"},
            expected_output_shape={"validated_args": "dict"},
            return_rule="run() must receive and use only the validated args returned by strict_json_argv_guard.",
            anti_patterns=["Do not omit this helper.", "Do not use payload directly in run().", "Do not re-parse sys.argv in run().", "Do not preserve placeholder spec fields."],
            requires=["script_argv_guard"],
            usage_policy="helper_required",
            priority=10000,
        )
    ],
)

BUILTIN_TOOL_CAPABILITIES["vision_understanding"] = replace(
    BUILTIN_TOOL_CAPABILITIES["vision_understanding"],
    helper_imports=["analyze_image_with_vision"],
    helper_module="backend.services.runtime_tools",
    required_env=["LLM_BASE_URL", "VISION_MODEL"],
    input_schema={
        "type": "object",
        "required": ["image_path", "prompt"],
        "properties": {
            "image_path": {
                "type": "string",
                "format": "file-path",
                "description": "Existing image file path (.png, .jpg, .jpeg, .webp) supplied as runtime input or Creator context.",
                "accepted_extensions": [".png", ".jpg", ".jpeg", ".webp"],
            },
            "prompt": {"type": "string", "description": "Question or instruction for understanding the image."},
        },
    },
    output_schema={
        "type": "object",
        "required": ["image_path", "description", "ocr_text", "model"],
        "properties": {
            "image_path": {"type": "string"},
            "description": {"type": "string"},
            "ocr_text": {"type": "string"},
            "model": {"type": "string"},
        },
    },
    functions=[
        ToolFunctionManifest(
            function_name="analyze_image_with_vision",
            import_path="backend.services.runtime_tools",
            short_description="Analyze an existing image with the configured vision-language model.",
            when_to_use="Use when a script declares vision_understanding and must inspect uploaded or runtime image content.",
            signature="analyze_image_with_vision(image_path: str, prompt: str = 'Describe this image.') -> dict[str, Any]",
            input_schema={"type": "object", "required": ["image_path", "prompt"], "properties": {"image_path": {"type": "string", "format": "file-path", "accepted_extensions": [".png", ".jpg", ".jpeg", ".webp"]}, "prompt": {"type": "string"}}},
            output_schema={"type": "object", "required": ["image_path", "description", "ocr_text", "model"], "properties": {"image_path": {"type": "string"}, "description": {"type": "string"}, "ocr_text": {"type": "string"}, "model": {"type": "string"}}},
            return_contract="Returns image_path, description, ocr_text, and model; script stdout must preserve needed fields.",
            example_call="from backend.services.runtime_tools import analyze_image_with_vision\nresult = analyze_image_with_vision(image_path=payload['image_path'], prompt=payload.get('prompt') or 'Describe this image.')",
            common_mistakes=["Do not use image_generation for image understanding.", "Do not ask the user to manually describe an uploaded image when this helper is selected.", "Do not write uploaded images into assets unless the user explicitly confirmed fixed Skill assets."],
            usage_policy="helper_preferred",
            required_env=["LLM_BASE_URL", "VISION_MODEL"],
            required_capabilities=["vision_understanding"],
            allowed_roles=["vision_analyzer", "generic_script", "composite_generator"],
        )
    ],
    snippets=[
        ToolSnippet(
            id="vision_understanding.analyze_image_with_vision",
            title="Understand an uploaded/runtime image",
            kind="minimal_usage",
            applies_to={"capabilities": ["vision_understanding"]},
            description="Call analyze_image_with_vision for existing image files. This is not image_generation and must not default uploaded images into assets.",
            code="from backend.services.runtime_tools import analyze_image_with_vision\n\nimage_path = payload['image_path']\nprompt = payload.get('prompt') or 'Describe this image.'\nresult = analyze_image_with_vision(image_path=image_path, prompt=prompt)\nreturn {'image_path': result['image_path'], 'description': result['description'], 'ocr_text': result.get('ocr_text', ''), 'model': result['model']}",
            expected_input_shape={"image_path": "path to .png/.jpg/.jpeg/.webp", "prompt": "string"},
            expected_output_shape={"image_path": "string", "description": "string", "ocr_text": "string", "model": "string"},
            return_rule="Return the helper result fields needed by downstream scripts.",
            anti_patterns=["Do not call image_generation.", "Do not require the user to manually describe the image.", "Do not copy Creator context uploads into assets by default."],
            requires=["vision_understanding"],
            usage_policy="helper_preferred",
            priority=150,
        )
    ],
    usage_policy="helper_preferred",
    prompt_guidance="图片内容理解使用 backend.services.runtime_tools.analyze_image_with_vision；不要混用 image_generation，不要默认写 assets。",
)

BUILTIN_TOOL_CAPABILITIES["text_generation"] = replace(
    BUILTIN_TOOL_CAPABILITIES["text_generation"],
    helper_imports=["generate_text_with_llm"],
    input_schema={"type": "object", "required": ["prompt"], "properties": {"prompt": {"type": "string"}}},
    output_schema={"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}},
    functions=[
        ToolFunctionManifest(
            function_name="generate_text_with_llm",
            import_path="backend.services.skill_runtime",
            short_description="Generate text with the host-configured language model.",
            when_to_use="Use when a script declares text_generation and needs open-ended text output from the host model.",
            signature="generate_text_with_llm(prompt: str, *, system: str = '', temperature: float = 0.7) -> str",
            input_schema={"type": "object", "required": ["prompt"], "properties": {"prompt": {"type": "string"}, "system": {"type": "string"}, "temperature": {"type": "number"}}},
            output_schema={"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}},
            return_contract="Returns generated text as a string; script stdout must map it into the declared output field.",
            example_call="from backend.services.skill_runtime import generate_text_with_llm\ntext = generate_text_with_llm(prompt=str(payload.get('prompt') or payload.get('text') or payload.get('user_request') or ''))",
            common_mistakes=["Do not call the helper and then ignore the returned text.", "Do not replace generated text with a fixed template."],
            usage_policy="helper_preferred",
            required_capabilities=["text_generation"],
        )
    ],
    usage_policy="helper_preferred",
    prompt_guidance="需要文本生成时，可优先使用 backend.services.skill_runtime.generate_text_with_llm；模型配置由宿主运行时注入。",
)

BUILTIN_TOOL_CAPABILITIES["file_output"] = replace(
    BUILTIN_TOOL_CAPABILITIES["file_output"],
    helper_imports=["create_text_file"],
    functions=[
        ToolFunctionManifest(
            function_name="create_text_file",
            import_path="backend.services.runtime_tools",
            short_description="Create a UTF-8 TXT file and return artifact paths.",
            when_to_use="Use for scripts that need to persist plain text as a .txt output artifact.",
            signature="create_text_file(text: str, filename: str | None = None, output_dir: str | None = None) -> dict[str, Any]",
            input_schema={"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}, "filename": {"type": "string"}, "output_dir": {"type": "string"}}},
            output_schema={"type": "object", "required": ["text_path", "file_outputs"], "properties": {"text_path": {"type": "string"}, "file_paths": {"type": "array", "items": {"type": "string"}}, "file_outputs": {"type": "array", "items": {"type": "string"}}}},
            artifact_outputs=[{"field": "text_path", "type": "file_path", "extensions": [".txt"], "root": "outputs"}],
            side_effects=["write_output_file"],
            return_contract="Returns {'text_path': path, 'file_paths': [path], 'file_outputs': [path]}; script stdout must preserve file_outputs.",
            example_call="from backend.services.runtime_tools import create_text_file\nresult = create_text_file(text=payload.get('text') or '', filename=payload.get('filename') or 'output.txt')",
            common_mistakes=["Do not use Path(...).write_text(...) directly when this helper is selected.", "Do not omit file_outputs from stdout."],
            usage_policy="helper_preferred",
            required_capabilities=["file_output"],
        )
    ],
    snippets=[
        ToolSnippet(
            id="file_output.create_text_file",
            title="Create TXT artifact",
            kind="file_output_usage",
            applies_to={"capabilities": ["file_output"]},
            description="Use the platform helper for TXT file outputs.",
            code="from backend.services.runtime_tools import create_text_file\n\nresult = create_text_file(text=str(payload.get('text') or payload.get('content') or ''), filename=payload.get('filename') or 'output.txt')\nreturn {'text_path': result['text_path'], 'file_outputs': result['file_outputs']}",
            expected_input_shape={"text": "string", "filename": "string?"},
            expected_output_shape={"text_path": "string", "file_outputs": ["string"]},
            return_rule="Return text_path and file_outputs from create_text_file.",
            anti_patterns=["Do not hand-write files with Path(...).write_text(...) when this helper is available.", "Do not omit file_outputs."],
            requires=["file_output"],
            usage_policy="helper_preferred",
            priority=120,
        )
    ],
    usage_policy="helper_preferred",
)

# Built-in document helpers are real callable functions, not just capability
# labels. Keep their manifest next to the registry entry so Creator can inject a
# safe import/call card only when implementation resolution selects the tool.
BUILTIN_TOOL_CAPABILITIES["pdf_generation"] = replace(
    BUILTIN_TOOL_CAPABILITIES["pdf_generation"],
    helper_imports=[
        "create_pdf",
        "create_pdf_document",
        "images_to_pdf",
        "merge_pdfs",
    ],
    functions=[
        ToolFunctionManifest(
            function_name="create_pdf",
            import_path="backend.services.runtime_tools",
            short_description="Create a Unicode-capable PDF from text and return artifact paths.",
            when_to_use="Use for scripts that need to produce a PDF artifact from text content.",
            signature="create_pdf(text: str | Iterable[Any], *, filename: str = 'output.pdf', output_dir: str | None = None, title: str | None = None) -> dict[str, Any]",
            input_schema={
                "type": "object",
                "required": ["text"],
                "properties": {
                    "text": {"type": "string"},
                    "filename": {"type": "string"},
                    "output_dir": {"type": "string"},
                    "title": {"type": "string"},
                },
            },
            output_schema={
                "type": "object",
                "required": ["pdf_path", "file_outputs"],
                "properties": {
                    "pdf_path": {"type": "string"},
                    "file_paths": {"type": "array", "items": {"type": "string"}},
                    "file_outputs": {"type": "array"},
                },
            },
            example_call=(
                "from backend.services.runtime_tools import create_pdf\n\n"
                "result = create_pdf(\n"
                "    text=payload[\"text_content\"],\n"
                "    filename=payload.get(\"output_filename\") or \"output.pdf\",\n"
                ")"
            ),
            usage_policy="helper_preferred",
            allowed_roles=["pdf_builder", "composite_generator"],
            required_capabilities=["pdf_generation"],
        ),
        ToolFunctionManifest(
            function_name="create_pdf_document",
            import_path="backend.services.runtime_tools",
            short_description="Create a structured PDF from document blocks and style controls.",
            when_to_use=(
                "Use when a script needs headings, paragraphs, images, tables, page breaks, "
                "font size, line spacing, indentation, or other structured document layout."
            ),
            signature=(
                "create_pdf_document(blocks: list[dict] | dict | str, *, "
                "styles: dict | None = None, filename: str = 'output.pdf', "
                "title: str | None = None) -> dict[str, Any]"
            ),
            input_schema={
                "type": "object",
                "required": ["blocks"],
                "properties": {
                    "blocks": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                    "styles": {"type": "object"},
                    "filename": {"type": "string"},
                    "title": {"type": "string"},
                },
            },
            output_schema={
                "type": "object",
                "required": ["pdf_path", "file_outputs"],
                "properties": {
                    "pdf_path": {"type": "string"},
                    "file_paths": {"type": "array", "items": {"type": "string"}},
                    "file_outputs": {"type": "array"},
                    "artifact_metadata": {"type": "object"},
                },
            },
            example_call=(
                "from backend.services.runtime_tools import create_pdf_document\n\n"
                "result = create_pdf_document(\n"
                "    blocks=[\n"
                "        {'type': 'title', 'text': payload.get('title') or 'Report'},\n"
                "        {'type': 'paragraph', 'text': payload['text_content']},\n"
                "    ],\n"
                "    styles={'body_font_size': 12, 'line_spacing': 1.5},\n"
                "    filename=payload.get('output_filename') or 'report.pdf',\n"
                ")"
            ),
            usage_policy="helper_preferred",
            allowed_roles=["pdf_builder", "composite_generator"],
            required_capabilities=["pdf_generation"],
        )
    ],
    snippets=[
        ToolSnippet(
            id="pdf_generation.create_pdf",
            title="Create a simple PDF",
            applies_to={"roles": ["pdf_builder", "composite_generator"], "capabilities": ["pdf_generation"]},
            description="Use the platform PDF helper for text-to-PDF artifact generation.",
            code=(
                "from backend.services.runtime_tools import create_pdf\n\n"
                "text = str(payload.get('text_content') or payload.get('text') or '')\n"
                "result = create_pdf(text=text, filename=payload.get('output_filename') or 'output.pdf')\n"
                "return {\n"
                "    'pdf_path': result['pdf_path'],\n"
                "    'file_outputs': result.get('file_outputs') or [result['pdf_path']],\n"
                "}"
            ),
            expected_input_shape={"text_content": "string", "output_filename": "string?"},
            expected_output_shape={"pdf_path": "string", "file_outputs": ["string"]},
            return_rule="Return pdf_path and file_outputs from create_pdf.",
            anti_patterns=[
                "Do not return {'pdf_path': result}; create_pdf returns a dict, not a string.",
                "Do not invent an import path.",
                "Do not use `from  import run`.",
                "Do not write outside OUTPUT_DIR.",
            ],
            requires=["pdf_generation"],
            usage_policy="helper_preferred",
            priority=120,
        ),
        ToolSnippet(
            id="pdf_generation.create_pdf_document",
            title="Create a structured PDF",
            applies_to={
                "roles": ["pdf_builder", "composite_generator"],
                "capabilities": ["pdf_generation"],
            },
            description="Use the platform structured PDF helper when layout, images, tables, or styles matter.",
            code=(
                "from backend.services.runtime_tools import create_pdf_document\n\n"
                "blocks = [\n"
                "    {'type': 'title', 'text': payload.get('title') or 'Report'},\n"
                "    {'type': 'paragraph', 'text': payload.get('text_content') or payload.get('text') or ''},\n"
                "]\n"
                "result = create_pdf_document(\n"
                "    blocks=blocks,\n"
                "    styles=payload.get('pdf_styles') or {},\n"
                "    filename=payload.get('output_filename') or 'report.pdf',\n"
                ")\n"
                "return {\n"
                "    'pdf_path': result['pdf_path'],\n"
                "    'file_outputs': result.get('file_outputs') or [result['pdf_path']],\n"
                "    'artifact_metadata': result.get('artifact_metadata', {}),\n"
                "}"
            ),
            expected_input_shape={
                "text_content": "string",
                "title": "string?",
                "pdf_styles": "object?",
                "output_filename": "string?",
            },
            expected_output_shape={
                "pdf_path": "string",
                "file_outputs": ["string"],
                "artifact_metadata": "object?",
            },
            return_rule="Return pdf_path and file_outputs from create_pdf_document.",
            anti_patterns=[
                "Do not manually write outside OUTPUT_DIR.",
                "Do not return the whole helper result as pdf_path.",
                "Use create_pdf_document instead of create_pdf when layout, images, or tables matter.",
            ],
            requires=["pdf_generation"],
            usage_policy="helper_preferred",
            priority=130,
        )
    ],
    usage_policy="helper_preferred",
)




def _document_generation_examples(helper: str, artifact: str) -> dict[str, Any]:
    if helper == "create_docx":
        return {
            "input_schema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "blocks": {"type": "array", "items": {"type": "object"}},
                    "paragraphs": {"type": "array"},
                    "title": {"type": "string"},
                    "output_filename": {"type": "string"},
                },
            },
            "example_call": (
                "from backend.services.runtime_tools import create_docx\n\n"
                "result = create_docx(\n"
                "    text=payload.get('text') or payload.get('content') or '',\n"
                "    blocks=payload.get('blocks'),\n"
                "    paragraphs=payload.get('paragraphs'),\n"
                "    title=payload.get('title'),\n"
                "    filename=payload.get('output_filename') or 'output.docx',\n"
                ")"
            ),
            "snippet": (
                "import json\n"
                "from backend.services.runtime_tools import create_docx\n\n"
                "text = payload.get('text') or payload.get('content') or ''\n"
                "blocks = payload.get('blocks')\n"
                "paragraphs = payload.get('paragraphs')\n"
                "result = create_docx(\n"
                "    text=text,\n"
                "    blocks=blocks,\n"
                "    paragraphs=paragraphs,\n"
                "    title=payload.get('title'),\n"
                "    filename=payload.get('output_filename') or 'output.docx',\n"
                ")\n"
                "# helper returns a dict; do not treat it as a string\n"
                "return {\n"
                "    'docx_path': result['docx_path'],\n"
                "    'file_paths': result.get('file_paths') or [result['docx_path']],\n"
                "    'file_outputs': result.get('file_outputs') or [result['docx_path']],\n"
                "}"
            ),
        }
    if helper == "create_pptx":
        return {
            "input_schema": {
                "type": "object",
                "properties": {
                    "slides": {"type": "array"},
                    "blocks": {"type": "array", "items": {"type": "object"}},
                    "title": {"type": "string"},
                    "output_filename": {"type": "string"},
                },
            },
            "example_call": (
                "from backend.services.runtime_tools import create_pptx\n\n"
                "slides = payload.get('slides') or payload.get('blocks') or [payload.get('text') or payload.get('content') or '']\n"
                "result = create_pptx(\n"
                "    slides=slides,\n"
                "    title=payload.get('title') or 'Generated Presentation',\n"
                "    filename=payload.get('output_filename') or 'output.pptx',\n"
                ")"
            ),
            "snippet": (
                "import json\n"
                "from backend.services.runtime_tools import create_pptx\n\n"
                "slides = payload.get('slides') or payload.get('blocks') or [payload.get('text') or payload.get('content') or '']\n"
                "result = create_pptx(\n"
                "    slides=slides,\n"
                "    title=payload.get('title') or 'Generated Presentation',\n"
                "    filename=payload.get('output_filename') or 'output.pptx',\n"
                ")\n"
                "# helper returns a dict; do not treat it as a string\n"
                "return {\n"
                "    'pptx_path': result['pptx_path'],\n"
                "    'file_paths': result.get('file_paths') or [result['pptx_path']],\n"
                "    'file_outputs': result.get('file_outputs') or [result['pptx_path']],\n"
                "}"
            ),
        }
    if helper == "create_xlsx":
        return {
            "input_schema": {
                "type": "object",
                "properties": {
                    "sheets": {"type": "array", "items": {"type": "object"}},
                    "headers": {"type": "array"},
                    "rows": {"type": "array"},
                    "output_filename": {"type": "string"},
                },
            },
            "example_call": (
                "from backend.services.runtime_tools import create_xlsx\n\n"
                "result = create_xlsx(\n"
                "    sheets=payload.get('sheets'),\n"
                "    headers=payload.get('headers'),\n"
                "    rows=payload.get('rows') or [],\n"
                "    filename=payload.get('output_filename') or 'output.xlsx',\n"
                ")"
            ),
            "snippet": (
                "import json\n"
                "from backend.services.runtime_tools import create_xlsx\n\n"
                "sheets = payload.get('sheets')\n"
                "headers = payload.get('headers')\n"
                "rows = payload.get('rows') or []\n"
                "result = create_xlsx(\n"
                "    sheets=sheets,\n"
                "    headers=headers,\n"
                "    rows=rows,\n"
                "    filename=payload.get('output_filename') or 'output.xlsx',\n"
                ")\n"
                "# helper returns a dict; do not treat it as a string\n"
                "return {\n"
                "    'xlsx_path': result['xlsx_path'],\n"
                "    'file_paths': result.get('file_paths') or [result['xlsx_path']],\n"
                "    'file_outputs': result.get('file_outputs') or [result['xlsx_path']],\n"
                "}"
            ),
        }
    return {
        "input_schema": {
            "type": "object",
            "properties": {
                "headers": {"type": "array"},
                "rows": {"type": "array"},
                "output_filename": {"type": "string"},
            },
        },
        "example_call": (
            "from backend.services.runtime_tools import create_csv\n\n"
            "result = create_csv(\n"
            "    headers=payload.get('headers'),\n"
            "    rows=payload.get('rows') or [],\n"
            "    filename=payload.get('output_filename') or 'output.csv',\n"
            ")"
        ),
        "snippet": (
            "import json\n"
            "from backend.services.runtime_tools import create_csv\n\n"
            "headers = payload.get('headers')\n"
            "rows = payload.get('rows') or []\n"
            "result = create_csv(\n"
            "    headers=headers,\n"
            "    rows=rows,\n"
            "    filename=payload.get('output_filename') or 'output.csv',\n"
            ")\n"
            "# helper returns a dict; do not treat it as a string\n"
            "return {\n"
            "    'csv_path': result['csv_path'],\n"
            "    'file_paths': result.get('file_paths') or [result['csv_path']],\n"
            "    'file_outputs': result.get('file_outputs') or [result['csv_path']],\n"
            "}"
        ),
    }


def _document_artifact_capability(capability: str, helper: str, artifact: str, package: str | None, imports: list[str], display: str) -> None:
    deps = [{"package": package, "imports": imports}] if package else []
    examples = _document_generation_examples(helper, artifact)
    output_schema = {
        "type": "object",
        "required": [f"{artifact}_path", "file_paths", "file_outputs"],
        "properties": {
            f"{artifact}_path": {"type": "string"},
            "file_paths": {"type": "array", "items": {"type": "string"}},
            "file_outputs": {"type": "array", "items": {"type": "string"}},
        },
    }
    BUILTIN_TOOL_CAPABILITIES[capability] = replace(
        BUILTIN_TOOL_CAPABILITIES[capability],
        helper_imports=[helper],
        dependencies=deps or BUILTIN_TOOL_CAPABILITIES[capability].dependencies,
        input_schema=examples["input_schema"],
        output_schema=output_schema,
        artifact_outputs=[{"field": f"{artifact}_path", "type": artifact}, {"field": "file_paths", "type": "file_list"}, {"field": "file_outputs", "type": "file_list"}],
        functions=[ToolFunctionManifest(
            function_name=helper,
            import_path="backend.services.runtime_tools",
            short_description=f"Create a {display} artifact and return platform artifact paths.",
            when_to_use=f"Use for user requests that explicitly need {display}/document/table output; do not route this to PDF unless the user asks for PDF.",
            signature=f"{helper}(..., filename: str = 'output.{artifact}') -> dict[str, Any]",
            input_schema=examples["input_schema"],
            output_schema=output_schema,
            artifact_outputs=[{"field": f"{artifact}_path", "type": artifact}, {"field": "file_paths", "type": "file_list"}, {"field": "file_outputs", "type": "file_list"}],
            example_call=examples["example_call"],
            example_return=f"{{'{artifact}_path': '/outputs/output.{artifact}', 'file_paths': ['/outputs/output.{artifact}'], 'file_outputs': ['/outputs/output.{artifact}']}}",
            example_stdout=f"print(json.dumps({{'{artifact}_path': result['{artifact}_path'], 'file_paths': result.get('file_paths') or [result['{artifact}_path']], 'file_outputs': result.get('file_outputs') or [result['{artifact}_path']]}}))",
            common_mistakes=[
                f"{helper} returns a dict, not a string; preserve {artifact}_path, file_paths, and file_outputs in stdout JSON.",
                "Do not drop file_outputs.",
                "Do not write outside OUTPUT_DIR.",
                "Do not convert Office/CSV requests to PDF unless PDF is explicitly requested.",
            ],
            trial_mode_behavior="Creates a minimal valid artifact under OUTPUT_DIR during trial runs.",
            usage_policy="helper_preferred",
            required_capabilities=[capability],
        )],
        snippets=[ToolSnippet(
            id=f"{capability}.{helper}",
            title=f"Create {display} artifact",
            applies_to={"capabilities": [capability]},
            description=f"Import and call {helper} with content fields; return stdout JSON with path fields for artifact validation.",
            code=examples["snippet"],
            expected_input_shape=examples["input_schema"].get("properties", {}),
            expected_output_shape={f"{artifact}_path": "string", "file_paths": ["string"], "file_outputs": ["string"]},
            return_rule=f"Final stdout JSON must preserve {artifact}_path, file_paths, and file_outputs from the helper result.",
            anti_patterns=[
                f"Do not call {helper}(filename=...) without passing content fields; that creates empty or generic artifacts.",
                "Do not treat the helper result as a string; it is a dict.",
                "Do not drop file_outputs.",
                "Do not route Office/CSV output to PDF.",
            ],
            requires=[capability], usage_policy="helper_preferred", priority=125,
        )],
        usage_policy="helper_preferred",
        trial_mode="minimal_file",
    )

_document_artifact_capability("docx_generation", "create_docx", "docx", "python-docx", ["docx"], "Word DOCX")
_document_artifact_capability("pptx_generation", "create_pptx", "pptx", "python-pptx", ["pptx"], "PowerPoint PPTX")
_document_artifact_capability("xlsx_generation", "create_xlsx", "xlsx", "openpyxl", ["openpyxl"], "Excel XLSX")
_document_artifact_capability("csv_generation", "create_csv", "csv", None, [], "CSV")


def _read_capability(capability: str, helper: str, source_ext: str, package: str | None, imports: list[str]) -> None:
    deps = [{"package": package, "imports": imports}] if package else []
    BUILTIN_TOOL_CAPABILITIES[capability] = replace(
        BUILTIN_TOOL_CAPABILITIES[capability],
        helper_imports=[helper],
        dependencies=deps or BUILTIN_TOOL_CAPABILITIES[capability].dependencies,
        output_schema={"type": "object", "properties": {"text": {"type": "string"}, "rows": {"type": "array"}, "columns": {"type": "array"}, "row_count": {"type": "integer"}, "source_path": {"type": "string"}}},
        functions=[ToolFunctionManifest(
            function_name=helper,
            import_path="backend.services.runtime_tools",
            short_description=f"Read {source_ext.upper()} input into structured JSON.",
            when_to_use=f"Use when the user supplies or asks to parse/read {source_ext.upper()} content; do not choose PDF parsing for Office/CSV files.",
            signature=f"{helper}(path: str, ...) -> dict[str, Any]",
            input_schema={"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}},
            output_schema={"type": "object", "properties": {"text": {"type": "string"}, "rows": {"type": "array"}, "columns": {"type": "array"}, "row_count": {"type": "integer"}, "source_path": {"type": "string"}}},
            example_call=f"from backend.services.runtime_tools import {helper}\nresult = {helper}(payload['input_path'])",
            example_return="{'text': '...', 'source_path': '/work/input.%s'}" % source_ext,
            example_stdout="print(json.dumps(result, ensure_ascii=False))",
            common_mistakes=["Do not use PDF readers for DOCX/PPTX/XLSX/CSV files.", "Helper returns a dict; serialize the dict as JSON."],
            trial_mode_behavior="Returns stable mock structured JSON during SKILL_TRIAL_RUN.",
            usage_policy="helper_preferred",
            required_capabilities=[capability],
        )],
        snippets=[ToolSnippet(
            id=f"{capability}.{helper}", title=f"Read {source_ext.upper()}", applies_to={"capabilities": [capability]},
            code=f"import json\nfrom backend.services.runtime_tools import {helper}\n\nresult = {helper}(payload['input_path'])\nreturn result",
            return_rule="Return the helper dict as stdout JSON, preserving text/rows/columns/source_path fields.",
            anti_patterns=["Do not treat helper result as a string.", "Do not route Office/CSV parsing to PDF."],
            requires=[capability], usage_policy="helper_preferred", priority=110,
        )],
        usage_policy="helper_preferred",
    )

_read_capability("docx_parsing", "read_docx_text", "docx", "python-docx", ["docx"])
_read_capability("pptx_parsing", "read_pptx_text", "pptx", "python-pptx", ["pptx"])
_read_capability("spreadsheet_read", "read_spreadsheet", "xlsx", "openpyxl", ["openpyxl"])
_read_capability("csv_read", "read_csv", "csv", None, [])

BUILTIN_TOOL_CAPABILITIES["unified_file_text_read"] = replace(
    BUILTIN_TOOL_CAPABILITIES["unified_file_text_read"],
    helper_imports=["read_file_text"],
    input_schema={"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}},
    output_schema={"type": "object", "required": ["text", "source_path", "file_type", "metadata"], "properties": {"text": {"type": "string"}, "source_path": {"type": "string"}, "file_type": {"type": "string"}, "metadata": {"type": "object"}}},
    functions=[ToolFunctionManifest(
        function_name="read_file_text",
        import_path="backend.services.runtime_tools",
        short_description="Read PDF/DOCX/PPTX/XLSX/CSV/TXT/MD into a uniform dict with text.",
        when_to_use="Use for scripts that need multi-format text ingestion; prefer this over guessing per-format helper names.",
        signature="read_file_text(path: str) -> dict[str, Any]",
        input_schema={"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}},
        output_schema={"type": "object", "required": ["text", "source_path", "file_type", "metadata"], "properties": {"text": {"type": "string"}, "source_path": {"type": "string"}, "file_type": {"type": "string"}, "metadata": {"type": "object"}}},
        return_contract="Returns a dict with text, source_path, file_type, and metadata; never a raw string.",
        example_call="from backend.services.runtime_tools import read_file_text\nresult = read_file_text(payload['input_path'])\ntext = result['text']",
        common_mistakes=["Do not use read_pdf_text.", "Do not use read_xlsx_text.", "Do not use read_txt_text.", "Helper returns a dict, not str."],
        usage_policy="helper_preferred",
        allowed_roles=["generic_script", "document_parser", "aggregator"],
        required_capabilities=["unified_file_text_read"],
    )],
    snippets=[ToolSnippet(
        id="unified_file_text_read.read_file_text",
        title="Read any supported file as text",
        applies_to={"capabilities": ["unified_file_text_read"]},
        code="from backend.services.runtime_tools import read_file_text\n\nresult = read_file_text(payload['input_path'])\ntext = result['text']\nreturn {'text': text, 'source_path': result['source_path'], 'file_type': result['file_type']}",
        return_rule="Use result['text']; preserve source_path/file_type when useful.",
        anti_patterns=["Do not invent read_pdf_text/read_xlsx_text/read_txt_text.", "Do not join helper result directly; it is a dict."],
        requires=["unified_file_text_read"],
        usage_policy="helper_preferred",
        priority=140,
    )],
    usage_policy="helper_preferred",
)

# Semantic metadata for built-in text/PDF readers.
BUILTIN_TOOL_CAPABILITIES["pdf_parsing"] = replace(
    BUILTIN_TOOL_CAPABILITIES["pdf_parsing"],
    capability_aliases=["pdf_parsing", "pdf_text_extraction", "document_parse"],
    semantic_tags=["pdf", "text_extraction", "document_parse"],
    accepted_input_extensions=[".pdf"],
    output_content_types=["text"],
    task_verbs=["parse", "extract", "read", "summarize_preprocess"],
    domain_terms=["pdf", "pdf解析", "提取pdf文本", "pdf转文本"],
    preference_score=0.55,
    tool_quality_score=0.55,
)
BUILTIN_TOOL_CAPABILITIES["unified_file_text_read"] = replace(
    BUILTIN_TOOL_CAPABILITIES["unified_file_text_read"],
    capability_aliases=["file_text_read", "multi_format_text_read", "pdf_text_extraction", "document_text_extraction"],
    semantic_tags=["pdf", "docx", "pptx", "spreadsheet", "csv", "markdown", "text_extraction"],
    accepted_input_extensions=[".pdf", ".docx", ".pptx", ".xlsx", ".xlsm", ".csv", ".tsv", ".txt", ".md"],
    output_content_types=["text"],
    task_verbs=["read", "extract", "parse", "summarize_preprocess"],
    domain_terms=["读取文本", "提取文本", "pdf转文本", "普通pdf纯文本", "摘要", "信息抽取"],
    preference_score=0.65,
    tool_quality_score=0.65,
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


def _env_name(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", (value or "").strip().upper()).strip("_")
    if not text:
        text = "TOOL_SECRET"
    if text[0].isdigit():
        text = f"TOOL_{text}"
    return text[:96]


def _default_secret_env_for_tool(tool_name: str) -> str:
    return f"{_env_name(tool_name or 'custom_tool')}_API_KEY"


def _strip_code_fence(text: str) -> str:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:python|py)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    return raw


def _schema_properties(schema: Any) -> dict[str, Any]:
    return schema.get("properties") if isinstance(schema, dict) and isinstance(schema.get("properties"), dict) else {}


def _schema_required(schema: Any) -> list[str]:
    required = schema.get("required") if isinstance(schema, dict) else []
    return [str(item) for item in required] if isinstance(required, list) else []


def _canonical_schema(schema: Any, *, fallback_properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    """Return canonical JSON Schema object.

    Compatibility: legacy frontend field-map input like
    {"field": {"type":"string"}} is structurally migrated to canonical
    schema. This is not business-field inference.
    """
    if isinstance(schema, dict) and schema.get("type") == "object" and isinstance(schema.get("properties"), dict):
        result = dict(schema)
        result["required"] = _schema_required(result)
        return result
    if isinstance(schema, dict) and "type" not in schema and "properties" not in schema:
        if all(isinstance(value, dict) for value in schema.values()):
            return {"type": "object", "properties": dict(schema), "required": required or []}
    return {"type": "object", "properties": fallback_properties or {}, "required": required or []}


def _normalized_preview(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        return {"type": "object", "keys": sorted(str(k) for k in data.keys())[:50], "preview": {str(k): data[k] for k in list(data.keys())[:10]}}
    if isinstance(data, list):
        return {"type": "array", "length": len(data), "first_item": data[0] if data else None}
    return {"type": type(data).__name__, "value": data}


def _redact_secrets(value: Any, secret_values: set[str] | None = None) -> Any:
    secrets = {str(item) for item in (secret_values or set()) if str(item)}
    if isinstance(value, str):
        out = value
        for secret in secrets:
            if secret:
                out = out.replace(secret, "***")
        return out
    if isinstance(value, dict):
        return {k: _redact_secrets(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_secrets(v, secrets) for v in value]
    return value


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


def _normalize_dependency_record(dep: Any) -> dict[str, Any]:
    if isinstance(dep, dict):
        imports = dep.get("imports") or dep.get("import_names") or []
        if isinstance(imports, str):
            imports = [imports]
        if not isinstance(imports, list):
            imports = []
        return {
            "package": str(dep.get("package") or dep.get("name") or dep.get("pip") or "").strip(),
            "imports": [str(item).strip() for item in imports if str(item).strip()],
            "version": str(dep.get("version") or "").strip(),
        }
    if isinstance(dep, str):
        return {"package": dep.strip(), "imports": [], "version": ""}
    return {"package": "", "imports": [], "version": ""}


def _manifest_dependency_records(manifest: dict[str, Any] | None) -> list[dict[str, Any]]:
    deps = (manifest or {}).get("dependencies")
    if not isinstance(deps, list):
        return []
    return [r for r in (_normalize_dependency_record(dep) for dep in deps) if r.get("package") or r.get("imports")]


def _script_import_roots(script_code: str) -> list[str]:
    """Return importable module roots used by the generated script.

    This is structural AST extraction. It is not tied to any business tool type.
    The result is used only to keep dependency declarations aligned with the
    actual imports in the generated Python script.
    """
    try:
        tree = ast.parse(script_code or "")
    except SyntaxError:
        return []

    roots: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = str(alias.name or "").split(".")[0].strip()
                if root and root not in roots:
                    roots.append(root)
        elif isinstance(node, ast.ImportFrom):
            root = str(node.module or "").split(".")[0].strip()
            if root and root not in roots:
                roots.append(root)
    return roots


def _normalized_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", str(value or "").strip().lower())


def _distribution_import_roots(package: str) -> list[str]:
    """Return import roots advertised by installed package metadata.

    This is a generic package-metadata lookup, not a business rule table.  It
    covers common cases where the pip distribution name and the import module
    name differ.  If metadata is unavailable, callers fall back to the generated
    script's AST imports without blocking authoring.
    """
    package_key = _normalized_distribution_name(package)
    if not package_key:
        return []
    roots: list[str] = []
    try:
        mapping = importlib.metadata.packages_distributions()
    except Exception:
        mapping = {}
    for root, dists in mapping.items():
        if not isinstance(dists, list):
            continue
        for dist in dists:
            if _normalized_distribution_name(dist) == package_key:
                if root and root not in roots:
                    roots.append(root)
                break
    return roots


def _safe_find_spec(module_name: str) -> Any:
    """Safe wrapper around importlib.util.find_spec.

    ``find_spec`` can raise ModuleNotFoundError for dotted names when a parent
    package is absent, for example ``sklearn.svm.SVC`` when ``sklearn`` is not
    installed in the backend process.  Dependency normalization must never crash
    the authoring stream; an absent module simply means the dependency should be
    installed later in the temporary execution environment.
    """
    name = str(module_name or "").strip()
    if not name:
        return None
    try:
        return importlib.util.find_spec(name)
    except (ImportError, ModuleNotFoundError, AttributeError, ValueError):
        return None


def _import_root_from_name(name: str) -> str:
    return str(name or "").strip().split(".")[0].strip()


def _unique_strings(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _normalize_dependencies_for_script(manifest: dict[str, Any], script_code: str) -> dict[str, Any]:
    """Normalize dependency import names against the actual generated script.

    This is generic protocol repair, not business logic.  The planner may emit
    class/function paths such as ``sklearn.svm.SVC`` in ``dependencies[].imports``.
    Those paths are not module roots and may not be importable before the package
    is installed.  We therefore align declared imports to AST import roots used
    by the generated script and never require the dependency to exist in the
    backend process before the temporary environment installs it.
    """
    payload = dict(manifest or {})
    deps = payload.get("dependencies")
    if not isinstance(deps, list):
        return payload

    script_roots = _unique_strings(_script_import_roots(script_code))
    script_root_set = set(script_roots)
    normalized: list[dict[str, Any]] = []

    for dep in deps:
        record = _normalize_dependency_record(dep)
        package = str(record.get("package") or "").strip()
        imports = [str(item).strip() for item in record.get("imports") or [] if str(item).strip()]

        metadata_roots = _distribution_import_roots(package)
        declared_roots = _unique_strings([_import_root_from_name(name) for name in imports])
        fixed_imports: list[str] = []

        # Prefer metadata roots that are actually imported by the generated script.
        if metadata_roots:
            fixed_imports = [root for root in metadata_roots if root in script_root_set] or metadata_roots

        # If metadata is unavailable because the package is not installed in the
        # backend, use the root of the model-declared import path.
        if not fixed_imports and declared_roots:
            fixed_imports = [root for root in declared_roots if root in script_root_set] or declared_roots

        # Last resort: keep already importable declared modules, but use a safe
        # find_spec wrapper so missing parent packages do not crash the request.
        if not fixed_imports:
            fixed_imports = [name for name in imports if _safe_find_spec(name) is not None]

        record["imports"] = _unique_strings(fixed_imports or script_roots or declared_roots or imports)
        normalized.append(record)

    payload["dependencies"] = normalized
    return payload


def _dependency_available(dep: Any) -> bool:
    record = _normalize_dependency_record(dep)
    imports = record.get("imports")
    if not isinstance(imports, list) or not imports:
        return False if record.get("package") else True
    return all(_safe_find_spec(str(module)) is not None for module in imports)



def _dependency_packages_for_install(manifest: dict[str, Any] | None) -> list[str]:
    packages: list[str] = []
    for record in _manifest_dependency_records(manifest):
        package = str(record.get("package") or "").strip()
        version = str(record.get("version") or "").strip()
        if not package or _dependency_available(record):
            continue
        spec = f"{package}{version}" if version and version.startswith(("==", ">=", "<=", "~=", ">", "<", "!=")) else package
        if spec not in packages:
            packages.append(spec)
    return packages


def _install_dependencies_to_target(packages: list[str], target_dir: Path, *, timeout_seconds: int = 180) -> dict[str, Any]:
    packages = [str(item).strip() for item in packages if str(item).strip()]
    if not packages:
        return {"success": True, "skipped": True, "installed": [], "target_dir": str(target_dir), "errors": []}
    target_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--target", str(target_dir), *packages]
    try:
        completed = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout_seconds, check=False)
    except Exception as exc:
        return {"success": False, "skipped": False, "installed": [], "target_dir": str(target_dir), "errors": [str(exc)]}
    if completed.returncode != 0:
        return {"success": False, "skipped": False, "installed": [], "target_dir": str(target_dir), "errors": [completed.stderr[-4000:] or completed.stdout[-4000:] or f"pip exited with code {completed.returncode}"]}
    return {"success": True, "skipped": False, "installed": packages, "target_dir": str(target_dir), "errors": []}


def _normalize_secret_record(item: Any, *, fallback_env: str = "") -> dict[str, Any] | None:
    if isinstance(item, str):
        env = _env_name(item)
        return {"env": env, "name": env, "description": "", "required": True}
    if not isinstance(item, dict):
        return None
    env = str(item.get("env") or item.get("name") or item.get("secret_env") or item.get("env_name") or "").strip() or fallback_env
    if not env:
        return None
    env = _env_name(env)
    return {"env": env, "name": str(item.get("name") or env), "description": str(item.get("description") or item.get("label") or ""), "required": bool(item.get("required", True))}


def _normalize_auth_plan(manifest: dict[str, Any] | None, request: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize the authentication contract only from explicit auth/secret signals.

    Important distinction:
    - permissions.env means "the generated script is allowed to read these environment
      variables". It can contain ordinary runtime knobs such as USER_AGENT or HTTP_TIMEOUT.
    - auth.secrets / required_secrets means "the user must configure credentials before
      registration/live execution".

    Therefore permissions.env must never by itself upgrade auth.required=no to yes.
    """
    manifest = manifest if isinstance(manifest, dict) else {}
    request = request if isinstance(request, dict) else {}
    tool_name = str(manifest.get("tool_name") or manifest.get("name") or request.get("tool_name") or "custom_tool")
    auth = manifest.get("auth") if isinstance(manifest.get("auth"), dict) else {}

    raw_required = str(auth.get("required") or auth.get("requires_auth") or "").strip().lower()
    if raw_required in {"true", "yes", "required", "1"}:
        required = "yes"
    elif raw_required in {"false", "no", "none", "not_required", "0", "public", "anonymous"}:
        required = "no"
    elif raw_required == "unknown":
        required = "unknown"
    else:
        required = "yes" if request.get("needs_secret") or request.get("requires_auth") else "no"

    secret_items: list[Any] = []
    for key in ("secrets", "required_secrets"):
        value = auth.get(key)
        if isinstance(value, list):
            secret_items.extend(value)

    # required_secrets is credential material. required_env / permissions.env are general
    # environment permissions and must not be treated as authentication.
    value = manifest.get("required_secrets")
    if isinstance(value, list):
        secret_items.extend(value)

    secrets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in secret_items:
        record = _normalize_secret_record(item)
        if record and record["env"] not in seen:
            seen.add(record["env"])
            secrets.append(record)

    if required == "yes" and not secrets:
        reason = str(auth.get("reason") or auth.get("description") or "").strip().lower()
        no_secret_markers = [
            "no additional secrets",
            "no secrets required",
            "no api key",
            "no token",
            "无需密钥",
            "不需要密钥",
            "不需要额外密钥",
            "无需 token",
            "不需要 token",
            "无需 api key",
            "不需要 api key",
        ]

        if any(marker in reason for marker in no_secret_markers):
            required = "no"
        else:
            env = _default_secret_env_for_tool(tool_name)
            secrets.append({
                "env": env,
                "name": env,
                "description": "API key or token required by this tool.",
                "required": True,
            })

    # Only explicit secrets can override a missing/unknown auth decision. Do not let
    # permissions.env or required_env do this.
    if required == "no" and secrets:
        required = "yes"

    return {"required": required, "reason": str(auth.get("reason") or auth.get("description") or ""), "secrets": secrets}


def _auth_secret_env_names(manifest: dict[str, Any] | None) -> list[str]:
    """Environment variables that are credentials and block registration if missing."""
    names: list[str] = []
    for secret in _normalize_auth_plan(manifest).get("secrets") or []:
        if isinstance(secret, dict) and secret.get("env") and secret.get("required", True):
            names.append(str(secret["env"]))
    normalized: list[str] = []
    seen: set[str] = set()
    for name in names:
        env = _env_name(name)
        if env not in seen:
            seen.add(env)
            normalized.append(env)
    return normalized


def _declared_env_names(manifest: dict[str, Any] | None) -> list[str]:
    """All env vars the script is allowed to read, including non-secret knobs."""
    manifest = manifest if isinstance(manifest, dict) else {}
    names: list[str] = []
    permissions = manifest.get("permissions") if isinstance(manifest.get("permissions"), dict) else {}
    if isinstance(permissions.get("env"), list):
        names.extend(str(item) for item in permissions["env"] if str(item).strip())
    if isinstance(manifest.get("required_env"), list):
        names.extend(str(item) for item in manifest["required_env"] if str(item).strip())
    names.extend(_auth_secret_env_names(manifest))
    normalized: list[str] = []
    seen: set[str] = set()
    for name in names:
        env = _env_name(name)
        if env not in seen:
            seen.add(env)
            normalized.append(env)
    return normalized


def _sync_auth_into_manifest(manifest: dict[str, Any], request: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(manifest or {})
    payload["runtime_type"] = "python_script"
    name = _slug(str(payload.get("tool_name") or payload.get("name") or (request or {}).get("tool_name") or "custom_tool"))
    payload["tool_name"] = name
    payload["name"] = name

    auth = _normalize_auth_plan(payload, request)
    secret_env_names = [str(secret["env"]) for secret in auth.get("secrets") or [] if isinstance(secret, dict) and secret.get("env")]

    permissions = payload.get("permissions") if isinstance(payload.get("permissions"), dict) else {}
    env_list = [_env_name(item) for item in permissions.get("env", [])] if isinstance(permissions.get("env"), list) else []
    for env in secret_env_names:
        if env not in env_list:
            env_list.append(env)

    payload["permissions"] = {
        "network": bool(permissions.get("network")),
        "read_files": bool(permissions.get("read_files")),
        "write_files": bool(permissions.get("write_files")),
        "subprocess": bool(permissions.get("subprocess")),
        "env": env_list,
        "timeout_seconds": int(permissions.get("timeout_seconds") or payload.get("timeout_seconds") or 30),
    }
    payload["auth"] = auth

    # required_env remains a non-secret runtime/config env declaration. It should not
    # imply auth. required_secrets is the only credential declaration.
    existing_required_env = payload.get("required_env") if isinstance(payload.get("required_env"), list) else []
    payload["required_env"] = [_env_name(item) for item in existing_required_env if str(item).strip()]
    payload["required_secrets"] = secret_env_names
    return payload


def _script_env_usage(script_code: str) -> list[str]:
    try:
        tree = ast.parse(script_code or "")
    except SyntaxError:
        return []
    names: set[str] = set()
    def literal(node: ast.AST | None) -> str | None:
        return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):
            target = node.value
            if isinstance(target, ast.Attribute) and target.attr == "environ" and isinstance(target.value, ast.Name) and target.value.id == "os":
                env = literal(node.slice)
                if env:
                    names.add(_env_name(env))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            func = node.func
            if func.attr == "getenv" and isinstance(func.value, ast.Name) and func.value.id == "os" and node.args:
                env = literal(node.args[0])
                if env:
                    names.add(_env_name(env))
            elif func.attr == "get" and isinstance(func.value, ast.Attribute) and func.value.attr == "environ" and isinstance(func.value.value, ast.Name) and func.value.value.id == "os" and node.args:
                env = literal(node.args[0])
                if env:
                    names.add(_env_name(env))
    return sorted(names)


def _auth_gate_for_manifest(manifest: dict[str, Any], *, require_config: bool = True) -> dict[str, Any]:
    manifest = manifest if isinstance(manifest, dict) else {}
    auth = _normalize_auth_plan(manifest)
    declared_env = _declared_env_names(manifest)
    secret_env = _auth_secret_env_names(manifest)
    configured_secret_env = [name for name in secret_env if bool(os.environ.get(name))]
    missing_secret_env = [name for name in secret_env if not os.environ.get(name)]
    configured_declared_env = [name for name in declared_env if bool(os.environ.get(name))]
    missing_declared_env = [name for name in declared_env if not os.environ.get(name)]

    required = auth.get("required") in {"yes", "unknown"} or bool(secret_env)
    status = "not_required" if not required else "needs_config" if missing_secret_env else "configured"
    return {
        "required": bool(required),
        "status": status,
        "reason": auth.get("reason") or "",
        "required_env": secret_env,
        "required_secrets": secret_env,
        "declared_env": declared_env,
        "optional_env": [name for name in declared_env if name not in secret_env],
        "configured_env": configured_declared_env,
        "configured_secrets": configured_secret_env,
        "missing_env": missing_secret_env,
        "missing_secrets": missing_secret_env,
        "missing_optional_env": [name for name in missing_declared_env if name not in secret_env],
        "can_run_trial": not missing_secret_env,
        "can_register": not missing_secret_env,
        "block_registration": bool(require_config and missing_secret_env),
        "auth": auth,
    }


def _auth_validation_report(manifest: dict[str, Any], script_code: str, *, require_auth_config: bool = True) -> dict[str, Any]:
    declared_env = set(_declared_env_names(manifest))
    secret_env = set(_auth_secret_env_names(manifest))
    used_env = set(_script_env_usage(script_code)) - _RUNTIME_ENV_NAMES
    undeclared = sorted(name for name in used_env if name not in declared_env)
    gate = _auth_gate_for_manifest(manifest, require_config=require_auth_config)
    auth = _normalize_auth_plan(manifest)
    errors: list[str] = []
    if undeclared:
        errors.append("script reads undeclared environment variables: " + ", ".join(undeclared))
    if require_auth_config and gate.get("missing_secrets"):
        errors.append("required auth configuration is missing: " + ", ".join(gate["missing_secrets"]))
    warnings: list[str] = []
    if secret_env and not (used_env & secret_env):
        warnings.append("manifest declares auth secrets, but script does not read them; confirm whether auth is actually needed.")
    unused_optional = sorted(name for name in declared_env - secret_env if name not in used_env)
    if unused_optional:
        warnings.append("declared non-secret env variables are not used by script: " + ", ".join(unused_optional))
    return {
        "errors": errors,
        "warnings": warnings,
        "used_env": sorted(used_env),
        "declared_env": sorted(declared_env),
        "declared_secret_env": sorted(secret_env),
        "undeclared_env": undeclared,
        "auth_gate": gate,
        "script_uses_auth": bool(used_env & secret_env),
        "planner_declared_auth": bool(auth.get("required") in {"yes", "unknown"} or secret_env),
    }


def _extract_env_values_from_payload(payload: dict[str, Any], manifest: dict[str, Any] | None = None) -> dict[str, str]:
    data = dict(payload or {})
    manifest = manifest if isinstance(manifest, dict) else {}
    declared = _declared_env_names(manifest)
    env_values: dict[str, str] = {}
    def put(name: str, value: Any) -> None:
        if value is None:
            return
        text = str(value)
        if not text or text in {"***", "********", "<redacted>"}:
            return
        env_values[_env_name(name)] = text
    for key in ("env", "secrets", "secret_values", "auth_config"):
        value = data.get(key)
        if isinstance(value, dict):
            for name, secret in value.items():
                put(str(name), secret)
    config = data.get("config") if isinstance(data.get("config"), dict) else {}
    for name in declared:
        if name in config:
            put(name, config.get(name))
    single_value = data.get("secret_value") or data.get("api_key") or data.get("token") or data.get("password")
    explicit_env = data.get("secret_env") or data.get("env_name") or data.get("api_key_env") or data.get("token_env")
    if single_value:
        if explicit_env:
            put(str(explicit_env), single_value)
        elif len(declared) == 1:
            put(declared[0], single_value)
    return env_values


def _apply_env_values(env_values: dict[str, str]) -> None:
    for name, value in (env_values or {}).items():
        if name and value:
            os.environ[_env_name(name)] = str(value)


def safe_adapter_path_for_tool_name(tool_name: str) -> str:
    path = (CUSTOM_TOOL_ADAPTER_DIR / f"{_slug(tool_name)}.py").resolve()
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _safe_adapter_module_path_for_name(tool_name: str) -> Path:
    return (CUSTOM_TOOL_ADAPTER_DIR / f"{_slug(tool_name)}.py").resolve()


# Registry / snippets compatibility functions.
def _function_from_dict(data: dict[str, Any]) -> ToolFunctionManifest:
    known = {field.name for field in dataclasses_fields(ToolFunctionManifest)}
    payload = {key: value for key, value in (data or {}).items() if key in known}
    payload.setdefault("function_name", "run")
    payload.setdefault("import_path", "")
    payload.setdefault("short_description", "")
    payload.setdefault("when_to_use", "")
    payload.setdefault("signature", "run(payload: dict, config: dict | None = None) -> dict")
    return ToolFunctionManifest(**payload)


def _snippet_from_dict(data: dict[str, Any]) -> ToolSnippet:
    known = {field.name for field in dataclasses_fields(ToolSnippet)}
    payload = {key: value for key, value in (data or {}).items() if key in known}
    payload.setdefault("id", _slug(str(payload.get("title") or "snippet"), fallback="snippet"))
    payload.setdefault("title", str(payload["id"]).replace("_", " ").title())
    if payload.get("kind") not in _ALLOWED_SNIPPET_KINDS:
        payload["kind"] = "minimal_usage"
    if not isinstance(payload.get("applies_to"), dict):
        payload["applies_to"] = {}
    for key in ("expected_input_shape", "expected_output_shape"):
        if not isinstance(payload.get(key), dict):
            payload[key] = {}
    for key in ("anti_patterns", "requires"):
        if not isinstance(payload.get(key), list):
            payload[key] = []
        payload[key] = [str(item) for item in payload[key] if str(item)]
    if payload.get("usage_policy") not in _ALLOWED_USAGE_POLICIES:
        payload["usage_policy"] = "self_implementation_allowed"
    try:
        payload["priority"] = int(payload.get("priority") or 0)
    except Exception:
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
    if "tool_name" in payload and "name" not in payload:
        payload["name"] = payload.get("tool_name")
    name = _slug(str(payload.get("name") or payload.get("tool_name") or "custom_tool"))
    payload["name"] = name
    payload.setdefault("display_name", str(payload.get("description") or name.replace("_", " ").title()))
    payload.setdefault("category", "custom")
    payload.setdefault("roles", ["generic_script"])
    payload.setdefault("tool_type", "python_script")
    payload.setdefault("adapter_path", safe_adapter_path_for_tool_name(name))
    payload.setdefault("created_by", "user")
    payload["input_schema"] = _canonical_schema(payload.get("input_schema"), fallback_properties={}, required=[])
    payload["output_schema"] = _canonical_schema(payload.get("output_schema"), fallback_properties={"success": {"type": "boolean"}, "result": {}, "error": {"type": "string"}}, required=["success"])
    payload = _sync_auth_into_manifest(payload, None)
    if not functions:
        adapter_import = f"backend.services.runtime_tools.custom_tools.{name}"
        functions = [{
            "function_name": name,
            "import_path": adapter_import,
            "short_description": str(payload.get("description") or payload.get("display_name") or name),
            "when_to_use": str(payload.get("description") or f"Use {name}."),
            "signature": f"{name}(payload: dict) -> dict",
            "input_schema": payload["input_schema"],
            "output_schema": payload["output_schema"],
            "return_contract": "Returns JSON-serializable dict from generated python_script run(payload, config=None).",
            "allowed_roles": payload.get("roles") or ["generic_script"],
            "required_capabilities": payload.get("required_capabilities") or ["deterministic_execution"],
        }]
    known = {field.name for field in dataclasses_fields(ToolCapability)}
    payload = {key: value for key, value in payload.items() if key in known}
    payload["functions"] = [_function_from_dict(item) for item in functions if isinstance(item, dict)]
    payload["snippets"] = [_snippet_from_dict(item) for item in snippets if isinstance(item, dict)]
    return ToolCapability(**payload)


def _capability_to_registry_record(capability: ToolCapability) -> dict[str, Any]:
    data = asdict(capability)
    data["enabled"] = data.pop("enabled_by_default")
    data["allowed_roles"] = capability.allowed_roles or capability.roles
    data["runtime_type"] = "python_script"
    return data


def _with_overrides(capability: ToolCapability) -> ToolCapability:
    override = _TOOL_OVERRIDES.get(capability.name, {})
    return replace(capability, enabled_by_default=override.get("enabled", capability.enabled_by_default), allow_creator_use=override.get("allow_creator_use", capability.allow_creator_use))


def _load_registered_tools_from_disk() -> None:
    _DISCOVERED_TOOL_CAPABILITIES.clear()
    for record in discover_creator_tool_records():
        try:
            cap = _capability_from_dict(record)
            _DISCOVERED_TOOL_CAPABILITIES[cap.name] = cap
        except Exception:
            continue
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
        if isinstance(record, dict):
            try:
                cap = _capability_from_dict(record)
                _REGISTERED_TOOL_CAPABILITIES[cap.name] = cap
            except Exception:
                continue


def persist_registered_tools() -> None:
    CUSTOM_TOOL_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    records = [_capability_to_registry_record(cap) for cap in _REGISTERED_TOOL_CAPABILITIES.values()]
    CUSTOM_TOOL_REGISTRY_PATH.write_text(json.dumps({"tools": records}, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def list_tool_capabilities() -> list[ToolCapability]:
    merged = {
        **BUILTIN_TOOL_CAPABILITIES,
        **_DISCOVERED_TOOL_CAPABILITIES,
        **_REGISTERED_TOOL_CAPABILITIES,
    }
    return [_with_overrides(cap) for cap in merged.values()]


def get_tool_capability(name: str) -> ToolCapability | None:
    key = (name or "").strip()
    cap = BUILTIN_TOOL_CAPABILITIES.get(key) or _DISCOVERED_TOOL_CAPABILITIES.get(key) or _REGISTERED_TOOL_CAPABILITIES.get(key)
    return _with_overrides(cap) if cap else None


def register_tool_capability(capability: ToolCapability) -> ToolCapability:
    if not capability.name:
        raise ValueError("registered tool capability name is required")
    _REGISTERED_TOOL_CAPABILITIES[capability.name] = capability
    return capability


def clear_registered_tool_capabilities() -> None:
    _REGISTERED_TOOL_CAPABILITIES.clear()


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



def update_registered_tool_state(name: str, *, enabled: bool | None = None, allow_creator_use: bool | None = None) -> ToolCapability | None:
    cap = _REGISTERED_TOOL_CAPABILITIES.get(name)
    if cap is None:
        return None
    payload = asdict(cap)
    if enabled is not None:
        payload["enabled_by_default"] = bool(enabled)
        payload["approval_status"] = "enabled" if enabled else "disabled"
    if allow_creator_use is not None:
        payload["allow_creator_use"] = bool(allow_creator_use)
    updated = ToolCapability(**payload)
    _REGISTERED_TOOL_CAPABILITIES[name] = updated
    return updated

def delete_registered_tool(name: str) -> bool:
    if name not in _REGISTERED_TOOL_CAPABILITIES:
        return False
    del _REGISTERED_TOOL_CAPABILITIES[name]
    _TOOL_OVERRIDES.pop(name, None)
    return True

def roles() -> list[str]:
    return sorted({role for cap in list_tool_capabilities() for role in cap.roles})


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


def capabilities_for_role(role: str, *, only_creator_enabled: bool = True) -> tuple[list[str], list[str]]:
    """Project registry metadata for display/legacy compatibility only.

    Creator compilation does not call this helper to authorize, require, forbid,
    or select tools. Strict capability authority remains the explicit Blueprint
    contract plus ToolCapability metadata.
    """
    normalized_role = (role or "").strip()
    capabilities = list_tool_capabilities()
    if only_creator_enabled:
        capabilities = [
            capability
            for capability in capabilities
            if capability.enabled_by_default and capability.allow_creator_use
        ]
    required = [
        capability.name
        for capability in capabilities
        if normalized_role in capability.roles
    ]
    if normalized_role == "search_reader":
        required = [name for name in required if name == "web_search"]
    return required, list(
        _LEGACY_DISPLAY_ROLE_FORBIDDEN_CAPABILITIES.get(normalized_role, [])
    )


def validate_capability_names(names: list[str]) -> list[str]:
    known = set(BUILTIN_TOOL_CAPABILITIES) | set(_REGISTERED_TOOL_CAPABILITIES)
    return [name for name in names if name not in known]


def tool_status(capability: ToolCapability) -> dict[str, Any]:
    cap = _with_overrides(capability)
    missing_dependencies = []
    for dep in cap.dependencies or []:
        record = _normalize_dependency_record(dep)
        if record.get("imports") and not _dependency_available(record):
            missing_dependencies.append(record.get("package") or ",".join(record.get("imports") or []))
    missing_env = [name for name in cap.required_env if not os.environ.get(name)]
    missing_secrets = [name for name in cap.required_secrets if not os.environ.get(name)]
    return {
        **_capability_to_registry_record(cap),
        "enabled": cap.enabled_by_default,
        "creator_available": bool(cap.enabled_by_default and cap.allow_creator_use),
        "configured": not missing_env and not missing_secrets,
        "missing_env": missing_env,
        "missing_secrets": missing_secrets,
        "missing_dependencies": missing_dependencies,
        "missing_runtime_helpers": [],
        "runtime_helpers_available": sorted(set([*cap.helper_imports, *[fn.function_name for fn in cap.functions if fn.function_name]])),
        "runtime_type": "python_script",
        "override_persistence": TOOL_OVERRIDE_PERSISTENCE,
    }


def snippets_for_tool(capability: ToolCapability) -> list[ToolSnippet]:
    if capability.snippets:
        return list(capability.snippets)
    fn = capability.functions[0] if capability.functions else _function_from_dict({})
    return [ToolSnippet(
        id=f"{capability.name}.minimal_usage",
        title=f"Use {capability.display_name}",
        applies_to={"roles": capability.roles, "capabilities": [capability.name], "failure_layers": ["final_platform_output_value_invalid", "artifact_missing"]},
        description=fn.when_to_use or capability.prompt_guidance or capability.display_name,
        code=f"from {fn.import_path} import {fn.function_name}\n\npayload = {{...}}\nresult = {fn.function_name}(payload)\nreturn result",
        expected_input_shape=fn.input_schema or capability.input_schema,
        expected_output_shape=fn.output_schema or capability.output_schema,
        return_rule="Return the generated tool result directly.",
        anti_patterns=["Do not pass secrets in payload.", "Do not write outside TOOL_OUTPUT_DIR/OUTPUT_DIR."],
        requires=[capability.name],
        usage_policy=capability.usage_policy,
        priority=80,
    )]


def format_tool_snippet(capability: ToolCapability, snippet: ToolSnippet) -> str:
    anti_items = list(snippet.anti_patterns)
    if snippet.kind == "file_output_usage" or capability.artifact_outputs or any(name in capability.name for name in ("pdf", "file", "docx", "pptx", "xlsx", "csv")):
        for item in (
            "Do not append 'outputs' to OUTPUT_DIR.",
            "Do not pass full paths to filename=; filename must be a basename.",
            "Do not rewrite helper-returned pdf_path/file_outputs.",
            "Do not replace '/tmp/' with 'outputs/'.",
        ):
            if item not in anti_items:
                anti_items.append(item)
    anti = "\n".join(f"- {item}" for item in anti_items) or "- Follow the tool contract."
    return "\n".join([
        "[Tool Snippet]",
        f"Tool: {capability.name}",
        f"Snippet: {snippet.id} ({snippet.kind}, priority={snippet.priority})",
        f"Use when: {snippet.description or snippet.title}",
        "Correct usage:",
        snippet.code.strip(),
        "Expected input shape:",
        json.dumps(snippet.expected_input_shape or {}, ensure_ascii=False, sort_keys=True),
        "Expected return:",
        json.dumps(snippet.expected_output_shape or {}, ensure_ascii=False, sort_keys=True),
        f"Return rule: {snippet.return_rule or 'Return a JSON-serializable dict.'}",
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
    try:
        ast.parse(snippet.code or "pass")
    except SyntaxError as exc:
        warnings.append(f"snippet is not a complete executable Python block: {exc}")
    return {"success": not errors, "errors": sorted(set(errors)), "warnings": sorted(set(warnings))}


def set_tool_snippets(name: str, snippets: list[ToolSnippet]) -> ToolCapability | None:
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
    cards: list[str] = []
    for fn in capability.functions:
        if not fn.import_path or not fn.function_name:
            continue
        cards.append("\n".join([
            f"Tool: {capability.name}.{fn.function_name}",
            f"Function: {fn.function_name}",
            f"Import: from {fn.import_path} import {fn.function_name}",
            f"Signature: {fn.signature}",
            f"usage_policy={fn.usage_policy or capability.usage_policy}",
            "Input schema:",
            json.dumps(fn.input_schema or {}, ensure_ascii=False, sort_keys=True),
            "Output schema:",
            json.dumps(fn.output_schema or {}, ensure_ascii=False, sort_keys=True),
            "Return contract:",
            fn.return_contract or "Returns a JSON-serializable value matching output_schema.",
            "Example return:",
            fn.example_return or "",
            "Example stdout:",
            fn.example_stdout or "",
            "Common mistakes:",
            json.dumps(fn.common_mistakes or [], ensure_ascii=False, sort_keys=True),
            "Required env:",
            json.dumps(fn.required_env or capability.required_env or [], ensure_ascii=False, sort_keys=True),
            "Required secrets:",
            json.dumps(fn.required_secrets or capability.required_secrets or [], ensure_ascii=False, sort_keys=True),
            "Artifact outputs:",
            json.dumps(fn.artifact_outputs or capability.artifact_outputs or [], ensure_ascii=False, sort_keys=True),
            "Side effects:",
            json.dumps(fn.side_effects or capability.side_effects or [], ensure_ascii=False, sort_keys=True),
            "Example call:",
            (fn.example_call or f"from {fn.import_path} import {fn.function_name}\nresult = {fn.function_name}(...)").strip(),
            "Runtime: python_script",
        ]))
    return cards


def resolve_tool_snippets_for_context(*, role: str, capabilities: list[str], tool_names: list[str], file_path: str, failure_layer: str | None = None, error_text: str | None = None, max_snippets: int = 5) -> list[dict[str, Any]]:
    requested = set(tool_names or []) | set(capabilities or [])
    out: list[dict[str, Any]] = []
    for cap in list_tool_capabilities():
        if requested and cap.name not in requested and not any(fn.function_name in requested for fn in cap.functions):
            continue
        if role and cap.roles and role not in cap.roles and not requested:
            continue
        for snippet in snippets_for_tool(cap):
            out.append({"tool": cap.name, **asdict(snippet), "formatted": format_tool_snippet(cap, snippet)})
            if len(out) >= max(1, min(int(max_snippets or 5), 10)):
                return out
    return out


def tool_snippet_prompt(snippets: list[dict[str, Any]]) -> str:
    if not snippets:
        return "当前脚本可用工具 Snippets: 无"
    return "当前脚本可用工具 Snippets：\n\n" + "\n\n---\n\n".join(str(item.get("formatted") or "") for item in snippets)




def _as_list_attr(entry: Any, attr: str) -> list[str]:
    raw = entry.get(attr) if isinstance(entry, dict) else getattr(entry, attr, None)
    return [str(item) for item in raw or [] if item]


def tool_layer_prompt_for_context(
    *,
    role: str = "",
    required_capabilities: list[str] | None = None,
    optional_capabilities: list[str] | None = None,
    allowed_capabilities: list[str] | None = None,
    forbidden_capabilities: list[str] | None = None,
    failure_layer: str | None = None,
    error_text: str | None = None,
) -> str:
    """Return a layered tool recommendation prompt for Creator script generation/repair."""
    required = [str(item) for item in required_capabilities or [] if item]
    optional = [str(item) for item in optional_capabilities or [] if item]
    allowed = [str(item) for item in allowed_capabilities or [] if item]
    forbidden = [str(item) for item in forbidden_capabilities or [] if item]
    visible_caps = required + optional + allowed

    helper_required: list[str] = []
    helper_preferred: list[str] = []
    self_allowed: list[str] = []
    model_caps = {"text_generation", "image_generation", "vision_understanding"} & set(required)
    for name in visible_caps:
        cap = get_tool_capability(name)
        if not cap:
            continue
        if cap.usage_policy == "helper_required":
            helper_required.append(name)
        elif cap.usage_policy == "helper_preferred":
            helper_preferred.append(name)
        else:
            self_allowed.append(name)

    lines = [
        "工具上下文（仅由显式 SkillPlan 能力合同解析，禁止按业务关键词扩权）：",
        f"- 显式 role：{role or '未声明'}",
        f"- 显式 required_capabilities：{', '.join(required) if required else '无'}",
        f"- 显式 optional_capabilities：{', '.join(optional) if optional else '无'}",
        f"- 显式 allowed_capabilities：{', '.join(allowed) if allowed else '无'}",
        "基础确定性实现能力：",
        "- 当前脚本可使用目标语言标准库完成确定性计算、解析、格式化、本地文件处理和结构转换。",
        "平台 helper 层：",
        f"- helper_required（必须调用）：{', '.join(helper_required) if helper_required else '无'}",
        f"- helper_preferred（推荐调用，不强制）：{', '.join(helper_preferred) if helper_preferred else '无'}",
        f"- self_implementation_allowed（允许自实现）：{', '.join(self_allowed) if self_allowed else '无'}",
        "第三层：模型能力层：",
        (
            "- 当前脚本 required_capabilities 明确包含模型能力：" + ", ".join(sorted(model_caps)) + "；才可提示/调用对应模型。"
            if model_caps
            else "- 当前脚本未明确要求 text_generation/image_generation/vision_understanding；不要注入 LLM_BASE_URL、TEXT_MODEL、IMAGE_MODEL 或 VISION_MODEL。"
        ),
        "第四层：禁止能力层：",
        f"- 当前 forbidden_capabilities：{', '.join(forbidden) if forbidden else '无'}。如脚本调用 forbidden helper，repair 应删除调用，不要扩大 SkillPlan。",
    ]
    if failure_layer:
        lines.append(f"当前 failure_layer：{failure_layer}")
    if error_text:
        lines.append("错误摘要：" + str(error_text)[-1000:])
    return "\n".join(lines)

def resolve_tools_for_skill_plan_entry(entry: Any) -> ToolResolveResult:
    role = str(entry.get("role") if isinstance(entry, dict) else getattr(entry, "role", "") or "")
    def raw_attr(name: str) -> Any:
        return entry.get(name) if isinstance(entry, dict) else getattr(entry, name, None)

    selected = [str(item) for item in (raw_attr("selected_tools") or []) if item]
    entry_path = next(
        (str(raw_attr(attr) or "") for attr in ("path", "file_path", "script_path") if raw_attr(attr)),
        "",
    )
    is_python_script = str(raw_attr("runtime") or "python") == "python" and entry_path.replace("\\", "/").startswith("scripts/")
    mandatory_caps = ["script_argv_guard"] if is_python_script else []
    strategies = raw_attr("implementation_strategy") or []
    slots = raw_attr("required_tool_slots") or []
    caps = [*mandatory_caps, *selected]
    for strategy in strategies:
        data = strategy if isinstance(strategy, dict) else getattr(strategy, "__dict__", {})
        if str(data.get("strategy") or "") in {"use_registered_tool", "registered_tool"} and data.get("tool_id"):
            caps.append(str(data["tool_id"]))
        elif str(data.get("strategy") or "") == "generate_code":
            # Backwards-compatible spelling; generation treats it as local_code.
            continue
    for slot in slots:
        if isinstance(slot, str):
            caps.append(slot)
            continue
        data = slot if isinstance(slot, dict) else getattr(slot, "__dict__", {})
        for value in [data.get("slot_id"), data.get("tool_id"), data.get("capability")]:
            if value:
                caps.append(str(value))
    # Compatibility only: raw capabilities are hints, but use them if no
    # normalized implementation data was supplied at all.
    if not caps:
        for attr in ("required_capabilities", "optional_capabilities", "allowed_capabilities"):
            raw = raw_attr(attr)
            if isinstance(raw, list):
                caps.extend(str(item) for item in raw if item)
    if not caps:
        outputs = set(str(item) for item in (raw_attr("outputs") or []) if item)
        artifact_contract = raw_attr("artifact_contract") if isinstance(raw_attr("artifact_contract"), dict) else {}
        artifact_fields = set(str(item) for item in (artifact_contract.get("stdout_fields") or artifact_contract.get("file_fields") or []) if item)
        side_effects = set(str(item) for item in (raw_attr("side_effects") or []) if item)
        wanted_fields = outputs | artifact_fields
        scored: list[tuple[int, str]] = []
        for candidate in list_tool_capabilities():
            if not candidate.enabled_by_default or not candidate.allow_creator_use:
                continue
            candidate_fields: set[str] = set()
            for fn in candidate.functions:
                props = (fn.output_schema or {}).get("properties") if isinstance(fn.output_schema, dict) else {}
                if isinstance(props, dict):
                    candidate_fields.update(str(key) for key in props)
                candidate_fields.update(str(item) for item in ((fn.output_schema or {}).get("required") or []) if item)
            props = (candidate.output_schema or {}).get("properties") if isinstance(candidate.output_schema, dict) else {}
            if isinstance(props, dict):
                candidate_fields.update(str(key) for key in props)
            candidate_fields.update(str(item) for item in ((candidate.output_schema or {}).get("required") or []) if item)
            score = len(wanted_fields & candidate_fields) * 10
            if side_effects and candidate.allow_external_side_effect:
                score += 1
            if role and role in candidate.roles:
                score += 1  # weak hint only; never enough without structural match
            if score >= 10:
                scored.append((score, candidate.name))
        caps.extend(name for _, name in sorted(scored, reverse=True))
    allowed_tools = []
    cards: list[str] = []
    dependencies: list[str] = []
    forbidden_imports: list[str] = []
    warnings: list[str] = []
    for name in caps:
        cap = get_tool_capability(name)
        if not cap:
            warnings.append(f"unknown capability {name!r}")
            continue
        if not cap.enabled_by_default or not cap.allow_creator_use:
            warnings.append(f"tool {name} is disabled or not allowed for Creator")
            continue
        if cap.roles and role and role not in cap.roles and name not in {"file_output", "script_argv_guard"}:
            warnings.append(f"tool {name} is not allowed for role {role}")
            continue
        if cap.name in allowed_tools:
            continue
        allowed_tools.append(name)
        cards.extend(function_cards_for_tool(cap))
        forbidden_imports.extend(list(cap.forbidden_direct_imports or []))
        for fn in cap.functions:
            forbidden_imports.extend(list(fn.forbidden_imports or []))
        for dep in cap.dependencies or []:
            record = _normalize_dependency_record(dep)
            if record.get("package"):
                dependencies.append(record["package"])
    snippets = resolve_tool_snippets_for_context(role=role, capabilities=allowed_tools, tool_names=allowed_tools, file_path="", max_snippets=10)
    layered_prompt = tool_layer_prompt_for_context(
        role=role,
        required_capabilities=_as_list_attr(entry, "required_capabilities"),
        optional_capabilities=_as_list_attr(entry, "optional_capabilities"),
        allowed_capabilities=_as_list_attr(entry, "allowed_capabilities"),
        forbidden_capabilities=_as_list_attr(entry, "forbidden_capabilities"),
    )
    return ToolResolveResult(
        allowed_tools=allowed_tools,
        allowed_helper_imports=sorted(set([
            helper
            for name in allowed_tools
            for cap in [get_tool_capability(name)]
            if cap
            for helper in [*cap.helper_imports, *[fn.function_name for fn in cap.functions if fn.function_name]]
            if helper
        ])),
        required_dependencies=sorted(set(dependencies)),
        forbidden_imports=sorted(set(forbidden_imports)),
        tool_function_cards=cards,
        tool_snippets=snippets,
        tool_usage_prompt="通用工具平台：工具选择来自 normalized plan 的 required_tool_slots / implementation_strategy / selected_tools / runtime_contract / artifact_contract；只有 selected tools 会注入 prompt；helper_preferred 不强制实现方式。\n"
        + ("\n\nSelected tool function cards:\n\n" + "\n\n---\n\n".join(cards) if cards else "\n\nSelected tool function cards: 无")
        + "\n\n" + layered_prompt + "\n" + tool_snippet_prompt(snippets),
        warnings=warnings,
    )


def build_tool_manifest_draft(description: dict[str, Any]) -> dict[str, Any]:
    description = dict(description or {})
    name = _slug(str(description.get("tool_name") or description.get("name") or description.get("display_name") or "custom_tool"))
    display_name = str(description.get("display_name") or description.get("tool_name") or name.replace("_", " ").title())
    input_schema = _canonical_schema(description.get("input_schema"), fallback_properties={"payload": {"type": "object", "description": str(description.get("input_description") or "Tool input payload.")}}, required=[])
    output_schema = _canonical_schema(description.get("output_schema"), fallback_properties={"success": {"type": "boolean"}, "result": {}, "error": {"type": "string"}}, required=["success"])
    permissions = description.get("permissions") if isinstance(description.get("permissions"), dict) else {
        "network": bool(description.get("needs_external_network") or description.get("allow_external_network")),
        "read_files": bool(description.get("reads_file") or description.get("needs_file_input")),
        "write_files": bool(description.get("generates_file")),
        "subprocess": bool(description.get("needs_subprocess")),
        "env": [str(item) for item in (description.get("required_env") or []) if str(item).strip()],
        "timeout_seconds": int(description.get("timeout_seconds") or 30),
    }
    artifact_policy = description.get("artifact_policy") if isinstance(description.get("artifact_policy"), dict) else {"file_fields": ["file_paths", "file_outputs"] if permissions.get("write_files") else []}
    roles_value = [str(item) for item in description.get("allowed_roles") or [] if item] or ["generic_script"]
    adapter_import = f"backend.services.runtime_tools.custom_tools.{name}"
    payload = {
        "runtime_type": "python_script",
        "name": name,
        "tool_name": name,
        "display_name": display_name,
        "description": str(description.get("description") or description.get("short_description") or display_name),
        "category": str(description.get("category") or "custom"),
        "tool_type": "python_script",
        "usage_policy": "self_implementation_allowed",
        "allowed_roles": roles_value,
        "roles": roles_value,
        "required_capabilities": ["deterministic_execution"] + (["file_output"] if permissions.get("write_files") else []),
        "input_schema": input_schema,
        "output_schema": output_schema,
        "dependencies": description.get("dependencies") if isinstance(description.get("dependencies"), list) else [],
        "permissions": permissions,
        "auth": description.get("auth") if isinstance(description.get("auth"), dict) else {},
        "artifact_policy": artifact_policy,
        "enabled": False,
        "enabled_by_default": False,
        "allow_creator_use": False,
        "approval_status": "draft",
        "test_status": "untested",
        "adapter_path": safe_adapter_path_for_tool_name(name),
        "version": "0.1.0",
        "functions": [{
            "function_name": name,
            "import_path": adapter_import,
            "short_description": str(description.get("short_description") or description.get("description") or display_name),
            "when_to_use": str(description.get("when_to_use") or f"Use when a Creator script needs {display_name}."),
            "signature": f"{name}(payload: dict) -> dict",
            "input_schema": input_schema,
            "output_schema": output_schema,
            "return_contract": "Returns the dict produced by generated python_script run(payload, config=None).",
            "allowed_roles": roles_value,
            "required_capabilities": ["deterministic_execution"],
        }],
        "snippets": [],
    }
    return _sync_auth_into_manifest(payload, description)


def generate_adapter_code(manifest: dict[str, Any]) -> str:
    manifest = _sync_auth_into_manifest(dict(manifest or {}), None)
    name = _slug(str(manifest.get("tool_name") or manifest.get("name") or "custom_tool"))
    required_outputs = _schema_required(_canonical_schema(manifest.get("output_schema"), fallback_properties={"success": {"type": "boolean"}, "result": {}}, required=["success"]))
    return f'''from __future__ import annotations

import json
import sys
from typing import Any


def run(payload: dict, config: dict | None = None) -> dict:
    payload = dict(payload or {{}})
    config = dict(config or {{}})
    result: dict[str, Any] = {{"success": True, "result": {{"payload_keys": sorted(payload.keys())}}}}
    for key in {required_outputs!r}:
        result.setdefault(key, True if key == "success" else None)
    return result


def {name}(payload: dict | None = None) -> dict:
    return run(dict(payload or {{}}), None)


def main() -> None:
    raw = sys.stdin.read().strip() or "{{}}"
    data = json.loads(raw)
    if isinstance(data, dict) and "payload" in data:
        payload = data.get("payload") or {{}}
        config = data.get("config") or {{}}
    else:
        payload = data if isinstance(data, dict) else {{}}
        config = {{}}
    print(json.dumps(run(payload, config), ensure_ascii=False))


if __name__ == "__main__":
    main()
'''



def _manifest_tool_function_name(manifest: dict[str, Any] | None) -> str:
    manifest = manifest if isinstance(manifest, dict) else {}
    return _slug(str(manifest.get("tool_name") or manifest.get("name") or "custom_tool"))


def _top_level_function_names(script_code: str) -> set[str]:
    try:
        tree = ast.parse(script_code or "")
    except SyntaxError:
        return set()
    return {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}

def _script_has_run_function(script_code: str) -> bool:
    return "run" in _top_level_function_names(script_code or "")


def _script_completeness_report(script_code: str, manifest: dict[str, Any]) -> dict[str, Any]:
    """Generic runtime completeness check.

    This is not business-specific. It only verifies that a python_script still has
    the platform-required internal run() entrypoint and the public tool function.
    """
    code = _strip_code_fence(script_code or "")
    errors: list[str] = []

    if not code.strip():
        return {
            "complete": False,
            "errors": ["script_code is empty"],
            "function_names": [],
            "line_count": 0,
        }

    try:
        ast.parse(code)
    except SyntaxError as exc:
        return {
            "complete": False,
            "errors": [f"script syntax error: {exc}"],
            "function_names": [],
            "line_count": len(code.splitlines()),
        }

    function_names = sorted(_top_level_function_names(code))
    tool_fn = _manifest_tool_function_name(manifest)

    if "run" not in function_names:
        errors.append("script must define run(payload, config=None)")

    if tool_fn and tool_fn not in function_names:
        errors.append(f"script must define public tool function {tool_fn}(payload, config=None)")

    try:
        functions = _top_level_function_nodes(code)
        public_node = functions.get(tool_fn)
        if public_node is not None and _is_run_delegate_function(public_node, "run") and "run" not in function_names:
            errors.append(f"public function {tool_fn} delegates to missing run()")
    except Exception:
        pass

    return {
        "complete": not errors,
        "errors": sorted(set(errors)),
        "function_names": function_names,
        "line_count": len([line for line in code.splitlines() if line.strip()]),
    }


def _looks_like_incomplete_repair(
    *,
    original_code: str,
    repaired_code: str,
    manifest: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Detect repair outputs that would destroy the previous complete runtime.

    A repair result must not replace the previous runtime if it removes run(),
    removes the public entrypoint, or collapses a non-trivial script into a tiny
    wrapper shell.
    """
    original = _strip_code_fence(original_code or "")
    repaired = _strip_code_fence(repaired_code or "")

    reasons: list[str] = []

    original_report = _script_completeness_report(original, manifest)
    repaired_report = _script_completeness_report(repaired, manifest)

    if not repaired_report.get("complete"):
        reasons.extend(repaired_report.get("errors") or [])

    original_names = set(original_report.get("function_names") or [])
    repaired_names = set(repaired_report.get("function_names") or [])
    tool_fn = _manifest_tool_function_name(manifest)

    if "run" in original_names and "run" not in repaired_names:
        reasons.append("repair model removed required run(payload, config=None) entrypoint")

    if tool_fn in original_names and tool_fn not in repaired_names:
        reasons.append(f"repair model removed public tool function {tool_fn}")

    original_line_count = int(original_report.get("line_count") or 0)
    repaired_line_count = int(repaired_report.get("line_count") or 0)

    if original_line_count >= 20 and repaired_line_count <= max(8, original_line_count // 4):
        reasons.append("repair model output is much shorter than original runtime and may be an incomplete shell")

    try:
        functions = _top_level_function_nodes(repaired)
        public_node = functions.get(tool_fn)
        if public_node is not None and _is_run_delegate_function(public_node, "run") and "run" not in repaired_names:
            reasons.append("repair model returned only a public wrapper that delegates to missing run()")
    except Exception:
        pass

    return bool(reasons), sorted(set(reasons))

def _ensure_named_entrypoint(script_code: str, manifest: dict[str, Any] | None) -> str:
    code = _strip_code_fence(script_code or "")
    if not code.strip():
        return code
    tool_fn = _manifest_tool_function_name(manifest)
    names = _top_level_function_names(code)
    if tool_fn in names:
        return code.rstrip() + "\n"
    suffix = "\n\n\ndef " + tool_fn + "(payload: dict | None = None, config: dict | None = None) -> dict:\n" \
             "    \"\"\"Public entrypoint for this generated tool; delegates to run().\"\"\"\n" \
             "    return run(dict(payload or {}), config)\n"
    return code.rstrip() + suffix

def _request_runtime_code(
    request: dict[str, Any],
    *,
    allow_display_fallback: bool = False,
) -> str:
    """Read executable runtime code from official protocol fields.

    This function is intentionally a transport/protocol extractor only.
    It must not validate whether code contains run(), because syntax/protocol
    validation belongs to validate_tool_manifest / repair stage.

    Official runtime fields:
    - runtime_code
    - full_adapter_code
    - internal_code
    - script_code

    Display fields are only allowed when allow_display_fallback=True.
    """
    request = request or {}

    for key in (
        "runtime_code",
        "full_adapter_code",
        "internal_code",
        "script_code",
    ):
        value = request.get(key)
        if isinstance(value, str) and value.strip():
            return _strip_code_fence(value)

    if allow_display_fallback:
        for key in (
            "adapter_code",
            "display_code",
            "public_api_code",
            "code_block",
        ):
            value = request.get(key)
            if isinstance(value, str) and value.strip():
                return _strip_code_fence(value)

    return ""

def _is_run_delegate_function(node: ast.FunctionDef, target_name: str = "run") -> bool:
    """Return True if a public function only delegates to run(...)."""
    body = [
        stmt for stmt in node.body
        if not (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        )
    ]

    if len(body) != 1 or not isinstance(body[0], ast.Return):
        return False

    value = body[0].value
    if not isinstance(value, ast.Call):
        return False

    return isinstance(value.func, ast.Name) and value.func.id == target_name


def _module_import_lines(script_code: str) -> list[str]:
    try:
        tree = ast.parse(script_code or "")
    except SyntaxError:
        return []

    lines: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            try:
                line = ast.unparse(node)
            except Exception:
                continue
            if line not in lines:
                lines.append(line)
    return lines


def _top_level_function_nodes(script_code: str) -> dict[str, ast.FunctionDef]:
    try:
        tree = ast.parse(script_code or "")
    except SyntaxError:
        return {}

    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }


def _function_source_with_name(node: ast.FunctionDef, name: str) -> str:
    import copy

    cloned = copy.deepcopy(node)
    cloned.name = name

    try:
        return ast.unparse(cloned).strip() + "\n"
    except Exception:
        return ""


def _public_api_display_code(
    manifest: dict[str, Any] | None,
    sample_input: dict[str, Any] | None = None,
    *,
    runtime_code: str | None = None,
) -> str:
    """Return code shown in the main editor.

    The main editor should show the public tool function's core implementation.
    The backend runner protocol run(...) and full implementation remain available
    through runtime_code / internal_code / debug_sections.
    """
    manifest = manifest if isinstance(manifest, dict) else {}
    tool_fn = _manifest_tool_function_name(manifest)
    code = _strip_code_fence(runtime_code or "")

    functions = _top_level_function_nodes(code)
    imports = _module_import_lines(code)

    public_node = functions.get(tool_fn)
    run_node = functions.get("run")

    sections: list[str] = []

    if imports:
        sections.append("\n".join(imports))

    if public_node is not None and not _is_run_delegate_function(public_node, "run"):
        public_source = _function_source_with_name(public_node, tool_fn)
    elif run_node is not None:
        public_source = _function_source_with_name(run_node, tool_fn)
    elif public_node is not None:
        public_source = _function_source_with_name(public_node, tool_fn)
    else:
        description = str(manifest.get("description") or f"Implement {tool_fn}.").strip()
        public_source = (
            f"def {tool_fn}(payload: dict | None = None, config: dict | None = None) -> dict:\n"
            f"    \"\"\"{description}\"\"\"\n"
            "    payload = dict(payload or {})\n"
            "    return {\"success\": True, \"result\": payload}\n"
        )

    sections.append(public_source.rstrip())

    helper_sources: list[str] = []
    for name, node in functions.items():
        if name in {"run", tool_fn, "main"}:
            continue
        src = _function_source_with_name(node, name)
        if src:
            helper_sources.append(src.rstrip())

    if helper_sources:
        sections.append(
            "# Internal helper functions used by the public tool implementation\n"
            + "\n\n".join(helper_sources)
        )

    return "\n\n".join(section for section in sections if section.strip()).rstrip() + "\n"


def _code_view_fields(
    *,
    manifest: dict[str, Any],
    runtime_code: str,
    sample_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return consistent frontend code fields.

    adapter_code/display_code/public_api_code are for main display.
    runtime_code/full_adapter_code/internal_code/script_code are executable.
    """
    display_code = _public_api_display_code(
        manifest,
        sample_input if isinstance(sample_input, dict) else {},
        runtime_code=runtime_code,
    )

    return {
        "display_code": display_code,
        "public_api_code": display_code,
        "adapter_code": display_code,
        "adapter_code_kind": "python_public_function",

        "runtime_code": runtime_code,
        "script_code": runtime_code,
        "full_adapter_code": runtime_code,
        "internal_code": runtime_code,
        "internal_code_kind": "python_script",
    }

def _debug_call_sections(*, script_code: str, manifest: dict[str, Any], payload: dict[str, Any], config: dict[str, Any] | None, runner_code: str, command: list[str], temporary_environment: dict[str, Any], stdout: str, stderr: str, result: Any, errors: list[str]) -> dict[str, Any]:
    config = config if isinstance(config, dict) else {}
    public_env = temporary_environment.get("env") if isinstance(temporary_environment, dict) else {}
    command_text = " ".join(command)
    manifest_text = json.dumps(manifest or {}, ensure_ascii=False, indent=2, default=str)
    payload_text = json.dumps({"payload": payload or {}, "config": config}, ensure_ascii=False, indent=2, default=str)
    env_obj = {"temporary_environment": temporary_environment, "runtime_env": public_env}
    output_text = json.dumps({"stdout": stdout[-4000:], "stderr": stderr[-4000:], "result": result, "errors": errors}, ensure_ascii=False, indent=2, default=str)
    sections = [
        {"id": "input_contract", "title": "1. 输入与工具合同", "summary": "展示前端传入的 payload/config 以及 planner 生成的 manifest。", "language": "json", "code": payload_text + "\n\n// manifest\n" + manifest_text, "collapsed": True},
        {"id": "generated_tool_code", "title": "2. 模型生成的工具代码 tool_impl.py", "summary": "这是 coder 生成并由后端实际执行的代码。", "language": "python", "code": script_code, "collapsed": False},
        {"id": "runner_code", "title": "3. 后端通用 runner 调用代码", "summary": "后端只通过统一 runner 导入 tool_impl.py 并调用 run(payload, config)。", "language": "python", "code": runner_code, "collapsed": True},
        {"id": "temporary_environment", "title": "4. 临时环境与执行命令", "summary": "展示临时工作目录、依赖目录、输出目录、PYTHONPATH 和执行命令。", "language": "json", "code": json.dumps({"command": command_text, "environment": env_obj}, ensure_ascii=False, indent=2, default=str), "collapsed": True},
        {"id": "execution_output", "title": "5. stdout / stderr / 返回结果", "summary": "展示脚本执行输出、错误和解析后的 JSON 结果。", "language": "json", "code": output_text, "collapsed": False},
    ]
    return {"call_chain": ["frontend payload/config", "planner manifest", "generated tool_impl.py", "generic tool_runner.py", "subprocess execution", "stdout JSON parse", "schema/artifact/auth validation"], "debug_sections": sections, "collapsible_blocks": sections}

def _script_tool_spec_errors(manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return ["manifest must be an object"]
    if str(manifest.get("runtime_type") or "") != "python_script":
        errors.append("manifest.runtime_type must be 'python_script'")
    if not str(manifest.get("tool_name") or manifest.get("name") or "").strip():
        errors.append("manifest.tool_name is required")
    for key in ("input_schema", "output_schema"):
        schema = manifest.get(key)
        if not isinstance(schema, dict):
            errors.append(f"manifest.{key} must be a JSON Schema object")
            continue
        if schema.get("type") != "object":
            errors.append(f"manifest.{key} must be canonical JSON Schema")
        if not isinstance(schema.get("properties"), dict):
            errors.append(f"manifest.{key}.properties must be an object")
        if "required" in schema and not isinstance(schema.get("required"), list):
            errors.append(f"manifest.{key}.required must be a list")
    deps = manifest.get("dependencies", [])
    if not isinstance(deps, list):
        errors.append("manifest.dependencies must be a list")
    else:
        for idx, dep in enumerate(deps):
            if not isinstance(dep, dict):
                errors.append(f"manifest.dependencies[{idx}] must be an object with package/imports")
                continue
            if not str(dep.get("package") or "").strip():
                errors.append(f"manifest.dependencies[{idx}].package is required")
            imports = dep.get("imports")
            if not isinstance(imports, list) or not all(str(item).strip() for item in imports):
                errors.append(f"manifest.dependencies[{idx}].imports must be a non-empty list")
    permissions = manifest.get("permissions")
    if not isinstance(permissions, dict):
        errors.append("manifest.permissions must be an object")
    else:
        for key in ("network", "read_files", "write_files", "subprocess"):
            if key in permissions and not isinstance(permissions.get(key), bool):
                errors.append(f"manifest.permissions.{key} must be boolean")
        if "env" in permissions and not isinstance(permissions.get("env"), list):
            errors.append("manifest.permissions.env must be a list")
    auth = _normalize_auth_plan(manifest)
    if auth.get("required") == "yes" and not auth.get("secrets"):
        errors.append(
            "manifest.auth.required=yes requires explicit auth.secrets. "
            "If identity is provided by normal payload fields and no external credential is needed, set auth.required=no."
        )
    return sorted(set(errors))


def _script_static_errors(script_code: str, manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    try:
        tree = ast.parse(script_code or "")
    except SyntaxError as exc:
        return [f"script syntax error: {exc}"]
    permissions = manifest.get("permissions") if isinstance(manifest.get("permissions"), dict) else {}
    allow_network = bool(permissions.get("network"))
    allow_subprocess = bool(permissions.get("subprocess"))
    run_func = next((node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run"), None)
    if run_func is None:
        errors.append("script must define run(payload: dict, config: dict | None = None) -> dict")
    else:
        args = [arg.arg for arg in run_func.args.args]
        if not args or args[0] != "payload":
            errors.append("run first argument must be payload")
        if len(args) >= 2 and args[1] != "config":
            errors.append("run second argument, if present, must be config")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots = {str(alias.name or "").split(".")[0] for alias in node.names}
            if not allow_network and roots & _NETWORK_IMPORT_ROOTS:
                errors.append("script imports network modules but manifest.permissions.network is false")
            if not allow_subprocess and roots & _SUBPROCESS_IMPORT_ROOTS:
                errors.append("script imports subprocess but manifest.permissions.subprocess is false")
            if roots & _DANGEROUS_IMPORT_ROOTS:
                errors.append("script imports dangerous modules: " + ", ".join(sorted(roots & _DANGEROUS_IMPORT_ROOTS)))
        elif isinstance(node, ast.ImportFrom):
            root = str(node.module or "").split(".")[0]
            if not allow_network and root in _NETWORK_IMPORT_ROOTS:
                errors.append("script imports network modules but manifest.permissions.network is false")
            if not allow_subprocess and root in _SUBPROCESS_IMPORT_ROOTS:
                errors.append("script imports subprocess but manifest.permissions.subprocess is false")
            if root in _DANGEROUS_IMPORT_ROOTS:
                errors.append(f"script imports dangerous module: {root}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _FORBIDDEN_BUILTIN_CALLS:
                errors.append(f"script must not call {func.id}")
            if isinstance(func, ast.Attribute):
                try:
                    func_text = ast.unparse(func)
                except Exception:
                    func_text = ""
                if not allow_subprocess and (func_text.startswith("os.system") or func_text.startswith("subprocess.")):
                    errors.append("script calls subprocess/os.system but manifest.permissions.subprocess is false")
                if func.attr in _UNSAFE_FS_ATTRS:
                    errors.append(f"script must not call unsafe filesystem method {func.attr}")
    tool_fn = _manifest_tool_function_name(manifest)
    if tool_fn and tool_fn != "run" and tool_fn not in _top_level_function_names(script_code):
        errors.append(f"script must expose public tool function {tool_fn}(payload, config=None) in addition to run")
    if re.search(r"(?:sk-|AKIA|-----BEGIN [A-Z ]*PRIVATE KEY-----)[A-Za-z0-9_\-+/=]{8,}", script_code or ""):
        errors.append("script appears to contain a hard-coded secret")
    return sorted(set(errors))



def _tool_runner_script_code() -> str:
    return r"""
from __future__ import annotations
import importlib.util, json, sys, traceback
from pathlib import Path

def _load_module(path: str):
    spec = importlib.util.spec_from_file_location("tool_impl", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load tool_impl.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def main() -> None:
    raw = sys.stdin.read().strip() or "{}"
    data = json.loads(raw)
    payload = data.get("payload") if isinstance(data, dict) else {}
    config = data.get("config") if isinstance(data, dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    if not isinstance(config, dict):
        config = {}
    module = _load_module(str(Path(__file__).with_name("tool_impl.py")))
    fn = getattr(module, "run", None)
    if not callable(fn):
        raise RuntimeError("tool_impl.py must define run(payload, config=None)")
    try:
        result = fn(payload, config)
    except TypeError:
        result = fn(payload)
    if not isinstance(result, dict):
        result = {"success": True, "result": result}
    print(json.dumps(result, ensure_ascii=False))

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc), "traceback": traceback.format_exc(limit=8)}, ensure_ascii=False))
        sys.exit(1)
"""


def _temporary_environment_result(
    *,
    success: bool,
    status: str,
    root: Path,
    script_path: Path,
    runner_path: Path,
    output_dir: Path,
    deps_dir: Path,
    packages: list[str],
    dependency_result: dict[str, Any],
    python_path_items: list[str],
    env: dict[str, str] | None = None,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    env = env or {}
    public_env_names = ["TOOL_OUTPUT_DIR", "OUTPUT_DIR", "TOOL_TRIAL_RUN", "SKILL_TRIAL_RUN", "PYTHONPATH"]
    return {
        "success": bool(success),
        "status": status,
        "errors": errors or [],
        "root_dir": str(root),
        "work_dir": str(root),
        "script_path": str(script_path),
        "runner_path": str(runner_path),
        "output_dir": str(output_dir),
        "dependency_dir": str(deps_dir),
        "packages_requested": packages,
        "dependency_environment": dependency_result,
        "pythonpath_entries": python_path_items,
        "env": {name: env.get(name, "") for name in public_env_names if name in env},
        "cleanup_policy": "temporary_directory_cleanup_after_validation",
    }


def _create_temporary_execution_environment(
    *,
    script_code: str,
    manifest: dict[str, Any],
    trial: bool = True,
) -> tuple[dict[str, Any], dict[str, str], tempfile.TemporaryDirectory[str]]:
    """Create the explicit temporary environment used by offline debugging.

    This is the front-end visible stage 4:
    1. create isolated temp root
    2. create output directory
    3. create dependency target directory
    4. write generated tool script
    5. write platform runner script
    6. install missing declared dependencies into dependency target
    7. prepare env/PYTHONPATH for subprocess execution
    """
    manifest = manifest if isinstance(manifest, dict) else {}
    temp_root = tempfile.TemporaryDirectory(prefix="creator_script_env_")
    root = Path(temp_root.name)
    script_path = root / "tool_impl.py"
    runner_path = root / "tool_runner.py"
    output_dir = root / "outputs"
    deps_dir = root / "deps"

    output_dir.mkdir(parents=True, exist_ok=True)
    deps_dir.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script_code, encoding="utf-8")
    runner_path.write_text(_tool_runner_script_code(), encoding="utf-8")

    packages = _dependency_packages_for_install(manifest)
    dependency_result = _install_dependencies_to_target(
        packages,
        deps_dir,
        timeout_seconds=int(os.environ.get("TOOL_AUTHOR_DEP_INSTALL_TIMEOUT_SECONDS", "180")),
    ) if packages else {
        "success": True,
        "skipped": True,
        "installed": [],
        "target_dir": str(deps_dir),
        "errors": [],
    }

    python_path_items = [str(root)]
    if packages and dependency_result.get("success"):
        python_path_items.insert(0, str(deps_dir))

    env = os.environ.copy()
    env["TOOL_OUTPUT_DIR"] = str(output_dir)
    env["OUTPUT_DIR"] = str(output_dir)
    env["TOOL_TRIAL_RUN"] = "1" if trial else "0"
    env["SKILL_TRIAL_RUN"] = "1" if trial else "0"
    old_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join([*python_path_items, old_pythonpath] if old_pythonpath else python_path_items)

    status = "created" if dependency_result.get("success") else "dependency_install_failed"
    metadata = _temporary_environment_result(
        success=bool(dependency_result.get("success")),
        status=status,
        root=root,
        script_path=script_path,
        runner_path=runner_path,
        output_dir=output_dir,
        deps_dir=deps_dir,
        packages=packages,
        dependency_result=dependency_result,
        python_path_items=python_path_items,
        env=env,
        errors=dependency_result.get("errors") or [],
    )
    return metadata, env, temp_root


def create_temporary_tool_environment(
    manifest: dict[str, Any],
    *,
    adapter_code: str | None = None,
    script_code: str | None = None,
    trial: bool = True,
) -> dict[str, Any]:
    """Create and immediately clean up a temporary execution environment.

    This helper is safe for a front-end stage/status endpoint. For actual
    validation, _run_generated_tool_script keeps the returned temp directory
    alive until the subprocess finishes.
    """
    manifest = _sync_auth_into_manifest(dict(manifest or {}), None)
    code = _strip_code_fence(str(script_code or adapter_code or generate_adapter_code(manifest)))
    metadata: dict[str, Any] = {}
    temp_root: tempfile.TemporaryDirectory[str] | None = None
    try:
        metadata, _env, temp_root = _create_temporary_execution_environment(script_code=code, manifest=manifest, trial=trial)
        metadata["cleaned_up"] = False
        return metadata
    finally:
        if temp_root is not None:
            temp_root.cleanup()
            if metadata:
                metadata["cleaned_up"] = True


def _run_generated_tool_script(*, script_code: str, manifest: dict[str, Any], payload: dict[str, Any], config: dict[str, Any] | None = None, trial: bool = True) -> dict[str, Any]:
    manifest = _sync_auth_into_manifest(manifest if isinstance(manifest, dict) else {}, None)
    payload = payload if isinstance(payload, dict) else {}
    config = config if isinstance(config, dict) else {}
    temporary_environment: dict[str, Any] = {}
    temp_root: tempfile.TemporaryDirectory[str] | None = None
    try:
        temporary_environment, env, temp_root = _create_temporary_execution_environment(script_code=script_code, manifest=manifest, trial=trial)
        dependency_result = temporary_environment.get("dependency_environment") or {}
        if not temporary_environment.get("success"):
            return {
                "success": False,
                "status": temporary_environment.get("status") or "temporary_environment_failed",
                "errors": temporary_environment.get("errors") or ["temporary environment creation failed"],
                "temporary_environment": temporary_environment,
                "dependency_environment": dependency_result,
                "stdout": "",
                "stderr": "",
                "result": None,
            }

        runner_path = Path(str(temporary_environment["runner_path"]))
        output_dir = Path(str(temporary_environment["output_dir"]))
        permissions = manifest.get("permissions") if isinstance(manifest.get("permissions"), dict) else {}
        timeout_seconds = int(permissions.get("timeout_seconds") or manifest.get("timeout_seconds") or 30)
        command = [sys.executable, str(runner_path)]
        runner_code = _tool_runner_script_code()
        completed = subprocess.run(
            command,
            input=json.dumps({"payload": payload, "config": config}, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
            env=env,
        )
        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()
        try:
            result = json.loads(stdout) if stdout else {}
        except Exception:
            debug = _debug_call_sections(script_code=script_code, manifest=manifest, payload=payload, config=config, runner_code=runner_code, command=command, temporary_environment=temporary_environment, stdout=stdout, stderr=stderr, result=None, errors=["script stdout is not valid JSON"])
            return {
                "success": False,
                "status": "invalid_stdout_json",
                "errors": ["script stdout is not valid JSON"],
                "stdout": stdout[-4000:],
                "stderr": stderr[-4000:],
                "returncode": completed.returncode,
                "temporary_environment": temporary_environment,
                "dependency_environment": dependency_result,
                "result": None,
                **debug,
            }
        if not isinstance(result, dict):
            result = {"success": True, "result": result}
        errors: list[str] = []
        if completed.returncode != 0 and result.get("success") is not False:
            errors.append(f"script exited with code {completed.returncode}")
        required_outputs = _schema_required(manifest.get("output_schema") if isinstance(manifest.get("output_schema"), dict) else {})
        missing_required = [key for key in required_outputs if key not in result]
        if missing_required:
            errors.append("script result missing required output fields: " + ", ".join(missing_required))
        artifact_policy = manifest.get("artifact_policy") if isinstance(manifest.get("artifact_policy"), dict) else {}
        file_fields = artifact_policy.get("file_fields") if isinstance(artifact_policy.get("file_fields"), list) else []
        for field in file_fields:
            value = result.get(str(field))
            paths = [value] if isinstance(value, str) else [str(item) for item in value if str(item).strip()] if isinstance(value, list) else []
            for item in paths:
                path = Path(item)
                if not path.is_absolute():
                    path = output_dir / path
                if not path.exists():
                    errors.append(f"declared artifact does not exist: {item}")
        redacted = _redact_secrets(result, set(os.environ.values()))
        status = "validated" if not errors and result.get("success") is not False else "failed"
        debug = _debug_call_sections(script_code=script_code, manifest=manifest, payload=payload, config=config, runner_code=runner_code, command=command, temporary_environment=temporary_environment, stdout=stdout, stderr=stderr, result=redacted, errors=errors)
        return {
            "success": not errors and result.get("success") is not False,
            "status": status,
            "errors": sorted(set(errors)),
            "warnings": [],
            "stdout": stdout[-4000:],
            "stderr": stderr[-4000:],
            "returncode": completed.returncode,
            "result": redacted,
            "return_keys": sorted(result.keys()),
            "preview": _normalized_preview(redacted),
            "temporary_environment": temporary_environment,
            "dependency_environment": dependency_result,
            "output_dir": str(output_dir),
            **debug,
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "status": "timeout",
            "errors": ["script execution timed out"],
            "warnings": [],
            "stdout": "",
            "stderr": "",
            "result": None,
            "temporary_environment": temporary_environment,
            "dependency_environment": temporary_environment.get("dependency_environment") if isinstance(temporary_environment, dict) else {},
        }
    except Exception as exc:
        return {
            "success": False,
            "status": "runner_failed",
            "errors": [str(exc)],
            "warnings": [],
            "stdout": "",
            "stderr": "",
            "result": None,
            "temporary_environment": temporary_environment,
            "dependency_environment": temporary_environment.get("dependency_environment") if isinstance(temporary_environment, dict) else {},
        }
    finally:
        if temp_root is not None:
            temp_root.cleanup()

def validate_tool_manifest(manifest: dict[str, Any], *, adapter_code: str | None = None, sample_input: dict[str, Any] | None = None, dynamic: bool = True, real_run: bool = False, require_auth_config: bool = True, direct_run: bool = False) -> dict[str, Any]:
    manifest = _sync_auth_into_manifest(dict(manifest or {}), None)
    script_code = _strip_code_fence(str(adapter_code or ""))
    if script_code.strip():
        manifest = _normalize_dependencies_for_script(manifest, script_code)
    errors = _script_tool_spec_errors(manifest)
    warnings: list[str] = []
    raw_sample_input = sample_input if isinstance(sample_input, dict) else {}
    if direct_run:
        resolved_sample_input, sample_notes = dict(raw_sample_input), []
    else:
        resolved_sample_input, sample_notes = resolve_tool_trial_sample_input(manifest, raw_sample_input)
        warnings.extend(sample_notes)
    if not script_code.strip():
        errors.append("adapter_code/script_code is required and must contain the generated Python script")
        auth_report = _auth_validation_report(manifest, "", require_auth_config=require_auth_config)
    else:
        errors.extend(_script_static_errors(script_code, manifest))
        auth_report = _auth_validation_report(manifest, script_code, require_auth_config=require_auth_config)
    if not direct_run:
        errors.extend(auth_report.get("errors") or [])
    warnings.extend(auth_report.get("warnings") or [])
    dynamic_result: dict[str, Any] = {"skipped": not dynamic}
    auth_gate = auth_report.get("auth_gate") or _auth_gate_for_manifest(manifest, require_config=require_auth_config)
    if dynamic and not errors:
        if auth_gate.get("missing_env") and not require_auth_config and not direct_run:
            dynamic_result = {"skipped": True, "reason": "auth_config_missing", "auth_gate": auth_gate, "temporary_environment": {"success": True, "status": "skipped_auth_config_missing", "reason": "auth config is missing; script execution is skipped until credentials are configured"}}
        else:
            dynamic_result = _run_generated_tool_script(
                script_code=script_code,
                manifest=manifest,
                payload=resolved_sample_input,
                config={},
                trial=False if direct_run else not real_run,
            )
            if not dynamic_result.get("success"):
                dyn_errors = dynamic_result.get("errors") or ["dynamic script validation failed"]
                if sample_notes:
                    warnings.append(
                        "dynamic trial used auto-filled sample_input; if the failure is only caused by unrealistic sample values, adjust sample input and rerun validation."
                    )
                errors.extend(dyn_errors)
    success = not errors
    block_registration = bool(auth_gate.get("block_registration") or (auth_gate.get("missing_env") and require_auth_config))
    return {
        "success": success,
        "status": "validated" if success else "failed",
        "errors": sorted(set(errors)),
        "warnings": sorted(set(warnings)),
        "sample_input": resolved_sample_input,
        "sample_notes": sample_notes,
        "dynamic_trial": dynamic_result,
        "real_run": {"skipped": not real_run},
        "dependency_environment": dynamic_result.get("dependency_environment") if isinstance(dynamic_result, dict) else {},
        "temporary_environment": dynamic_result.get("temporary_environment") if isinstance(dynamic_result, dict) else {},
        "debug_sections": dynamic_result.get("debug_sections") if isinstance(dynamic_result, dict) else [],
        "collapsible_blocks": dynamic_result.get("collapsible_blocks") if isinstance(dynamic_result, dict) else [],
        "call_chain": dynamic_result.get("call_chain") if isinstance(dynamic_result, dict) else [],
        "auth_gate": auth_gate,
        "auth_review": {
            "planner_declared_auth": auth_report.get("planner_declared_auth"),
            "script_uses_auth": auth_report.get("script_uses_auth"),
            "used_env": auth_report.get("used_env") or [],
            "declared_env": auth_report.get("declared_env") or [],
            "undeclared_env": auth_report.get("undeclared_env") or [],
        },
        "block_registration": block_registration,
        "can_register": bool(success and not block_registration),
        "tool_card_preview": [],
        "snippet_preview": [],
        "snippet_validations": [],
    }


def safe_adapter_path_for_manifest(manifest: dict[str, Any]) -> str:
    return safe_adapter_path_for_tool_name(str(manifest.get("tool_name") or manifest.get("name") or "custom_tool"))


def normalize_tool_manifest_adapter_path(manifest: dict[str, Any]) -> dict[str, Any]:
    payload = _sync_auth_into_manifest(dict(manifest or {}), None)
    name = _slug(str(payload.get("tool_name") or payload.get("name") or "custom_tool"))
    payload["runtime_type"] = "python_script"
    payload["tool_name"] = name
    payload["name"] = name
    payload["adapter_path"] = safe_adapter_path_for_tool_name(name)
    adapter_import = f"backend.services.runtime_tools.custom_tools.{name}"
    functions = []
    for item in payload.get("functions") or []:
        if isinstance(item, dict):
            fn = dict(item)
            fn["function_name"] = name
            fn["import_path"] = adapter_import
            functions.append(fn)
    if functions:
        payload["functions"] = functions
    return payload


def write_registered_adapter(manifest: dict[str, Any], adapter_code: str | None) -> dict[str, Any]:
    payload = normalize_tool_manifest_adapter_path(manifest)
    name = _slug(str(payload.get("tool_name") or payload.get("name") or "custom_tool"))
    script_code = _strip_code_fence(str(adapter_code or generate_adapter_code(payload)))
    if f"def {name}(" not in script_code:
        script_code += f"""

def {name}(payload: dict | None = None) -> dict:
    return run(dict(payload or {{}}), None)
"""
    path = _safe_adapter_module_path_for_name(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(script_code, encoding="utf-8")
    payload.update({"enabled": True, "enabled_by_default": True, "allow_creator_use": True, "approval_status": "approved", "test_status": payload.get("test_status") or "validated", "adapter_path": safe_adapter_path_for_tool_name(name), "updated_at": _utc_now()})
    payload.setdefault("created_at", payload["updated_at"])
    return payload


async def _complete_author_model(task: str, messages: list[dict[str, str]], *, reason: str) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
    try:
        from .llm_proxy import complete_chat_once
        from .model_router import route_model
        route = route_model(task, reason=reason)
        timeout = float(os.environ.get(f"TOOL_AUTHOR_{task.upper()}_TIMEOUT_SECONDS", os.environ.get("TOOL_AUTHOR_LLM_TIMEOUT_SECONDS", "120")))
        text = await asyncio.wait_for(complete_chat_once(messages, route.model), timeout=timeout)
        parsed = _json_from_model_text(text)
        if task == "code" and not parsed and (text or "").strip():
            parsed = {"script_code": _strip_code_fence(text)}
        return parsed, route.ack(), None
    except Exception as exc:
        return {}, None, str(exc)


async def _run_planner(request: dict[str, Any], model_notes: list[str], warnings: list[str]) -> dict[str, Any]:
    """Step 2 planner: clarify requirements and decide auth generically.

    The planner is intentionally business-agnostic. It may ask clarification
    questions and it may declare auth requirements, but it must not select or
    trigger any old wrapper family.
    """
    request = dict(request or {})
    payload = {
        key: request.get(key)
        for key in [
            "description",
            "requirement",
            "requirements",
            "tool_name",
            "tool_type",
            "input_description",
            "output_description",
            "sample_input",
            "needs_secret",
            "requires_auth",
            "needs_external_network",
            "generates_file",
            "high_risk",
            "operation",
            "config",
            "allow_external_network",
            "authoring_context",
            "reference_code",
            "reference_snippet",
            "reference_files",
            "constraints",
            "expected_usage",
        ]
        if key in request
    }
    messages = [
        {
            "role": "system",
            "content": (
                "You are planner_model for a generic tool creation platform. Return strict JSON only. "
                "Workflow step 2: read the user's requirement and optional reference code, ask only necessary clarification questions, "
                "and decide whether the tool needs authentication. There is exactly one runtime: python_script. "
                "Do not output wrapper_family, helper_contract, context APIs, or backend-specific business rules. "
                "Return shape: {"
                "\"needs_clarification\": false, "
                "\"clarification_questions\": [], "
                "\"tool_name\": \"snake_case\", "
                "\"description\": \"...\", "
                "\"manifest\": {...}, "
                "\"sample_input\": {}, "
                "\"implementation_notes\": []}. "
                "manifest must contain runtime_type='python_script', tool_name, description, canonical JSON Schema input_schema/output_schema, dependencies, permissions, artifact_policy, and auth. "
                "For file path inputs, input_schema properties must declare format='file-path'. For PDF inputs also declare contentMediaType='application/pdf' or accepted_extensions=['.pdf']; for image inputs declare contentMediaType='image/png' or accepted_extensions with image extensions. "
                "Do not invent local test file paths in sample_input; if trial needs PDF/image files, backend will use backend/samples/sample.pdf or backend/samples/sample.png. "
                "auth must be {required:'yes'|'no'|'unknown', reason:'...', secrets:[{env:'ENV_NAME', description:'...', required:true}]}. "
                "Only auth.secrets / required_secrets are credentials. "
                "Payload fields such as user_id, task_id, task_type, account_id, tenant_id, username, or other request data are normal input fields, not secrets by themselves. "
                "If identity or routing is provided in payload/request data and no API key/token/password/secret is required, set auth.required='no' and auth.secrets=[]. "
                "Do not set auth.required='yes' unless an external credential, API key, token, password, OAuth secret, or private key must be configured outside the payload. "
                "permissions.env is only an allow-list for environment variables and must not by itself imply auth. "
                "dependencies must be objects: {package:'pip-package-name', imports:['python_import_name'], version:''}. "
                "permissions must include booleans network/read_files/write_files/subprocess and env list. "
                "Do not enforce any tool-specific output fields beyond the schema you define."
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
    ]
    model_plan, ack, err = await _complete_author_model("planner", messages, reason="creator_tool_step2_clarify_auth_plan")
    if ack:
        model_notes.append(f"planner_model={ack['model']}")
    if err:
        warnings.append(f"planner_model unavailable: {err}")
    plan = model_plan if isinstance(model_plan, dict) else {}
    manifest = plan.get("manifest") if isinstance(plan.get("manifest"), dict) else {}
    name = _slug(str(manifest.get("tool_name") or manifest.get("name") or plan.get("tool_name") or request.get("tool_name") or request.get("operation") or "custom_tool"))
    base = build_tool_manifest_draft({
        **request,
        "tool_name": name,
        "description": plan.get("description") or request.get("description") or request.get("requirement") or name,
        "input_schema": manifest.get("input_schema"),
        "output_schema": manifest.get("output_schema"),
        "dependencies": manifest.get("dependencies") if isinstance(manifest.get("dependencies"), list) else [],
        "permissions": manifest.get("permissions") if isinstance(manifest.get("permissions"), dict) else None,
        "artifact_policy": manifest.get("artifact_policy") if isinstance(manifest.get("artifact_policy"), dict) else None,
        "auth": manifest.get("auth") if isinstance(manifest.get("auth"), dict) else None,
    })
    merged = {**base, **manifest, "runtime_type": "python_script", "tool_name": name, "name": name}
    merged["input_schema"] = _canonical_schema(merged.get("input_schema"), fallback_properties={}, required=[])
    merged["output_schema"] = _canonical_schema(merged.get("output_schema"), fallback_properties={"success": {"type": "boolean"}, "result": {}, "error": {"type": "string"}}, required=["success"])
    merged.setdefault("dependencies", [])
    merged.setdefault("permissions", base["permissions"])
    merged.setdefault("artifact_policy", base["artifact_policy"])
    merged = _sync_auth_into_manifest(merged, request)
    sample = plan.get("sample_input") if isinstance(plan.get("sample_input"), dict) else {}
    if not sample and isinstance(request.get("sample_input"), dict):
        sample = request["sample_input"]
    questions = plan.get("clarification_questions") or plan.get("questions") or []
    if isinstance(questions, str):
        questions = [questions]
    if not isinstance(questions, list):
        questions = []
    questions = [str(q).strip() for q in questions if str(q).strip()][:5]
    needs_clarification = bool(plan.get("needs_clarification") or questions)
    auth_gate = _auth_gate_for_manifest(merged, require_config=True)
    return {
        "needs_clarification": needs_clarification,
        "questions": questions,
        "clarification_questions": questions,
        "tool_name": name,
        "tool_kind": "python_script_tool",
        "operation": str(request.get("operation") or request.get("description") or request.get("requirement") or ""),
        "manifest": merged,
        "sample_input": sample,
        "implementation_notes": plan.get("implementation_notes") if isinstance(plan.get("implementation_notes"), list) else [],
        "ready_for_code_generation": not needs_clarification and not bool(auth_gate.get("block_registration")),
        "requires_config": bool(auth_gate.get("required") and auth_gate.get("status") == "needs_config"),
        "requires_live_test": False,
        "requires_authoring_tools": False,
        "authoring_tool_plan": [],
        "auth_gate": auth_gate,
        "model_notes": [],
    }


async def _repair_script_tool_spec_with_model(*, request: dict[str, Any], plan: dict[str, Any], errors: list[str], model_notes: list[str], warnings: list[str]) -> dict[str, Any]:
    messages = [
        {"role": "system", "content": "You are tool_spec_repair_model. Return strict JSON only. Repair manifest for python_script runtime. Do not write code. Do not output wrapper_family or helper contracts. Schemas must be canonical JSON Schema objects. Dependencies must have package/imports. Permissions must be explicit booleans. Auth must be represented only as manifest.auth plus permissions.env."},
        {"role": "user", "content": json.dumps({"request": request, "current_plan": plan, "validation_errors": errors}, ensure_ascii=False)},
    ]
    repaired, ack, err = await _complete_author_model("planner", messages, reason="creator_tool_script_spec_repair")
    if ack:
        model_notes.append(f"tool_spec_repair_model={ack['model']}")
    if err:
        warnings.append(f"tool_spec_repair_model unavailable: {err}")
        return plan
    manifest = repaired.get("manifest") if isinstance(repaired, dict) and isinstance(repaired.get("manifest"), dict) else {}
    if not manifest:
        return plan
    updated = dict(plan)
    updated["manifest"] = _sync_auth_into_manifest(manifest, request)
    if isinstance(repaired.get("sample_input"), dict):
        updated["sample_input"] = repaired["sample_input"]
    return updated


async def _author_sample_input_with_model(*, request: dict[str, Any], plan: dict[str, Any], manifest: dict[str, Any], wrapper_family: str | None = None, model_notes: list[str], warnings: list[str]) -> dict[str, Any]:
    if isinstance(request.get("sample_input"), dict) and request.get("sample_input"):
        return request["sample_input"]
    if isinstance(plan.get("sample_input"), dict) and plan.get("sample_input"):
        return plan["sample_input"]
    messages = [{"role": "system", "content": "You are sample_input_model. Return strict JSON only: {\"sample_input\": {...}, \"notes\": []}. Generate one safe runnable test payload conforming to manifest.input_schema. Do not include secrets, API keys, tokens, or passwords."}, {"role": "user", "content": json.dumps({"request": request, "manifest": manifest}, ensure_ascii=False)}]
    result, ack, err = await _complete_author_model("planner", messages, reason="creator_tool_script_sample_input")
    if ack:
        model_notes.append(f"sample_input_model={ack['model']}")
    if err:
        warnings.append(f"sample_input_model unavailable, used empty fallback: {err}")
        return {}
    sample = result.get("sample_input") if isinstance(result, dict) else None
    return sample if isinstance(sample, dict) else {}


async def _author_script_with_model(*, request: dict[str, Any], plan: dict[str, Any], manifest: dict[str, Any], sample_input: dict[str, Any], model_notes: list[str], warnings: list[str]) -> str:
    messages = [
        {"role": "system", "content": "You are code_model. Return strict JSON only: {\"script_code\":\"...python code...\"}. Generate a complete Python script implementing the tool. The script must define run(payload: dict, config: dict | None = None) -> dict AND a public tool-specific function named after manifest.tool_name that delegates to run. Do not define execute_task. Do not use wrapper_family or platform context APIs. If writing files, write only under os.environ['TOOL_OUTPUT_DIR'] or OUTPUT_DIR. Return a JSON-serializable dict conforming to manifest.output_schema. Use only manifest.dependencies and Python standard library. Dependency imports in code must be actual Python module roots, not class/function names. If manifest.auth.required=yes, read required secrets from the env names declared in manifest.permissions.env/auth.secrets. Never hard-code secret values. Never require secrets in payload. When TOOL_TRIAL_RUN=1 and a required env var is missing, do not fail; return a successful dry-run response indicating auth_required=true and missing_env=[...]."},
        {"role": "user", "content": json.dumps({"user_request": {"description": request.get("description"), "operation": request.get("operation"), "tool_name": request.get("tool_name"), "input_description": request.get("input_description"), "output_description": request.get("output_description")}, "manifest": manifest, "sample_input": sample_input, "implementation_notes": plan.get("implementation_notes") or []}, ensure_ascii=False)},
    ]
    result, ack, err = await _complete_author_model("code", messages, reason="creator_tool_script_code")
    if ack:
        model_notes.append(f"code_model={ack['model']}")
    if err:
        warnings.append(f"code_model unavailable: {err}")
        return ""
    code = _strip_code_fence(str(result.get("script_code") or result.get("adapter_code") or result.get("code") or "")) if isinstance(result, dict) else ""
    return _ensure_named_entrypoint(code, manifest)


async def _repair_script_with_model(
    *,
    request: dict[str, Any],
    manifest: dict[str, Any],
    sample_input: dict[str, Any],
    script_code: str,
    validation: dict[str, Any],
    human_feedback: str = "",
    model_notes: list[str],
    warnings: list[str],
) -> str:
    original_code = _strip_code_fence(script_code or "")

    messages = [
        {
            "role": "system",
            "content": (
                "You are code_repair_model. Return strict JSON only: {\"script_code\":\"...python code...\"}. "
                "Repair the complete standalone python_script module according to the user's human_feedback. "
                "Do not return only a public wrapper. Do not return a diff. "
                "The returned script_code must define run(payload: dict, config: dict | None = None) -> dict "
                "and the public tool-specific function named after manifest.tool_name. "
                "The public function may delegate to run(), but run() must be present in the same returned script. "
                "Preserve helper functions that are still needed. "
                "Do not introduce wrapper_family, execute_task, platform context APIs, or backend-specific tool branches. "
                "Respect manifest.auth and permissions.env. Use env vars for secrets. Never hard-code secrets. "
                "Fix validation errors, stderr, traceback, and human feedback while preserving the full previous implementation. "
                "If no meaningful change is possible, still return the best complete corrected script, not an empty response."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "manifest": manifest,
                    "sample_input": sample_input,
                    "current_full_script_code": original_code,
                    "validation": validation,
                    "human_feedback": human_feedback,
                    "repair_goal": "Modify the existing implementation to satisfy human_feedback. Return the full corrected script.",
                    "forbidden_repair_outputs": [
                        "empty script_code",
                        "only imports plus public wrapper",
                        "public function that calls run() when run() is missing",
                        "diff or patch only",
                        "markdown explanation instead of JSON",
                        "partial code excerpt",
                    ],
                },
                ensure_ascii=False,
                default=str,
            ),
        },
    ]

    result, ack, err = await _complete_author_model(
        "code",
        messages,
        reason="creator_tool_script_repair",
    )

    if ack:
        model_notes.append(f"repair_code_model={ack['model']}")

    if err:
        warnings.append(f"repair_code_model unavailable: {err}")
        return original_code

    repaired = (
        _strip_code_fence(
            str(
                result.get("script_code")
                or result.get("adapter_code")
                or result.get("code")
                or ""
            )
        )
        if isinstance(result, dict)
        else ""
    )

    if not repaired.strip():
        warnings.append("repair_code_model returned empty script_code; kept previous runtime_code")
        return original_code

    repaired = _ensure_named_entrypoint(repaired, manifest)

    incomplete, reasons = _looks_like_incomplete_repair(
        original_code=original_code,
        repaired_code=repaired,
        manifest=manifest,
    )

    if incomplete:
        warnings.append(
            "repair_code_model returned incomplete script; kept previous runtime_code. "
            + "；".join(reasons)
        )
        return original_code

    return repaired


async def _validate_tool_manifest_with_repair(
    *,
    request: dict[str, Any],
    manifest: dict[str, Any],
    script_code: str,
    sample_input: dict[str, Any],
    dynamic: bool = True,
    real_run: bool = False,
    require_auth_config: bool = False,
    model_notes: list[str],
    warnings: list[str],
    max_attempts: int = 3,
    human_feedback: str = "",
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Validate one sample execution and repair script failures before surfacing them.

    Single-sample production/trial validation is a repair signal, not a terminal
    frontend failure.  If the adapter code is present and validation localizes a
    script/sample execution error, run the normal patch repair loop first.  Only
    return a failed validation after repair attempts are exhausted or the repair
    proposal is unsafe/incomplete.
    """

    current_code = _ensure_named_entrypoint(script_code, manifest)
    current_sample = sample_input if isinstance(sample_input, dict) else {}
    repair_log: list[dict[str, Any]] = []
    validation: dict[str, Any] = {}

    attempts = max(1, int(max_attempts or 1))
    for attempt in range(attempts):
        validation = validate_tool_manifest(
            manifest,
            adapter_code=current_code,
            sample_input=current_sample,
            dynamic=dynamic,
            real_run=real_run,
            require_auth_config=require_auth_config,
        )
        current_sample = validation.get("sample_input") if isinstance(validation.get("sample_input"), dict) else current_sample
        if validation.get("success"):
            break

        repair_log.append({
            "attempt": attempt + 1,
            "errors": validation.get("errors") or [],
            "dynamic_trial": validation.get("dynamic_trial") or {},
            "auth_review": validation.get("auth_review") or {},
            "real_run": bool(real_run),
        })

        if attempt >= attempts - 1 or not str(current_code or "").strip():
            break

        repaired_code = await _repair_script_with_model(
            request=request,
            manifest=manifest,
            sample_input=current_sample,
            script_code=current_code,
            validation=validation,
            human_feedback=human_feedback,
            model_notes=model_notes,
            warnings=warnings,
        )
        repaired_code = _ensure_named_entrypoint(repaired_code, manifest)

        incomplete, reasons = _looks_like_incomplete_repair(
            original_code=current_code,
            repaired_code=repaired_code,
            manifest=manifest,
        )
        if incomplete:
            warnings.append(
                "single-sample validation repair returned incomplete script; kept previous runtime_code. "
                + "；".join(reasons)
            )
            break

        current_code = repaired_code

    validation = dict(validation or {})
    validation["repair_log"] = repair_log
    if repair_log and not validation.get("success"):
        validation["status"] = "failed_after_repair"
    elif repair_log and validation.get("success"):
        validation["status"] = "validated_after_repair"
    return current_code, current_sample, validation, repair_log


async def _author_snippet_with_model(*, request: dict[str, Any], manifest: dict[str, Any], sample_input: dict[str, Any], model_notes: list[str], warnings: list[str]) -> dict[str, Any] | None:
    """Step 5: summarize a generic reusable snippet and IO contract.

    This is not a hard-coded business snippet. The model summarizes how the
    completed tool should be called by sandbox / skill creator / workflow code.
    """
    messages = [
        {
            "role": "system",
            "content": (
                "You are snippet_model for a generic tool platform. Return strict JSON only: {\"snippet\": {...}}. "
                "Summarize the completed tool for downstream callers. Include: "
                "direct_usage code that calls run_tool(payload), indirect_usage guidance for sandbox/skill creator/workflow, "
                "input_io fields, output_io fields, required inputs, required outputs, auth notes, artifact notes, and integration cautions. "
                "Do not add tool-specific backend rules. Do not invent fields not present in manifest schemas. Do not include secrets in examples."
            ),
        },
        {"role": "user", "content": json.dumps({"request": request, "manifest": manifest, "sample_input": sample_input}, ensure_ascii=False, default=str)},
    ]
    result, ack, err = await _complete_author_model("planner", messages, reason="creator_tool_step5_snippet_summary")
    if ack:
        model_notes.append(f"snippet_model={ack['model']}")
    if err:
        warnings.append(f"snippet_model unavailable; used deterministic snippet: {err}")
        return None
    snippet = result.get("snippet") if isinstance(result, dict) else None
    return snippet if isinstance(snippet, dict) else None


def _script_tool_snippet(manifest: dict[str, Any], sample_input: dict[str, Any]) -> dict[str, Any]:
    tool_name = _slug(str(manifest.get("tool_name") or manifest.get("name") or "custom_tool"))
    input_schema = _canonical_schema(manifest.get("input_schema"), fallback_properties={}, required=[])
    output_schema = _canonical_schema(manifest.get("output_schema"), fallback_properties={}, required=[])
    auth = _normalize_auth_plan(manifest)
    return {
        "id": f"{tool_name}.generic_usage",
        "title": f"Use {tool_name}",
        "kind": "minimal_usage",
        "description": str(manifest.get("description") or f"Run {tool_name} with a JSON payload."),
        "direct_usage": {
            "code": "payload = " + json.dumps(sample_input or {}, ensure_ascii=False, indent=2) + "\nresult = run_tool(payload)\nreturn result",
            "entrypoint": "run_tool(payload)",
            "public_entrypoint": f"{tool_name}(payload, config=None)",
        },
        "indirect_usage": {
            "for_sandbox": "Use this snippet when a sandbox step needs this tool. Pass a dict that conforms to input_io.schema and consume only output_io.required_fields unless optional fields are documented.",
            "for_skill_creator": "Reference this tool_contract/snippet when generating SKILL.md or runtime scripts. Do not infer extra IO beyond the schemas.",
            "for_workflow": "Use call_contract.public_entrypoint or platform run_tool with the payload object. Keep secrets out of payload.",
        },
        "code": "payload = " + json.dumps(sample_input or {}, ensure_ascii=False, indent=2) + "\nresult = run_tool(payload)\nreturn result",
        "input_io": {"schema": input_schema, "fields": _schema_field_summaries(input_schema), "required_fields": _schema_required(input_schema), "sample_input": sample_input or {}},
        "output_io": {"schema": output_schema, "fields": _schema_field_summaries(output_schema), "required_fields": _schema_required(output_schema)},
        "auth": {"required": auth.get("required") or "no", "secrets": auth.get("secrets") or [], "reason": auth.get("reason") or ""},
        "artifact_io": manifest.get("artifact_policy") if isinstance(manifest.get("artifact_policy"), dict) else {"file_fields": []},
        "expected_input_shape": input_schema,
        "expected_output_shape": output_schema,
        "return_rule": "Return the generated tool result directly; downstream modules should rely on output_io.schema.",
        "anti_patterns": ["Do not pass secrets in payload.", "Do not invent input/output fields outside the tool contract.", "Do not write files outside TOOL_OUTPUT_DIR unless the artifact policy allows it."],
        "requires": ["deterministic_execution"],
        "usage_policy": "self_implementation_allowed",
        "priority": 80,
    }


def _schema_field_summaries(schema: Any) -> list[dict[str, Any]]:
    schema = schema if isinstance(schema, dict) else {}
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = set(_schema_required(schema))
    rows: list[dict[str, Any]] = []
    for name, spec in properties.items():
        spec = spec if isinstance(spec, dict) else {}
        rows.append({
            "name": str(name),
            "type": spec.get("type") or "any",
            "required": str(name) in required,
            "description": spec.get("description") or "",
            "default": spec.get("default") if "default" in spec else None,
        })
    return rows


def _fallback_tool_contract(*, request: dict[str, Any], manifest: dict[str, Any], script_code: str, sample_input: dict[str, Any], validation: dict[str, Any]) -> dict[str, Any]:
    tool_name = _slug(str(manifest.get("tool_name") or manifest.get("name") or request.get("tool_name") or "custom_tool"))
    auth = _normalize_auth_plan(manifest)
    permissions = manifest.get("permissions") if isinstance(manifest.get("permissions"), dict) else {}
    input_schema = _canonical_schema(manifest.get("input_schema"), fallback_properties={}, required=[])
    output_schema = _canonical_schema(manifest.get("output_schema"), fallback_properties={}, required=[])
    snippet = _script_tool_snippet(manifest, sample_input if isinstance(sample_input, dict) else {})
    return {
        "tool_name": tool_name,
        "display_name": str(manifest.get("display_name") or tool_name.replace("_", " ").title()),
        "function_name": _manifest_tool_function_name(manifest),
        "runtime_type": "python_script",
        "purpose": str(manifest.get("description") or request.get("description") or request.get("requirement") or ""),
        "when_to_use": str(manifest.get("when_to_use") or manifest.get("description") or ""),
        "input_contract": {
            "schema": input_schema,
            "fields": _schema_field_summaries(input_schema),
            "required_fields": _schema_required(input_schema),
            "sample_input": sample_input if isinstance(sample_input, dict) else {},
        },
        "output_contract": {
            "schema": output_schema,
            "fields": _schema_field_summaries(output_schema),
            "required_fields": _schema_required(output_schema),
        },
        "call_contract": {
            "internal_entrypoint": "run(payload, config=None)",
            "public_entrypoint": f"{_manifest_tool_function_name(manifest)}(payload, config=None)",
            "platform_snippet_entrypoint": "run_tool(payload)",
            "returns": "JSON-serializable dict conforming to output_contract.schema",
        },
        "snippet": snippet,
        "direct_usage": snippet.get("direct_usage"),
        "indirect_usage": snippet.get("indirect_usage"),
        "artifacts": manifest.get("artifact_policy") if isinstance(manifest.get("artifact_policy"), dict) else {"file_fields": []},
        "auth": {
            "required": auth.get("required") or "no",
            "reason": auth.get("reason") or "",
            "secrets": auth.get("secrets") or [],
        },
        "permissions": permissions,
        "dependencies": manifest.get("dependencies") if isinstance(manifest.get("dependencies"), list) else [],
        "validation_summary": {
            "success": bool(validation.get("success")) if isinstance(validation, dict) else False,
            "status": validation.get("status") if isinstance(validation, dict) else "unknown",
            "errors": validation.get("errors") if isinstance(validation, dict) else [],
            "can_register": validation.get("can_register") if isinstance(validation, dict) else False,
        },
        "integration_notes": [
            "This contract is generic and derived from manifest/code/validation; downstream modules should not infer hidden IO.",
            "Call the public entrypoint with a dict payload, or let the platform call run(payload, config=None).",
            "Do not pass secrets in payload; use declared environment variables when auth.required is yes.",
            "Downstream callers should depend on output_contract.required_fields, not incidental debug fields.",
        ],
    }


async def _summarize_tool_contract_with_model(*, request: dict[str, Any], manifest: dict[str, Any], script_code: str, sample_input: dict[str, Any], validation: dict[str, Any], model_notes: list[str], warnings: list[str]) -> dict[str, Any]:
    """Use the planner model to summarize the completed generic tool contract.

    This summary is intentionally business-agnostic: the model must describe the
    generated tool's observed contract from manifest/code/validation, not enforce
    any special rule for a particular tool type.
    """
    fallback = _fallback_tool_contract(request=request, manifest=manifest, script_code=script_code, sample_input=sample_input, validation=validation)
    script_excerpt = (script_code or "")[:12000]
    dynamic_trial = validation.get("dynamic_trial") if isinstance(validation, dict) and isinstance(validation.get("dynamic_trial"), dict) else {}
    observed_result = dynamic_trial.get("result") if isinstance(dynamic_trial, dict) else None
    messages = [
        {
            "role": "system",
            "content": (
                "You are tool_contract_model for a generic tool creation platform. "
                "Return strict JSON only: {\"tool_contract\": {...}}. "
                "Summarize what the completed generated tool does, how downstream modules should call it, "
                "its input schema, output schema, required fields, auth requirements, artifacts, dependencies, "
                "and integration cautions. Do not add business-specific validation rules. Do not change schemas. "
                "Do not invent secrets. Base the summary only on manifest, generated code, sample input, and validation result."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "request": {k: request.get(k) for k in ["description", "tool_name", "operation", "input_description", "output_description"]},
                    "manifest": manifest,
                    "sample_input": sample_input,
                    "validation": {
                        "success": validation.get("success") if isinstance(validation, dict) else False,
                        "status": validation.get("status") if isinstance(validation, dict) else "unknown",
                        "errors": validation.get("errors") if isinstance(validation, dict) else [],
                        "warnings": validation.get("warnings") if isinstance(validation, dict) else [],
                        "auth_gate": validation.get("auth_gate") if isinstance(validation, dict) else {},
                        "observed_result": observed_result,
                    },
                    "generated_script_excerpt": script_excerpt,
                    "required_output_fields": _schema_required(manifest.get("output_schema") if isinstance(manifest, dict) else {}),
                },
                ensure_ascii=False,
                default=str,
            ),
        },
    ]
    result, ack, err = await _complete_author_model("planner", messages, reason="creator_tool_contract_summary")
    if ack:
        model_notes.append(f"tool_contract_model={ack['model']}")
    if err:
        warnings.append(f"tool_contract_model unavailable; used deterministic contract: {err}")
        return fallback
    contract = result.get("tool_contract") if isinstance(result, dict) else None
    if not isinstance(contract, dict):
        warnings.append("tool_contract_model returned invalid JSON shape; used deterministic contract")
        return fallback
    # Preserve canonical machine-readable fields even if the model writes a more
    # human friendly summary.
    merged = {**fallback, **contract}
    merged["tool_name"] = fallback["tool_name"]
    merged["function_name"] = fallback["function_name"]
    merged["runtime_type"] = "python_script"
    merged["input_contract"] = fallback["input_contract"] | (contract.get("input_contract") if isinstance(contract.get("input_contract"), dict) else {})
    merged["output_contract"] = fallback["output_contract"] | (contract.get("output_contract") if isinstance(contract.get("output_contract"), dict) else {})
    merged["auth"] = fallback["auth"] | (contract.get("auth") if isinstance(contract.get("auth"), dict) else {})
    merged["call_contract"] = fallback["call_contract"] | (contract.get("call_contract") if isinstance(contract.get("call_contract"), dict) else {})
    return merged


async def author_tool(request: dict[str, Any]) -> dict[str, Any]:
    """Generic five-step authoring workflow.

    Step 1: receive requirement and optional reference code.
    Step 2: planner clarifies requirements and decides auth; configure stores auth.
    Step 3: coder writes a complete python_script.
    Step 4: human trial/debug can run generated code and revise with feedback.
    Step 5: model summarizes snippet + IO + indirect usage contract for sandbox/skill creator.
    """
    request = dict(request or {})
    model_notes: list[str] = []
    warnings: list[str] = []
    raw_action = str(request.get("action") or "generate").strip().lower()
    action_aliases = {
        "plan": "clarify",
        "ask": "clarify",
        "requirements": "clarify",
        "auth": "configure",
        "authorize": "configure",
        "save_auth": "configure",
        "save_config": "configure",
        "code": "generate",
        "generate_code": "generate",
        "live_test": "trial_run",
        "test": "trial_run",
        "debug": "trial_run",
        "run": "trial_run",
        "feedback": "revise",
        "repair": "revise",
        "summary": "summarize",
        "snippet": "summarize",
        "contract": "summarize",
    }
    action = action_aliases.get(raw_action, raw_action)
    if action not in {"clarify", "configure", "generate", "trial_run", "finalize", "revise", "summarize"}:
        raise ValueError("action must be one of clarify/configure/generate/trial_run/finalize/revise/summarize")

    if action == "configure":
        saved = save_tool_authoring_config(request)
        manifest = _sync_auth_into_manifest(request.get("manifest") if isinstance(request.get("manifest"), dict) else {}, request)
        auth_gate = _auth_gate_for_manifest(manifest)
        return {
            "workflow_step": 2,
            "current_step": "auth_configured" if saved.get("configured") else "auth_config_needed",
            "current_phase": "auth_configured" if saved.get("configured") else "auth_config_needed",
            "needs_clarification": False,
            "questions": [],
            "clarification_questions": [],
            "manifest": manifest,
            "auth_gate": auth_gate,
            "config_status": saved,
            "validation": {"success": bool(saved.get("configured")), "status": "auth_configured" if saved.get("configured") else "auth_config_needed", "errors": [] if saved.get("configured") else saved.get("missing_secrets") or [], "warnings": [], "auth_gate": auth_gate},
            "model_notes": model_notes,
            "warnings": warnings,
            "requires_human_confirmation": True,
            "can_register": bool(auth_gate.get("can_register")),
        }

    if action == "clarify":
        plan = await _run_planner(request, model_notes, warnings)
        manifest = _sync_auth_into_manifest(plan.get("manifest") if isinstance(plan.get("manifest"), dict) else {}, request)
        auth_gate = _auth_gate_for_manifest(manifest)
        return {
            "workflow_step": 2,
            "current_step": "clarification_needed" if plan.get("needs_clarification") else "planned",
            "current_phase": "clarification_needed" if plan.get("needs_clarification") else "auth_reviewed",
            "needs_clarification": bool(plan.get("needs_clarification")),
            "questions": plan.get("questions") or [],
            "clarification_questions": plan.get("clarification_questions") or plan.get("questions") or [],
            "manifest": manifest,
            "sample_input": plan.get("sample_input") or {},
            "auth_gate": auth_gate,
            "requires_config": bool(auth_gate.get("required") and auth_gate.get("status") == "needs_config"),
            "validation": {"success": not bool(plan.get("needs_clarification")), "status": "planned", "errors": [], "warnings": [], "auth_gate": auth_gate},
            "snippet": None,
            "model_notes": model_notes,
            "warnings": warnings,
            "requires_human_confirmation": True,
            "can_register": False,
        }

    if action == "trial_run":
        manifest = _sync_auth_into_manifest(
            request.get("manifest") if isinstance(request.get("manifest"), dict) else {},
            request,
        )
        sample_input = request.get("sample_input") if isinstance(request.get("sample_input"), dict) else {}

        script_code = _ensure_named_entrypoint(
            _request_runtime_code(request),
            manifest,
        )

        script_code, sample_input, validation, _repair_log = await _validate_tool_manifest_with_repair(
            request=request,
            manifest=manifest,
            script_code=script_code,
            sample_input=sample_input,
            dynamic=True,
            real_run=bool(request.get("real_run") or request.get("allow_external_network")),
            require_auth_config=False,
            model_notes=model_notes,
            warnings=warnings,
            max_attempts=3,
            human_feedback=str(request.get("human_feedback") or request.get("feedback") or ""),
        )
        warnings.extend(validation.get("sample_notes") or [])

        return {
            "workflow_step": 4,
            "current_step": "trial_run_complete",
            "current_phase": "trial_run_success" if validation.get("success") else "trial_run_needs_feedback",
            "needs_clarification": False,
            "questions": [],
            "manifest": manifest,
            **_code_view_fields(
                manifest=manifest,
                runtime_code=script_code,
                sample_input=sample_input,
            ),
            "sample_input": sample_input,
            "validation": validation,
            "auth_gate": validation.get("auth_gate") or _auth_gate_for_manifest(manifest),
            "debug_sections": validation.get("debug_sections") or [],
            "collapsible_blocks": validation.get("collapsible_blocks") or [],
            "call_chain": validation.get("call_chain") or [],
            "model_notes": model_notes,
            "warnings": warnings,
            "ready_for_feedback": True,
            "requires_human_confirmation": True,
            "can_register": bool(validation.get("can_register")),
        }

    if action == "finalize":
        manifest = _sync_auth_into_manifest(
            request.get("manifest") if isinstance(request.get("manifest"), dict) else {},
            request,
        )
        sample_input = request.get("sample_input") if isinstance(request.get("sample_input"), dict) else {}

        script_code = _ensure_named_entrypoint(
            _request_runtime_code(request),
            manifest,
        )

        script_code, sample_input, validation, _repair_log = await _validate_tool_manifest_with_repair(
            request=request,
            manifest=manifest,
            script_code=script_code,
            sample_input=sample_input,
            dynamic=True,
            real_run=bool(request.get("allow_external_network")),
            require_auth_config=True,
            model_notes=model_notes,
            warnings=warnings,
            max_attempts=3,
            human_feedback=str(request.get("human_feedback") or request.get("feedback") or ""),
        )
        warnings.extend(validation.get("sample_notes") or [])

        snippet = None
        tool_contract = None

        if validation.get("success"):
            snippet = await _author_snippet_with_model(
                request=request,
                manifest=manifest,
                sample_input=sample_input,
                model_notes=model_notes,
                warnings=warnings,
            ) or _script_tool_snippet(manifest, sample_input)

            tool_contract = await _summarize_tool_contract_with_model(
                request=request,
                manifest=manifest,
                script_code=script_code,
                sample_input=sample_input,
                validation=validation,
                model_notes=model_notes,
                warnings=warnings,
            )
            tool_contract.setdefault("snippet", snippet)

        return {
            "workflow_step": 5,
            "current_step": "finalized" if validation.get("success") else "finalize_failed",
            "current_phase": "contract_ready" if validation.get("success") else "finalize_needs_fix",
            "needs_clarification": False,
            "questions": [],
            "manifest": manifest,

            **_code_view_fields(
                manifest=manifest,
                runtime_code=script_code,
                sample_input=sample_input,
            ),

            "sample_input": sample_input,
            "validation": validation,
            "auth_gate": validation.get("auth_gate"),
            "debug_sections": validation.get("debug_sections") or [],
            "collapsible_blocks": validation.get("collapsible_blocks") or [],
            "call_chain": validation.get("call_chain") or [],

            "tool_contract": tool_contract,
            "tool_summary": tool_contract,
            "snippet": snippet,

            "model_notes": model_notes,
            "warnings": warnings,
            "requires_human_confirmation": True,
            "can_register": bool(validation.get("can_register", False)),
        }

    if action == "summarize":
        manifest = _sync_auth_into_manifest(
            request.get("manifest") if isinstance(request.get("manifest"), dict) else {},
            request,
        )
        sample_input = request.get("sample_input") if isinstance(request.get("sample_input"), dict) else {}

        script_code = _ensure_named_entrypoint(
            _request_runtime_code(request),
            manifest,
        )

        validation = (
            request.get("validation")
            if isinstance(request.get("validation"), dict)
            else validate_tool_manifest(
                manifest,
                adapter_code=script_code,
                sample_input=sample_input,
                dynamic=False,
                require_auth_config=False,
            )
        )
        if isinstance(validation, dict):
            if not isinstance(validation.get("sample_input"), dict):
                resolved_sample_input, sample_notes = resolve_tool_trial_sample_input(manifest, sample_input)
                validation = {**validation, "sample_input": resolved_sample_input, "sample_notes": sample_notes}
            sample_input = validation.get("sample_input") if isinstance(validation.get("sample_input"), dict) else sample_input
            warnings.extend(validation.get("sample_notes") or [])

        snippet = await _author_snippet_with_model(
            request=request,
            manifest=manifest,
            sample_input=sample_input,
            model_notes=model_notes,
            warnings=warnings,
        ) or _script_tool_snippet(manifest, sample_input)

        tool_contract = await _summarize_tool_contract_with_model(
            request=request,
            manifest=manifest,
            script_code=script_code,
            sample_input=sample_input,
            validation=validation,
            model_notes=model_notes,
            warnings=warnings,
        )
        tool_contract.setdefault("snippet", snippet)

        return {
            "workflow_step": 5,
            "current_step": "contract_ready",
            "current_phase": "snippet_and_io_summarized",
            "needs_clarification": False,
            "questions": [],
            "manifest": manifest,

            **_code_view_fields(
                manifest=manifest,
                runtime_code=script_code,
                sample_input=sample_input,
            ),

            "sample_input": sample_input,
            "validation": validation,
            "auth_gate": validation.get("auth_gate") if isinstance(validation, dict) else _auth_gate_for_manifest(manifest),
            "tool_contract": tool_contract,
            "tool_summary": tool_contract,
            "snippet": snippet,

            "model_notes": model_notes,
            "warnings": warnings,
            "can_register": bool(validation.get("can_register", False)) if isinstance(validation, dict) else False,
        }

    if action == "revise":
        manifest = _sync_auth_into_manifest(
            request.get("manifest") if isinstance(request.get("manifest"), dict) else {},
            request,
        )
        sample_input = request.get("sample_input") if isinstance(request.get("sample_input"), dict) else {}

        raw_script_code = _request_runtime_code(
            request,
            allow_display_fallback=False,
        )

        human_feedback = str(
            request.get("human_feedback")
            or request.get("review_feedback")
            or request.get("feedback")
            or ""
        ).strip()

        if not raw_script_code.strip():
            validation = {
                "success": False,
                "status": "missing_runtime_code",
                "errors": [
                    "revise requires previous full runtime_code/full_adapter_code/internal_code/script_code; display-only adapter_code is not enough"
                ],
                "warnings": [],
                "auth_gate": _auth_gate_for_manifest(manifest, require_config=False),
                "debug_sections": [],
                "collapsible_blocks": [],
                "call_chain": [],
                "can_register": False,
            }

            return {
                "workflow_step": 4,
                "current_step": "revision_missing_runtime_code",
                "current_phase": "revision_needs_previous_runtime_code",
                "needs_clarification": False,
                "questions": [],
                "manifest": manifest,
                "sample_input": sample_input,
                "validation": validation,
                "auth_gate": validation["auth_gate"],
                "debug_sections": [],
                "collapsible_blocks": [],
                "call_chain": [],
                "human_feedback": human_feedback,
                "repair_replaced_code": False,
                "repair_status": "missing_runtime_code",
                "model_notes": model_notes,
                "warnings": warnings,
                "requires_human_confirmation": True,
                "ready_for_feedback": True,
                "ready_for_debug": True,
                "can_register": False,
            }

        # 这里不再把“解析失败 / 缺 run()”当成 missing。
        # 因为 raw_script_code 已经来自官方 runtime 字段。
        # repair_model 可以基于这份完整代码继续修。
        script_code = _ensure_named_entrypoint(raw_script_code, manifest)

        initial_validation = validate_tool_manifest(
            manifest,
            adapter_code=script_code,
            sample_input=sample_input,
            dynamic=True,
            real_run=False,
            require_auth_config=False,
        )

        repaired_code = await _repair_script_with_model(
            request=request,
            manifest=manifest,
            sample_input=sample_input,
            script_code=script_code,
            validation=request.get("validation") if isinstance(request.get("validation"), dict) else initial_validation,
            human_feedback=human_feedback,
            model_notes=model_notes,
            warnings=warnings,
        )

        repaired_code = _ensure_named_entrypoint(repaired_code, manifest)

        repair_replaced_code = repaired_code.strip() != script_code.strip()

        validation = validate_tool_manifest(
            manifest,
            adapter_code=repaired_code,
            sample_input=sample_input,
            dynamic=True,
            real_run=False,
            require_auth_config=False,
        )

        if not repair_replaced_code:
            validation = {
                **validation,
                "success": False,
                "status": "repair_incomplete_or_no_change",
                "errors": sorted(set([
                    *(validation.get("errors") or []),
                    "repair model did not return a changed complete script; previous runtime_code was kept",
                ])),
                "warnings": sorted(set([
                    *(validation.get("warnings") or []),
                    *warnings,
                ])),
                "can_register": False,
            }

        snippet = None
        tool_contract = None

        if validation.get("success"):
            snippet = await _author_snippet_with_model(
                request=request,
                manifest=manifest,
                sample_input=sample_input,
                model_notes=model_notes,
                warnings=warnings,
            ) or _script_tool_snippet(manifest, sample_input)

            tool_contract = await _summarize_tool_contract_with_model(
                request=request,
                manifest=manifest,
                script_code=repaired_code,
                sample_input=sample_input,
                validation=validation,
                model_notes=model_notes,
                warnings=warnings,
            )
            tool_contract.setdefault("snippet", snippet)

        return {
            "workflow_step": 4,
            "current_step": "code_revised" if repair_replaced_code else "repair_incomplete",
            "current_phase": "revision_validated" if validation.get("success") else "revision_needs_more_feedback",
            "needs_clarification": False,
            "questions": [],
            "manifest": manifest,

            **_code_view_fields(
                manifest=manifest,
                runtime_code=repaired_code,
                sample_input=sample_input,
            ),

            "sample_input": sample_input,
            "validation": validation,
            "auth_gate": validation.get("auth_gate") or _auth_gate_for_manifest(manifest),
            "debug_sections": validation.get("debug_sections") or [],
            "collapsible_blocks": validation.get("collapsible_blocks") or [],
            "call_chain": validation.get("call_chain") or [],

            "human_feedback": human_feedback,
            "repair_replaced_code": repair_replaced_code,
            "repair_status": "changed" if repair_replaced_code else "no_change",

            "tool_contract": tool_contract,
            "tool_summary": tool_contract,
            "snippet": snippet,

            "model_notes": model_notes,
            "warnings": warnings,
            "requires_human_confirmation": True,
            "ready_for_feedback": True,
            "ready_for_debug": True,
            "code_generated": True,
            "code_generation_complete": True,
            "can_register": bool(validation.get("can_register")),
        }

    # Step 3: generate code. Do not run business-specific rules; only generic validation.
    plan = await _run_planner(request, model_notes, warnings)
    manifest = _sync_auth_into_manifest(
        plan.get("manifest") if isinstance(plan.get("manifest"), dict) else {},
        request,
    )
    auth_gate = _auth_gate_for_manifest(manifest)

    if plan.get("needs_clarification"):
        return {
            "workflow_step": 2,
            "current_step": "clarification_needed",
            "current_phase": "waiting_for_user_clarification",
            "needs_clarification": True,
            "questions": plan.get("questions") or [],
            "clarification_questions": plan.get("clarification_questions") or plan.get("questions") or [],
            "manifest": manifest,
            "sample_input": plan.get("sample_input") or {},
            "auth_gate": auth_gate,
            "requires_config": bool(auth_gate.get("required") and auth_gate.get("status") == "needs_config"),
            "validation": {
                "success": False,
                "status": "clarification_needed",
                "errors": [],
                "warnings": [],
                "auth_gate": auth_gate,
            },
            "model_notes": model_notes,
            "warnings": warnings,
            "can_register": False,
        }

    spec_repair_log: list[dict[str, Any]] = []

    for attempt in range(3):
        spec_errors = _script_tool_spec_errors(manifest)
        if not spec_errors:
            break

        spec_repair_log.append({
            "attempt": attempt + 1,
            "errors": spec_errors,
        })

        plan = await _repair_script_tool_spec_with_model(
            request=request,
            plan={**plan, "manifest": manifest},
            errors=spec_errors,
            model_notes=model_notes,
            warnings=warnings,
        )

        manifest = _sync_auth_into_manifest(
            plan.get("manifest") if isinstance(plan.get("manifest"), dict) else {},
            request,
        )

    spec_errors = _script_tool_spec_errors(manifest)

    if spec_errors:
        auth_gate = _auth_gate_for_manifest(manifest)

        return {
            "workflow_step": 2,
            "current_step": "tool_spec_invalid",
            "current_phase": "tool_spec_needs_repair",
            "needs_clarification": False,
            "questions": [],
            "clarification_questions": [],
            "manifest": manifest,

            # 没有生成可执行代码，四类代码字段都返回空，避免前端误用旧代码。
            "display_code": "",
            "public_api_code": "",
            "adapter_code": "",
            "adapter_code_kind": "python_public_function",
            "runtime_code": "",
            "script_code": "",
            "full_adapter_code": "",
            "internal_code": "",
            "internal_code_kind": "python_script",

            "sample_input": plan.get("sample_input") or {},
            "validation": {
                "success": False,
                "status": "tool_spec_invalid",
                "errors": spec_errors,
                "warnings": [],
                "spec_repair_log": spec_repair_log,
                "auth_gate": auth_gate,
            },
            "auth_gate": auth_gate,
            "snippet": None,
            "tool_contract": None,
            "tool_summary": None,
            "model_notes": model_notes,
            "warnings": warnings,
            "requires_human_confirmation": True,
            "can_register": False,
        }

    sample_input = await _author_sample_input_with_model(
        request=request,
        plan=plan,
        manifest=manifest,
        wrapper_family=None,
        model_notes=model_notes,
        warnings=warnings,
    )

    script_code = await _author_script_with_model(
        request=request,
        plan=plan,
        manifest=manifest,
        sample_input=sample_input,
        model_notes=model_notes,
        warnings=warnings,
    )

    if not script_code.strip():
        script_code = generate_adapter_code(manifest)
        warnings.append("code_model returned empty script_code; used deterministic template")

    script_code = _ensure_named_entrypoint(script_code, manifest)

    script_code, sample_input, validation, repair_log = await _validate_tool_manifest_with_repair(
        request=request,
        manifest=manifest,
        script_code=script_code,
        sample_input=sample_input,
        dynamic=True,
        real_run=False,
        require_auth_config=False,
        model_notes=model_notes,
        warnings=warnings,
        max_attempts=3,
    )
    validation["spec_repair_log"] = spec_repair_log

    snippet = await _author_snippet_with_model(
        request=request,
        manifest=manifest,
        sample_input=sample_input,
        model_notes=model_notes,
        warnings=warnings,
    ) or _script_tool_snippet(manifest, sample_input)

    tool_contract = await _summarize_tool_contract_with_model(
        request=request,
        manifest=manifest,
        script_code=script_code,
        sample_input=sample_input,
        validation=validation,
        model_notes=model_notes,
        warnings=warnings,
    )
    tool_contract.setdefault("snippet", snippet)

    code_generated = bool(script_code.strip())
    validation_success = bool(validation.get("success"))

    return {
        "workflow_step": 3,
        "current_step": "validated" if validation_success else "code_generated",
        "current_phase": "validated" if validation_success else "code_generated_needs_manual_trial",

        "needs_clarification": False,
        "questions": [],
        "clarification_questions": [],

        "tool_kind": "python_script_tool",
        "operation": (
            plan.get("operation")
            or request.get("operation")
            or request.get("description")
            or request.get("requirement")
            or ""
        ),

        "manifest": manifest,

        # 关键修改：
        # 主框展示 display_code / adapter_code，只展示公开函数核心实现；
        # 折叠框和后续执行使用 runtime_code / script_code / full_adapter_code / internal_code。
        **_code_view_fields(
            manifest=manifest,
            runtime_code=script_code,
            sample_input=sample_input,
        ),

        "sample_input": sample_input,
        "validation": validation,
        "auth_gate": validation.get("auth_gate") or _auth_gate_for_manifest(manifest),

        "debug_sections": validation.get("debug_sections") or [],
        "collapsible_blocks": validation.get("collapsible_blocks") or [],
        "call_chain": validation.get("call_chain") or [],

        "tool_contract": tool_contract,
        "tool_summary": tool_contract,
        "snippet": snippet if code_generated else None,

        "model_notes": model_notes,
        "warnings": warnings,

        "requires_human_confirmation": True,
        "requires_config": False,
        "requires_live_test": True,
        "requires_authoring_tools": False,
        "ready_for_code_generation": True,
        "code_generated": code_generated,
        "code_generation_complete": code_generated,
        "ready_for_debug": code_generated,
        "can_register": validation.get("can_register", False),
    }

async def stream_author_tool(request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    """Stream events for the generic five-step authoring workflow."""
    yield {"event": "started", "workflow_step": 1, "message": "已接收需求和参考代码"}
    yield {"event": "planning", "workflow_step": 2, "message": "模型正在追问、规划输入输出并判断授权"}

    result = await author_tool(request)
    result = result if isinstance(result, dict) else {}

    if result.get("needs_clarification"):
        yield {
            "event": "clarification_questions",
            "workflow_step": 2,
            "message": "模型需要补充信息",
            "questions": result.get("questions") or result.get("clarification_questions") or [],
            "clarification_questions": result.get("clarification_questions") or result.get("questions") or [],
            "manifest": result.get("manifest") or {},
            "auth_gate": result.get("auth_gate") or {},
        }

    if result.get("auth_gate"):
        yield {
            "event": "auth_review",
            "workflow_step": 2,
            "message": "授权判断完成",
            "auth_gate": result.get("auth_gate") or {},
            "manifest": result.get("manifest") or {},
        }

    display_code = str(
        result.get("display_code")
        or result.get("public_api_code")
        or result.get("adapter_code")
        or ""
    )

    runtime_code = str(
        result.get("runtime_code")
        or result.get("full_adapter_code")
        or result.get("internal_code")
        or result.get("script_code")
        or ""
    )

    if display_code or runtime_code:
        yield {
            "event": "model_delta",
            "workflow_step": 3,
            "message": "模型代码已生成",
            "delta": display_code or runtime_code,
            "display_code": display_code,
            "public_api_code": display_code,
            "adapter_code": display_code,
            "runtime_code": runtime_code,
            "script_code": runtime_code,
            "full_adapter_code": runtime_code,
            "internal_code": runtime_code,
            "adapter_code_kind": result.get("adapter_code_kind") or "python_public_function",
            "internal_code_kind": result.get("internal_code_kind") or "python_script",
        }

    validation = result.get("validation") if isinstance(result.get("validation"), dict) else {}

    if validation:
        yield {
            "event": "validation",
            "workflow_step": 4 if (display_code or runtime_code) else 2,
            "message": "代码校验完成" if validation.get("success") else "代码已生成，但需要人工试运行/反馈",
            "success": bool(validation.get("success")),
            "errors": validation.get("errors") or [],
            "warnings": validation.get("warnings") or [],
            "validation": validation,
            "debug_sections": validation.get("debug_sections") or [],
            "collapsible_blocks": validation.get("collapsible_blocks") or [],
        }

    debug_sections = (
        validation.get("debug_sections")
        or validation.get("collapsible_blocks")
        or []
    ) if isinstance(validation, dict) else []

    if debug_sections:
        yield {
            "event": "debug_trace",
            "workflow_step": 4,
            "message": "调试调用链已生成",
            "debug_sections": debug_sections,
            "collapsible_blocks": debug_sections,
            "call_chain": validation.get("call_chain") or [],
        }

    dynamic_trial = validation.get("dynamic_trial") if isinstance(validation, dict) else {}
    temporary_environment = (
        dynamic_trial.get("temporary_environment")
        if isinstance(dynamic_trial, dict)
        else validation.get("temporary_environment")
        if isinstance(validation, dict)
        else {}
    )

    if temporary_environment:
        yield {
            "event": "temporary_environment",
            "workflow_step": 4,
            "message": "临时环境创建完成",
            "temporary_environment": temporary_environment,
        }

    tool_contract = (
        result.get("tool_contract")
        if isinstance(result.get("tool_contract"), dict)
        else result.get("tool_summary")
        if isinstance(result.get("tool_summary"), dict)
        else {}
    )

    if tool_contract:
        yield {
            "event": "tool_contract",
            "workflow_step": 5,
            "message": "工具功能、Snippet 和输入输出合同已生成",
            "tool_contract": tool_contract,
            "tool_summary": tool_contract,
            "snippet": result.get("snippet") or tool_contract.get("snippet"),
        }

    yield {
        "event": "final_result",
        "message": "流程完成" if (display_code or runtime_code or tool_contract) else "流程完成，但未得到代码",
        **result,
    }

    yield {"event": "completed", "result": result}


def _tool_authoring_config_store_path() -> Path:
    raw = os.environ.get("TOOL_AUTHORING_CONFIG_STORE_PATH", "").strip()
    return Path(raw).expanduser().resolve() if raw else (CONFIG_DIR / "tool_authoring_configs.json").resolve()


def _tool_config_session_id(payload: dict[str, Any] | None = None) -> str:
    payload = payload or {}
    seed = str(payload.get("session_id") or payload.get("tool_name") or payload.get("operation") or payload.get("description") or "default")
    return _slug(seed, fallback="default")


def _load_tool_authoring_config_store_from_disk() -> None:
    global _TOOL_AUTHORING_CONFIG_LOADED
    if _TOOL_AUTHORING_CONFIG_LOADED:
        return
    _TOOL_AUTHORING_CONFIG_LOADED = True
    path = _tool_authoring_config_store_path()
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    sessions = payload.get("sessions") if isinstance(payload, dict) else {}
    if isinstance(sessions, dict):
        for key, value in sessions.items():
            if isinstance(value, dict):
                _TOOL_AUTHORING_CONFIG_STORE[str(key)] = value


def _persist_tool_authoring_config_store_to_disk() -> None:
    path = _tool_authoring_config_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_sessions: dict[str, Any] = {}
    for session_id, record in _TOOL_AUTHORING_CONFIG_STORE.items():
        safe = dict(record)
        safe.pop("env_values", None)  # Do not persist secret values to disk.
        safe["config"] = _redact_secrets(safe.get("config") or {}, set(os.environ.values()))
        safe_sessions[session_id] = safe
    payload = {"version": 1, "updated_at": _utc_now(), "sessions": safe_sessions}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def save_tool_authoring_config(payload: dict[str, Any]) -> dict[str, Any]:
    _load_tool_authoring_config_store_from_disk()
    data = dict(payload or {})
    manifest = _sync_auth_into_manifest(data.get("manifest") if isinstance(data.get("manifest"), dict) else {}, data)
    session_id = _tool_config_session_id(data)
    config = data.get("config") if isinstance(data.get("config"), dict) else {}
    sample_input = data.get("sample_input") if isinstance(data.get("sample_input"), dict) else {}
    env_values = _extract_env_values_from_payload(data, manifest)
    _apply_env_values(env_values)
    auth_gate = _auth_gate_for_manifest(manifest)
    configured_env = sorted(set([*auth_gate.get("configured_env", []), *env_values.keys()]))
    record = {
        "tool_name": str(data.get("tool_name") or data.get("operation") or manifest.get("tool_name") or session_id),
        "config": config,
        "sample_input": sample_input,
        "env_values": env_values,
        "configured_env": configured_env,
        "configured_secrets": configured_env,
        "config_refs": {name: f"env:{name}" for name in configured_env},
        "auth_override": data.get("auth_override") if isinstance(data.get("auth_override"), dict) else {},
        "updated_at": _utc_now(),
    }
    _TOOL_AUTHORING_CONFIG_STORE[session_id] = record
    _persist_tool_authoring_config_store_to_disk()
    auth_gate = _auth_gate_for_manifest(manifest)
    return {"success": True, "session_id": session_id, "configured": bool(auth_gate.get("status") in {"configured", "not_required"}), "config": _redact_secrets(config, set(env_values.values())), "sample_input": sample_input, "configured_env": configured_env, "configured_secrets": configured_env, "missing_env": auth_gate.get("missing_env") or [], "missing_secrets": auth_gate.get("missing_secrets") or [], "config_refs": record["config_refs"], "auth_override": record["auth_override"], "auth_gate": auth_gate, "platform_env_prefix": "", "persisted": True, "store_path": str(_tool_authoring_config_store_path())}


def tool_authoring_config_status(session_id: str = "default", manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    _load_tool_authoring_config_store_from_disk()
    key = _tool_config_session_id({"session_id": session_id})
    stored = _TOOL_AUTHORING_CONFIG_STORE.get(key)
    if stored and isinstance(stored.get("env_values"), dict):
        _apply_env_values(stored.get("env_values") or {})
    normalized_manifest = _sync_auth_into_manifest(manifest or {}, {"session_id": session_id}) if manifest else {}
    auth_gate = _auth_gate_for_manifest(normalized_manifest) if normalized_manifest else {"required": False, "status": "not_required", "required_env": [], "required_secrets": [], "configured_env": [], "configured_secrets": [], "missing_env": [], "missing_secrets": [], "can_register": True, "block_registration": False}
    if not stored:
        return {"success": True, "configured": bool(auth_gate.get("status") in {"configured", "not_required"}), "session_id": key, "config": {}, "sample_input": {}, "configured_env": auth_gate.get("configured_env") or [], "configured_secrets": auth_gate.get("configured_secrets") or [], "missing_env": auth_gate.get("missing_env") or [], "missing_secrets": auth_gate.get("missing_secrets") or [], "config_refs": {}, "auth_override": {}, "auth_gate": auth_gate, "platform_env_prefix": "", "persisted": False, "store_path": str(_tool_authoring_config_store_path())}
    configured_env = sorted(set(stored.get("configured_env") or []))
    return {"success": True, "configured": bool(auth_gate.get("status") in {"configured", "not_required"} or configured_env), "session_id": key, "config": _redact_secrets(stored.get("config") or {}, set((stored.get("env_values") or {}).values()) if isinstance(stored.get("env_values"), dict) else set()), "sample_input": stored.get("sample_input") or {}, "configured_env": configured_env or auth_gate.get("configured_env") or [], "configured_secrets": configured_env or auth_gate.get("configured_secrets") or [], "missing_env": auth_gate.get("missing_env") or [], "missing_secrets": auth_gate.get("missing_secrets") or [], "config_refs": stored.get("config_refs") or {}, "auth_override": stored.get("auth_override") or {}, "auth_gate": auth_gate, "platform_env_prefix": "", "updated_at": stored.get("updated_at"), "persisted": True, "store_path": str(_tool_authoring_config_store_path())}


_load_registered_tools_from_disk()
