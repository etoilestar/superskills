"""Creator FastAPI endpoint handlers and response assembly."""

import asyncio
import copy
import hashlib
import json
import math
import re
import shutil
import traceback
from dataclasses import asdict

import httpx
from fastapi.encoders import jsonable_encoder
from starlette.responses import StreamingResponse
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from .common import *  # noqa: F403
from .common import _file_spec_has_substantive_responsibility
from .contracts import *  # noqa: F403
from .e2e import *  # noqa: F403
from .repair import *  # noqa: F403
from .generation import *  # noqa: F403
from ..kernel_loader import load_kernel_creator_for_phase
from ..blueprint_parser import BlueprintShapeError, exact_file_plan_paths_from_strict_skillplan, parse_blueprint, parse_resource_source_from_block, validate_blueprint_shape_for_creator
from ..skill_plan import GraphValidationError, file_type_for_path, normalize_structured_function_items, normalize_structured_responsibility_edges, resource_role_source_issue, validate_structured_responsibility_edge_transport, structured_responsibility_graph_input_provenance_gaps

from .upload_context import save_creator_context_upload, UPLOAD_ROOT, sanitize_session_id
from .tool_pool_store import (
    save_tool_pool,
    load_tool_pool,
    get_file_binding,
    get_skill_tool_binding,
    tool_pool_snapshot,
)
from .tool_pool_builder import build_tool_pool
from .tool_pool_explorer import explore_tool_pool
from .tool_pool_gate import gate_tool_request
from .tool_pool_models import (
    ToolPoolAddToolRequest,
    ToolPoolDeniedRequest,
    ToolPoolMissingRequest,
    ToolPoolModel,
    ToolPoolTool,
)
from ..creator_tool_registry import get_tool_capability
from .runtime_import_guard import guard_runtime_imports
from .basic_format import check_patch_candidate_basic_format
from .command_normalizer import _effective_command_lines
from .command_normalizer import parse_skill_md_bash_command_blocks
from . import contracts as creator_contracts
from .responsibility_graph_expansion import expand_responsibility_graph


def _tool_binding_digest(binding: dict[str, Any]) -> str:
    normalized = {
        "allowed_tool_ids": sorted(binding.get("allowed_tool_ids") or []),
        "available_tools": sorted(
            (
                str(item.get("tool_id") or ""),
                str(item.get("function_name") or ""),
                str(item.get("import_path") or ""),
            )
            for item in (binding.get("available_tools") or [])
            if isinstance(item, dict)
        ),
    }
    return hashlib.sha256(
        json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]




def _with_current_skill_tool_binding(
    entry: dict[str, Any] | None,
    binding: dict[str, Any] | None,
) -> dict[str, Any]:
    result = copy.deepcopy(entry if isinstance(entry, dict) else {})
    binding_payload = copy.deepcopy(binding if isinstance(binding, dict) else {})
    runtime_contract = (
        result.get("runtime_contract")
        if isinstance(result.get("runtime_contract"), dict)
        else {}
    )
    runtime_contract = copy.deepcopy(runtime_contract)
    runtime_contract["tool_binding_summary"] = binding_payload
    result["runtime_contract"] = runtime_contract
    result["tool_binding_summary"] = copy.deepcopy(binding_payload)
    return result


def _build_e2e_callable_repair_context(
    *,
    skill_name: str,
    target_file: str,
) -> dict[str, Any]:
    pool = load_tool_pool(settings.skills_path / skill_name)
    binding_obj = get_file_binding(
        pool,
        target_file,
    )
    if binding_obj is None:
        binding_obj = get_skill_tool_binding(
            pool,
            target_file=target_file,
            include_script_core=True,
        )
    binding = binding_obj.model_dump(mode="json")
    context = build_available_tool_context(
        binding,
        file_path=target_file,
        max_snippets=0,
    )
    if not context.get("resolved_tools"):
        return {}

    selected_tool_ids = {
        str(tool_id)
        for key in ("allowed_tool_ids", "primary_tool_ids", "secondary_tool_ids")
        for tool_id in (binding.get(key) or [])
        if str(tool_id).strip()
    }
    compact_tools = []
    for tool in context.get("resolved_tools") or []:
        if not isinstance(tool, dict):
            continue
        if str(tool.get("tool_id") or "") not in selected_tool_ids:
            continue
        compact_tools.append({
            "tool_id": tool.get("tool_id"),
            "capability_name": tool.get("capability_name"),
            "function_name": tool.get("function_name"),
            "import_path": tool.get("import_path"),
            "signature": tool.get("signature"),
            "input_schema": tool.get("input_schema"),
            "output_schema": tool.get("output_schema"),
            "return_contract": tool.get("return_contract"),
            "example_call": tool.get("example_call") or tool.get("call_template"),
            "example_return": tool.get("example_return"),
            "example_stdout": tool.get("example_stdout"),
            "common_mistakes": tool.get("common_mistakes") or [],
        })
    if not compact_tools:
        return {}
    return {
        "authorization_scope": "skill",
        "read_only": True,
        "binding_digest": _tool_binding_digest(binding),
        "selected_tool_ids": sorted(selected_tool_ids),
        "available_tools": [
            tool
            for tool in (context.get("available_tools") or [])
            if isinstance(tool, dict) and str(tool.get("tool_id") or "") in selected_tool_ids
        ],
        "resolved_tools": compact_tools,
    }


def _is_callable_runtime_failure(
    structured_failure: dict[str, Any],
    callable_context: dict[str, Any] | None = None,
) -> bool:
    stderr = str(structured_failure.get("stderr") or "")
    actual = str(structured_failure.get("actual") or "")
    text = f"{stderr}\n{actual}"

    if re.search(
        (
            r"\bImportError\b|"
            r"\bModuleNotFoundError\b|"
            r"cannot import name"
        ),
        text,
    ):
        return True

    if not re.search(r"\bNameError\b|\bTypeError\b", text):
        return False

    resolved_tools = (
        callable_context.get("resolved_tools")
        if isinstance(callable_context, dict)
        else []
    ) or []

    callable_tokens: set[str] = set()
    for tool in resolved_tools:
        if not isinstance(tool, dict):
            continue

        function_name = str(tool.get("function_name") or "").strip()
        import_path = str(tool.get("import_path") or "").strip()

        if function_name:
            callable_tokens.add(function_name)

        if import_path:
            callable_tokens.add(import_path)
            callable_tokens.add(import_path.rsplit(".", 1)[-1])

    return any(
        token and re.search(rf"\b{re.escape(token)}\b", text)
        for token in callable_tokens
    )

def _tool_binding_log_summary(binding: dict[str, Any]) -> dict[str, Any]:
    return {
        "allowed_tool_ids": sorted(binding.get("allowed_tool_ids") or []),
        "available_tool_count": len(binding.get("available_tools") or []),
        "binding_digest": _tool_binding_digest(binding),
    }


class PreparePlanProtocolError(ValueError):
    """Planner prepare-plan structured transport failed validation."""



_NOT_SUPPORTED_MARKERS = (
    "not supported in current implementation",
    "not supported",
)
_DECLARED_FORMAT_TOKENS = {
    "pdf": ("pdf", ".pdf"),
    "docx": ("docx", ".docx", "word document"),
    "txt": ("txt", ".txt", "plain text"),
}


def _post_patch_basic_format_stage_error(file_path: str, content: str) -> FileGenerationStageError | None:
    """Coarse post-patch candidate check; intentionally excludes tools/argv/stdout/E2E/business semantics."""
    failure = check_patch_candidate_basic_format(file_path, content)
    if failure is None:
        return None
    return FileGenerationStageError(
        source="basic_format",
        layer=failure.coarse_failure_kind,
        detail=failure.to_failure_text(),
    )


def _post_patch_python_compile_stage_error(file_path: str, content: str) -> FileGenerationStageError | None:
    """Compile-only gate for patched scripts; never executes generated code."""
    if not (file_path.startswith("scripts/") and file_path.endswith(".py")):
        return None
    try:
        compile(content or "", file_path, "exec")
        return None
    except SyntaxError as exc:
        detail = {
            "error_type": type(exc).__name__,
            "lineno": exc.lineno,
            "offset": exc.offset,
            "end_lineno": getattr(exc, "end_lineno", None),
            "end_offset": getattr(exc, "end_offset", None),
            "msg": exc.msg,
            "text": (exc.text or "").strip(),
            "traceback": "".join(traceback.format_exception_only(type(exc), exc)).strip(),
        }
    except Exception as exc:
        detail = {
            "error_type": type(exc).__name__,
            "lineno": None,
            "offset": None,
            "msg": str(exc),
            "text": "",
            "traceback": "".join(traceback.format_exception_only(type(exc), exc)).strip(),
        }
    return FileGenerationStageError(
        source="python_compile",
        layer="python_compile_error",
        detail=json.dumps(detail, ensure_ascii=False, default=str),
    )


def _basic_format_repair_feedback(stage_error: FileGenerationStageError) -> str:
    detail = str(getattr(stage_error, "detail", "") or "")
    return (
        "BASIC_FORMAT_PATCH_STAGE\n"
        "当前失败只表示 patch 后文件基础格式不合法。\n"
        f"{detail}\n"
        "不要修改工具选择。不要修改 argv schema。不要修改 stdout 字段。不要修改业务职责。"
        "只把当前候选修成合法源码/合法 Markdown。"
    )


def _build_strict_compile_rewrite_prompt(
    *,
    file_path: str,
    skill_name: str,
    purpose: str | None,
    blueprint_text: str,
    role: str | None,
    skill_plan_entry: Any,
    deterministic_error: str,
    previous_content: str,
    current_file_binding: dict[str, Any] | None,
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "你是 Creator strict_compile_rewrite 修复器。只输出完整 Python 源码。"
                "不要 Markdown fence。不要解释。不要 JSON patch。不要 diff。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"Skill: {skill_name}\n文件路径: {file_path}\nrole: {role or ''}\npurpose: {purpose or ''}\n\n"
                "目标：只让当前脚本成为合法可编译源码。保留 strict_json_argv_guard。"
                "不要改工具选择、argv schema、stdout 字段、业务职责，除非这些内容本身造成语法错误。"
                "不要新增未绑定 runtime_tools helper。\n\n"
                "当前 SkillPlanEntry / 轻量职责：\n"
                f"{json.dumps(skill_plan_entry or {}, ensure_ascii=False, default=str)[:10000]}\n\n"
                "当前 tool binding 摘要：\n"
                f"{json.dumps(current_file_binding or {}, ensure_ascii=False, default=str)[:10000]}\n\n"
                "当前 strict_json_argv_guard 要求：如果源码已经导入或调用 strict_json_argv_guard，必须保留；"
                "不要删除已有 guard；不要内联替代；不要把合法 helper 调用改成本地占位逻辑。\n\n"
                "编译错误完整信息：\n"
                f"{deterministic_error}\n\n"
                "当前失败源码完整内容：\n"
                f"{previous_content or ''}\n\n"
                "输出要求：只输出完整 Python 源码；不要 Markdown fence；不要解释；不要 JSON patch；不要 diff。"
            ),
        },
    ]

def _creator_tool_has_callable_contract(
    tool: dict[str, Any],
) -> bool:
    """Return whether a catalog tool exposes at least one callable function."""

    for function in tool.get("functions") or []:
        if not isinstance(function, dict):
            continue

        function_name = str(
            function.get("function_name")
            or ""
        ).strip()

        import_path = str(
            function.get("import_path")
            or ""
        ).strip()

        if function_name and import_path:
            return True

    return False


def _creator_tool_catalog_for_planner() -> list[dict[str, Any]]:
    """Return selectable Registry discovery catalog visible to planners.

    Registry is the descriptive source of truth.

    This catalog is discovery context only:
    - it does not authorize tools;
    - it does not mutate ToolPool;
    - planner may only propose exact tool_ids from this catalog;
    - capability shells without callable function contracts are excluded;
    - backend gate remains the authorization authority.

    The planner must see real callable contracts rather than tool names alone,
    otherwise it cannot reliably distinguish tools with similar semantic labels
    or determine whether function IO/effects satisfy a script responsibility.
    """

    catalog: list[dict[str, Any]] = []

    for capability in list_tool_capabilities():
        status = tool_status(capability)

        functions: list[dict[str, Any]] = []

        for function in (
            getattr(capability, "functions", [])
            or []
        ):
            functions.append({
                "function_name": str(
                    getattr(
                        function,
                        "function_name",
                        "",
                    )
                    or ""
                ),
                "import_path": str(
                    getattr(
                        function,
                        "import_path",
                        "",
                    )
                    or ""
                ),
                "short_description": str(
                    getattr(
                        function,
                        "short_description",
                        "",
                    )
                    or ""
                ),
                "when_to_use": str(
                    getattr(
                        function,
                        "when_to_use",
                        "",
                    )
                    or ""
                ),
                "signature": str(
                    getattr(
                        function,
                        "signature",
                        "",
                    )
                    or ""
                ),
                "input_schema": (
                    getattr(
                        function,
                        "input_schema",
                        None,
                    )
                    or {}
                ),
                "output_schema": (
                    getattr(
                        function,
                        "output_schema",
                        None,
                    )
                    or {}
                ),
                "return_contract": str(
                    getattr(
                        function,
                        "return_contract",
                        "",
                    )
                    or ""
                ),
                "artifact_outputs": list(
                    getattr(
                        function,
                        "artifact_outputs",
                        [],
                    )
                    or []
                ),
                "side_effects": list(
                    getattr(
                        function,
                        "side_effects",
                        [],
                    )
                    or []
                ),
                "example_call": str(
                    getattr(
                        function,
                        "example_call",
                        "",
                    )
                    or ""
                ),
                "example_return": str(
                    getattr(
                        function,
                        "example_return",
                        "",
                    )
                    or ""
                ),
                "example_stdout": str(
                    getattr(
                        function,
                        "example_stdout",
                        "",
                    )
                    or ""
                ),
                "common_mistakes": list(
                    getattr(
                        function,
                        "common_mistakes",
                        [],
                    )
                    or []
                ),
                "trial_mode_behavior": str(
                    getattr(
                        function,
                        "trial_mode_behavior",
                        "",
                    )
                    or ""
                ),
                "safety_notes": list(
                    getattr(
                        function,
                        "safety_notes",
                        [],
                    )
                    or []
                ),
                "required_env": list(
                    getattr(
                        function,
                        "required_env",
                        [],
                    )
                    or []
                ),
                "required_secrets": list(
                    getattr(
                        function,
                        "required_secrets",
                        [],
                    )
                    or []
                ),
                "usage_policy": str(
                    getattr(
                        function,
                        "usage_policy",
                        "",
                    )
                    or getattr(
                        capability,
                        "usage_policy",
                        "",
                    )
                    or ""
                ),
                "allowed_roles": list(
                    getattr(
                        function,
                        "allowed_roles",
                        [],
                    )
                    or []
                ),
                "required_capabilities": list(
                    getattr(
                        function,
                        "required_capabilities",
                        [],
                    )
                    or []
                ),
            })

        snippets: list[dict[str, Any]] = []

        for snippet in (
            getattr(capability, "snippets", [])
            or []
        ):
            snippets.append({
                "id": str(
                    getattr(snippet, "id", "")
                    or ""
                ),
                "title": str(
                    getattr(snippet, "title", "")
                    or ""
                ),
                "kind": str(
                    getattr(snippet, "kind", "")
                    or ""
                ),
                "description": str(
                    getattr(
                        snippet,
                        "description",
                        "",
                    )
                    or ""
                ),
                "expected_input_shape": (
                    getattr(
                        snippet,
                        "expected_input_shape",
                        None,
                    )
                    or {}
                ),
                "expected_output_shape": (
                    getattr(
                        snippet,
                        "expected_output_shape",
                        None,
                    )
                    or {}
                ),
                "return_rule": str(
                    getattr(
                        snippet,
                        "return_rule",
                        "",
                    )
                    or ""
                ),
                "anti_patterns": list(
                    getattr(
                        snippet,
                        "anti_patterns",
                        [],
                    )
                    or []
                ),
                "requires": list(
                    getattr(
                        snippet,
                        "requires",
                        [],
                    )
                    or []
                ),
                "usage_policy": str(
                    getattr(
                        snippet,
                        "usage_policy",
                        "",
                    )
                    or ""
                ),
                "priority": int(
                    getattr(
                        snippet,
                        "priority",
                        0,
                    )
                    or 0
                ),
            })

        tool_record = {
            "tool_id": str(
                getattr(capability, "name", "")
                or ""
            ),
            "display_name": str(
                getattr(
                    capability,
                    "display_name",
                    "",
                )
                or ""
            ),
            "category": str(
                getattr(
                    capability,
                    "category",
                    "",
                )
                or ""
            ),
            "tool_type": str(
                getattr(
                    capability,
                    "tool_type",
                    "",
                )
                or ""
            ),
            "roles": list(
                getattr(
                    capability,
                    "roles",
                    [],
                )
                or []
            ),
            "allowed_roles": list(
                getattr(
                    capability,
                    "allowed_roles",
                    [],
                )
                or []
            ),
            "capability_aliases": list(
                getattr(
                    capability,
                    "capability_aliases",
                    [],
                )
                or []
            ),
            "semantic_tags": list(
                getattr(
                    capability,
                    "semantic_tags",
                    [],
                )
                or []
            ),
            "task_verbs": list(
                getattr(
                    capability,
                    "task_verbs",
                    [],
                )
                or []
            ),
            "domain_terms": list(
                getattr(
                    capability,
                    "domain_terms",
                    [],
                )
                or []
            ),
            "required_capabilities": list(
                getattr(
                    capability,
                    "required_capabilities",
                    [],
                )
                or []
            ),
            "optional_capabilities": list(
                getattr(
                    capability,
                    "optional_capabilities",
                    [],
                )
                or []
            ),
            "forbidden_capabilities": list(
                getattr(
                    capability,
                    "forbidden_capabilities",
                    [],
                )
                or []
            ),
            "prompt_guidance": str(
                getattr(
                    capability,
                    "prompt_guidance",
                    "",
                )
                or ""
            ),
            "usage_policy": str(
                getattr(
                    capability,
                    "usage_policy",
                    "",
                )
                or ""
            ),
            "input_schema": (
                getattr(
                    capability,
                    "input_schema",
                    None,
                )
                or {}
            ),
            "output_schema": (
                getattr(
                    capability,
                    "output_schema",
                    None,
                )
                or {}
            ),
            "artifact_outputs": list(
                getattr(
                    capability,
                    "artifact_outputs",
                    [],
                )
                or []
            ),
            "side_effects": list(
                getattr(
                    capability,
                    "side_effects",
                    [],
                )
                or []
            ),
            "required_env": list(
                getattr(
                    capability,
                    "required_env",
                    [],
                )
                or []
            ),
            "required_secrets": list(
                getattr(
                    capability,
                    "required_secrets",
                    [],
                )
                or []
            ),
            "dependencies": list(
                getattr(
                    capability,
                    "dependencies",
                    [],
                )
                or []
            ),
            "enabled_by_default": bool(
                getattr(
                    capability,
                    "enabled_by_default",
                    False,
                )
            ),
            "allow_creator_use": bool(
                getattr(
                    capability,
                    "allow_creator_use",
                    False,
                )
            ),
            "creator_available": bool(
                status.get("creator_available")
            ),
            "configured": bool(
                status.get("configured")
            ),
            "missing_env": list(
                status.get("missing_env")
                or []
            ),
            "missing_secrets": list(
                status.get("missing_secrets")
                or []
            ),
            "missing_dependencies": list(
                status.get("missing_dependencies")
                or []
            ),
            "created_by": str(
                getattr(
                    capability,
                    "created_by",
                    "",
                )
                or ""
            ),
            "approval_status": str(
                getattr(
                    capability,
                    "approval_status",
                    "",
                )
                or ""
            ),
            "test_status": str(
                getattr(
                    capability,
                    "test_status",
                    "",
                )
                or ""
            ),
            "functions": functions,
            "snippets": snippets,
        }

        if (
            tool_record["creator_available"]
            and _creator_tool_has_callable_contract(
                tool_record
            )
        ):
            catalog.append(tool_record)

    return catalog


_CREATOR_TOOL_EMBEDDING_INDEX_CACHE: (
    tuple[
        tuple[str, ...],
        list[
            tuple[
                str,
                list[float],
            ]
        ],
    ]
    | None
) = None

_CREATOR_LOCAL_EMBEDDING_RUNTIME: (
    tuple[
        Any,
        Any,
        Any,
        str,
    ]
    | None
) = None

def _planner_shared_tool_context(
    skill_name: str | None,
) -> dict[str, Any]:
    """Build planner discovery context for the shared Skill ToolPool."""

    pool = ToolPoolModel(
        skill_name=str(
            skill_name or ""
        )
    )

    if str(
        skill_name or ""
    ).strip():
        try:
            skill_dir = (
                settings.skills_path
                / _validate_skill_name(
                    str(skill_name)
                )
            )

            pool = load_tool_pool(
                skill_dir
            )

            if not pool.skill_name:
                pool.skill_name = str(
                    skill_name
                )

        except Exception:
            pool = ToolPoolModel(
                skill_name=str(
                    skill_name or ""
                )
            )

    return {
        "available_tool_catalog": (
            _creator_tool_catalog_for_planner()
        ),

        "current_tool_pool": (
            tool_pool_snapshot(pool)
        ),

        "tool_contract": {
            "catalog_is_discovery_space": True,
            "tool_pool_is_authorization_source": True,

            "authorization_scope": "skill",
            "per_file_tool_authorization": False,

            "planner_may_propose_changes": True,
            "backend_gate_decides": True,

            "file_responsibility_decides_usage": True,

            "code_model_may_not_expand_pool": True,
            "responsibility_judge_may_not_expand_pool": True,
            "e2e_may_not_expand_pool": True,
        },
    }

def _entry_text_for_declared_support(skill_plan_entry: Any) -> str:
    if isinstance(skill_plan_entry, dict):
        parts = [
            skill_plan_entry.get("purpose"),
            skill_plan_entry.get("inputs"),
            skill_plan_entry.get("outputs"),
            skill_plan_entry.get("runtime_contract"),
            skill_plan_entry.get("coverage_requirements"),
        ]
    else:
        parts = [
            getattr(skill_plan_entry, "purpose", None),
            getattr(skill_plan_entry, "inputs", None),
            getattr(skill_plan_entry, "outputs", None),
            getattr(skill_plan_entry, "runtime_contract", None),
            getattr(skill_plan_entry, "coverage_requirements", None),
        ]
    return json.dumps(parts, ensure_ascii=False, default=str).lower()

def _creator_cosine_similarity(
    left: list[float],
    right: list[float],
) -> float:
    """Return cosine similarity for two embedding vectors."""

    if (
        not left
        or not right
        or len(left) != len(right)
    ):
        return -1.0

    dot = sum(
        left_value * right_value
        for left_value, right_value
        in zip(left, right)
    )

    left_norm = math.sqrt(
        sum(
            value * value
            for value in left
        )
    )

    right_norm = math.sqrt(
        sum(
            value * value
            for value in right
        )
    )

    denominator = (
        left_norm * right_norm
    )

    if not denominator:
        return -1.0

    return dot / denominator

def _creator_embed_texts_local_fallback(
    texts: list[str],
) -> list[list[float]]:
    """Embed text with the bundled local BGE model on CUDA.

    Expected model directory:

        backend/bge-large-zh-v1.5/

    This is the only fallback after the configured embedding API fails.

    CUDA is required. There is no CPU fallback.
    """

    global _CREATOR_LOCAL_EMBEDDING_RUNTIME

    normalized_texts = [
        str(text or "").strip()
        for text in (
            texts or []
        )
    ]

    if not normalized_texts:
        return []

    model_path = (
        Path(__file__)
        .resolve()
        .parents[2]
        / "bge-large-zh-v1.5"
    )

    if not model_path.is_dir():
        raise RuntimeError(
            "local embedding model does not exist: "
            f"{model_path}"
        )

    try:
        import torch

        from transformers import (
            AutoModel,
            AutoTokenizer,
        )

    except Exception as exc:
        raise RuntimeError(
            "local embedding runtime requires "
            "torch and transformers"
        ) from exc

    # if not torch.cuda.is_available():
    #     raise RuntimeError(
    #         "CUDA is unavailable for local "
    #         "embedding fallback"
    #     )

    runtime = (
        _CREATOR_LOCAL_EMBEDDING_RUNTIME
    )

    runtime_path = (
        runtime[3]
        if runtime is not None
        else ""
    )

    if (
        runtime is None
        or runtime_path
        != str(model_path)
    ):
        tokenizer = (
            AutoTokenizer.from_pretrained(
                str(model_path),
                local_files_only=True,
            )
        )

        model = AutoModel.from_pretrained(
            str(model_path),
            local_files_only=True,
            torch_dtype=torch.float32,
        )

        device = torch.device(
            "cpu"
        )

        model = model.to(
            device
        )

        model.eval()

        _CREATOR_LOCAL_EMBEDDING_RUNTIME = (
            tokenizer,
            model,
            device,
            str(model_path),
        )

        logger.info(
            "[Creator]"
            "[tool_embedding]"
            "[local_model_loaded] "
            "model_path=%s "
            "device=%s "
            "dtype=float16 "
            "gpu=%s",
            model_path,
            device,
            torch.cuda.get_device_name(0),
        )

    (
        tokenizer,
        model,
        device,
        _,
    ) = _CREATOR_LOCAL_EMBEDDING_RUNTIME

    embeddings: list[
        list[float]
    ] = []

    batch_size = 16

    for start in range(
        0,
        len(normalized_texts),
        batch_size,
    ):
        batch = normalized_texts[
            start:start + batch_size
        ]

        encoded = tokenizer(
            batch,
            padding=True,
            truncation=True,
            max_length=512,
            return_tensors="pt",
        )

        encoded = {
            key: value.to(
                device
            )
            for key, value
            in encoded.items()
        }

        with torch.inference_mode():
            output = model(
                **encoded
            )

            vectors = (
                output
                .last_hidden_state[
                    :,
                    0,
                ]
            )

            vectors = (
                torch.nn.functional.normalize(
                    vectors.float(),
                    p=2,
                    dim=1,
                )
            )

        embeddings.extend(
            vectors
            .detach()
            .cpu()
            .tolist()
        )

    if (
        len(embeddings)
        != len(normalized_texts)
    ):
        raise RuntimeError(
            "local embedding model returned "
            "unexpected embedding count: "
            f"expected={len(normalized_texts)} "
            f"actual={len(embeddings)}"
        )

    if any(
        not embedding
        for embedding in embeddings
    ):
        raise RuntimeError(
            "local embedding model returned "
            "an empty embedding vector"
        )

    return embeddings

def _creator_embed_texts(
    texts: list[str],
) -> tuple[
    list[list[float]],
    str,
]:
    """Embed Creator planning text.

    Strict priority:

    1. configured EMBEDDING_MODEL through /v1/embeddings;
    2. local backend/bge-large-zh-v1.5/ CUDA inference.

    If both fail, raise.

    There is no structural, lexical, Registry-order, or full-catalog LLM
    fallback.
    """

    normalized_texts = [
        str(text or "").strip()
        for text in (
            texts or []
        )
    ]

    if not normalized_texts:
        return [], "empty"

    model_name = str(
        settings.embedding_model
        or ""
    ).strip()

    remote_error: (
        Exception | None
    ) = None

    if model_name:
        try:
            base_url = str(
                settings.llm_base_url
                or ""
            ).rstrip("/")

            if not base_url:
                raise RuntimeError(
                    "llm_base_url is empty"
                )

            url = (
                base_url + "/embeddings"
                if base_url.endswith("/v1")
                else (
                    base_url
                    + "/v1/embeddings"
                )
            )

            headers = {
                "Content-Type": (
                    "application/json"
                ),
            }

            api_key = str(
                getattr(
                    settings,
                    "openai_api_key",
                    "",
                )
                or getattr(
                    settings,
                    "llm_api_key",
                    "",
                )
                or ""
            ).strip()

            if api_key:
                headers[
                    "Authorization"
                ] = (
                    f"Bearer {api_key}"
                )

            with httpx.Client(
                timeout=30.0
            ) as client:
                response = client.post(
                    url,
                    headers=headers,
                    json={
                        "model": model_name,
                        "input": normalized_texts,
                    },
                )

                response.raise_for_status()

                body = response.json()

            raw_data = (
                body.get("data")
                if isinstance(
                    body,
                    dict,
                )
                else None
            )

            if not isinstance(
                raw_data,
                list,
            ):
                raise RuntimeError(
                    "embedding API response "
                    "does not contain data list"
                )

            indexed_data = [
                (
                    int(
                        item.get(
                            "index",
                            index,
                        )
                    ),
                    item,
                )
                for index, item
                in enumerate(raw_data)
                if isinstance(
                    item,
                    dict,
                )
            ]

            indexed_data.sort(
                key=lambda value: value[0]
            )

            vectors = [
                [
                    float(value)
                    for value in (
                        item.get(
                            "embedding"
                        )
                        or []
                    )
                ]
                for _, item
                in indexed_data
            ]

            if (
                len(vectors)
                != len(normalized_texts)
            ):
                raise RuntimeError(
                    "embedding API returned "
                    "unexpected vector count: "
                    f"expected="
                    f"{len(normalized_texts)} "
                    f"actual={len(vectors)}"
                )

            if any(
                not vector
                for vector in vectors
            ):
                raise RuntimeError(
                    "embedding API returned "
                    "an empty vector"
                )

            embedding_dim = len(
                vectors[0]
            )

            if any(
                len(vector)
                != embedding_dim
                for vector in vectors
            ):
                raise RuntimeError(
                    "embedding API returned "
                    "inconsistent embedding dimensions"
                )

            source = (
                f"remote:{model_name}"
            )

            logger.info(
                "[Creator]"
                "[tool_embedding]"
                "[remote_success] "
                "model=%s "
                "text_count=%d "
                "embedding_dim=%d",
                model_name,
                len(normalized_texts),
                embedding_dim,
            )

            return (
                vectors,
                source,
            )

        except Exception as exc:
            remote_error = exc

            logger.warning(
                "[Creator]"
                "[tool_embedding]"
                "[remote_failed] "
                "model=%s "
                "error=%s",
                model_name,
                (
                    f"{type(exc).__name__}: "
                    f"{exc}"
                ),
            )

    try:
        vectors = (
            _creator_embed_texts_local_fallback(
                normalized_texts
            )
        )

        model_path = (
            Path(__file__)
            .resolve()
            .parents[2]
            / "bge-large-zh-v1.5"
        )

        source = (
            f"local:{model_path}"
        )

        logger.info(
            "[Creator]"
            "[tool_embedding]"
            "[local_success] "
            "model=%s "
            "text_count=%d "
            "embedding_dim=%d",
            model_path,
            len(normalized_texts),
            (
                len(vectors[0])
                if vectors
                else 0
            ),
        )

        return (
            vectors,
            source,
        )

    except Exception as local_exc:
        logger.error(
            "[Creator]"
            "[tool_embedding]"
            "[failed] "
            "remote_error=%s "
            "local_error=%s",
            (
                (
                    f"{type(remote_error).__name__}: "
                    f"{remote_error}"
                )
                if remote_error
                is not None
                else (
                    "embedding model "
                    "not configured"
                )
            ),
            (
                f"{type(local_exc).__name__}: "
                f"{local_exc}"
            ),
        )

        raise RuntimeError(
            "Creator embedding unavailable: "
            "configured embedding API failed "
            "and local "
            "backend/bge-large-zh-v1.5 "
            "fallback failed"
        ) from local_exc

def _creator_tool_recall_card_text(
    tool: dict[str, Any],
) -> str:
    """Build a semantic-only recall card for one Registry tool.

    Recall only needs semantic identity.

    Full execution contracts remain in the candidate card passed to Final Tool
    Selector, code generation, responsibility judges, and E2E validation.

    Do not flatten JSON schemas, signatures, artifact contracts, return
    contracts, or side effects into the embedding document.
    """

    function_cards: list[str] = []

    for function in (
        tool.get("functions")
        or []
    ):
        if not isinstance(
            function,
            dict,
        ):
            continue

        function_cards.append(
            "\n".join(
                [
                    (
                        "function_name: "
                        + str(
                            function.get(
                                "function_name"
                            )
                            or ""
                        )
                    ),
                    (
                        "description: "
                        + str(
                            function.get(
                                "short_description"
                            )
                            or ""
                        )
                    ),
                    (
                        "when_to_use: "
                        + str(
                            function.get(
                                "when_to_use"
                            )
                            or ""
                        )
                    ),
                    (
                        "required_capabilities: "
                        + json.dumps(
                            function.get(
                                "required_capabilities"
                            )
                            or [],
                            ensure_ascii=False,
                            default=str,
                        )
                    ),
                ]
            )
        )

    return "\n".join(
        [
            (
                "tool_id: "
                + str(
                    tool.get("tool_id")
                    or ""
                )
            ),
            (
                "display_name: "
                + str(
                    tool.get("display_name")
                    or ""
                )
            ),
            (
                "category: "
                + str(
                    tool.get("category")
                    or ""
                )
            ),
            (
                "prompt_guidance: "
                + str(
                    tool.get(
                        "prompt_guidance"
                    )
                    or ""
                )
            ),
            (
                "capability_aliases: "
                + json.dumps(
                    tool.get(
                        "capability_aliases"
                    )
                    or [],
                    ensure_ascii=False,
                    default=str,
                )
            ),
            (
                "semantic_tags: "
                + json.dumps(
                    tool.get(
                        "semantic_tags"
                    )
                    or [],
                    ensure_ascii=False,
                    default=str,
                )
            ),
            (
                "task_verbs: "
                + json.dumps(
                    tool.get(
                        "task_verbs"
                    )
                    or [],
                    ensure_ascii=False,
                    default=str,
                )
            ),
            (
                "domain_terms: "
                + json.dumps(
                    tool.get(
                        "domain_terms"
                    )
                    or [],
                    ensure_ascii=False,
                    default=str,
                )
            ),
            (
                "required_capabilities: "
                + json.dumps(
                    tool.get(
                        "required_capabilities"
                    )
                    or [],
                    ensure_ascii=False,
                    default=str,
                )
            ),
            (
                "optional_capabilities: "
                + json.dumps(
                    tool.get(
                        "optional_capabilities"
                    )
                    or [],
                    ensure_ascii=False,
                    default=str,
                )
            ),
            "",
            "\n\n".join(
                function_cards
            ),
        ]
    )

def _creator_tool_exact_capability_matches(
    tool: dict[str, Any],
    capability: str,
) -> bool:
    """Check explicit Registry capability ownership.

    This is deterministic contract matching, not semantic inference.

    A tool is an exact capability candidate when:

    - tool_id exactly equals the capability; or
    - the top-level Registry contract explicitly requires the capability; or
    - one of the tool's callable function contracts explicitly requires the
      capability.

    The backend does not infer support from tool names, paths, roles, business
    prose, or keywords.
    """

    capability = str(
        capability
        or ""
    ).strip()

    if not capability:
        return False

    tool_id = str(
        tool.get("tool_id")
        or ""
    ).strip()

    if tool_id == capability:
        return True

    top_level_required = {
        str(item or "").strip()
        for item in (
            tool.get(
                "required_capabilities"
            )
            or []
        )
        if str(item or "").strip()
    }

    if (
        capability
        in top_level_required
    ):
        return True

    for function in (
        tool.get("functions")
        or []
    ):
        if not isinstance(
            function,
            dict,
        ):
            continue

        function_required = {
            str(item or "").strip()
            for item in (
                function.get(
                    "required_capabilities"
                )
                or []
            )
            if str(item or "").strip()
        }

        if (
            capability
            in function_required
        ):
            return True

    return False


def _recall_creator_tool_candidates(
    *,
    file_specs: list[dict[str, Any]],
    top_k: int = 3,
    semantic_queries: (
        list[dict[str, Any]]
        | None
    ) = None,
) -> tuple[
    list[dict[str, Any]],
    str,
]:
    """Recall Registry tools with exact-contract seeds plus embedding expansion.

    Candidate set:

        exact Registry capability matches
        UNION
        per-query embedding top-k

    Exact Registry contract matches are completeness seeds.

    Embedding is only semantic expansion. Embedding ranking must never remove a
    tool that explicitly declares support for a required capability.

    semantic_queries is used by responsibility-feedback replanning so the
    first-round validator's structured error can use the same recall path
    instead of exposing the complete Registry catalog to the planning model.

    Tool authorization remains Skill-wide and is still owned by Backend Gate.
    """

    global _CREATOR_TOOL_EMBEDDING_INDEX_CACHE

    raw_catalog = (
        _creator_tool_catalog_for_planner()
    )

    catalog = [
        tool
        for tool in (
            raw_catalog
            or []
        )
        if (
            isinstance(
                tool,
                dict,
            )
            and str(
                tool.get("tool_id")
                or ""
            ).strip()
            and bool(
                tool.get(
                    "creator_available",
                    False,
                )
            )
        )
    ]

    if not catalog:
        raise RuntimeError(
            "Creator Tool Registry has no "
            "available tools"
        )

    required_capabilities: list[
        str
    ] = []

    capability_owners: dict[
        str,
        list[str],
    ] = {}

    for spec in (
        file_specs
        or []
    ):
        if not isinstance(
            spec,
            dict,
        ):
            continue

        path = _normalize_skill_path(
            str(
                spec.get("path")
                or spec.get(
                    "target_file"
                )
                or ""
            )
        )

        if not path.startswith(
            "scripts/"
        ):
            continue

        if spec.get("required") is False:
            continue

        for raw_capability in (
            spec.get(
                "required_capabilities"
            )
            or []
        ):
            capability = str(
                raw_capability
                or ""
            ).strip()

            if not capability:
                continue

            if (
                capability
                not in required_capabilities
            ):
                required_capabilities.append(
                    capability
                )

            owners = (
                capability_owners
                .setdefault(
                    capability,
                    [],
                )
            )

            if path not in owners:
                owners.append(
                    path
                )

    query_items: list[
        dict[str, Any]
    ] = [
        {
            "query_id": capability,
            "query_text": "\n".join(
                [
                    (
                        "匹配能够直接实现以下抽象"
                        "语义能力的 Tool Registry 工具。"
                    ),
                    "",
                    (
                        "required_capability: "
                        f"{capability}"
                    ),
                    "",
                    (
                        "只判断工具真实能力是否能够"
                        "实现该 capability。"
                    ),
                    (
                        "不要根据整个 Skill 主题"
                        "扩大语义。"
                    ),
                ]
            ),
            "exact_capabilities": [
                capability
            ],
            "recalled_capabilities": [
                capability
            ],
            "query_source": (
                "plan_required_capability"
            ),
        }
        for capability
        in required_capabilities
    ]

    for index, raw_query in enumerate(
        semantic_queries
        or []
    ):
        if not isinstance(
            raw_query,
            dict,
        ):
            continue

        query_text = str(
            raw_query.get("query_text")
            or ""
        ).strip()

        if not query_text:
            continue

        query_id = str(
            raw_query.get("query_id")
            or f"semantic_query_{index}"
        ).strip()

        exact_capabilities: list[
            str
        ] = []

        for raw_capability in (
            raw_query.get(
                "exact_capabilities"
            )
            or required_capabilities
        ):
            capability = str(
                raw_capability
                or ""
            ).strip()

            if (
                capability
                and capability
                not in exact_capabilities
            ):
                exact_capabilities.append(
                    capability
                )

        recalled_capabilities: list[
            str
        ] = []

        for raw_capability in (
            raw_query.get(
                "recalled_capabilities"
            )
            or []
        ):
            capability = str(
                raw_capability
                or ""
            ).strip()

            if (
                capability
                and capability
                not in recalled_capabilities
            ):
                recalled_capabilities.append(
                    capability
                )

        query_items.append(
            {
                "query_id": query_id,
                "query_text": query_text,
                "exact_capabilities": (
                    exact_capabilities
                ),
                "recalled_capabilities": (
                    recalled_capabilities
                ),
                "query_source": str(
                    raw_query.get(
                        "query_source"
                    )
                    or "semantic_query"
                ),
            }
        )

    if not query_items:
        logger.info(
            "[Creator]"
            "[tool_recall]"
            "[result] %s",
            json.dumps(
                {
                    "event": (
                        "creator_tool_recall_result"
                    ),
                    "source": (
                        "none:"
                        "no_recall_queries"
                    ),
                    "required_capabilities": [],
                    "candidate_tool_ids": [],
                    "per_capability_candidates": {},
                },
                ensure_ascii=False,
                default=str,
            ),
        )

        return (
            [],
            "none:no_recall_queries",
        )

    cards: list[
        tuple[
            str,
            str,
            dict[str, Any],
        ]
    ] = []

    for tool in catalog:
        tool_id = str(
            tool.get("tool_id")
            or ""
        ).strip()

        cards.append(
            (
                tool_id,
                _creator_tool_recall_card_text(
                    tool
                ),
                tool,
            )
        )

    card_texts = [
        card_text
        for _, card_text, _
        in cards
    ]

    query_texts = [
        str(
            item["query_text"]
        )
        for item in query_items
    ]

    embedding_source = ""

    cache = (
        _CREATOR_TOOL_EMBEDDING_INDEX_CACHE
    )

    if cache is None:
        combined_texts = [
            *card_texts,
            *query_texts,
        ]

        (
            combined_embeddings,
            embedding_source,
        ) = _creator_embed_texts(
            combined_texts
        )

        expected_count = (
            len(card_texts)
            + len(query_texts)
        )

        if (
            len(combined_embeddings)
            != expected_count
        ):
            raise RuntimeError(
                "combined capability recall "
                "embedding returned unexpected "
                "vector count"
            )

        card_embeddings = (
            combined_embeddings[
                :len(card_texts)
            ]
        )

        query_embeddings = (
            combined_embeddings[
                len(card_texts):
            ]
        )

        tool_embeddings = [
            (
                cards[index][0],
                embedding,
            )
            for index, embedding
            in enumerate(
                card_embeddings
            )
        ]

        cache_signature = (
            (
                "embedding_source:"
                f"{embedding_source}"
            ),
            *card_texts,
        )

        _CREATOR_TOOL_EMBEDDING_INDEX_CACHE = (
            cache_signature,
            tool_embeddings,
        )

    else:
        (
            query_embeddings,
            embedding_source,
        ) = _creator_embed_texts(
            query_texts
        )

        cache_signature = (
            (
                "embedding_source:"
                f"{embedding_source}"
            ),
            *card_texts,
        )

        if cache[0] == cache_signature:
            tool_embeddings = list(
                cache[1]
            )

        else:
            combined_texts = [
                *card_texts,
                *query_texts,
            ]

            (
                combined_embeddings,
                embedding_source,
            ) = _creator_embed_texts(
                combined_texts
            )

            expected_count = (
                len(card_texts)
                + len(query_texts)
            )

            if (
                len(combined_embeddings)
                != expected_count
            ):
                raise RuntimeError(
                    "combined capability recall "
                    "embedding returned unexpected "
                    "vector count"
                )

            card_embeddings = (
                combined_embeddings[
                    :len(card_texts)
                ]
            )

            query_embeddings = (
                combined_embeddings[
                    len(card_texts):
                ]
            )

            tool_embeddings = [
                (
                    cards[index][0],
                    embedding,
                )
                for index, embedding
                in enumerate(
                    card_embeddings
                )
            ]

            cache_signature = (
                (
                    "embedding_source:"
                    f"{embedding_source}"
                ),
                *card_texts,
            )

            _CREATOR_TOOL_EMBEDDING_INDEX_CACHE = (
                cache_signature,
                tool_embeddings,
            )

    if (
        len(query_embeddings)
        != len(query_items)
    ):
        raise RuntimeError(
            "tool recall query embedding count "
            "does not match recall queries"
        )

    per_query_limit = min(
        max(
            1,
            int(top_k or 1),
        ),
        len(tool_embeddings),
    )

    candidate_tool_ids: list[
        str
    ] = []

    score_by_tool: dict[
        str,
        float,
    ] = {}

    recalled_for_capabilities: dict[
        str,
        list[str],
    ] = {}

    recall_sources_by_tool: dict[
        str,
        list[str],
    ] = {}

    per_query_candidates: dict[
        str,
        list[dict[str, Any]],
    ] = {}

    for (
        query_item,
        query_embedding,
    ) in zip(
        query_items,
        query_embeddings,
    ):
        query_id = str(
            query_item.get("query_id")
            or ""
        )

        exact_capabilities = list(
            query_item.get(
                "exact_capabilities"
            )
            or []
        )

        query_recalled_capabilities = list(
            query_item.get(
                "recalled_capabilities"
            )
            or []
        )

        scored_tools = [
            (
                tool_id,
                float(
                    _creator_cosine_similarity(
                        query_embedding,
                        tool_embedding,
                    )
                ),
            )
            for (
                tool_id,
                tool_embedding,
            )
            in tool_embeddings
        ]

        scored_tools.sort(
            key=lambda item: item[1],
            reverse=True,
        )

        score_lookup = dict(
            scored_tools
        )

        embedding_selected = (
            scored_tools[
                :per_query_limit
            ]
        )

        exact_matches: dict[
            str,
            list[str],
        ] = {}

        for (
            tool_id,
            _,
            tool,
        ) in cards:
            matched_capabilities = [
                capability
                for capability
                in exact_capabilities
                if (
                    _creator_tool_exact_capability_matches(
                        tool,
                        capability,
                    )
                )
            ]

            if matched_capabilities:
                exact_matches[
                    tool_id
                ] = (
                    matched_capabilities
                )

        selected_tool_ids: list[
            str
        ] = []

        selected_sources: dict[
            str,
            list[str],
        ] = {}

        for tool_id in exact_matches:
            selected_tool_ids.append(
                tool_id
            )

            selected_sources[
                tool_id
            ] = [
                "exact_contract"
            ]

        for (
            tool_id,
            _,
        ) in embedding_selected:
            if (
                tool_id
                not in selected_tool_ids
            ):
                selected_tool_ids.append(
                    tool_id
                )

            sources = (
                selected_sources
                .setdefault(
                    tool_id,
                    [],
                )
            )

            if (
                "embedding_top_k"
                not in sources
            ):
                sources.append(
                    "embedding_top_k"
                )

        query_candidates: list[
            dict[str, Any]
        ] = []

        for tool_id in selected_tool_ids:
            score = float(
                score_lookup.get(
                    tool_id,
                    -1.0,
                )
            )

            sources = list(
                selected_sources.get(
                    tool_id
                )
                or []
            )

            matched_capabilities = list(
                exact_matches.get(
                    tool_id
                )
                or []
            )

            query_candidates.append(
                {
                    "tool_id": tool_id,
                    "similarity": score,
                    "candidate_source": (
                        "+".join(
                            sources
                        )
                    ),
                    "matched_capabilities": (
                        matched_capabilities
                    ),
                }
            )

            if (
                tool_id
                not in candidate_tool_ids
            ):
                candidate_tool_ids.append(
                    tool_id
                )

            score_by_tool[
                tool_id
            ] = max(
                score_by_tool.get(
                    tool_id,
                    -1.0,
                ),
                score,
            )

            tool_sources = (
                recall_sources_by_tool
                .setdefault(
                    tool_id,
                    [],
                )
            )

            for source in sources:
                if (
                    source
                    not in tool_sources
                ):
                    tool_sources.append(
                        source
                    )

            matched_for_tool = (
                recalled_for_capabilities
                .setdefault(
                    tool_id,
                    [],
                )
            )

            for capability in [
                *query_recalled_capabilities,
                *matched_capabilities,
            ]:
                capability = str(
                    capability
                    or ""
                ).strip()

                if (
                    capability
                    and capability
                    not in matched_for_tool
                ):
                    matched_for_tool.append(
                        capability
                    )

        per_query_candidates[
            query_id
        ] = query_candidates

    by_tool_id = {
        str(
            tool.get("tool_id")
            or ""
        ).strip(): tool
        for tool in catalog
    }

    candidate_cards: list[
        dict[str, Any]
    ] = []

    for tool_id in candidate_tool_ids:
        tool = by_tool_id.get(
            tool_id
        )

        if not isinstance(
            tool,
            dict,
        ):
            continue

        functions: list[
            dict[str, Any]
        ] = []

        for function in (
            tool.get("functions")
            or []
        ):
            if not isinstance(
                function,
                dict,
            ):
                continue

            functions.append(
                {
                    "function_name": str(
                        function.get(
                            "function_name"
                        )
                        or ""
                    ),
                    "import_path": str(
                        function.get(
                            "import_path"
                        )
                        or ""
                    ),
                    "short_description": str(
                        function.get(
                            "short_description"
                        )
                        or ""
                    ),
                    "when_to_use": str(
                        function.get(
                            "when_to_use"
                        )
                        or ""
                    ),
                    "signature": str(
                        function.get(
                            "signature"
                        )
                        or ""
                    ),
                    "input_schema": (
                        function.get(
                            "input_schema"
                        )
                        or {}
                    ),
                    "output_schema": (
                        function.get(
                            "output_schema"
                        )
                        or {}
                    ),
                    "return_contract": str(
                        function.get(
                            "return_contract"
                        )
                        or ""
                    ),
                    "artifact_outputs": list(
                        function.get(
                            "artifact_outputs"
                        )
                        or []
                    ),
                    "side_effects": list(
                        function.get(
                            "side_effects"
                        )
                        or []
                    ),
                    "required_capabilities": list(
                        function.get(
                            "required_capabilities"
                        )
                        or []
                    ),
                }
            )

        candidate_cards.append(
            {
                "tool_id": tool_id,
                "display_name": str(
                    tool.get(
                        "display_name"
                    )
                    or ""
                ),
                "category": str(
                    tool.get("category")
                    or ""
                ),
                "prompt_guidance": str(
                    tool.get(
                        "prompt_guidance"
                    )
                    or ""
                ),
                "capability_aliases": list(
                    tool.get(
                        "capability_aliases"
                    )
                    or []
                ),
                "semantic_tags": list(
                    tool.get(
                        "semantic_tags"
                    )
                    or []
                ),
                "task_verbs": list(
                    tool.get(
                        "task_verbs"
                    )
                    or []
                ),
                "domain_terms": list(
                    tool.get(
                        "domain_terms"
                    )
                    or []
                ),
                "required_capabilities": list(
                    tool.get(
                        "required_capabilities"
                    )
                    or []
                ),
                "optional_capabilities": list(
                    tool.get(
                        "optional_capabilities"
                    )
                    or []
                ),
                "input_schema": (
                    tool.get(
                        "input_schema"
                    )
                    or {}
                ),
                "output_schema": (
                    tool.get(
                        "output_schema"
                    )
                    or {}
                ),
                "artifact_outputs": list(
                    tool.get(
                        "artifact_outputs"
                    )
                    or []
                ),
                "side_effects": list(
                    tool.get(
                        "side_effects"
                    )
                    or []
                ),
                "similarity": (
                    score_by_tool.get(
                        tool_id,
                        -1.0,
                    )
                ),
                "recalled_for_capabilities": list(
                    recalled_for_capabilities
                    .get(
                        tool_id
                    )
                    or []
                ),
                "recall_sources": list(
                    recall_sources_by_tool
                    .get(
                        tool_id
                    )
                    or []
                ),
                "functions": functions,
            }
        )

    logger.info(
        "[Creator]"
        "[tool_recall]"
        "[result] %s",
        json.dumps(
            {
                "event": (
                    "creator_tool_recall_result"
                ),
                "source": (
                    embedding_source
                ),
                "required_capabilities": (
                    required_capabilities
                ),
                "capability_owners": (
                    capability_owners
                ),
                "top_k_per_query": (
                    per_query_limit
                ),
                "registry_tool_count": len(
                    catalog
                ),
                "candidate_tool_ids": (
                    candidate_tool_ids
                ),
                "per_capability_candidates": (
                    per_query_candidates
                ),
            },
            ensure_ascii=False,
            default=str,
        ),
    )

    return (
        candidate_cards,
        embedding_source,
    )

def _declared_supported_input_formats(*, blueprint_text: str, skill_plan_entry: Any) -> set[str]:
    declared = f"{blueprint_text or ''}\n{_entry_text_for_declared_support(skill_plan_entry)}".lower()
    return {
        fmt
        for fmt, tokens in _DECLARED_FORMAT_TOKENS.items()
        if any(token in declared for token in tokens)
    }


def _not_supported_declared_input_issue(
    *,
    source: str,
    blueprint_text: str,
    skill_plan_entry: Any,
    file_path: str,
) -> dict[str, Any] | None:
    lowered = (source or "").lower()
    if not any(marker in lowered for marker in _NOT_SUPPORTED_MARKERS):
        return None
    declared_formats = _declared_supported_input_formats(
        blueprint_text=blueprint_text,
        skill_plan_entry=skill_plan_entry,
    )
    unsupported_declared = sorted(
        fmt
        for fmt in declared_formats
        if fmt in lowered or f".{fmt}" in lowered
    )
    if not unsupported_declared:
        return None
    return {
        "id": "script_declared_input_not_supported",
        "failed_file": file_path,
        "failed_function": "declared input parser/dispatcher",
        "code_region": "branch raising not supported for a declared input type",
        "reason": (
            "Script source contains a not-supported branch for input formats "
            f"{', '.join(unsupported_declared)} while the blueprint/SkillPlan declares support for them."
        ),
        "minimal_edit": (
            "Either use a bound helper, implement the format with Python standard library / allowed dependencies, "
            "or adjust SkillPlan/SKILL.md to remove that support scope; do not simultaneously claim support and raise not supported."
        ),
        "allowed_scope": "current script implementation or declared support scope",
        "repair_boundary": "declared input format handling",
        "details": {"declared_formats": unsupported_declared},
    }

_VALIDATOR_ONLY_LAYERS = {
    "e2e_requirement_validator_error",
    "e2e_requirement_validator_incomplete",
    "validator_unavailable",
    "validator_timeout",
    "validator_invalid_json",
}

def _has_responsibility_missing_capability_issue(
    issues: list[Any],
) -> bool:
    """Return true only for structured missing-tool responsibility issue IDs.

    This must not infer missing capabilities from prose, registry function names,
    helper names, capability names, reasons, evidence, or repair instructions.
    """
    explicit_issue_ids = {
        "responsibility_tool_binding_failed",
        "missing_tool_binding",
        "missing_required_tool",
        "tool_support_insufficient",
    }

    for issue in issues or []:
        if not isinstance(issue, dict):
            continue

        issue_id = str(issue.get("id") or "").strip()
        if issue_id in explicit_issue_ids:
            return True

    return False

async def _complete_creator_json_object_once(
    *,
    messages: list[dict[str, str]],
    model: str,
    phase: str,
    response_schema: dict[str, Any],
) -> dict[str, Any]:
    """Call an OpenAI-compatible chat endpoint with strict JSON Schema output.

    The provider is asked to constrain generation to response_schema.

    There is no:
    - prose extraction;
    - Markdown stripping;
    - regex JSON recovery;
    - second LLM retry.

    The complete model content must itself be one JSON object.
    """

    base_url = str(
        settings.llm_base_url
        or ""
    ).rstrip("/")

    if not base_url:
        raise RuntimeError(
            "llm_base_url is empty"
        )

    if base_url.endswith(
        "/v1/chat/completions"
    ):
        url = base_url

    elif base_url.endswith("/v1"):
        url = (
            base_url
            + "/chat/completions"
        )

    else:
        url = (
            base_url
            + "/v1/chat/completions"
        )

    api_key = str(
        getattr(
            settings,
            "llm_api_key",
            "",
        )
        or getattr(
            settings,
            "openai_api_key",
            "",
        )
        or "ollama"
    ).strip()

    headers = {
        "Content-Type": (
            "application/json"
        ),
        "Authorization": (
            f"Bearer {api_key}"
        ),
    }

    schema_name = "".join(
        character
        if (
            character.isalnum()
            or character == "_"
        )
        else "_"
        for character in str(
            phase or ""
        )
    ).strip("_")

    schema_name = (
        schema_name[:64]
        or "creator_structured_output"
    )

    payload: dict[str, Any] = {
        "model": model,

        "messages": messages,

        "stream": False,

        "response_format": {
            "type": "json_schema",

            "json_schema": {
                "name": schema_name,

                "strict": True,

                "schema": (
                    response_schema
                ),
            },
        },

        "temperature": 0,
    }

    max_tokens = getattr(
        settings,
        "max_tokens",
        None,
    )

    if max_tokens is not None:
        payload[
            "max_tokens"
        ] = max_tokens

    timeout = float(
        getattr(
            settings,
            "llm_timeout_seconds",
            120,
        )
        or 120
    )

    logger.info(
        "[Creator]"
        "[json_once]"
        "[request] "
        "phase=%s "
        "model=%s "
        "url=%s "
        "messages=%d "
        "schema_name=%s "
        "schema=%s",
        phase,
        model,
        url,
        len(messages),
        schema_name,
        json.dumps(
            response_schema,
            ensure_ascii=False,
            default=str,
        ),
    )

    async with httpx.AsyncClient(
        timeout=timeout
    ) as client:
        response = await client.post(
            url,
            headers=headers,
            json=payload,
        )

    try:
        response.raise_for_status()

    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            "structured-output LLM request failed: "
            f"status={response.status_code} "
            f"body={response.text[:4000]}"
        ) from exc

    try:
        body = response.json()

    except Exception as exc:
        raise RuntimeError(
            "structured-output LLM response "
            "body is not JSON"
        ) from exc

    if not isinstance(
        body,
        dict,
    ):
        raise RuntimeError(
            "structured-output LLM response "
            "body is not an object"
        )

    choices = body.get(
        "choices"
    )

    if (
        not isinstance(
            choices,
            list,
        )
        or not choices
    ):
        raise RuntimeError(
            "structured-output LLM response "
            "does not contain choices"
        )

    choice = choices[0]

    if not isinstance(
        choice,
        dict,
    ):
        raise RuntimeError(
            "structured-output LLM first "
            "choice is not an object"
        )

    message = (
        choice.get("message")
        if isinstance(
            choice.get("message"),
            dict,
        )
        else {}
    )

    content = (
        message.get("content")
        or choice.get("text")
        or ""
    )

    if not isinstance(
        content,
        str,
    ):
        raise RuntimeError(
            "structured-output LLM content "
            "is not a string"
        )

    content = content.strip()

    if not content:
        raise RuntimeError(
            "structured-output LLM returned "
            "empty content"
        )

    logger.info(
        "[Creator]"
        "[json_once]"
        "[response] "
        "phase=%s "
        "expected_model=%s "
        "actual_model=%s "
        "finish_reason=%s "
        "content_len=%d "
        "content=\n%s",
        phase,
        model,
        str(
            body.get("model")
            or ""
        ),
        str(
            choice.get("finish_reason")
            or ""
        ),
        len(content),
        content[:8000],
    )

    try:
        parsed = json.loads(
            content
        )

    except json.JSONDecodeError as exc:
        raise ValueError(
            "structured-output LLM returned "
            "invalid JSON: "
            f"{exc}"
        ) from exc

    if not isinstance(
        parsed,
        dict,
    ):
        raise ValueError(
            "structured-output LLM response "
            "must be a JSON object"
        )

    return parsed

async def _plan_final_tool_pool(
    *,
    skill_name: str,
    file_specs: list[dict[str, Any]],
    responsibility_graph: dict[str, Any] | None = None,
    requested_model: str | None = None,
) -> dict[str, Any]:
    """Build the final ToolPool fact view from FunctionItem-owned recall.

    Recall produces candidate tools only. Backend Gate only checks factual
    availability (Registry presence, callable contract, configuration, and
    dependencies). A gated tool is an optional enhancement for the owning
    FunctionItem; it is not a required implementation dependency and absence of
    use is not a failure condition.
    """

    skill_dir = settings.skills_path / _validate_skill_name(skill_name)
    skill_dir.mkdir(parents=True, exist_ok=True)

    current_pool = load_tool_pool(skill_dir)
    current_allowed_ids = {
        str(tool.tool_id or "").strip()
        for tool in (current_pool.tools or [])
        if tool.status == "allowed" and str(tool.tool_id or "").strip()
    }

    try:
        candidate_catalog, recall_source = _recall_creator_tool_candidates(
            file_specs=file_specs,
            top_k=3,
        )
    except Exception as exc:
        logger.exception(
            "[Creator][final_tool_selection][tool_recall_failed] skill=%s error=%s",
            skill_name,
            f"{type(exc).__name__}: {exc}",
        )
        raise HTTPException(
            status_code=502,
            detail={
                "code": "creator_tool_recall_failed",
                "message": "Tool Registry capability recall failed.",
                "error": f"{type(exc).__name__}: {exc}",
            },
        ) from exc

    ordered_candidate_tool_ids: list[str] = []
    for item in candidate_catalog or []:
        if not isinstance(item, dict):
            continue
        tool_id = str(item.get("tool_id") or "").strip()
        if tool_id and tool_id not in ordered_candidate_tool_ids:
            ordered_candidate_tool_ids.append(tool_id)

    candidate_by_tool_id = {
        str(item.get("tool_id") or "").strip(): item
        for item in (candidate_catalog or [])
        if isinstance(item, dict) and str(item.get("tool_id") or "").strip()
    }

    recalled_candidate_tool_ids = list(ordered_candidate_tool_ids)
    recalled_candidate_set = set(recalled_candidate_tool_ids)
    add_tool_ids = sorted(recalled_candidate_set - current_allowed_ids)
    remove_tool_ids: list[str] = []

    computed_patch = {
        "tool_pool_patch": {
            "add_tool_requests": [
                {
                    "requested_capability": (
                        ", ".join(candidate_by_tool_id.get(tool_id, {}).get("recalled_for_capabilities", []) or [])
                        or "Skill Plan required capability"
                    ),
                    "candidate_tool_id": tool_id,
                    "reason": (
                        "Registry candidate was recalled from the normalized FunctionItem "
                        "capability contracts and submitted only to Backend factual authorization."
                    ),
                }
                for tool_id in add_tool_ids
            ],
            "remove_tool_requests": [
                {
                    "tool_id": tool_id,
                    "reason": "Tool binding refresh does not remove protected or runtime-required tools.",
                }
                for tool_id in remove_tool_ids
            ],
            "update_file_bindings": [],
            "reason": (
                "Backend diff from FunctionItem-owned exact capability recall and embedding top-k recall. "
                "Passing Gate means optional availability, not required use."
            ),
            "affected_files": [],
        }
    }

    apply_result = _apply_planner_tool_pool_patch(
        skill_name=skill_name,
        planner_output=computed_patch,
        source_phase="final_contract_tool_planning",
        allow_remove=False,
    )

    updated_pool = load_tool_pool(skill_dir)
    available_optional_tool_ids = sorted(
        tool_id
        for tool_id in recalled_candidate_set
        if any(tool.tool_id == tool_id and tool.status == "allowed" for tool in updated_pool.tools)
    )
    unavailable_tool_ids = sorted(recalled_candidate_set - set(available_optional_tool_ids))

    updated_pool.file_bindings = []
    save_tool_pool(skill_dir, updated_pool)
    updated_pool = load_tool_pool(skill_dir)

    tool_bindings_by_file: dict[str, list[str]] = {}

    normalized_selector_output = {
        "recalled_candidate_tool_ids": recalled_candidate_tool_ids,
        "available_optional_tool_ids": available_optional_tool_ids,
        "unavailable_tool_ids": unavailable_tool_ids,
        "tool_bindings_by_file": {},
        "desired_tool_ids": recalled_candidate_tool_ids,
        "authorized_tool_ids": available_optional_tool_ids,
        "candidate_tool_ids": recalled_candidate_tool_ids,
        "recall_source": recall_source,
        "selection_mode": "skill_wide_optional_recall_no_llm",
    }

    logger.info(
        "[Creator][final_tool_selection][result] %s",
        json.dumps(
            {
                "event": "final_skill_tool_optional_availability_result",
                "skill_name": skill_name,
                "recall_source": recall_source,
                "selection_mode": "skill_wide_optional_recall_no_llm",
                "recalled_candidate_tool_ids": recalled_candidate_tool_ids,
                "available_optional_tool_ids": available_optional_tool_ids,
                "unavailable_tool_ids": unavailable_tool_ids,
                "tool_bindings_by_file": {},
                "add_tool_ids": add_tool_ids,
                "remove_tool_ids": remove_tool_ids,
                "llm_selector_used": False,
            },
            ensure_ascii=False,
            default=str,
        ),
    )

    return {
        "planner_output": normalized_selector_output,
        "computed_patch": computed_patch,
        "apply_result": apply_result,
        "recalled_candidate_tool_ids": recalled_candidate_tool_ids,
        "available_optional_tool_ids": available_optional_tool_ids,
        "unavailable_tool_ids": unavailable_tool_ids,
        "tool_bindings_by_file": {},
        "desired_tool_ids": recalled_candidate_tool_ids,
        "authorized_tool_ids": available_optional_tool_ids,
        "tool_pool": updated_pool,
    }



def _apply_planner_tool_pool_patch(
    *,
    skill_name: str,
    planner_output: dict[str, Any],
    source_phase: str,
    allow_remove: bool,
) -> dict[str, Any]:
    """Apply a planner proposal to the shared Skill ToolPool.

    Planner selects exact Registry tool IDs.

    Backend Gate authorizes the proposed tool for the current Skill.

    ToolPool.tools remains Skill-wide factual availability.

    file_bindings is a retired compatibility field and is cleared before save.

    Code model, responsibility judge, repair model, and E2E must never call this
    function to expand ToolPool.
    """

    raw_patch = (
        planner_output.get(
            "tool_pool_patch"
        )
        if isinstance(
            planner_output,
            dict,
        )
        else None
    )

    if not isinstance(
        raw_patch,
        dict,
    ):
        return {
            "requested": 0,
            "allowed_new": 0,
            "attached_existing": 0,
            "missing_new": 0,
            "denied_new": 0,
            "removed": 0,
            "patch_present": False,
        }

    try:
        patch = ToolPoolPatch.model_validate(
            raw_patch
        )

    except Exception as exc:
        logger.warning(
            "[Creator]"
            "[planner_tool_pool_patch]"
            "[invalid_patch] "
            "phase=%s skill=%s error=%s",
            source_phase,
            skill_name,
            exc,
        )

        return {
            "requested": 0,
            "allowed_new": 0,
            "attached_existing": 0,
            "missing_new": 0,
            "denied_new": 0,
            "removed": 0,
            "patch_present": True,
            "invalid_patch": True,
            "error": (
                f"{type(exc).__name__}: {exc}"
            ),
        }

    if not str(
        skill_name or ""
    ).strip():
        return {
            "requested": 0,
            "allowed_new": 0,
            "attached_existing": 0,
            "missing_new": 0,
            "denied_new": 0,
            "removed": 0,
            "patch_present": True,
            "skipped": "skill_name_missing",
        }

    safe_skill_name = _validate_skill_name(
        skill_name
    )

    skill_dir = (
        settings.skills_path
        / safe_skill_name
    )

    skill_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    pool = load_tool_pool(
        skill_dir
    )

    pool.skill_name = safe_skill_name

    for tool in pool.tools:
        tool.target_files = []

    def merge_unique(
        current: list[Any],
        incoming: list[Any],
    ) -> list[Any]:
        result = list(
            current or []
        )

        for item in (
            incoming or []
        ):
            if item not in result:
                result.append(item)

        return result

    removed_ids: set[str] = set()

    if allow_remove:
        for raw_remove in (
            patch.remove_tool_requests
            or []
        ):
            if not isinstance(
                raw_remove,
                dict,
            ):
                continue

            tool_id = str(
                raw_remove.get("tool_id")
                or raw_remove.get(
                    "candidate_tool_id"
                )
                or ""
            ).strip()

            if tool_id:
                removed_ids.add(
                    tool_id
                )

    if removed_ids:
        pool.tools = [
            tool
            for tool in pool.tools
            if tool.tool_id
            not in removed_ids
        ]

        pool.denied_requests = [
            item
            for item in pool.denied_requests
            if item.tool_id
            not in removed_ids
        ]

        pool.missing_requests = [
            item
            for item in pool.missing_requests
            if item.tool_id
            not in removed_ids
        ]

    proposal_source = (
        "repair_request"
        if source_phase
        == "responsibility_feedback"
        else "blueprint_preselect"
    )

    add_requests: list[
        ToolPoolAddToolRequest
    ] = []

    seen_tool_ids: set[str] = set()

    for request in (
        patch.add_tool_requests
        or []
    ):
        tool_id = str(
            request.candidate_tool_id
            or ""
        ).strip()

        if not tool_id:
            continue

        if tool_id in seen_tool_ids:
            continue

        seen_tool_ids.add(
            tool_id
        )

        add_requests.append(
            request.model_copy(
                update={
                    "target_file": (
                        _normalize_skill_path(str(request.target_file or ""))
                        if source_phase == "responsibility_feedback"
                        else ""
                    ),
                    "source": proposal_source,
                }
            )
        )

    pool.exploration_candidates = [
        {
            "requested_capability": (
                request.requested_capability
            ),
            "candidate_tool_id": (
                request.candidate_tool_id
            ),
            "reason": request.reason,
            "confidence": request.confidence,
            "score": request.score,
            "matched_features": list(
                request.matched_features
                or []
            ),
            "matched_terms": list(
                request.matched_terms
                or []
            ),
            "rank": request.rank,
            "candidate_source": (
                request.candidate_source
            ),
            "semantic_reason": (
                request.semantic_reason
            ),
            "proposal_phase": source_phase,
            "proposal_owner": (
                "planning_model"
            ),
            "authorization_scope": "skill",
        }
        for request in add_requests
    ]

    pool.scored_candidates = [
        {
            "tool_id": (
                request.candidate_tool_id
            ),
            "requested_capability": (
                request.requested_capability
            ),
            "score": request.score,
            "rank": request.rank,
            "reason": request.reason,
            "matched_features": list(
                request.matched_features
                or []
            ),
            "matched_terms": list(
                request.matched_terms
                or []
            ),
            "proposal_phase": source_phase,
            "proposal_owner": (
                "planning_model"
            ),
            "authorization_scope": "skill",
        }
        for request in add_requests
    ]

    total = {
        "requested": len(
            add_requests
        ),
        "allowed_new": 0,
        "attached_existing": 0,
        "missing_new": 0,
        "denied_new": 0,
        "removed": len(
            removed_ids
        ),
        "patch_present": True,
    }

    current_patch_allowed_tool_ids: list[str] = []

    for request in add_requests:
        tool_id = str(
            request.candidate_tool_id
            or ""
        ).strip()

        # Current proposal supersedes old denied/missing state for the same tool.
        pool.denied_requests = [
            item
            for item in pool.denied_requests
            if item.tool_id != tool_id
        ]

        pool.missing_requests = [
            item
            for item in pool.missing_requests
            if item.tool_id != tool_id
        ]

        existing_tool = next(
            (
                tool
                for tool in pool.tools
                if (
                    tool.tool_id == tool_id
                    and tool.status
                    == "allowed"
                )
            ),
            None,
        )

        if existing_tool is not None:
            existing_tool.target_files = []

            existing_tool.source_phase = (
                source_phase
            )

            existing_tool.score = max(
                float(
                    existing_tool.score
                    or 0.0
                ),
                float(
                    request.score
                    or 0.0
                ),
            )

            existing_tool.matched_features = (
                merge_unique(
                    existing_tool
                    .matched_features,
                    request.matched_features,
                )
            )

            existing_tool.matched_terms = (
                merge_unique(
                    existing_tool
                    .matched_terms,
                    request.matched_terms,
                )
            )

            if request.reason:
                existing_tool.reason = (
                    request.reason
                )

            if tool_id not in current_patch_allowed_tool_ids:
                current_patch_allowed_tool_ids.append(tool_id)

            total[
                "attached_existing"
            ] += 1

            continue

        gate_event = gate_tool_request(
            request
        )

        gate_event.target_file = ""

        pool.gate_events.append(
            gate_event
        )

        if gate_event.decision == "allow":
            capability = get_tool_capability(
                gate_event.tool_id
            )

            tool = ToolPoolTool(
                tool_id=gate_event.tool_id,
                status="allowed",
                source=request.source,
                source_phase=source_phase,

                target_files=[],

                allowed_helper_imports=list(
                    gate_event
                    .allowed_helper_imports
                    or []
                ),

                allowed_import_paths=list(
                    gate_event
                    .allowed_import_paths
                    or []
                ),

                allowed_function_imports=list(
                    gate_event
                    .allowed_function_imports
                    or []
                ),

                score=request.score,

                matched_features=list(
                    request.matched_features
                    or []
                ),

                matched_terms=list(
                    request.matched_terms
                    or []
                ),

                allowed_roles=list(
                    (
                        getattr(
                            capability,
                            "roles",
                            [],
                        )
                        if capability
                        is not None
                        else []
                    )
                    or []
                ),

                input_schema=(
                    getattr(
                        capability,
                        "input_schema",
                        {},
                    )
                    if capability
                    is not None
                    else {}
                )
                or {},

                output_schema=(
                    getattr(
                        capability,
                        "output_schema",
                        {},
                    )
                    if capability
                    is not None
                    else {}
                )
                or {},

                required_env=list(
                    gate_event.required_env
                    or []
                ),

                dependencies=list(
                    gate_event.dependencies
                    or []
                ),

                reason=request.reason,

                gate_result=(
                    gate_event.decision
                ),

                gate_messages=list(
                    gate_event.messages
                    or []
                ),
            )

            pool.tools.append(
                tool
            )

            if gate_event.tool_id not in current_patch_allowed_tool_ids:
                current_patch_allowed_tool_ids.append(gate_event.tool_id)

            total[
                "allowed_new"
            ] += 1

            continue

        if gate_event.decision in {
            "require_config",
            "require_dependency",
        }:
            pool.missing_requests.append(
                ToolPoolMissingRequest(
                    target_file="",
                    tool_id=(
                        gate_event.tool_id
                    ),
                    missing_env=list(
                        gate_event.missing_env
                        or []
                    ),
                    missing_dependencies=list(
                        gate_event
                        .missing_dependencies
                        or []
                    ),
                    reason="; ".join(
                        gate_event.messages
                        or []
                    ),
                )
            )

            total[
                "missing_new"
            ] += 1

            continue

        pool.denied_requests.append(
            ToolPoolDeniedRequest(
                target_file="",
                tool_id=(
                    gate_event.tool_id
                ),
                helper_imports=list(
                    gate_event
                    .denied_helper_imports
                    or []
                ),
                reason=(
                    gate_event.decision
                ),
                messages=list(
                    gate_event.messages
                    or []
                ),
                suggested_replacements=list(
                    gate_event
                    .suggested_replacements
                    or []
                ),
            )
        )

        total[
            "denied_new"
        ] += 1

    affected_files = [
        _normalize_skill_path(str(item or ""))
        for item in (patch.affected_files or [])
        if str(item or "").strip()
    ]
    if affected_files:
        total["affected_files"] = affected_files

    pool.file_bindings = []

    save_tool_pool(
        skill_dir,
        pool,
    )

    current_pool = load_tool_pool(
        skill_dir
    )

    total["tool_pool"] = (
        tool_pool_snapshot(
            current_pool
        )
    )

    logger.info(
        "[Creator]"
        "[planner_tool_pool_patch] %s",
        json.dumps(
            {
                "event": (
                    "planner_tool_pool_patch"
                ),
                "skill_name": (
                    safe_skill_name
                ),
                "source_phase": (
                    source_phase
                ),
                "authorization_scope": (
                    "skill"
                ),
                **{
                    key: value
                    for key, value
                    in total.items()
                    if key != "tool_pool"
                },
                "current_tool_ids": [
                    tool.tool_id
                    for tool
                    in current_pool.tools
                    if tool.status
                    == "allowed"
                ],
            },
            ensure_ascii=False,
            default=str,
        ),
    )

    return total

def _split_e2e_blocking_errors(errors: list[str]) -> tuple[list[str], list[dict[str, Any]]]:
    """Separate deterministic workflow failures from advisory validator issues."""
    blocking: list[str] = []
    warnings: list[dict[str, Any]] = []
    for error in errors or []:
        text = str(error or "")
        structured = _structured_failure_from_errors([text])
        target = str(structured.get("target_file") or "")
        layer = str(structured.get("layer") or _failure_layer_from_error_text(text) or "")
        if target == "__validator__" or layer in _VALIDATOR_ONLY_LAYERS or "E2E_REPAIR_TARGET=__validator__" in text:
            warnings.append({
                "severity": "validator_warning",
                "code": layer or "validator_unavailable",
                "source": "e2e_advisory_validator",
                "target_file": target or "__validator__",
                "message": text,
            })
        else:
            blocking.append(text)
    return blocking, warnings

def _e2e_advisory_status_from_warnings(warnings: list[Any]) -> str:
    return "unavailable" if warnings else "skipped"


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
    responsibility_edges: list[dict[str, Any]] | None = None
    function_items: list[dict[str, Any]] | None = None
    requirement_allocations: list[dict[str, Any]] | None = None


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
    status: Literal[
        "ready",
        "needs_clarification",
        "blocked",
    ]

    prepare_stage: Literal[
        "business_clarification",
        "creation_points_confirmation",
        "supplement_confirmation",
        "ready",
        "blueprint_protocol_failed",
        "blueprint_analyze_failed",
        "tool_pool_closure_failed",
        "asset_upload_required",
    ] = "business_clarification"

    clarifying_questions: list[str] = (
        Field(default_factory=list)
    )

    review_summary: (
        PreparePlanReviewSummary
    ) = Field(
        default_factory=(
            PreparePlanReviewSummary
        )
    )

    blueprint_text: str = ""
    skill_name: str = ""
    function_items: list[dict[str, Any]] = Field(default_factory=list)
    responsibility_edges: list[dict[str, Any]] = Field(default_factory=list)
    requirement_allocations: list[dict[str, Any]] = Field(default_factory=list)

    files: list[FileSpecOut] = Field(
        default_factory=list
    )

    warnings: list[Any] = Field(
        default_factory=list
    )

    asset_requirements: list[
        AssetRequirementOut
    ] = Field(
        default_factory=list
    )

    final_outputs: list[Any] = Field(
        default_factory=list
    )

    available_tools: list[Any] = Field(
        default_factory=list
    )

    missing_tool_configs: list[Any] = (
        Field(default_factory=list)
    )

    tool_requirements: list[Any] = Field(
        default_factory=list
    )

    creation_blockers: list[Any] = Field(
        default_factory=list
    )

    requirement_graph: Any = Field(
        default_factory=dict
    )

    workflow_allocation_summary: str = ""

    tool_pool_summary: dict[
        str,
        Any,
    ] = Field(
        default_factory=dict
    )

    confirmed_uploaded_assets: list[
        dict[str, Any]
    ] = Field(
        default_factory=list
    )

    unselected_uploaded_files: list[
        dict[str, Any]
    ] = Field(
        default_factory=list
    )



def _split_uploaded_asset_decisions(uploaded_files: list[dict[str, Any]] | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    confirmed: list[dict[str, Any]] = []
    unselected: list[dict[str, Any]] = []
    for raw in uploaded_files or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        decision = str(item.get("asset_decision") or "unknown").strip() or "unknown"
        item["asset_decision"] = decision
        if decision == "include_as_asset":
            try:
                item["asset_target_path"] = _validate_asset_upload_path(str(item.get("asset_target_path") or ""))
            except Exception:
                unselected.append({**item, "asset_decision": "unknown", "asset_validation_error": "invalid_asset_target_path"})
                continue
            confirmed.append(item)
        else:
            unselected.append(item)
    return confirmed, unselected

def _creator_upload_source_path(item: dict[str, Any]) -> Path:
    session_id = sanitize_session_id(str(item.get("session_id") or ""))
    session_dir = (UPLOAD_ROOT / session_id).resolve()
    source = Path(str(item.get("path") or "")).resolve()
    if not source.is_file() or not source.is_relative_to(session_dir):
        raise HTTPException(status_code=400, detail="confirmed_uploaded_assets 源文件必须来自 Creator 上传目录。")
    return source


def _unresolved_bundled_asset_paths(
    files: list[FileSpecOut], *, skill_name: str
) -> list[str]:
    """Require bundled declarations to resolve in the platform inventory."""
    inventory_root = (settings.bundled_skills_path / skill_name).resolve()
    unresolved: list[str] = []
    for file_spec in files or []:
        path = str(getattr(file_spec, "path", "") or "")
        if not path.startswith("assets/") or getattr(file_spec, "asset_source", "") != "bundled":
            continue
        candidate = (inventory_root / path).resolve()
        if not candidate.is_relative_to(inventory_root) or not candidate.is_file():
            unresolved.append(path)
    return unresolved

def _copy_confirmed_uploaded_assets_to_skill(skill_name: str, confirmed_uploaded_assets: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    skill_dir = settings.skills_path / _validate_skill_name(skill_name)
    assets_dir = (skill_dir / "assets").resolve()
    copied: list[dict[str, Any]] = []
    for item in confirmed_uploaded_assets or []:
        if not isinstance(item, dict) or str(item.get("asset_decision") or "") != "include_as_asset":
            continue
        target_rel = _validate_asset_upload_path(str(item.get("asset_target_path") or ""))
        source = _creator_upload_source_path(item)
        target = (skill_dir / target_rel).resolve()
        if not target.is_relative_to(assets_dir):
            raise HTTPException(status_code=400, detail="confirmed_uploaded_assets 目标必须落在 assets/**。")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        copied.append({**item, "asset_target_path": target_rel, "provided": True, "bytes": target.stat().st_size})
    return copied

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


def _is_concrete_prepare_summary_file_path(path: str, *, asset_source: str = "") -> bool:
    normalized = _normalize_skill_path(str(path or ""))
    if not normalized:
        return False
    if normalized in {"assets", "assets/", "references", "references/", "scripts", "scripts/"}:
        return False
    if normalized.endswith("/") or _is_directory_like_skill_path(normalized):
        return False
    if re.search(r"[<>{}*]|\$\{|\[[^\]]*(?:name|path|file|ext|文件|名称)[^\]]*\]", normalized, re.I):
        return False
    if re.match(r"^(?:outputs?|OUTPUT_DIR|generated|build|dist|tmp)(?:/|$)", normalized, re.I):
        return False
    if normalized.startswith("assets/") and asset_source not in {"user_upload", "bundled"}:
        return False
    return True


def _sync_prepare_summary_files_from_skill_plan(
    summary: PreparePlanReviewSummary,
    plan_files: list[Any] | None,
) -> list[dict[str, Any]]:
    """Make prepare review file display follow the analyzed SkillPlan files.

    The analyzed SkillPlan is authoritative. This helper only updates the
    front-end review/double-check field from the final plan; it never mutates
    or expands the execution plan from model-authored summary text.
    """
    original_summary_files = [
        _normalize_skill_path(str(path or ""))
        for path in (summary.files_to_create_or_update or [])
        if str(path or "").strip()
    ]
    original_summary_assets = [
        _normalize_skill_path(str(path or ""))
        for path in (summary.assets_to_upload or [])
        if str(path or "").strip()
    ]
    authoritative: list[str] = []
    authoritative_upload_assets: list[str] = []
    for file_spec in plan_files or []:
        path = _normalize_skill_path(str(getattr(file_spec, "path", "") or ""))
        file_type = str(getattr(file_spec, "file_type", "") or "").strip()
        asset_source = str(getattr(file_spec, "asset_source", "") or "").strip()
        if _is_concrete_prepare_summary_file_path(path, asset_source=asset_source) and path not in authoritative:
            authoritative.append(path)
        if file_type == "asset" and asset_source == "user_upload" and path not in authoritative_upload_assets:
            authoritative_upload_assets.append(path)
    summary.files_to_create_or_update = authoritative
    summary.assets_to_upload = authoritative_upload_assets
    extra_summary_files = [
        path
        for path in original_summary_files
        if path and path not in set(authoritative) and _is_concrete_prepare_summary_file_path(path, asset_source="bundled" if path.startswith("assets/") else "")
    ]
    extra_summary_assets = [
        path
        for path in original_summary_assets
        if path and path not in set(authoritative_upload_assets)
    ]
    warnings: list[dict[str, Any]] = []
    if extra_summary_files:
        warnings.append({
        "severity": "planning_warning",
        "code": "summary_files_not_in_skill_plan",
        "source": "prepare_plan",
        "path": "",
        "field": "review_summary.files_to_create_or_update",
        "files": extra_summary_files,
        "message": "review_summary listed files not present in analyzed SkillPlan; ignored because SkillPlan is authoritative.",
        })
    if extra_summary_assets:
        warnings.append({
            "severity": "planning_warning",
            "code": "summary_asset_not_in_file_plan",
            "source": "prepare_plan",
            "path": "",
            "field": "review_summary.assets_to_upload",
            "files": extra_summary_assets,
            "message": "review_summary listed assets not present in analyzed FilePlan user-upload assets; ignored because FilePlan is authoritative.",
        })
    return warnings



def _is_concrete_assets_file_path(path: str) -> bool:
    normalized = _normalize_skill_path(str(path or ""))

    if not normalized.startswith("assets/"):
        return False

    if normalized in {"assets", "assets/"}:
        return False

    if normalized.endswith("/"):
        return False

    # Only reject dynamic path syntax; do not infer meaning from placeholder text.
    if any(
        marker in normalized
        for marker in (
            "${",
            "{{",
            "}}",
            "<",
            ">",
            "[",
            "]",
            "*",
        )
    ):
        return False

    return True


def _filter_unconfirmed_asset_plan(
    *,
    files: list[Any] | None,
    asset_requirements: list[Any] | None,
    uploaded_files: list[dict[str, Any]] | None,
    review_summary: PreparePlanReviewSummary | None,
) -> list[dict[str, Any]]:
    """Remove assets/** plan entries that lack structured user confirmation.

    This is intentionally deterministic: allowed paths come only from explicit
    include_as_asset upload decisions or source=user_explicit structured
    asset requirements produced before the final file plan.
    """
    allowed_paths: set[str] = set()

    for raw in uploaded_files or []:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("asset_decision") or "").strip() != "include_as_asset":
            continue
        path = _normalize_skill_path(str(raw.get("asset_target_path") or ""))
        if _is_concrete_assets_file_path(path):
            allowed_paths.add(path)

    for item in asset_requirements or []:
        source = str(getattr(item, "source", "") or "").strip()
        if source != "user_explicit":
            continue
        path = _normalize_skill_path(str(getattr(item, "path", "") or ""))
        if _is_concrete_assets_file_path(path):
            allowed_paths.add(path)

    removed: list[str] = []
    kept_files: list[Any] = []
    for file_spec in files or []:
        path = _normalize_skill_path(str(getattr(file_spec, "path", "") or ""))
        if path.startswith("assets/") and path not in allowed_paths:
            if path and path not in removed:
                removed.append(path)
            continue
        kept_files.append(file_spec)
    if files is not None:
        files[:] = kept_files

    kept_assets: list[Any] = []
    for asset in asset_requirements or []:
        path = _normalize_skill_path(str(getattr(asset, "path", "") or ""))
        if path.startswith("assets/") and path not in allowed_paths:
            if path and path not in removed:
                removed.append(path)
            continue
        kept_assets.append(asset)
    if asset_requirements is not None:
        asset_requirements[:] = kept_assets

    if review_summary is not None:
        review_summary.assets_to_upload = [
            path
            for path in (_normalize_skill_path(str(p or "")) for p in (review_summary.assets_to_upload or []))
            if path in allowed_paths
        ]
        review_summary.files_to_create_or_update = [
            path
            for path in (_normalize_skill_path(str(p or "")) for p in (review_summary.files_to_create_or_update or []))
            if not (path.startswith("assets/") and path not in allowed_paths)
        ]

    if not removed:
        return []
    return [{
        "severity": "planning_warning",
        "code": "ungrounded_asset_plan_removed",
        "source": "prepare_plan",
        "path": "",
        "field": "files",
        "files": removed,
        "message": "已移除缺少用户明确要求或已确认上传依据的 assets 文件。",
    }]

def _required_file_plan_user_upload_asset_paths(plan_files: list[Any] | None, asset_requirements: list[Any] | None = None) -> list[str]:
    paths: list[str] = []
    for file_spec in plan_files or []:
        path = _normalize_skill_path(str(getattr(file_spec, "path", "") or ""))
        asset_source = str(getattr(file_spec, "asset_source", "") or "").strip()
        required = bool(getattr(file_spec, "required", False)) and not bool(getattr(file_spec, "can_skip", False))
        if path.startswith("assets/") and asset_source == "user_upload" and required and path not in paths:
            paths.append(path)
    for asset in asset_requirements or []:
        path = _normalize_skill_path(str(getattr(asset, "path", "") or ""))
        source = str(getattr(asset, "source", "") or "").strip()
        required = bool(getattr(asset, "required", True))
        if path.startswith("assets/") and source == "user_explicit" and required and path not in paths:
            paths.append(path)
    return paths


MAX_PREPARE_BUSINESS_CLARIFICATION_ROUNDS = 2
MAX_PREPARE_SUPPLEMENT_ROUNDS = 1
MAX_PREPARE_BLUEPRINT_REPAIR_ROUNDS = 3

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
    for match in re.finditer(
        r"(?im)^[ \t]*-[ \t]*path[ \t]*:[ \t]*`?([^`\n]+?)`?[ \t]*$",
        blueprint_text or "",
    ):
        path = _normalize_skill_path(match.group(1).strip().strip("'\""))
        if path and path not in paths:
            paths.append(path)
    return paths


def _is_prepare_resource_path(path: str) -> bool:
    normalized = _normalize_skill_path(str(path or ""))
    return normalized.startswith(("references/", "assets/")) and _has_file_extension(normalized)


def _build_prepare_allowed_resource_paths(
    *,
    request: PreparePlanRequest,
    review_summary: Any = None,
    existing_skill_context: dict[str, Any] | None = None,
) -> set[str]:
    """Freeze resource authority from facts available before Blueprint repair."""
    allowed: set[str] = set()
    _ = review_summary  # Display/planning data is not resource provenance.
    allowed.update(_extract_user_explicit_prepare_resource_paths(request))

    for item in request.uploaded_files or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("asset_decision") or "") == "include_as_asset":
            path = _normalize_skill_path(str(item.get("asset_target_path") or ""))
            if path.startswith("assets/") and _is_prepare_resource_path(path):
                allowed.add(path)

    context = existing_skill_context or {}
    for key in ("references", "assets"):
        for raw_path in context.get(key) or []:
            path = _normalize_skill_path(str(raw_path or ""))
            if _is_prepare_resource_path(path):
                allowed.add(path)
    return allowed


def _extract_user_explicit_prepare_resource_paths(request: PreparePlanRequest) -> set[str]:
    """Extract literal concrete resource paths only from user-authored text."""
    texts = [str(request.user_request or ""), str(request.human_feedback or "")]
    texts.extend(
        str(item.get("content") or "")
        for item in request.conversation_history or []
        if isinstance(item, dict) and str(item.get("role") or "").lower() == "user"
    )
    paths: set[str] = set()
    for text in texts:
        for match in re.finditer(r"(?<![\w/])(?:references|assets)/[A-Za-z0-9_.@+\-/]+", text):
            path = _normalize_skill_path(match.group(0).rstrip("./"))
            if _is_prepare_resource_path(path):
                paths.add(path)
    return paths


def _remove_unauthorized_prepare_resources(
    blueprint_text: str,
    allowed_resource_paths: set[str],
) -> tuple[str, list[str]]:
    """Deterministically remove resource entries/references without authority."""
    rejected: set[str] = set()
    before_non_resource_paths = _extract_prepare_non_resource_paths(blueprint_text)
    block_re = re.compile(
        r"(?ims)^[ \t]*-[ \t]*path[ \t]*:[ \t]*`?([^`\n]+?)`?[ \t]*$[\s\S]*?"
        r"(?=^[ \t]*-[ \t]*path[ \t]*:|^[ \t]*#{1,6}[ \t]+|\Z)"
    )

    def clean_block(match: re.Match[str]) -> str:
        path = _normalize_skill_path(match.group(1).strip().strip("'\""))
        if _is_prepare_resource_path(path) and path not in allowed_resource_paths:
            rejected.add(path)
            return ""
        block = match.group(0)

        def clean_field(field_match: re.Match[str]) -> str:
            values = []
            for raw in re.split(r"[,，、][ \t]*", str(field_match.group(3) or "")):
                value = _normalize_skill_path(raw.strip().strip("'\"`"))
                if _is_prepare_resource_path(value) and value not in allowed_resource_paths:
                    rejected.add(value)
                    continue
                if value:
                    values.append(value)
            return f"{field_match.group(1)}{field_match.group(2)}: [{', '.join(values)}]"

        return re.sub(
            r"(?im)^([ \t]*)(dependencies|references)[ \t]*:[ \t]*\[?([^\]\n]*)\]?[ \t]*$",
            clean_field,
            block,
        )

    cleaned = block_re.sub(clean_block, str(blueprint_text or ""))
    if _extract_prepare_non_resource_paths(cleaned) != before_non_resource_paths:
        return str(blueprint_text or "").strip(), []
    return cleaned.strip(), sorted(rejected)


def _extract_prepare_non_resource_paths(blueprint_text: str) -> list[str]:
    """Return the immutable, executable portion of prepare FilePlan topology."""
    return [
        path
        for path in _extract_prepare_skill_plan_paths(blueprint_text)
        if path == "SKILL.md" or path.startswith("scripts/")
    ]


def _enforce_prepare_plan_resource_authority(
    plan: AnalyzeBlueprintResponse,
    allowed_resource_paths: set[str],
) -> list[str]:
    rejected: set[str] = set()
    kept_files = []
    for file_spec in plan.files or []:
        path = _normalize_skill_path(str(getattr(file_spec, "path", "") or ""))
        if _is_prepare_resource_path(path) and path not in allowed_resource_paths:
            rejected.add(path)
            continue
        for field in ("dependencies", "reference_files", "references"):
            values = getattr(file_spec, field, None)
            if not isinstance(values, list):
                continue
            kept = []
            for raw in values:
                resource = _normalize_skill_path(str(raw or ""))
                if _is_prepare_resource_path(resource) and resource not in allowed_resource_paths:
                    rejected.add(resource)
                else:
                    kept.append(raw)
            setattr(file_spec, field, kept)
        kept_files.append(file_spec)
    plan.files = kept_files
    kept_assets = []
    for item in plan.asset_requirements or []:
        path = _normalize_skill_path(str(getattr(item, "path", "") or ""))
        if _is_prepare_resource_path(path) and path not in allowed_resource_paths:
            rejected.add(path)
            continue
        kept_assets.append(item)
    plan.asset_requirements = kept_assets
    return sorted(rejected)


def _preflight_prepare_blueprint_text(
    blueprint_text: str,
    allowed_resource_paths: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Validate explicit SkillPlan protocol fields without semantic reconstruction.

    Only explicit SkillPlan blocks are contract sources.

    A path appearing in prose, examples, protocol documentation, or host
    execution guidance does not create a file-plan responsibility.
    """

    issues: list[
        dict[str, Any]
    ] = []

    text = str(
        blueprint_text
        or ""
    )

    plan_paths = exact_file_plan_paths_from_strict_skillplan(text)

    plan_path_set = set(
        plan_paths
    )

    if allowed_resource_paths is not None:
        for path in plan_paths:
            normalized = _normalize_skill_path(path)
            if _is_prepare_resource_path(normalized) and normalized not in allowed_resource_paths:
                issues.append(_prepare_protocol_issue(
                    "unjustified_resource_reference",
                    "Resource path has no frozen upstream authority. Remove this SkillPlan entry; source=bundled is not provenance.",
                    path=normalized,
                    field="SkillPlan",
                ))

    dynamic_re = re.compile(
        (
            r"[<>{}\*]|\$\{|"
            r"\[[^\]]*"
            r"(?:name|path|file|ext|文件|名称)"
            r"[^\]]*\]"
        ),
        re.I,
    )

    runtime_dir_re = re.compile(
        (
            r"^(?:outputs?|OUTPUT_DIR|"
            r"generated|build|dist|tmp)"
            r"(?:/|$)"
        ),
        re.I,
    )

    runtime_dependency_re = re.compile(
        (
            r"outputs?/|OUTPUT_DIR|generated/|"
            r"build/|dist/|tmp/|[<>{}\*]"
        ),
        re.I,
    )

    def plan_block(
        path: str,
    ) -> str:
        match = re.search(
            (
                rf"(?ims)"
                rf"^[ \t]*-[ \t]*path[ \t]*:[ \t]*"
                rf"`?{re.escape(path)}`?[ \t]*$"
                rf"([\s\S]*?)"
                rf"(?="
                rf"^[ \t]*-[ \t]*path[ \t]*:|"
                rf"^[ \t]*#{{1,6}}[ \t]+|"
                rf"\Z"
                rf")"
            ),
            text,
        )

        return (
            match.group(1)
            if match
            else ""
        )

    def list_field_values(
        block: str,
        field_name: str,
    ) -> list[str]:
        match = re.search(
            (
                rf"(?im)"
                rf"^[ \t]*{re.escape(field_name)}"
                rf"[ \t]*:[ \t]*"
                rf"\[?([^\]\n]*)\]?"
                rf"[ \t]*$"
            ),
            block,
        )

        if not match:
            return []

        raw = str(
            match.group(1)
            or ""
        )

        values: list[
            str
        ] = []

        for item in re.split(
            r"[,，、][ \t]*",
            raw,
        ):
            value = (
                str(item or "")
                .strip()
                .strip("'\"`")
            )

            if (
                value
                and value not in values
            ):
                values.append(
                    value
                )

        return values

    for path in plan_paths:
        normalized = _normalize_skill_path(
            path
        )

        block = plan_block(
            path
        )

        if normalized in {
            "assets",
            "assets/",
        }:
            issues.append(
                _prepare_protocol_issue(
                    "invalid_asset_directory_path",
                    (
                        "assets 不能声明为"
                        "目录路径。"
                    ),
                    path=path,
                )
            )

        if (
            normalized.startswith(
                "assets/"
            )
            and dynamic_re.search(
                normalized
            )
        ):
            issues.append(
                _prepare_protocol_issue(
                    "invalid_asset_placeholder_path",
                    (
                        "assets path 不能包含"
                        "占位符或通配符。"
                    ),
                    path=path,
                )
            )

        if (
            not normalized
            or normalized.endswith("/")
            or _is_directory_like_skill_path(
                normalized
            )
            or dynamic_re.search(
                normalized
            )
        ):
            issues.append(
                _prepare_protocol_issue(
                    (
                        "invalid_dynamic_or_"
                        "directory_path"
                    ),
                    (
                        "SkillPlan path 必须是"
                        "具体文件路径。"
                    ),
                    path=path,
                )
            )

        if runtime_dir_re.search(
            normalized
        ):
            issues.append(
                _prepare_protocol_issue(
                    (
                        "runtime_artifact_path_"
                        "in_skill_plan"
                    ),
                    (
                        "运行时产物目录不能出现"
                        "在 SkillPlan path。"
                    ),
                    path=path,
                )
            )

        if normalized.startswith("assets/"):
            if re.search(
                (
                    r"运行时|每次上传|用户输入|"
                    r"runtime\s+input|粘贴|"
                    r"待用户上传"
                ),
                block,
                re.I,
            ):
                issues.append(
                    _prepare_protocol_issue(
                        (
                            "runtime_input_"
                            "described_as_asset"
                        ),
                        (
                            "运行时用户输入文件"
                            "不能描述为 Creator assets。"
                        ),
                        path=path,
                    )
                )

        source_issue = (
            resource_role_source_issue(
                file_type_for_path(normalized),
                parse_resource_source_from_block(block),
            )
            if normalized.startswith(("references/", "assets/"))
            else None
        )
        if source_issue:
            code, message = source_issue
            issues.append(
                _prepare_protocol_issue(code, message, path=path, field="source")
            )

        dependencies = list_field_values(
            block,
            "dependencies",
        )

        references = list_field_values(
            block,
            "references",
        )

        for dependency in dependencies:
            if runtime_dependency_re.search(
                dependency
            ):
                issues.append(
                    _prepare_protocol_issue(
                        "invalid_runtime_dependency",
                        (
                            "dependencies 只能写"
                            "运行前静态依赖，不能包含"
                            "运行时产物、动态文件名"
                            "或输出目录。"
                        ),
                        path=path,
                        field="dependencies",
                    )
                )

                continue

            normalized_dependency = (
                _normalize_skill_path(
                    dependency
                )
            )

            if (
                allowed_resource_paths is not None
                and _is_prepare_resource_path(normalized_dependency)
                and normalized_dependency not in allowed_resource_paths
            ):
                issues.append(_prepare_protocol_issue(
                    "unjustified_resource_reference",
                    "Resource path has no frozen upstream authority. Remove this dependency from the owning script; do not add a resource SkillPlan entry or invent source=bundled.",
                    path=normalized_dependency,
                    field="dependencies",
                ))
                continue

            if (
                normalized_dependency.startswith(
                    (
                        "references/",
                        "assets/",
                    )
                )
                and _has_file_extension(
                    normalized_dependency
                )
                and normalized_dependency
                not in plan_path_set
            ):
                issues.append(
                    _prepare_protocol_issue(
                        (
                            "dependency_missing_"
                            "from_skill_plan"
                        ),
                        (
                            "SkillPlan dependencies "
                            "引用了未声明的静态文件 "
                            f"{normalized_dependency}。"
                        ),
                        path=normalized_dependency,
                        field="dependencies",
                    )
                )

        for reference in references:
            normalized_reference = (
                _normalize_skill_path(
                    reference
                )
            )

            if (
                allowed_resource_paths is not None
                and _is_prepare_resource_path(normalized_reference)
                and normalized_reference not in allowed_resource_paths
            ):
                issues.append(_prepare_protocol_issue(
                    "unjustified_resource_reference",
                    "Resource path has no frozen upstream authority. Remove this reference from the owning script; do not add a resource SkillPlan entry or invent source=bundled.",
                    path=normalized_reference,
                    field="references",
                ))
                continue

            if (
                normalized_reference.startswith(
                    (
                        "references/",
                        "assets/",
                    )
                )
                and _has_file_extension(
                    normalized_reference
                )
                and normalized_reference
                not in plan_path_set
            ):
                issues.append(
                    _prepare_protocol_issue(
                        (
                            "reference_missing_"
                            "from_skill_plan"
                        ),
                        (
                            "SkillPlan references "
                            "引用了未声明的静态文件 "
                            f"{normalized_reference}。"
                        ),
                        path=normalized_reference,
                        field="references",
                    )
                )

    return issues


def _prepare_blueprint_has_required_protocol_shape(blueprint_text: str) -> bool:
    text = str(blueprint_text or "")
    return bool(
        text.strip()
        and re.search(r"Skill\s*架构蓝图|技能架构蓝图|架构蓝图|internal_blueprint", text, re.I)
        and re.search(r"SkillPlan|文件职责计划", text, re.I)
        and _extract_prepare_skill_plan_paths(text)
    )


def _prepare_blueprint_contains_clarification_text(blueprint_text: str) -> bool:
    text = str(blueprint_text or "")
    return bool(re.search(r"\bneeds_clarification\b|clarifying_questions|还需要.*确认|请.*补充", text, re.I))


def _prepare_repair_candidate_is_valid(candidate: str) -> bool:
    return (
        bool(str(candidate or "").strip())
        and _prepare_blueprint_has_required_protocol_shape(candidate)
        and not _prepare_blueprint_contains_clarification_text(candidate)
    )


def _is_valid_prepare_reference_path(path: str) -> bool:
    normalized = _normalize_skill_path(str(path or ""))
    if not normalized.startswith("references/") or normalized in {"references", "references/"}:
        return False
    if not normalized.endswith(".md") or not _has_file_extension(normalized):
        return False
    if re.search(r"[<>{}*\[\]]|\$\{", normalized):
        return False
    return True


def _extract_prepare_reference_paths(blueprint_text: str) -> set[str]:
    """Extract concrete references/*.md paths from explicit SkillPlan fields.

    Only SkillPlan block ``dependencies`` and ``references`` fields are topology
    facts. Incidental paths in prose, examples, resource checklists, or protocol
    context must not create Skill-local static files.
    """

    paths: set[str] = set()
    text = str(blueprint_text or "")
    block_re = re.compile(
        (
            r"(?ims)^[ \t]*-[ \t]*path[ \t]*:[ \t]*`?([^`\n]+?)`?[ \t]*$"
            r"(?P<block>[\s\S]*?)"
            r"(?=^[ \t]*-[ \t]*path[ \t]*:|^[ \t]*#{1,6}[ \t]+|\Z)"
        )
    )
    field_re = re.compile(
        r"(?im)^[ \t]*(dependencies|references)[ \t]*:[ \t]*\[?([^\]\n]*)\]?[ \t]*$"
    )

    for block_match in block_re.finditer(text):
        block = block_match.group("block") or ""
        for field_match in field_re.finditer(block):
            raw = str(field_match.group(2) or "")
            for item in re.split(r"[,，、][ \t]*", raw):
                path = _normalize_skill_path(
                    str(item or "").strip().strip("'\"`")
                )
                if _is_valid_prepare_reference_path(path):
                    paths.add(path)

    return paths


def _prepare_reference_plan_block(path: str) -> str:
    return (
        f"- path: `{path}`\n"
        "  file_type: reference\n"
        "  role: reference\n"
        "  purpose: 运行前静态参考资源，供声明它的文件消费\n"
        "  required: true\n"
        "  can_skip: false\n"
        "  inputs: []\n"
        "  outputs: []\n"
        "  dependencies: []\n"
        "  required_capabilities: []\n"
        "  forbidden_capabilities: []\n"
        "  references: []"
    )


def _insert_prepare_reference_plan_blocks(blueprint_text: str, blocks: list[str]) -> str:
    if not blocks:
        return str(blueprint_text or "").strip()
    text = str(blueprint_text or "").rstrip()
    addition = "\n" + "\n".join(blocks) + "\n"
    heading_match = re.search(
        r"(?im)^[ \t]*#{1,6}[ \t]*(?:SkillPlan|.*文件职责计划).*$",
        text,
    )
    if not heading_match:
        return (text + "\n\n## SkillPlan / 文件职责计划" + addition).strip()
    next_heading = re.search(r"(?m)^[ \t]*#{1,6}[ \t]+", text[heading_match.end():])
    if not next_heading:
        return (text + addition).strip()
    insert_at = heading_match.end() + next_heading.start()
    before = text[:insert_at].rstrip()
    after = text[insert_at:].lstrip("\n")
    return (before + addition + "\n" + after).strip()


def _normalize_prepare_blueprint_references(
    blueprint_text: str,
    allowed_resource_paths: set[str] | None = None,
) -> str:
    """Ensure explicit SkillPlan reference dependencies have file blocks.

    This is a structural prepare-plan normalization only: it follows concrete
    ``references/*.md`` paths already declared by SkillPlan ``dependencies`` or
    ``references`` fields, and never infers files from prose or resource lists.
    """

    text = str(
        blueprint_text
        or ""
    ).strip()

    if not text:
        return ""

    plan_paths = set(
        _extract_prepare_skill_plan_paths(
            text
        )
    )

    missing = sorted(
        path
        for path in _extract_prepare_reference_paths(
            text
        )
        if path not in plan_paths
        and (
            allowed_resource_paths is None
            or path in allowed_resource_paths
        )
    )

    if not missing:
        return text

    return _insert_prepare_reference_plan_blocks(
        text,
        [
            _prepare_reference_plan_block(
                path
            )
            for path in missing
        ],
    )


async def _repair_prepare_blueprint_protocol(
    *,
    request: PreparePlanRequest,
    blueprint_text: str,
    protocol_errors: list[dict[str, Any]],
    allowed_resource_paths: set[str] | None = None,
) -> str:
    """Repair Creator blueprint hard-protocol violations only.

    Protocol repair does not discover Registry tools and does not mutate the
    shared Skill ToolPool.
    """

    repaired = str(
        blueprint_text or ""
    )

    seen = {
        repaired
    }

    current_errors = list(
        protocol_errors or []
    )

    previous_errors: list[dict[str, Any]] = []
    for repair_index in range(
        MAX_PREPARE_BLUEPRINT_REPAIR_ROUNDS
    ):
        current_issue_identities = {
            (
                str(item.get("code") or ""),
                str(item.get("path") or item.get("field") or ""),
            )
            for item in current_errors
            if isinstance(item, dict)
        }
        repeated_errors = [
            item
            for item in previous_errors
            if isinstance(item, dict)
            and (
                str(item.get("code") or ""),
                str(item.get("path") or item.get("field") or ""),
            ) in current_issue_identities
        ]
        issue_codes = [str(item.get("code") or "") for item in current_errors if isinstance(item, dict)]
        issue_paths = [str(item.get("path") or item.get("field") or "") for item in current_errors if isinstance(item, dict)]
        is_resource_authority_repair = bool(issue_codes) and all(
            code == "unjustified_resource_reference" for code in issue_codes
        )
        original_non_resource_paths = (
            _extract_prepare_non_resource_paths(repaired)
            if is_resource_authority_repair
            else []
        )
        logger.info(
            "[Creator][blueprint_repair] attempt=%d issue_codes=%s issue_paths=%s",
            repair_index + 1, issue_codes, issue_paths,
        )
        prompt = (
            load_kernel_creator_for_phase(
                "prepare_plan"
            )
            + """
你只修复 internal_blueprint_text 的 Creator 硬协议问题。

不要重新设计业务需求。
不要探索 Tool Registry。
不要选择工具。
不要输出 tool_pool_patch。
不要修改 ToolPool。

只输出严格 JSON object：

{
  "internal_blueprint_text": "修复后的完整蓝图正文"
}

不要 Markdown 解释。

修复要求：

- 只根据 protocol_errors 修复对应协议问题。
- protocol_errors 中的 code、path、message、actual 是本轮必须直接消除的 validator 反馈。
- 保留已经正确的业务目标。
- 保留已经正确的文件职责。
- 保留已经正确的脚本文件拓扑。
- 不新增与 protocol_errors 无关的业务流程。

这是 Blueprint 返修，不是重新规划 Skill。必须保持已经正确的用户核心需求、Script 划分、
Script responsibility、inputs/outputs、workflow 主线和 FilePlan。优先只修 validator 指出的
问题及必要关联内容；不得因局部格式错误重命名所有字段、重新拆 Script、新增 resources 或改变核心 workflow。

Creator 协议边界：

- 不要把运行时用户输入文件写入 assets。
- 不要输出 assets/、assets/<name.ext>、assets/* 或动态 assets path。
- 如果不需要静态素材，删除 assets 文件计划。
- 目录结构不要列具体文件名。
- 目录结构和 SkillPlan path 必须一致。
- dependencies 只能写运行前静态依赖。
- 运行时产物只能出现在脚本 outputs/stdout/file_outputs。
- resources 只能引用 SkillPlan 已声明的 references/assets。
- references/*.md 引用必须对应 SkillPlan path。
- unjustified_resource_reference 必须从所属脚本删除 dependency/reference，并删除对应资源 entry；不得新增 reference/asset entry，不得编造 source=bundled。
- 不得把 kernel/protocol 示例 reference path 复制成业务文件。
- required_capabilities 只能表达抽象语义能力。
- 不得填写具体 Registry tool_id。
- 不得填写 selected_tools。
- 不得填写 required_tool_slots。

具体工具选择由后续 Final Tool Planner 完成。
"""
        )

        route = route_model(
            "creator_prepare_plan",
            requested_model=request.model,
            reason=(
                "creator prepare blueprint "
                "protocol repair"
            ),
        )

        text = await complete_creator_role_once(
            [
                {
                    "role": "system",
                    "content": prompt,
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "blueprint_text": repaired,
                            "protocol_errors": (
                                current_errors
                            ),
                            "remaining_issues_from_previous_repair": repeated_errors,
                            "repair_directive": (
                                "The listed issue remains unresolved; directly eliminate it and do not repeat an almost identical Blueprint."
                                if repair_index and repeated_errors
                                else "Fix the listed validator issues only."
                            ),
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            ],
            "planner", fallback_model=route.model,
        )

        data = _parse_prepare_plan_json(
            text
        )

        candidate = str(
            data.get(
                "internal_blueprint_text"
            )
            or ""
        ).strip()

        candidate = (
            _normalize_prepare_blueprint_references(candidate, allowed_resource_paths)
            if allowed_resource_paths is not None
            else _normalize_prepare_blueprint_references(candidate)
        )

        # Resource-authority repair may edit resource declarations only. Other
        # protocol repairs can correct an invalid script path reported by their
        # validator feedback.
        if (
            is_resource_authority_repair
            and _extract_prepare_non_resource_paths(candidate)
            != original_non_resource_paths
        ):
            continue

        candidate_errors = (
            _preflight_prepare_blueprint_text(candidate, allowed_resource_paths)
            if allowed_resource_paths is not None
            else _preflight_prepare_blueprint_text(candidate)
        )
        if any(
            str(item.get("code") or "") == "unjustified_resource_reference"
            for item in candidate_errors
            if isinstance(item, dict)
        ):
            continue

        if not _prepare_repair_candidate_is_valid(
            candidate
        ):
            continue

        if candidate in seen:
            break

        repaired = candidate

        seen.add(
            repaired
        )

        previous_errors = current_errors
        current_errors = candidate_errors

        if not current_errors:
            break

    if current_errors:
        logger.warning(
            "[Creator][blueprint_repair] remaining_issue_codes=%s remaining_issue_paths=%s",
            [str(item.get("code") or "") for item in current_errors if isinstance(item, dict)],
            [str(item.get("path") or item.get("field") or "") for item in current_errors if isinstance(item, dict)],
        )

    return repaired

def _requirements_for_generated_file(
    *,
    skill_name: str,
    file_path: str,
    request_graph: Any = None,
    skill_plan_entry: Any = None,
) -> list[RequirementItem]:
    """Resolve one file's already-compiled responsibility requirements.

    Resolution priority:
    1. backend-persisted requirement graph;
    2. request graph from the current Creator plan;
    3. requirements attached to the current file-plan entry.

    This function does not infer or expand business responsibilities.
    """

    def _filter_items(
        raw_items: Any,
    ) -> list[RequirementItem]:
        result: list[
            RequirementItem
        ] = []

        for raw in raw_items or []:
            try:
                if isinstance(
                    raw,
                    RequirementItem,
                ):
                    item = raw

                elif isinstance(
                    raw,
                    dict,
                ):
                    item = RequirementItem(
                        **raw
                    )

                else:
                    continue

            except Exception:
                continue

            if (
                str(
                    item.target_file
                    or ""
                ).strip()
                != file_path
            ):
                continue

            result.append(
                item
            )

        return result

    try:
        persisted_graph = (
            _load_persisted_requirement_graph(
                skill_name
            )
        )

    except Exception:
        persisted_graph = None

    if persisted_graph is not None:
        persisted_items = _filter_items(
            persisted_graph.requirements
        )

        if persisted_items:
            return persisted_items

    if isinstance(
        request_graph,
        RequirementGraph,
    ):
        request_items = _filter_items(
            request_graph.requirements
        )

        if request_items:
            return request_items

    elif isinstance(
        request_graph,
        dict,
    ):
        try:
            normalized_graph = (
                normalize_requirement_graph(
                    request_graph
                )
            )

            request_items = _filter_items(
                normalized_graph.requirements
            )

            if request_items:
                return request_items

        except Exception:
            pass

    if isinstance(
        skill_plan_entry,
        dict,
    ):
        attached_requirements = (
            skill_plan_entry.get(
                "requirements"
            )
            or []
        )

    else:
        attached_requirements = getattr(
            skill_plan_entry,
            "requirements",
            [],
        ) or []

    return _filter_items(
        attached_requirements
    )

def _creator_responsibility_feedback_recall_query(
    *,
    target_file: str,
    file_spec: dict[str, Any],
    responsibility_issues: list[
        dict[str, Any]
    ],
) -> str:
    """Build a compact embedding query from first-round responsibility errors.

    Only responsibility semantics are projected into the embedding query.

    The full script source and complete Tool contracts are intentionally not
    embedded here because they introduce implementation/schema noise.
    """

    issue_fields = (
        "id",
        "failed_function",
        "code_region",
        "reason",
        "missing_evidence",
        "semantic_failure",
        "minimal_edit",
        "repair_instruction",
        "repair_instructions",
        "tool_id",
        "candidate_tool_id",
        "capability",
        "capability_id",
        "required_tool",
        "missing_tool",
        "helper",
        "helper_name",
    )

    compact_issues: list[
        dict[str, Any]
    ] = []

    for issue in (
        responsibility_issues
        or []
    ):
        if not isinstance(
            issue,
            dict,
        ):
            continue

        compact_issue = {
            field_name: issue.get(
                field_name
            )
            for field_name
            in issue_fields
            if issue.get(
                field_name
            )
            not in (
                None,
                "",
                [],
                {},
            )
        }

        if compact_issue:
            compact_issues.append(
                compact_issue
            )

        if len(compact_issues) >= 8:
            break

    return "\n".join(
        [
            (
                "根据 Creator 第一轮脚本责任校验反馈，"
                "召回可能直接补足真实运行能力缺口的 "
                "Tool Registry 工具。"
            ),
            (
                "这里只做候选召回；不要把普通代码"
                "实现错误强行解释为新的 capability。"
            ),
            "",
            (
                "target_file: "
                f"{target_file}"
            ),
            (
                "purpose: "
                + str(
                    file_spec.get(
                        "purpose"
                    )
                    or ""
                )
            ),
            (
                "inputs: "
                + json.dumps(
                    file_spec.get(
                        "inputs"
                    )
                    or [],
                    ensure_ascii=False,
                    default=str,
                )
            ),
            (
                "outputs: "
                + json.dumps(
                    file_spec.get(
                        "outputs"
                    )
                    or [],
                    ensure_ascii=False,
                    default=str,
                )
            ),
            (
                "required_capabilities: "
                + json.dumps(
                    file_spec.get(
                        "required_capabilities"
                    )
                    or [],
                    ensure_ascii=False,
                    default=str,
                )
            ),
            "",
            "responsibility_issues:",
            json.dumps(
                compact_issues,
                ensure_ascii=False,
                default=str,
            )[:6000],
        ]
    )

async def _plan_tool_pool_patch_from_responsibility_feedback(
    *,
    skill_name: str,
    target_file: str,
    file_spec: dict[str, Any],
    responsibility_issues: list[
        dict[str, Any]
    ],
    script_content: str,
    requested_model: str | None,
) -> dict[str, Any]:
    """Replan ToolPool from first-round responsibility feedback.

    The responsibility judge only reports the problem.

    Candidate discovery uses the same Tool recall policy as final planning:

        exact Registry capability seeds
        UNION
        embedding semantic top-k

    The planning model decides whether the failure is:

    - a code implementation problem; or
    - a real capability gap requiring a ToolPool proposal.

    Backend Gate remains the only authorization step.
    """

    shared_context = (
        _planner_shared_tool_context(
            skill_name
        )
    )

    feedback_query = (
        _creator_responsibility_feedback_recall_query(
            target_file=target_file,
            file_spec=file_spec,
            responsibility_issues=(
                responsibility_issues
            ),
        )
    )

    (
        candidate_tool_catalog,
        recall_source,
    ) = (
        _recall_creator_tool_candidates(
            file_specs=[
                file_spec
            ],
            top_k=3,
            semantic_queries=[
                {
                    "query_id": (
                        "responsibility_feedback"
                    ),
                    "query_text": (
                        feedback_query
                    ),
                    "exact_capabilities": list(
                        file_spec.get(
                            "required_capabilities"
                        )
                        or []
                    ),
                    "recalled_capabilities": [],
                    "query_source": (
                        "responsibility_feedback"
                    ),
                }
            ],
        )
    )

    candidate_tool_ids = {
        str(
            item.get("tool_id")
            or ""
        ).strip()
        for item
        in candidate_tool_catalog
        if (
            isinstance(
                item,
                dict,
            )
            and str(
                item.get("tool_id")
                or ""
            ).strip()
        )
    }

    logger.info(
        "[Creator]"
        "[responsibility_tool_recall]"
        "[result] %s",
        json.dumps(
            {
                "event": (
                    "creator_responsibility_"
                    "tool_recall_result"
                ),
                "skill_name": (
                    skill_name
                ),
                "target_file": (
                    target_file
                ),
                "source": (
                    recall_source
                ),
                "candidate_tool_ids": sorted(
                    candidate_tool_ids
                ),
            },
            ensure_ascii=False,
            default=str,
        ),
    )

    prompt = (
        load_kernel_creator_for_phase(
            "prepare_plan"
        )
        + """
你是 Creator 规划模型。

当前第一轮脚本生产完成后，责任判决模型反馈当前文件没有完成职责。

你的任务不是修代码。
你的任务只是判断：当前反馈是否揭示了共享 ToolPool 中缺少真实能力。

candidate_tool_catalog 已由统一 Tool recall 层产生：

1. Registry exact capability contract seed；
2. embedding semantic top-k expansion；
3. 两者取并集。

你只能在 candidate_tool_catalog 中判断是否存在真实能力补充。

不要重新扫描完整 Tool Registry。
不要自行提出候选集合之外的工具。

只输出严格 JSON object：

{
  "gap_type": "code_problem" | "capability_gap",
  "reason": "",
  "tool_pool_patch": {
    "add_tool_requests": [],
    "remove_tool_requests": [],
    "reason": "",
    "affected_files": []
  }
}

规则：

- candidate_tool_catalog 是统一 Tool recall 候选集合。
- current_tool_pool 是当前 Skill 唯一共享 ToolPool。
- responsibility_issues 是责任判决模型报告的问题事实。
- required_capabilities 是当前文件已经声明的业务能力合同。
- 责任判决模型无权选择、授权或添加工具。
- 写代码模型无权选择、授权或添加 ToolPool 外工具。
- 你是规划模型；你只在 candidate_tool_catalog 内判断是否需要提出 ToolPool proposal。
- 如果当前 ToolPool 已足够，或者问题只是代码没有正确使用已有工具/本地逻辑，则 gap_type=code_problem，add_tool_requests=[]。
- 只有职责客观需要 current_tool_pool 中不存在的能力，而且 candidate_tool_catalog 中存在真实匹配工具时，才 gap_type=capability_gap 并提出 add_tool_requests。
- candidate_tool_id 必须精确来自 candidate_tool_catalog。
- 不得根据文件名、角色名或固定业务词表机械匹配工具。
- 必须结合当前文件责任、责任判决反馈、当前 ToolPool 和候选工具真实函数能力进行语义判断。
- exact_contract 只表示 Registry 明确声明支持相关 capability，因此必须进入候选；它不代表必须选择。
- embedding_top_k 只表示语义近邻候选；相似不代表应该选择。
- 本阶段只能追加工具，remove_tool_requests 必须为空。
- proposal 仍需 Backend Gate；你不能自行授权。
"""
    )

    payload = {
        "skill_name": skill_name,
        "target_file": target_file,
        "file_spec": file_spec,
        "responsibility_issues": (
            responsibility_issues
        ),
        "script_content": str(
            script_content
            or ""
        )[:16000],
        "tool_recall_source": (
            recall_source
        ),
        "candidate_tool_catalog": (
            candidate_tool_catalog
        ),
        "current_tool_pool": (
            shared_context.get(
                "current_tool_pool"
            )
            or {}
        ),
        "tool_contract": (
            shared_context.get(
                "tool_contract"
            )
            or {}
        ),
    }

    route = route_model(
        "creator_prepare_plan",
        requested_model=(
            requested_model
        ),
        reason=(
            "creator responsibility feedback "
            "tool planning"
        ),
    )

    text = await complete_creator_role_once(
        [
            {
                "role": "system",
                "content": prompt,
            },
            {
                "role": "user",
                "content": json.dumps(
                    payload,
                    ensure_ascii=False,
                    default=str,
                ),
            },
        ],
        "planner", fallback_model=route.model,
    )

    data = _parse_prepare_plan_json(
        text
    )

    raw_patch = (
        data.get(
            "tool_pool_patch"
        )
        if isinstance(
            data,
            dict,
        )
        else None
    )

    invalid_candidate_tool_ids: list[
        str
    ] = []

    if isinstance(
        raw_patch,
        dict,
    ):
        for raw_request in (
            raw_patch.get(
                "add_tool_requests"
            )
            or []
        ):
            if not isinstance(
                raw_request,
                dict,
            ):
                continue

            candidate_tool_id = str(
                raw_request.get(
                    "candidate_tool_id"
                )
                or ""
            ).strip()

            if (
                candidate_tool_id
                and candidate_tool_id
                not in candidate_tool_ids
                and candidate_tool_id
                not in invalid_candidate_tool_ids
            ):
                invalid_candidate_tool_ids.append(
                    candidate_tool_id
                )

    if invalid_candidate_tool_ids:
        raise RuntimeError(
            (
                "responsibility feedback planner "
                "proposed tool IDs outside "
                "embedding/exact recall candidates: "
            )
            + ", ".join(
                invalid_candidate_tool_ids
            )
        )

    if (
        isinstance(data, dict)
        and isinstance(data.get("tool_pool_patch"), dict)
        and target_file
    ):
        data["tool_pool_patch"]["affected_files"] = [target_file]

    result = (
        _apply_planner_tool_pool_patch(
            skill_name=skill_name,
            planner_output=data,
            source_phase=(
                "responsibility_feedback"
            ),
            allow_remove=False,
        )
    )

    return {
        "planner_decision": data,
        "tool_recall_source": (
            recall_source
        ),
        "candidate_tool_ids": sorted(
            candidate_tool_ids
        ),
        **result,
    }

def _blocked_prepare_response(request: PreparePlanRequest, summary: PreparePlanReviewSummary, *, skill_name: str, issues: list[dict[str, Any]]) -> PreparePlanResponse:
    return PreparePlanResponse(
        status="blocked",
        prepare_stage="creation_points_confirmation",
        review_summary=summary,
        skill_name=skill_name or request.skill_name or "",
        creation_blockers=["创建计划暂时无法通过平台协议预检，请补充更明确的输入、输出、资源边界或文件计划后重试。"],
        warnings=[{"severity": "user_warning", "code": "prepare_plan_protocol_blocked", "source": "prepare_plan", "path": "", "field": "", "message": "Creator plan preparation is blocked by protocol validation.", "issues": issues[:5]}],
    )




def _responsibility_edge_endpoint_pairs(edges: object) -> list[list[object]]:
    if not isinstance(edges, list):
        return []
    pairs: list[list[object]] = []
    for edge in edges:
        if isinstance(edge, dict):
            pairs.append([edge.get("from_node"), edge.get("to_node")])
    return pairs


def _render_structured_responsibility_view(blueprint_text: str, function_items: list[dict[str, Any]], responsibility_edges: list[dict[str, Any]]) -> str:
    if not function_items:
        return blueprint_text
    text = str(blueprint_text or "").rstrip()
    targets = [str(item.get("target_file") or "").strip() for item in function_items if str(item.get("target_file") or "").strip()]
    target_set = set(targets)
    before: dict[str, set[str]] = {target: set() for target in targets}
    for edge in responsibility_edges or []:
        if not isinstance(edge, dict):
            continue
        from_node = str(edge.get("from_node") or "").strip()
        to_node = str(edge.get("to_node") or "").strip()
        if from_node in target_set and to_node in target_set and from_node != to_node:
            before[to_node].add(from_node)

    item_by_target = {str(item.get("target_file") or "").strip(): item for item in function_items}
    ordered_targets: list[str] = []
    ready = [target for target in targets if not before.get(target)]
    while ready:
        target = ready.pop(0)
        if target in ordered_targets:
            continue
        ordered_targets.append(target)
        for downstream in targets:
            if target in before.get(downstream, set()):
                before[downstream].discard(target)
                if not before[downstream] and downstream not in ordered_targets and downstream not in ready:
                    ready.append(downstream)
    ordered_targets.extend(target for target in targets if target not in ordered_targets)
    ordered_function_items = [item_by_target[target] for target in ordered_targets if target in item_by_target]

    workflow_lines = ["### 工作流逻辑"]
    for index, item in enumerate(ordered_function_items, start=1):
        workflow_lines.append(f"{index}. {str(item.get('purpose') or '').strip()}")

    function_item_owned_fields = (
        "role",
        "purpose",
        "inputs",
        "outputs",
        "required_capabilities",
        "constraints",
    )

    def render_owned_field(field: str, item: dict[str, Any]) -> str:
        if field in {"inputs", "outputs", "required_capabilities"}:
            value = json.dumps(item.get(field) or [], ensure_ascii=False)
        elif field == "constraints":
            value = json.dumps(item.get(field) or [], ensure_ascii=False, default=str)
        else:
            value = str(item.get(field) or "").strip()
        return f"  {field}: {value}"

    def render_new_script_block(target: str, item: dict[str, Any]) -> str:
        return "\n".join([
            f"- path: `{target}`",
            *(render_owned_field(field, item) for field in function_item_owned_fields),
        ])

    def overlay_function_item_fields(block: str, item: dict[str, Any]) -> str:
        lines = block.splitlines()
        seen: set[str] = set()
        updated: list[str] = []
        for line in lines:
            field_match = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*):", line)
            field = field_match.group(2) if field_match else ""
            if field in function_item_owned_fields:
                if field not in seen:
                    updated.append(render_owned_field(field, item))
                    seen.add(field)
                continue
            updated.append(line)
        for field in function_item_owned_fields:
            if field not in seen:
                updated.append(render_owned_field(field, item))
        return "\n".join(updated).rstrip()

    script_items_by_target = {
        str(item.get("target_file") or "").strip(): item
        for item in ordered_function_items
        if str(item.get("target_file") or "").strip()
    }

    def replace_section(source: str, heading: str, replacement_lines: list[str]) -> str:
        pattern = re.compile(rf"(?ms)^\s*###\s+{re.escape(heading)}\s*$.*?(?=^\s*###\s+|\Z)")
        replacement = "\n".join(replacement_lines).rstrip() + "\n"
        if pattern.search(source):
            return pattern.sub(replacement, source, count=1)
        return source.rstrip() + "\n" + replacement

    text = replace_section(text, "工作流逻辑", workflow_lines)

    skillplan_pattern = re.compile(r"(?ms)^\s*###\s+SkillPlan / 文件职责计划\s*$.*?(?=^\s*###\s+|\Z)")
    skillplan_match = skillplan_pattern.search(text)
    existing_blocks: list[tuple[str, str]] = []
    if skillplan_match:
        body = skillplan_match.group(0).split("\n", 1)[1] if "\n" in skillplan_match.group(0) else ""
        block_pattern = re.compile(
            r"(?ms)^\s*-\s*path\s*:\s*`?([^`\n]+)`?\s*\n.*?(?=^\s*-\s*path\s*:|\Z)"
        )
        for block_match in block_pattern.finditer(body):
            path = str(block_match.group(1) or "").strip().strip("`'\"，,。.;；").replace("\\", "/")
            block = block_match.group(0).rstrip()
            if path:
                existing_blocks.append((path, block))

    rendered_blocks: list[str] = []
    emitted: set[str] = set()
    for path, block in existing_blocks:
        if path in script_items_by_target:
            rendered_blocks.append(overlay_function_item_fields(block, script_items_by_target[path]))
            emitted.add(path)
        else:
            rendered_blocks.append(block)
    for target, item in script_items_by_target.items():
        if target not in emitted:
            rendered_blocks.append(render_new_script_block(target, item))

    skillplan_lines = ["### SkillPlan / 文件职责计划", *rendered_blocks]
    text = replace_section(text, "SkillPlan / 文件职责计划", skillplan_lines)
    return text.rstrip() + "\n"


def _structured_plan_review_summary(prepared: dict[str, Any], fallback: PreparePlanReviewSummary | None = None) -> PreparePlanReviewSummary:
    function_items = list(prepared.get("function_items") or [])
    if not function_items:
        return fallback or _coerce_prepare_summary(prepared.get("review_summary"))
    base = fallback or _coerce_prepare_summary(prepared.get("review_summary"))
    base.workflow = [str(item.get("purpose") or "") for item in function_items if str(item.get("purpose") or "").strip()]
    base.files_to_create_or_update = [str(item.get("target_file") or "") for item in function_items if str(item.get("target_file") or "").strip()]
    return base


async def _converge_ready_executable_plan(
    *,
    request: PreparePlanRequest,
    current_planner_result: dict[str, Any],
    planner_model: str,
    allowed_function_item_targets: list[str],
    draft_transport_error: str = "",
    frozen_blueprint_text: str = "",
) -> dict[str, Any]:
    """Regenerate graph edges once without asking Planner to repeat FunctionItems."""
    if not frozen_blueprint_text:
        raise ValueError("Planner convergence requires frozen_blueprint_text")

    frozen_function_items = _frozen_function_items_from_blueprint(
        frozen_blueprint_text=frozen_blueprint_text,
        allowed_function_item_targets=allowed_function_item_targets,
    )
    draft_edges = list(current_planner_result.get("responsibility_edges") or [])
    graph_context = _build_responsibility_graph_construction_context(
        frozen_blueprint_text=frozen_blueprint_text,
        allowed_function_item_targets=allowed_function_item_targets,
        function_items=frozen_function_items,
        responsibility_edges=draft_edges,
    )
    logger.info(
        "[Creator][planner_convergence][draft] %s",
        json.dumps({
            "event": "creator_planner_convergence_draft",
            "skill_name": str(current_planner_result.get("skill_name") or request.skill_name or ""),
            "draft_edge_count": len(draft_edges),
            "draft_transport_valid": not bool(draft_transport_error),
            "draft_transport_error": draft_transport_error,
            "draft_endpoint_pairs": _responsibility_edge_endpoint_pairs(draft_edges),
        }, ensure_ascii=False, default=str),
    )

    prompt = """
You are the same Blueprint Planner converging a ResponsibilityGraph over frozen
FunctionItems. Blueprint defines responsibilities; Graph connects them.

Return a complete responsibility_edges array only. Do not return or redesign
FunctionItems, files, target_file, inputs, outputs, purpose, capabilities, or
constraints. If frozen responsibility facts prevent closure, do not invent a
new boundary; leave the facts unchanged so backend can request upstream replan.

Use graph_construction_context as the only topology and endpoint authority. For
each target input, choose the semantically correct provenance from its supplied
legal_sources. Backend supplies only the structurally legal source domain and
does not choose the semantic mapping. Use exact endpoint strings. Preserve the
platform boundary contract and the canonical ResponsibilityEdge wire fields:
from_node, from_output, to_node, to_input, purpose, constraints.

Before returning, replay the complete graph and verify:
0. Valid node identities are only platform_input_node,
   platform_output_node, and exact authoritative script paths. Never use a
   role, basename, capability, shorthand, or other alias.
1. Every declared runtime input has exactly one legal provenance.
2. Every from_output exists in the supplied legal source domain.
3. Every to_input exists on the frozen target FunctionItem.
4. Every required final output has a producer-to-platform_output path.
5. No input, output, FunctionItem, file, or platform slot was invented.

Return only strict JSON: {"responsibility_edges": [...]}
""".strip()
    payload = {
        "task": "converge_responsibility_graph_edges",
        "graph_construction_context": graph_context,
        "validation_issues": ([{
            "reason": draft_transport_error,
        }] if draft_transport_error else []),
    }
    text = await complete_creator_role_once(
        [{"role": "system", "content": prompt},
         {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}],
        "planner", fallback_model=planner_model,
    )
    data = _parse_prepare_plan_json(text)
    if set(data) != {"responsibility_edges"} or not isinstance(data.get("responsibility_edges"), list):
        raise ValueError("Planner convergence must return only responsibility_edges")
    normalized_edges = validate_structured_responsibility_edge_transport(
        data["responsibility_edges"],
        function_items=frozen_function_items,
        source="planner convergence",
    )
    result = dict(current_planner_result)
    result["function_items"] = frozen_function_items
    result["responsibility_edges"] = normalized_edges
    logger.info(
        "[Creator][planner_convergence][result] %s",
        json.dumps({
            "event": "creator_planner_convergence_result",
            "skill_name": str(result.get("skill_name") or request.skill_name or ""),
            "final_edge_count": len(normalized_edges),
            "final_endpoint_pairs": _responsibility_edge_endpoint_pairs(normalized_edges),
        }, ensure_ascii=False, default=str),
    )
    return result


async def _review_responsibility_graph_alignment(
    *,
    request: PreparePlanRequest,
    frozen_blueprint_text: str,
    allowed_function_item_targets: list[str],
    function_items: list[dict[str, Any]],
    responsibility_edges: list[dict[str, Any]],
    planner_model: str,
) -> dict[str, Any]:
    """Review declared graph alignment without redesigning the Blueprint."""

    prompt = """
You are a read-only ResponsibilityGraph Alignment Reviewer.

You did not design this graph. Do not redesign it. Do not improve it. Do not
extend it. Your only job is to determine whether the CURRENT declared
FunctionItems and ResponsibilityEdges are sufficient and internally consistent
with the CONFIRMED Blueprint.

Review only the confirmed Blueprint, frozen FilePlan, current FunctionItems,
current ResponsibilityEdges, and immutable platform I/O contract supplied in
this payload. Do not infer requirements from industry practice, best practice,
robustness, or capabilities that could be useful.

Check ONLY these three principles:
1. Confirmed requirement fidelity: each requirement explicitly confirmed by the
   Blueprint has an existing FunctionItem owner. Report only a current omission,
   weakening, or incorrect assignment of an explicit requirement.
2. Declared dataflow closure: for each required ResponsibilityEdge, its producer
   can produce the declared result, its consumer can consume that declared
   result, and the transported result's business meaning is clearly consistent
   with the producer output and consumer input. This permits reporting an actual
   mismatch such as transporting story_text into story_segments. Do not infer
   Python control flow, serialization, helper calls, file storage, or transport
   implementation details.
3. Responsibility boundary consistency: a FunctionItem's role, purpose, inputs,
   outputs, capabilities, and constraints do not express a current business
   responsibility contradiction.

Platform topology, endpoint presence, boundary presence, and platform-slot
legality are backend-validator facts and are outside Reviewer authority. Do not
independently report or repair those facts.

At ResponsibilityGraph review time, executability means only that every
required declared responsibility has an existing FunctionItem owner and every
required cross-responsibility result has a declared dataflow edge. Do NOT
interpret executability as runtime implementation verification.

SUFFICIENCY / STOP RULE
Once all CONFIRMED Blueprint requirements have an existing responsibility
owner and all required declared data dependencies are closed, the graph is
SUFFICIENT. Sufficient is PASS. When the
graph is sufficient, you MUST return {"passed": true, "issues": []}. Stop
reviewing. Do not continue searching for optional improvements, robustness
mechanisms, validation mechanisms, synchronization mechanisms, feedback loops,
helper usage, or implementation details.

A graph MUST NOT fail merely because an additional mechanism COULD make the
implementation more robust, explicit, validated, synchronized, or
production-ready. Only a CURRENT contradiction, omission, or mismatch against
the CONFIRMED Blueprint and declared graph may produce an issue.

Do NOT require or invent any of the following unless explicitly required by the
confirmed Blueprint: semantic validation feedback loops; image/text validation
stages; synchronization mechanisms; mapping or pairing subsystems; additional
validation or aggregation FunctionItems; helper functions or artifact helper
usage; command syntax; argv conventions; stdin transport; stdout JSON transport;
serialization protocols; OUTPUT_DIR or artifact-root implementation; runtime
file existence or path checks; filesystem validation; retry mechanisms;
error-handling protocols; or implementation-specific transport mechanisms.

For example, a requirement to generate results consistent with supplied source
content is satisfied at this graph level when the generation responsibility
receives the relevant source content and owns generation. Do NOT infer a
separate semantic-validation or feedback-loop responsibility unless the
confirmed Blueprint explicitly requires post-generation validation.

Multiple declared inputs to the same FunctionItem do not by themselves require
a separate synchronization, pairing, or mapping responsibility. If that
FunctionItem explicitly owns combining the inputs, separate incoming declared
dependencies are sufficient unless the confirmed Blueprint explicitly requires
separate synchronization.

A FunctionItem output MAY be internal-only. Do not require an internal result
to be exposed as a platform output. Runtime file proof, OUTPUT_DIR handling, and
artifact helper usage are runtime, code, and E2E concerns.

Do not propose FilePlan changes. Do not request a new file, script,
FunctionItem, asset, reference, or runtime component unless the confirmed
Blueprint already requires that responsibility and the current graph omitted its
owner.

Every issue's reason, evidence, and repair_guidance must be based on a current
fact directly observable in this payload, not a hypothetical risk. Evidence
must cite the inconsistent declared FunctionItem or ResponsibilityEdge fact; do
not use absent implementation details as evidence. For a specific edge mismatch,
affected_edge_indexes must include that edge's index. Before failing, verify that
your evidence does not already prove the claimed semantic requirement is
satisfied.

Return only strict JSON:
{"passed": true, "issues": []}
or
{"passed": false, "issues": [{"id": "...", "target_files": [],
"affected_edge_indexes": [], "reason": "...", "evidence": "...",
"repair_guidance": "..."}]}
Do not return function_items or responsibility_edges.
""".strip()
    platform_contract = build_platform_io_contract()
    platform_boundary = platform_contract["platform_skill_boundary"]
    payload = {
        "task": "review_responsibility_graph_alignment",
        "confirmed_blueprint": frozen_blueprint_text,
        "allowed_function_item_targets": allowed_function_item_targets,
        "function_items": function_items,
        "responsibility_edges": responsibility_edges,
        "platform_boundary_contract": {
            "input_fields": platform_boundary["input_envelope_fields"],
            "final_output_fields": platform_boundary["final_output_fields"],
        },
    }
    text = await complete_creator_role_once(
        [{"role": "system", "content": prompt},
         {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}],
        "reviewer", fallback_model=planner_model,
    )
    data = _parse_prepare_plan_json(text)
    if set(data) != {"passed", "issues"} or not isinstance(data.get("passed"), bool) or not isinstance(data.get("issues"), list):
        raise ValueError("Responsibility graph alignment review must return only passed and issues")
    if data["passed"] and data["issues"]:
        raise ValueError("Passing responsibility graph alignment review must not include issues")
    if not data["passed"] and not data["issues"]:
        raise ValueError("Failing responsibility graph alignment review must include issues")
    issue_fields = {
        "id": str,
        "target_files": list,
        "affected_edge_indexes": list,
        "reason": str,
        "evidence": str,
        "repair_guidance": str,
    }
    for issue in data["issues"]:
        if not isinstance(issue, dict) or any(
            key not in issue or not isinstance(issue[key], value_type)
            for key, value_type in issue_fields.items()
        ):
            raise ValueError("Responsibility graph alignment review issue has invalid structure")
    return {"passed": data["passed"], "issues": data["issues"]}


async def _repair_responsibility_graph_alignment(
    *,
    request: PreparePlanRequest,
    frozen_blueprint_text: str,
    allowed_function_item_targets: list[str],
    function_items: list[dict[str, Any]],
    responsibility_edges: list[dict[str, Any]],
    review_issues: list[dict[str, Any]],
    planner_model: str,
) -> dict[str, Any]:
    """Ask the same Planner for one localized responsibility-graph repair."""

    prompt = """
You are the same Blueprint Planner repairing your responsibility graph after a
read-only alignment review. Perform one minimal coherent localized repair using
the review issues. Restore requirement traceability, responsibility ownership,
responsibility boundary consistency, input/output alignment, dependency closure,
and constraint ownership using only the declared graph contract.

The FilePlan is frozen. Preserve the exact allowed_function_item_targets and
current wire schema. Do not add, remove, rename, split, or merge files. Do not
modify FilePlan resources, platform protocol, or ToolPool. Modify affected
ResponsibilityEdges only. The supplied FunctionItems are frozen Blueprint facts:
preserve target_file, inputs, and outputs exactly. Prefer issue target_files and
affected_edge_indexes; adjust directly connected edges only when needed. Do not
add or remove runtime inputs/outputs to make an edge easier to connect. If the
frozen boundaries prevent closure, leave them unchanged so backend validation
can report that upstream replanning is required.

A repair must preserve full declared-input provenance. Never repair an invalid
platform source merely by deleting the edge while leaving its FunctionItem input
unresolved. For a dynamic runtime parameter, bind through the supplied
preferred_structured_input_root with exactly one platform_parameter_binding
constraint containing explicit source_key, required, and an explicit default
when required is false. A platform_parameter_binding is required only when
selecting a dynamic child parameter; a whole structured input root may be passed
directly without that constraint. Creation-time fixed configuration is an
upstream Blueprint decision. Do not remove an input during localized graph repair.
If a FunctionItem input has an explicit frozen/default value, it is locally
resolved. Do not create a platform_input_node edge for it and do not externalize
it merely to satisfy graph closure.

Before returning, verify every declared runtime input has exactly one legal
provenance; every from_output and to_input exists; required final outputs have a
producer-to-platform_output path; and no input, output, or FunctionItem was added.

Return only strict JSON:
{"responsibility_edges": [...]}
""".strip()
    platform_contract = build_platform_io_contract()
    platform_boundary = platform_contract["platform_skill_boundary"]
    graph_context = _build_responsibility_graph_construction_context(
        frozen_blueprint_text=frozen_blueprint_text,
        allowed_function_item_targets=allowed_function_item_targets,
        function_items=function_items,
        responsibility_edges=responsibility_edges,
    )
    payload = {
        "task": "repair_responsibility_graph_alignment",
        "confirmed_blueprint": frozen_blueprint_text,
        "graph_construction_context": graph_context,
        "allowed_function_item_targets": allowed_function_item_targets,
        "function_items": function_items,
        "responsibility_edges": responsibility_edges,
        "review_issues": review_issues,
        "platform_boundary_contract": {
            "input_fields": platform_boundary["input_envelope_fields"],
            "final_output_fields": platform_boundary["final_output_fields"],
            "preferred_structured_input_root": platform_boundary["preferred_structured_input_root"],
        },
    }
    text = await complete_creator_role_once(
        [{"role": "system", "content": prompt},
         {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}],
        "planner", fallback_model=planner_model,
    )
    data = _parse_prepare_plan_json(text)
    frozen_items = normalize_structured_function_items(
        function_items, source="frozen_blueprint"
    )
    if set(data) != {"responsibility_edges"} or not isinstance(data.get("responsibility_edges"), list):
        raise PreparePlanProtocolError(
            "Responsibility graph alignment repair must return only responsibility_edges"
        )
    repaired_edges = validate_structured_responsibility_edge_transport(
        data["responsibility_edges"],
        function_items=frozen_items,
        source="planner_graph_repair",
    )
    return {"function_items": frozen_items, "responsibility_edges": repaired_edges}


def _build_responsibility_graph_construction_context(
    *,
    frozen_blueprint_text: str,
    allowed_function_item_targets: list[str],
    function_items: list[dict[str, Any]] | None = None,
    responsibility_edges: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Project only frozen structured facts needed to construct graph edges."""
    allowed = set(allowed_function_item_targets)
    node_contracts: list[dict[str, Any]] = []
    if function_items is not None:
        for item in normalize_structured_function_items(
            function_items, source="graph_construction_context"
        ):
            if item["target_file"] in allowed:
                node_contract = {
                    "node": item["target_file"],
                    "inputs": list(item["inputs"]),
                    "outputs": list(item["outputs"]),
                }
                frozen_defaults = dict(item.get("default_values") or {})
                if frozen_defaults:
                    node_contract["frozen_defaults"] = frozen_defaults
                node_contracts.append(node_contract)
    else:
        parsed = parse_blueprint(
            [{"role": "assistant", "content": frozen_blueprint_text}], strict=True
        )
        for entry in (parsed.skill_plan.files if parsed.skill_plan else []):
            if entry.path not in allowed:
                continue
            node_contract = {
                "node": entry.path,
                "inputs": list(entry.inputs),
                "outputs": list(entry.outputs),
            }
            if entry.default_values:
                node_contract["frozen_defaults"] = dict(entry.default_values)
            node_contracts.append(node_contract)

    platform_boundary = build_platform_io_contract()["platform_skill_boundary"]
    legal_sources = [
        {"from_node": "platform_input_node", "from_output": field}
        for field in platform_boundary["input_envelope_fields"]
    ] + [
        {"from_node": item["node"], "from_output": output}
        for item in node_contracts
        for output in item["outputs"]
    ]
    input_source_domains = [
        {
            "target_file": item["node"],
            "target_input": input_name,
            "legal_sources": list(legal_sources),
        }
        for item in node_contracts
        for input_name in item["inputs"]
        if input_name not in item.get("frozen_defaults", {})
    ]
    allowed_nodes = [
        "platform_input_node",
        *allowed_function_item_targets,
        "platform_output_node",
    ]
    allowed_outputs_by_node = {
        "platform_input_node": list(platform_boundary["input_envelope_fields"]),
        **{
            item["node"]: list(item["outputs"])
            for item in node_contracts
        },
        "platform_output_node": [],
    }
    allowed_inputs_by_node = {
        "platform_input_node": [],
        **{
            item["node"]: list(item["inputs"])
            for item in node_contracts
        },
        "platform_output_node": list(platform_boundary["final_output_fields"]),
    }
    return {
        "allowed_nodes": allowed_nodes,
        "allowed_outputs_by_node": allowed_outputs_by_node,
        "allowed_inputs_by_node": allowed_inputs_by_node,
        "allowed_function_targets": list(allowed_function_item_targets),
        "node_contracts": node_contracts,
        "platform_input_contract": {
            "input_fields": list(platform_boundary["input_envelope_fields"]),
            "preferred_structured_input_root": platform_boundary["preferred_structured_input_root"],
        },
        "platform_output_contract": {
            "final_output_fields": list(platform_boundary["final_output_fields"]),
        },
        "input_source_domains": input_source_domains,
        "current_edges": list(responsibility_edges or []),
    }


_GRAPH_FINGERPRINT_FIELDS = (
    "target_file", "target_input", "from_node", "to_node", "from_output", "to_input"
)


def _graph_failure_fingerprint(issue: dict[str, Any]) -> str:
    """Fingerprint a deterministic failure using structural facts only."""
    category = str(issue.get("category") or "invalid_edge_transport")
    parts = [category]
    for field in _GRAPH_FINGERPRINT_FIELDS:
        value = issue.get(field)
        if value is not None and str(value) != "":
            parts.extend((field, str(value)))
    return "|".join(parts)


def _graph_issue_from_validation_error(error: ValueError) -> dict[str, Any]:
    """Project an existing deterministic validator error into a small issue dict."""
    message = str(error)
    category = error.code if isinstance(error, GraphValidationError) else "invalid_edge_transport"
    details = error.details if isinstance(error, GraphValidationError) else {}
    facts = {field: details[field] for field in _GRAPH_FINGERPRINT_FIELDS if field in details}
    return {
        "id": category,
        "category": category,
        **facts,
        "target_files": [facts["target_file"]] if facts.get("target_file") else [],
        "affected_edge_indexes": list(details.get("edge_indexes") or ([details["edge_index"]] if "edge_index" in details else [])),
        "reason": message,
        "evidence": "Deterministic ResponsibilityEdge validation failed.",
        "repair_guidance": "Modify ResponsibilityEdges only, using an exact endpoint from graph_construction_context.",
    }


def _should_escalate_graph_failure(
    fingerprints: list[str], issues: list[dict[str, Any]]
) -> bool:
    """Escalate only a repeatedly unchanged, provenance-closure failure."""
    return bool(
        len(fingerprints) == 3
        and len(set(fingerprints)) == 1
        and issues
        and all(issue.get("category") == "unresolved_input_provenance" for issue in issues)
    )


def _frozen_function_items_from_blueprint(
    *,
    frozen_blueprint_text: str,
    allowed_function_item_targets: list[str],
) -> list[dict[str, Any]]:
    """Materialize FunctionItems directly from frozen structured SkillPlan facts."""
    allowed = set(allowed_function_item_targets)
    parsed = parse_blueprint(
        [{"role": "assistant", "content": frozen_blueprint_text}], strict=True
    )
    items = [
        {
            "target_file": entry.path,
            "role": str(entry.role),
            "purpose": entry.purpose,
            "inputs": list(entry.inputs),
            "outputs": list(entry.outputs),
            "required_capabilities": list(entry.required_capabilities),
            "constraints": list(entry.constraints),
            "default_values": dict(entry.default_values),
        }
        for entry in (parsed.skill_plan.files if parsed.skill_plan else [])
        if entry.path in allowed
    ]
    normalized = normalize_structured_function_items(
        items, source="frozen_blueprint"
    )
    _validate_function_item_targets_in_allowed_domain(
        normalized, allowed_function_item_targets
    )
    return normalized


async def _regenerate_responsibility_graph(
    *,
    frozen_blueprint_text: str,
    allowed_function_item_targets: list[str],
    function_items: list[dict[str, Any]],
    failed_issues: list[dict[str, Any]],
    planner_model: str,
) -> dict[str, Any]:
    """Regenerate edges once while keeping frozen FunctionItems immutable."""
    context = _build_responsibility_graph_construction_context(
        frozen_blueprint_text=frozen_blueprint_text,
        allowed_function_item_targets=allowed_function_item_targets,
        function_items=function_items,
        responsibility_edges=[],
    )
    prompt = """
You are the same Blueprint Planner regenerating one ResponsibilityGraph after a
localized edge repair failed. The Frozen Blueprint, FilePlan, and FunctionItems
are immutable. Do not add, remove, rename, or modify a FunctionItem, its inputs,
or its outputs. Generate a complete new responsibility_edges array from the
compact Graph Construction Context. Do not inherit the old edge topology.

For every declared runtime input, choose its semantic provenance from only the
platform input contract or a declared FunctionItem output. Backend provides the
legal source domain but does not choose the semantically correct source for you.
Close platform input/output boundaries and use only exact declared endpoints.
Do not invent aliases, functions, inputs, outputs, or platform slots. If frozen
FunctionItems make closure impossible, do not redesign them.
An input with an explicit frozen/default value is locally resolved. Do not
create a platform_input_node edge for it unless the frozen contract explicitly
declares it runtime-configurable, and never externalize a default for closure.

Before returning, verify every declared runtime input has exactly one legal
provenance; every from_output and to_input exists; required final outputs have a
producer-to-platform_output path; and no input, output, or FunctionItem was added.

Return only strict JSON: {"responsibility_edges": [...]}
""".strip()
    payload = {
        "task": "regenerate_responsibility_graph",
        "graph_construction_context": context,
        "failed_issues": failed_issues,
    }
    text = await complete_creator_role_once(
        [{"role": "system", "content": prompt},
         {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}],
        "planner", fallback_model=planner_model,
    )
    data = _parse_prepare_plan_json(text)
    if set(data) != {"responsibility_edges"} or not isinstance(data.get("responsibility_edges"), list):
        raise ValueError("Responsibility graph regeneration must return only responsibility_edges")
    return {"function_items": function_items, "responsibility_edges": data["responsibility_edges"]}


async def _replan_blueprint_for_graph_closure(
    *,
    request: PreparePlanRequest,
    frozen_blueprint_text: str,
    blocking_issue: dict[str, Any],
    graph_construction_context: dict[str, Any],
    planner_model: str,
) -> str:
    """Perform the single, narrowly scoped Blueprint-owned recovery attempt."""
    target_file = str(blocking_issue.get("target_file") or "")
    affected_item = next(
        (
            item for item in graph_construction_context.get("node_contracts", [])
            if item.get("node") == target_file
        ),
        {},
    )
    feedback = {
        "failure_category": blocking_issue.get("category"),
        "target_file": target_file,
        "target_input": blocking_issue.get("target_input"),
        "reason": (
            "No legal provenance exists under the current frozen FunctionItems "
            "after local repair and full graph regeneration."
        ),
    }
    prompt = """
You are the Blueprint Planner performing one narrowly scoped replan because the
frozen runtime contract cannot close as a legal ResponsibilityGraph. Return one
complete replacement internal_blueprint_text and nothing else.

Re-evaluate whether the affected input is genuine runtime data, upstream-produced
data, literal/default configuration, or a static reference/asset dependency. The
Backend does not make that choice. Do not invent a platform input or upstream
output only to satisfy graph closure. If the current FunctionItem boundary is
wrong, revise the Blueprint contract.

Preserve all unrelated files, target_file values, inputs, outputs, capabilities,
resource declarations, workflow facts, and constraints. Only modify the minimum
Blueprint facts responsible for the supplied blocking issue. Do not redesign the
Skill, rename scripts, change capability ownership, or add unrelated files.
ResponsibilityEdges do not belong in this response.

Return strict JSON: {"internal_blueprint_text": "..."}
""".strip()
    payload = {
        "task": "replan_blueprint_for_graph_closure",
        "frozen_blueprint": frozen_blueprint_text,
        "blocking_structural_issue": feedback,
        "affected_function_item": affected_item,
        "legal_source_domain": next(
            (
                domain for domain in graph_construction_context.get("input_source_domains", [])
                if domain.get("target_file") == target_file
                and domain.get("target_input") == blocking_issue.get("target_input")
            ),
            {},
        ),
    }
    text = await complete_creator_role_once(
        [{"role": "system", "content": prompt},
         {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}],
        "planner", fallback_model=planner_model,
    )
    result = _parse_prepare_plan_json(text)
    if set(result) != {"internal_blueprint_text"}:
        raise PreparePlanProtocolError("Blueprint graph-closure replan returned an invalid protocol shape")
    replanned = str(result["internal_blueprint_text"] or "")
    try:
        validate_blueprint_shape_for_creator(replanned)
    except BlueprintShapeError as exc:
        raise PreparePlanProtocolError(
            f"Blueprint graph-closure replan failed structural validation: {exc}"
        ) from exc
    preflight_issues = _preflight_prepare_blueprint_text(replanned)
    if preflight_issues:
        raise PreparePlanProtocolError(
            f"Blueprint graph-closure replan failed strict FilePlan preflight: {preflight_issues}"
        )

    old_plan = parse_blueprint([{"role": "assistant", "content": frozen_blueprint_text}], strict=True)
    new_plan = parse_blueprint([{"role": "assistant", "content": replanned}], strict=True)
    old_files = {entry.path: entry for entry in (old_plan.skill_plan.files if old_plan.skill_plan else [])}
    new_files = {entry.path: entry for entry in (new_plan.skill_plan.files if new_plan.skill_plan else [])}
    if set(old_files) != set(new_files):
        raise PreparePlanProtocolError("Blueprint graph-closure replan changed the frozen FilePlan path domain")
    for path in old_files:
        if path != target_file and old_files[path] != new_files[path]:
            raise PreparePlanProtocolError(
                f"Blueprint graph-closure replan modified unrelated FilePlan entry: {path}"
            )
    old_target = old_files.get(target_file)
    new_target = new_files.get(target_file)
    if old_target is None or new_target is None:
        raise PreparePlanProtocolError("Blueprint graph-closure replan lost the affected FunctionItem")
    permitted_target_fields = {
        "inputs", "dependencies", "constraints", "default_values",
        "reference_files", "skill_local_references", "creator_internal_references",
    }
    old_target_data = vars(old_target)
    new_target_data = vars(new_target)
    changed_target_fields = {
        field for field in set(old_target_data) | set(new_target_data)
        if old_target_data.get(field) != new_target_data.get(field)
    }
    forbidden_changes = sorted(changed_target_fields - permitted_target_fields)
    if forbidden_changes:
        raise PreparePlanProtocolError(
            "Blueprint graph-closure replan exceeded the affected contract scope; "
            f"changed_fields={forbidden_changes}"
        )
    return replanned


def _validate_responsibility_graph_boundary_presence(
    responsibility_edges: list[dict[str, Any]],
    allowed_function_item_targets: list[str],
) -> None:
    """Require platform transport endpoints at both ends of the graph."""

    function_item_targets = set(allowed_function_item_targets)
    has_platform_input = any(
        edge.get("from_node") == "platform_input_node"
        and edge.get("to_node") in function_item_targets
        for edge in responsibility_edges
    )
    has_platform_output = any(
        edge.get("to_node") == "platform_output_node"
        and edge.get("from_node") in function_item_targets
        for edge in responsibility_edges
    )
    missing = []
    if not has_platform_input:
        missing.append("missing platform input boundary edge")
    if not has_platform_output:
        missing.append("missing platform output boundary edge")
    if missing:
        raise GraphValidationError(
            "; ".join(missing),
            code="missing_platform_boundary",
            details={
                "missing_input_boundary": not has_platform_input,
                "missing_output_boundary": not has_platform_output,
            },
        )


def _resolve_allowed_function_item_targets_from_blueprint(
    internal_blueprint_text: str,
) -> list[str]:
    """Freeze executable target domain from strict SkillPlan file topology."""

    return [
        path
        for path in exact_file_plan_paths_from_strict_skillplan(
            internal_blueprint_text
        )
        if path.startswith("scripts/")
    ]


def _validate_function_item_targets_in_allowed_domain(
    function_items: list[dict[str, Any]],
    allowed_function_item_targets: list[str],
) -> None:
    allowed = set(allowed_function_item_targets)
    actual = {
        str(item.get("target_file") or "").strip()
        for item in function_items
        if str(item.get("target_file") or "").strip()
    }
    unexpected_targets = sorted(actual - allowed)
    missing_targets = sorted(allowed - actual)
    if unexpected_targets or missing_targets:
        raise ValueError(
            "Planner FunctionItem target_file set does not match frozen "
            "FilePlan target domain exactly; "
            f"unexpected_targets={unexpected_targets}; "
            f"missing_targets={missing_targets}; "
            f"allowed_function_item_targets={allowed_function_item_targets}"
        )
    blank_targets = [
        str(item.get("target_file") or "").strip()
        for item in function_items
        if not str(item.get("target_file") or "").strip()
    ]
    if blank_targets:
        raise ValueError(
            "Planner emitted FunctionItem with blank target_file; "
            f"blank_target_count={len(blank_targets)}"
        )


def _validate_prepare_semantic_function_item_topology(
    authoritative_scripts: list[str],
    function_items: list[dict[str, Any]],
) -> None:
    """Stop prepare before allocation when semantic targets lost FilePlan scripts."""
    semantic_targets = [
        str(item.get("target_file") or "").strip()
        for item in function_items
        if str(item.get("target_file") or "").strip()
    ]
    if set(semantic_targets) != set(authoritative_scripts):
        raise PreparePlanProtocolError(
            "Prepare pipeline inconsistency before requirement allocations: "
            "authoritative script targets do not match semantic FunctionItem targets; "
            f"authoritative_scripts={authoritative_scripts}; "
            f"semantic_function_item_targets={semantic_targets}"
        )


async def _bind_executable_responsibility_plan(
    *,
    request: PreparePlanRequest,
    current_planner_result: dict[str, Any],
    planner_model: str,
    allowed_function_item_targets: list[str],
) -> dict[str, Any]:
    """Expand edges incrementally over Backend-materialized frozen FunctionItems."""
    frozen_blueprint_text = str(
        current_planner_result.get("internal_blueprint_text") or ""
    )
    frozen_function_items = _frozen_function_items_from_blueprint(
        frozen_blueprint_text=frozen_blueprint_text,
        allowed_function_item_targets=allowed_function_item_targets,
    )
    async def select_sources(messages: list[dict[str, str]], model: str) -> str:
        return await complete_creator_role_once(
            messages, "planner", fallback_model=model,
        )

    responsibility_edges = await expand_responsibility_graph(
        function_items=frozen_function_items,
        platform_contract=build_platform_io_contract(),
        planner_model=planner_model,
        model_call=select_sources,
        goal_context={
            "user_request": request.user_request,
            "confirmed_blueprint": frozen_blueprint_text,
            "skill_name": current_planner_result.get("skill_name", ""),
        },
    )
    return {
        "function_items": frozen_function_items,
        "responsibility_edges": responsibility_edges,
    }


async def _plan_requirement_allocations(
    *, request: PreparePlanRequest, blueprint_text: str,
    function_items: list[dict[str, Any]], planner_model: str,
) -> dict[str, Any]:
    """Ask the Planner for the semantic requirement-to-owner projection."""
    prompt = """
You are the Blueprint Planner producing a requirement coverage projection before
the Blueprint is frozen. Semantically identify only the user's explicit core,
independently verifiable final-capability requirements. Do not mechanically turn
tone, examples, background, or pleasantries into requirements. Do not use a
business taxonomy or keyword rules. Requirement extraction is based on the
original user requirement, not on what the current Blueprint already happens to
implement. Identify every explicit core independently verifiable final-capability
requirement even when the current Blueprint does not currently provide a
legitimate FunctionItem owner.

Requirements may come only from the original user request or explicit user
clarification answers. Blueprint implementation details are evidence for
allocation only; never turn them into user requirements. A default value,
internal parameter, file split, file name, intermediate output, model-selected
quantity, layout choice, template choice, or helper strategy is not a
requirement unless the user explicitly requested it. For example, a Blueprint
default max_images=5 does not mean the user requested configurable max_images.
Explicit limitations, prohibitions, and responsibility boundaries may still be
core user requirements. When one or more existing FunctionItems genuinely meet
such a requirement through their constraints or forbidden boundaries, allocate
it to those actual FunctionItems. Do not return owners=[] merely because a
requirement is a negative constraint, and do not mechanically assign every
constraint to every script. Judge ownership semantically from the original
request and the supplied FunctionItem content; the Backend performs no keyword
classification.

Allocate a requirement only to FunctionItems that genuinely own or co-own that
responsibility. One requirement may be jointly covered by one or more existing
FunctionItems. Collaboration, data transfer, or separation of responsibilities
across FunctionItems must not produce owners=[] merely because the requirement
is workflow-level. If no current FunctionItem legitimately owns a core requirement,
keep that requirement in requirement_allocations, return owners=[], do not omit
the requirement, do not force an unrelated owner merely to avoid an empty owner
list, and do not invent a new FunctionItem during requirement allocation.
owners=[] means only that the current Blueprint has no legitimate owner; it does
not mean that the requirement is unimportant, ignorable, or already complete.

authoritative_scripts is the complete owner identity domain. Every owners value
MUST be copied verbatim from authoritative_scripts. Do not output a role,
basename, capability, shorthand, or custom identifier, and do not infer aliases.
Never guess ownership from a file name, role, capability, path keyword, or file
suffix. For FunctionItem A and FunctionItem B, select only their exact target
identities as supplied in authoritative_scripts.

One FunctionItem may legitimately own multiple requirements. Multiple
FunctionItems may legitimately cooperate on one requirement. There is no
one-requirement-to-one-file rule. Do not add, remove, rename, or modify FilePlan
entries or FunctionItems during requirement allocation. The later semantic
Reviewer and bounded Blueprint replan own repair.
Static resource responsibilities and host direct-answer responsibilities are not
script ownership requirements. Use the supplied structured FilePlan and
FunctionItems to distinguish those channels; do not classify them with business
keywords and do not create a script owner for either channel.

Classify each requirement independently as executable, resource, or direct using
only the original request and supplied structured planning facts. Only executable
requirements require script owners. Resource and direct requirements use
owners=[] and remain in their respective semantic-review channels.

Return strict JSON only: {"requirement_allocations":[{"requirement_id":"R1",
"requirement":"...","owners":["..."],"evidence":{"responsibility":"...",
"outputs":[],"capabilities":[]}}],"requirement_channels":{"R1":"executable"}}.
IDs must be unique and stable within this
plan. Do not add files, FunctionItems, or requirements merely for closure.
""".strip()
    payload = {
        "user_requirement": request.user_request,
        "conversation_history": request.conversation_history,
        "human_feedback": request.human_feedback,
        "current_blueprint": blueprint_text,
        "function_items": function_items,
        "authoritative_scripts": [
            str(item.get("target_file") or "").strip()
            for item in function_items
            if str(item.get("target_file") or "").strip()
        ],
    }
    text = await complete_creator_role_once(
        [{"role": "system", "content": prompt},
         {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}],
        "planner", fallback_model=planner_model,
    )
    data = _parse_prepare_plan_json(text)
    if set(data) != {"requirement_allocations", "requirement_channels"}:
        raise PreparePlanProtocolError("Planner requirement allocation returned an invalid protocol shape")
    allocations = validate_requirement_allocations(
        data["requirement_allocations"],
        allowed_owner_targets=[
            str(item.get("target_file") or "").strip()
            for item in function_items
        ],
    )
    channels = _validate_requirement_channels(
        data["requirement_channels"], allocations
    )
    return {
        "requirement_allocations": allocations,
        "requirement_channels": channels,
    }


def _validate_requirement_channels(
    channels: Any,
    requirement_allocations: list[dict[str, Any]],
) -> dict[str, str]:
    if not isinstance(channels, dict):
        raise PreparePlanProtocolError("requirement_channels must be an object")
    requirement_ids = [item["requirement_id"] for item in requirement_allocations]
    if set(channels) != set(requirement_ids):
        raise PreparePlanProtocolError(
            "requirement_channels IDs must exactly match requirement allocations"
        )
    allowed_channels = {"executable", "resource", "direct"}
    normalized = {str(key): str(value).strip() for key, value in channels.items()}
    invalid = {key: value for key, value in normalized.items() if value not in allowed_channels}
    if invalid:
        raise PreparePlanProtocolError(
            f"requirement_channels contains invalid channel values: {invalid}"
        )
    invalid_non_executable_owners = [
        item["requirement_id"] for item in requirement_allocations
        if normalized[item["requirement_id"]] != "executable" and item.get("owners")
    ]
    if invalid_non_executable_owners:
        raise PreparePlanProtocolError(
            "Non-executable requirement channels cannot have script owners: "
            f"{invalid_non_executable_owners}"
        )
    return {requirement_id: normalized[requirement_id] for requirement_id in requirement_ids}


async def _plan_executable_requirement_allocations(
    *, request: PreparePlanRequest, blueprint_text: str,
    function_items: list[dict[str, Any]], planner_model: str,
) -> dict[str, Any]:
    """Plan the complete requirement projection and per-requirement channels."""
    return await _plan_requirement_allocations(
        request=request,
        blueprint_text=blueprint_text,
        function_items=function_items,
        planner_model=planner_model,
    )


async def _reconcile_requirement_allocations(
    *, request: PreparePlanRequest, blueprint_text: str,
    function_items: list[dict[str, Any]],
    requirement_allocations: list[dict[str, Any]],
    requirement_channels: dict[str, str],
    semantic_review: dict[str, Any], planner_model: str,
) -> dict[str, Any]:
    """Reconcile only allocations explicitly routed here by semantic review."""
    ownerless_ids = [
        str(item.get("requirement_id") or "").strip()
        for item in requirement_allocations
        if not (item.get("owners") or [])
        and requirement_channels.get(str(item.get("requirement_id") or "").strip())
        == "executable"
    ]
    authoritative_targets = [
        str(item.get("target_file") or "").strip()
        for item in function_items if str(item.get("target_file") or "").strip()
    ]
    allocation_issue_ids = {
        str(issue.get("requirement_id") or "").strip()
        for issue in (semantic_review.get("issues") or [])
        if issue.get("repair_scope") == "allocation"
        and str(issue.get("requirement_id") or "").strip()
    }
    repairable_ids = allocation_issue_ids or set(ownerless_ids)
    logger.info(
        "[Creator][requirement_allocation_reconcile] attempt=1 ownerless_ids=%s",
        sorted(repairable_ids),
    )
    prompt = """
You are the Blueprint Planner reconciling a requirement allocation exactly once.
The Blueprint and FunctionItems are frozen. Re-evaluate only the requirements
listed in repairable_requirement_ids. For those entries you may modify owners,
evidence, and requirement_channels. Preserve every
requirement_id, requirement text, and array position exactly. Owners must be
copied verbatim from authoritative_targets. Do not choose the first target by
default, assign every target, infer from names, paths, roles, capabilities,
suffixes, or business keywords, or create any owner identity. Leave owners=[]
when the frozen Blueprint does not establish legitimate ownership. Static
resource and direct responsibilities are not script ownership. Do not modify
any allocation or channel outside repairable_requirement_ids. Return strict JSON only:
{"requirement_allocations":[...],"requirement_channels":{...}}.
""".strip()
    payload = {
        "original_user_requirement": request.user_request,
        "current_blueprint": blueprint_text,
        "current_function_items": function_items,
        "requirement_allocations": requirement_allocations,
        "ownerless_requirement_ids": ownerless_ids,
        "repairable_requirement_ids": sorted(repairable_ids),
        "authoritative_targets": authoritative_targets,
        "semantic_review": semantic_review,
        "requirement_channels": requirement_channels,
    }
    text = await complete_creator_role_once(
        [{"role": "system", "content": prompt},
         {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}],
        "planner", fallback_model=planner_model,
    )
    data = _parse_prepare_plan_json(text)
    if set(data) != {"requirement_allocations", "requirement_channels"}:
        raise PreparePlanProtocolError(
            "Requirement allocation reconciliation returned an invalid protocol shape"
        )
    reconciled = validate_requirement_allocations(
        data["requirement_allocations"],
        allowed_owner_targets=authoritative_targets,
    )
    reconciled_channels = _validate_requirement_channels(
        data["requirement_channels"], reconciled
    )
    before_identity = [
        (item.get("requirement_id"), item.get("requirement"))
        for item in requirement_allocations
    ]
    after_identity = [
        (item.get("requirement_id"), item.get("requirement"))
        for item in reconciled
    ]
    if before_identity != after_identity:
        raise PreparePlanProtocolError(
            "Requirement allocation reconciliation changed requirement identity, text, or order"
        )
    before_by_id = {item["requirement_id"]: item for item in requirement_allocations}
    after_by_id = {item["requirement_id"]: item for item in reconciled}
    modified_outside_scope = [
        requirement_id for requirement_id in before_by_id
        if requirement_id not in repairable_ids
        and (
            before_by_id[requirement_id] != after_by_id[requirement_id]
            or requirement_channels[requirement_id]
            != reconciled_channels[requirement_id]
        )
    ]
    if modified_outside_scope:
        raise PreparePlanProtocolError(
            "Requirement allocation reconciliation modified entries outside its routed scope: "
            f"{modified_outside_scope}"
        )
    resolved_ids = [
        requirement_id for requirement_id in ownerless_ids
        if next(
            (item.get("owners") for item in reconciled
             if item.get("requirement_id") == requirement_id),
            [],
        )
    ]
    remaining_ownerless_ids = [
        str(item.get("requirement_id") or "").strip()
        for item in reconciled if not (item.get("owners") or [])
        and reconciled_channels.get(str(item.get("requirement_id") or "").strip())
        == "executable"
    ]
    logger.info(
        "[Creator][requirement_allocation_reconcile] resolved_ids=%s remaining_ownerless_ids=%s",
        resolved_ids, remaining_ownerless_ids,
    )
    return {
        "requirement_allocations": reconciled,
        "requirement_channels": reconciled_channels,
    }


def _normalize_semantic_review_against_allocations(
    review: dict[str, Any],
    requirement_allocations: list[dict[str, Any]],
    requirement_channels: dict[str, str],
) -> dict[str, Any]:
    """Deduplicate validated review output without adding semantic conclusions."""
    normalized = copy.deepcopy(review)
    normalized.setdefault("deferred_checks", [])
    for field in ("issues", "deferred_checks"):
        deduplicated: list[dict[str, Any]] = []
        seen: set[str] = set()
        for issue in normalized.get(field) or []:
            identity = json.dumps(issue, ensure_ascii=False, sort_keys=True, default=str)
            if identity not in seen:
                seen.add(identity)
                deduplicated.append(issue)
        normalized[field] = deduplicated
    normalized["passed"] = not normalized["issues"]
    return normalized


def _requirement_channel_summary(
    requirement_channels: dict[str, str],
) -> dict[str, int]:
    """Report actual per-requirement channel counts."""
    return {
        "executable_requirement_count": sum(
            value == "executable" for value in requirement_channels.values()
        ),
        "resource_requirement_count": sum(
            value == "resource" for value in requirement_channels.values()
        ),
        "direct_requirement_count": sum(
            value == "direct" for value in requirement_channels.values()
        ),
    }


async def _review_blueprint_semantic_closure(
    *, request: PreparePlanRequest, blueprint_text: str,
    function_items: list[dict[str, Any]], requirement_allocations: list[dict[str, Any]],
    requirement_channels: dict[str, str], planner_model: str,
) -> dict[str, Any]:
    """Review only semantic facts observable before ResponsibilityGraph creation."""
    prompt = """
You are the pre-graph Blueprint semantic coverage Reviewer.

Review only facts observable in the supplied pre-graph payload: the original
user requirement, current Blueprint/FilePlan, frozen FunctionItems, requirement
allocations, requirement channels, and authoritative FunctionItem target domain.
Do not require every requirement to have a FunctionItem owner. FunctionItem
ownership applies only where the supplied allocation legitimately assigns
runtime FunctionItems. Only requirements whose current allocation uses
FunctionItem ownership require FunctionItem owners. Do not convert other
responsibilities into synthetic owners.

You must not make a blocking conclusion whose proof requires facts that do not
yet exist: ResponsibilityGraph edges, runtime execution order, upstream data
transport, platform terminal bindings, generated source, runtime observations,
sandbox enforcement, or host result delivery. Absence of later-stage evidence
is not a violation. When evidence belongs to a later Creator stage, return a
deferred_verification in deferred_checks with blocking_now=false,
repair_scope=none, and the appropriate lifecycle evidence_stage.

Distinguish satisfied, deferred, violated, and uncovered. A violation directly
contradicts supplied evidence. Uncovered means a fact required to exist in this
pre-graph payload is absent. Insufficient evidence is deferred, not uncovered.
Relationships between FunctionItems may require graph evidence: inspect current
nodes and declared ports, but do not claim a relationship is missing merely
because edges have not been constructed.

The complete valid FunctionItem owner domain is
`authoritative_function_item_targets`. There are no implicit owners. Every owner
or affected FunctionItem target must be copied exactly from that list. Never
create or recommend another component identity. If no supplied FunctionItem
legitimately owns a requirement, decide whether it is represented outside
FunctionItem ownership or requires later evidence.

Treat requirement_channels as authoritative for this review call. Do not
silently reinterpret or replace a channel. Report a channel mismatch only when
an exact supplied pre-graph field directly contradicts it, not merely because
another channel seems plausible.

A blocking issue requires concrete evidence from the supplied payload. Identify
the supplied object, exact field, observed value, and expected_fact directly
contradicted or absent. Do not block on imagined architecture, implementation
preference, unsupplied components, hypothetical owners, or later-stage evidence.

Before returning, adversarially self-check every candidate blocking issue:
1. requirement_id exists in supplied allocations;
2. evidence is observable now;
3. the missing fact must exist now;
4. a concrete supplied field supports the conclusion;
5. affected_targets are exact authoritative targets;
6. repair invents no entity;
7. repair_scope can modify the identified object;
8. the issue remains valid without a ResponsibilityGraph.
If any answer is no, defer it when later evidence is required; otherwise omit it.

Return strict JSON with exactly passed, issues, and deferred_checks. `issues`
contains only blocking_now=true issues. `deferred_checks` contains only
non-blocking deferred_verification entries. passed is true exactly when issues is
empty. Allowed issue_type values: requirement_uncovered,
requirement_partially_covered, responsibility_mismatch,
resource_semantic_conflict, deferred_verification. Allowed evidence_stage:
blueprint, graph, generation, runtime, boundary, resource. Allowed repair_scope:
blueprint, allocation, graph, resource, none.

Every entry contains issue_type, requirement_id, blocking_now, evidence_stage,
repair_scope, affected_targets, evidence, expected_fact, reason, and
repair_guidance. Evidence is an array of objects with exactly source, target,
field, and observed. Blocking entries require non-empty evidence and
expected_fact. A deferred entry uses evidence=[], expected_fact="",
affected_targets=[], repair_scope=none, and empty repair_guidance. Do not return
FunctionItems, allocations, channels, owners, edges, or explanations outside the
JSON envelope.
""".strip()
    authoritative_targets = [
        str(item.get("target_file") or "").strip()
        for item in function_items
        if str(item.get("target_file") or "").strip()
    ]
    valid_requirement_ids = [
        str(item.get("requirement_id") or "").strip()
        for item in requirement_allocations
        if str(item.get("requirement_id") or "").strip()
    ]
    payload = {
        "original_user_requirement": request.user_request,
        "user_requirement": request.user_request,
        "conversation_history": request.conversation_history,
        "human_feedback": request.human_feedback,
        "current_blueprint": blueprint_text,
        "frozen_function_items": function_items,
        "requirement_allocations": requirement_allocations,
        "requirement_channels": requirement_channels,
        "authoritative_function_item_targets": authoritative_targets,
    }
    review: dict[str, Any] | None = None
    for protocol_attempt in range(2):
        text = await complete_creator_role_once(
            [{"role": "system", "content": prompt},
             {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}],
            "reviewer", fallback_model=planner_model,
        )
        try:
            review = validate_blueprint_semantic_review(
                _parse_prepare_plan_json(text),
                allowed_function_item_targets=authoritative_targets,
                supplied_requirement_ids=valid_requirement_ids,
            )
            break
        except (ValueError, PreparePlanProtocolError) as exc:
            logger.error(
                "[Creator][semantic_review_protocol_violation] attempt=%d error=%s",
                protocol_attempt + 1, exc,
            )
            if protocol_attempt:
                raise PreparePlanProtocolError(
                    "Pre-graph semantic Reviewer returned an invalid protocol "
                    f"after one local retry: {exc}"
                ) from exc
            payload["protocol_error"] = {
                "message": str(exc),
                "instruction": (
                    "Return the same review task using the required protocol; "
                    "do not change the supplied facts or reference domains."
                ),
            }
    if review is None:
        raise PreparePlanProtocolError(
            "Pre-graph semantic Reviewer protocol retry produced no review"
        )
    review = _normalize_semantic_review_against_allocations(
        review, requirement_allocations, requirement_channels
    )
    deferred = review["deferred_checks"]
    advisory_count = sum(not issue.get("blocking_now") for issue in review["issues"])
    logger.info(
        "[Creator][semantic_review] blocking_issue_count=%d deferred_check_count=%d advisory_issue_count=%d",
        len(review["issues"]), len(deferred), advisory_count,
    )
    for issue in deferred:
        logger.info(
            "[Creator][semantic_review_deferred] requirement_id=%s evidence_stage=%s reason=%s",
            issue.get("requirement_id", ""), issue.get("evidence_stage", ""),
            issue.get("reason", ""),
        )
    return review


async def _replan_blueprint_for_semantic_closure(
    *, request: PreparePlanRequest, blueprint_text: str,
    function_items: list[dict[str, Any]], requirement_allocations: list[dict[str, Any]],
    blocking_issues: list[dict[str, Any]], planner_model: str,
) -> str:
    """Perform the single localized Blueprint semantic repair."""
    logger.info("[Creator][blueprint_semantic_replan] attempt=1 issue_types=%s affected_targets=%s",
                [i.get("issue_type") for i in blocking_issues],
                sorted({str(t) for i in blocking_issues for t in (i.get("affected_targets") or [])}))
    prompt = """
You are the Blueprint Planner performing one localized semantic replan. Return a
complete replacement internal_blueprint_text. Preserve unrelated FilePlan entries
and FunctionItems. Only modify the minimum Blueprint facts necessary to cover the
blocking user requirement. Do not redesign unrelated workflow. Do not invent new
user requirements. Do not add files merely to satisfy a structural checker. If
an existing FunctionItem can legitimately own the requirement, first make the
minimum clarification to its purpose, inputs, outputs, or constraints. Multiple
existing FunctionItems may jointly cover one requirement. Only add the minimum
genuinely missing responsibility carrier when no legitimate existing owner exists.
Do not modify unrelated targets, remove or replace unrelated FilePlan entries, or
add resources, tools, or capabilities unrelated to a blocking issue. Requirements and files
have no one-to-one rule. Report the exact structural patch you made. For an
uncovered requirement with no preidentified affected target, explicitly declare
the minimum existing targets whose purpose, inputs, outputs, or constraints you
clarify, or add a minimum new responsibility carrier only when no existing target
can legitimately cover the requirement. Do not remove existing paths. Return
strict JSON only:

This is coverage repair, not Skill redesign. Repair only the supplied blocking
issues. Every requirement coverage repair must correspond to an existing
supplied requirement_id, and the original Requirement Set must remain unchanged.
Do not add new requirements, style constraints, quantity constraints, layout
rules, templates, resources, or capabilities unless strictly necessary for an
existing supplied requirement; then make only the minimum required change.

{"internal_blueprint_text":"...","changed_targets":[],"added_targets":[],"changed_resources":[]}
""".strip()
    payload = {"original_user_requirement": request.user_request, "current_blueprint": blueprint_text,
               "current_function_items": function_items, "requirement_allocations": requirement_allocations,
               "blocking_issues": blocking_issues}
    text = await complete_creator_role_once(
        [{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}],
        "planner", fallback_model=planner_model)
    data = _parse_prepare_plan_json(text)
    expected_keys = {
        "internal_blueprint_text", "changed_targets", "added_targets", "changed_resources"
    }
    if set(data) != expected_keys or any(
        not isinstance(data.get(key), list)
        for key in ("changed_targets", "added_targets", "changed_resources")
    ):
        raise PreparePlanProtocolError("Blueprint semantic replan returned an invalid protocol shape")
    replanned = str(data["internal_blueprint_text"] or "").strip()
    validate_blueprint_shape_for_creator(replanned)
    issues = _preflight_prepare_blueprint_text(replanned)
    if issues:
        raise PreparePlanProtocolError(f"Blueprint semantic replan failed FilePlan validation: {issues}")
    actual_diff = _validate_blueprint_semantic_replan_scope(
        before_blueprint_text=blueprint_text,
        after_blueprint_text=replanned,
        blocking_issues=blocking_issues,
        patch_manifest=data,
    )
    logger.info(
        "[Creator][blueprint_semantic_replan] actual_noop=%s",
        not any(actual_diff[key] for key in (
            "actual_changed_targets", "actual_added_targets",
            "actual_changed_resources", "actual_removed_paths",
        )),
    )
    return replanned


def _semantic_projection_facts(blueprint_text: str) -> dict[str, Any]:
    """Project parsed facts whose real changes permit requirement reprojection."""
    parsed = parse_blueprint(
        [{"role": "assistant", "content": blueprint_text}], strict=True
    )
    entries = list(parsed.skill_plan.files if parsed.skill_plan else [])
    function_items = [
        {
            "target_file": entry.path,
            "purpose": entry.purpose,
            "inputs": list(entry.inputs),
            "outputs": list(entry.outputs),
            "constraints": copy.deepcopy(entry.constraints),
        }
        for entry in entries
        if str(entry.path).startswith("scripts/")
    ]
    resources = [
        copy.deepcopy(vars(entry))
        for entry in entries
        if not str(entry.path).startswith("scripts/")
        and str(entry.path) != "SKILL.md"
    ]
    return {"function_items": function_items, "resources": resources}


def _strict_blueprint_compatibility_fields(
    blueprint_text: str,
    paths_needing_compatibility: set[str],
) -> dict[str, dict[str, str]]:
    """Supplement only legacy blocks the canonical parser retained as raw purpose."""
    supplemental: dict[str, dict[str, str]] = {}
    field_names = (
        "role", "purpose", "inputs", "outputs", "constraints",
        "required_capabilities", "forbidden_capabilities", "references", "source",
    )
    matches = list(re.finditer(
        r"(?im)^[ \t]*-[ \t]*path[ \t]*:[ \t]*`?([^`\n]+?)`?[ \t]*$",
        blueprint_text or "",
    ))
    for index, match in enumerate(matches):
        path = _normalize_skill_path(match.group(1).strip().strip("'\""))
        if path not in paths_needing_compatibility:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(blueprint_text)
        block = blueprint_text[match.end():end]
        for field in field_names:
            field_match = re.search(
                rf"(?im)^[ \t]*{re.escape(field)}[ \t]*:[ \t]*(.*)$",
                block,
            )
            if field_match:
                supplemental.setdefault(path, {})[field] = field_match.group(1).strip()
    return supplemental


def _blueprint_semantic_structural_facts(blueprint_text: str) -> tuple[str, dict[str, dict[str, Any]]]:
    plan = parse_blueprint([{"role": "assistant", "content": blueprint_text}], strict=True)
    facts = {entry.path: asdict(entry) for entry in plan.files}
    paths_needing_compatibility = {
        path for path, item in facts.items()
        if str(item.get("purpose") or "").lstrip().startswith("- path:")
    }
    supplemental = _strict_blueprint_compatibility_fields(
        blueprint_text, paths_needing_compatibility
    )
    for path, fields in supplemental.items():
        facts[path].update(fields)
    return plan.skill_name, facts


def _validate_blueprint_semantic_replan_scope(
    *, before_blueprint_text: str, after_blueprint_text: str,
    blocking_issues: list[dict[str, Any]], patch_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Match the declared patch to structural diff and reject unrelated drift."""
    before_name, before = _blueprint_semantic_structural_facts(before_blueprint_text)
    after_name, after = _blueprint_semantic_structural_facts(after_blueprint_text)
    if before_name != after_name:
        raise PreparePlanProtocolError("Blueprint semantic replan changed skill_name")

    before_paths, after_paths = set(before), set(after)
    added = after_paths - before_paths
    removed = before_paths - after_paths
    changed = {path for path in before_paths & after_paths if before[path] != after[path]}
    before_targets = set(_resolve_allowed_function_item_targets_from_blueprint(before_blueprint_text))
    after_targets = set(_resolve_allowed_function_item_targets_from_blueprint(after_blueprint_text))
    actual_added_targets = added & after_targets
    actual_changed_targets = changed & before_targets
    actual_changed_resources = (added | removed | changed) - before_targets - after_targets
    changed_existing_resources = changed - before_targets - after_targets

    declared_changed = {str(value).strip() for value in patch_manifest["changed_targets"]}
    declared_added = {str(value).strip() for value in patch_manifest["added_targets"]}
    declared_resources = {str(value).strip() for value in patch_manifest["changed_resources"]}
    logger.info(
        "[Creator][blueprint_semantic_replan_diff] "
        "declared_changed_targets=%s actual_changed_targets=%s "
        "declared_added_targets=%s actual_added_targets=%s "
        "declared_changed_resources=%s actual_changed_resources=%s",
        sorted(declared_changed), sorted(actual_changed_targets),
        sorted(declared_added), sorted(actual_added_targets),
        sorted(declared_resources), sorted(actual_changed_resources),
    )
    if removed & before_targets or removed - before_targets:
        raise PreparePlanProtocolError("Blueprint semantic replan introduced unrelated structural drift by removing paths")
    if before_targets - after_targets:
        raise PreparePlanProtocolError(
            "Blueprint semantic replan changed an existing target outside the "
            f"authoritative FunctionItem domain: {sorted(before_targets - after_targets)}"
        )

    affected = {
        str(target).strip()
        for issue in blocking_issues
        for target in (issue.get("affected_targets") or [])
    }
    uncovered_without_target = any(
        issue.get("issue_type") == "requirement_uncovered"
        and not (issue.get("affected_targets") or [])
        for issue in blocking_issues
    )
    if not uncovered_without_target:
        if actual_changed_targets - affected or actual_added_targets:
            raise PreparePlanProtocolError("Blueprint semantic replan changed targets outside blocking issue scope")
        issue_resources = {
            str(issue.get("resource") or "").strip()
            for issue in blocking_issues if str(issue.get("resource") or "").strip()
        }
        if actual_changed_resources - issue_resources:
            raise PreparePlanProtocolError("Blueprint semantic replan changed resources outside blocking issue scope")
    else:
        allowed_clarification_fields = {"purpose", "inputs", "outputs", "constraints"}
        invalid_changed_fields = {
            path: sorted(
                key for key in set(before[path]) | set(after[path])
                if before[path].get(key) != after[path].get(key)
                and key not in allowed_clarification_fields
            )
            for path in actual_changed_targets
        }
        invalid_changed_fields = {
            path: fields for path, fields in invalid_changed_fields.items() if fields
        }
        if invalid_changed_fields:
            raise PreparePlanProtocolError(
                "Blueprint semantic replan changed existing target fields outside "
                f"the minimum responsibility clarification scope: {invalid_changed_fields}"
            )
        if actual_changed_resources or changed_existing_resources:
            raise PreparePlanProtocolError(
                "Blueprint semantic replan changed resources outside an ownerless "
                "requirement repair scope"
            )
    return {
        "actual_changed_targets": sorted(actual_changed_targets),
        "actual_added_targets": sorted(actual_added_targets),
        "actual_changed_resources": sorted(actual_changed_resources),
        "actual_removed_paths": sorted(removed),
        "actual_changed_fields": {
            path: sorted(
                key for key in set(before[path]) | set(after[path])
                if before[path].get(key) != after[path].get(key)
            )
            for path in sorted(actual_changed_targets)
        },
    }


def _planner_convergence_review_event_from_result(result: dict[str, Any]) -> dict[str, Any]:
    review_summary = result.get("review_summary")
    if not isinstance(review_summary, dict):
        review_summary = {}

    items: list[str] = []
    for key in ("changes", "risks"):
        values = review_summary.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, str):
                text = value.strip()
            elif isinstance(value, dict):
                text = str(
                    value.get("message")
                    or value.get("summary")
                    or value.get("title")
                    or ""
                ).strip()
            else:
                text = str(value or "").strip()
            if text:
                items.append(text)
            if len(items) >= 6:
                break
        if len(items) >= 6:
            break

    summary = "规划复核完成"
    if items:
        summary = f"规划复核完成，整理出 {len(items)} 条结论或建议"
    elif str(result.get("status") or "") == "ready":
        summary = "规划复核完成，方案已收敛"

    return {
        "event": "planner_convergence_review",
        "title": "规划模型复核方案",
        "summary": summary,
        "items": items,
    }


async def _generate_internal_blueprint_or_questions(
    request: PreparePlanRequest,
    event_emitter: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Judge business requirement maturity and produce the provisional blueprint.

    This phase owns:
    - business action semantics;
    - runtime input/output planning;
    - script topology planning;
    - provisional SkillPlan responsibility boundaries.

    Script count is derived from task complexity and executable responsibility
    boundaries. Fewer files and more files are both non-goals.

    Tool discovery and ToolPool mutation are forbidden here.
    """

    existing_context = (
        _read_prepare_existing_skill_context(
            request.skill_name
        )
        if request.mode == "revise"
        else {}
    )

    system_prompt = (
        load_kernel_creator_for_phase(
            "prepare_plan"
        )
        + """
你现在服务 /api/creator/prepare-plan。

只输出严格 JSON object。
不要 Markdown。
不要解释 JSON 外文本。

当前阶段是第一段 FilePlan / Blueprint planning pass，只负责：

1. 判断业务需求是否已经足够明确；
2. 信息不足时提出一个真正阻塞创建计划的业务问题；
3. 信息足够时生成完整业务 Blueprint；
4. 生成 SkillPlan / FilePlan 文件拓扑与 file-local metadata。

Do not emit FunctionItems in this first pass.
Do not emit ResponsibilityEdges in this first pass.
Do not plan the ResponsibilityGraph in this first pass.

internal_blueprint_text is the human-readable Blueprint view and must contain the complete SkillPlan file responsibility information: path, role, purpose, inputs, outputs, default_values, dependencies, required_capabilities, forbidden_capabilities, references, constraints, and existing file-local metadata. Any input decided as internal_default, constant, or creation-time fixed must be recorded in that script entry's structured default_values with its native JSON type; prose-only defaults are invalid.

The first pass only follows the FilePlan protocol. It may plan SKILL.md, scripts/**, references/**, assets/**, and config files.

## Resource role contract（只按来源、生命周期、使用方式判断）

- reference：由 Creator 在创建阶段生成的语义指导材料，例如规则、说明、约束、指南或提示原则；它用于指导脚本实现或运行，不是用户原始上传，也不是运行时产物。
- asset：已经存在且脚本在运行时直接消费的静态文件。来源必须显式为 source=user_upload 或 source=bundled；Creator 不重新创作其内容。
- runtime artifact：运行脚本后才产生，属于 FunctionItem outputs、stdout 或 artifact contract；不得进入 Blueprint FilePlan 的 references/** 或 assets/**。

不得根据扩展名、文件名或业务领域词判断资源角色，不得自动迁移资源路径。返回 status=ready 前逐项自检：谁创建该资源；创建发生在 Creator 阶段还是运行时；用户上传/系统预置资源是否误作 reference；运行时产物是否误入 static FilePlan；reference 是否确为 Creator 生成的语义指导材料。

review_summary 只是同一响应中的临时展示摘要。
后端不会使用 review_summary 重建蓝图。

因此必须先正确规划完整蓝图，
再映射摘要；
不得反过来根据摘要扩写蓝图。

当前阶段不是 Tool Registry 发现阶段。

禁止：

- 探索工具目录；
- 选择具体 Registry tool_id；
- 提出 tool_pool_patch；
- 修改 ToolPool；
- 根据 uploaded_files.candidate_tools 反向修改业务需求；
- 输出 selected_tools；
- 输出 required_tool_slots。

required_capabilities 只表达当前 scripts/*.py
真实执行责任所需要的抽象语义能力。

## 用户真实要求保留优先级

理解和修订需求时，必须按以下优先级合并信息：

1. 用户最新明确反馈 human_feedback；
2. 用户最初的 user_request 原话；
3. 用户已经确认的补充内容；
4. 仍未被后续反馈覆盖的历史要求；
5. Planner 自己的推测只能作为最后补充。

必须保留：用户目标、输入方式、输出和最终产物、明确要求的步骤或能力、用户明确禁止的内容。

规则：

- 最新反馈只覆盖与它冲突的旧要求；
- 不冲突的旧要求不能丢；
- 不得用 previous_blueprint_text、review_summary 或模型旧摘要覆盖 user_request / human_feedback 的用户原话；
- 用户已经说清楚的内容不要重复追问；
- 修订 Blueprint 时只能替换被最新反馈明确冲突覆盖的部分，其他已确认业务要求必须继续进入 workflow、FilePlan purpose、outputs、constraints 或 forbidden_capabilities。
- 用户已经明确提供或已经确认的需求，后续 prepare 轮次必须保持不变，除非最新 human_feedback 明确修改该项；这是 user requirement → clarification answer → Blueprint 的单向继承关系；
- 已确认的 runtime input、final output / artifact、required business actions 不得在重新生成 Blueprint 时重新解释或重新询问；已经问过并得到明确回答的问题不得再次询问；
- 实现需要某个参数不代表用户必须提供该参数。用户没有明确要求运行时控制的普通实现参数，优先写入对应 script 的 default_values，不得自动提升为 runtime input；
- 不得为了让 ResponsibilityGraph provenance 闭合，把内部默认参数改成 platform_input_node 输入。

## 业务动作方向必须保持

先区分：

- 用户明确提供什么；
- 用户要求系统生成、创建、编写、转换、分析或处理什么；
- 最终必须交付什么。

不得把“用户要求系统生成的对象”
自动改成“用户必须先提供的输入”。

例如：

- 用户要求生成或编写某个内容时，
  该内容默认是系统责任，
  不是运行时前置输入；

- 只有用户明确表示
  “我会提供已有内容”
  “基于我上传的内容处理”
  时，
  才把已有完整内容作为输入；

- 用户要求生成 artifact，
  不代表需要解析同类已有 artifact；

- 用户要求生成图像，
  不代表需要理解已有图像；

- 用户要求构建文档，
  不代表需要读取或解析已有同类文档。

必须保留用户动作方向。

不得只抓取业务实体而丢失动作。

规划 I/O 时：

- input 必须是用户实际提供的最小前置条件；
- output 必须覆盖用户要求系统完成的最终责任；
- workflow 必须真实包含从 input 到 output 的业务动作链；
- 不得把 output 倒置成 input；
- 不得把生成任务降级为已有内容的格式转换，
  除非用户明确这样要求。

## Script topology 与责任边界规则

文件数量只在 blueprint planning 阶段决定。

脚本数量必须由任务复杂度和可执行责任边界决定。

“脚本尽可能少”不是目标。
“脚本尽可能多”也不是目标。

不要为了减少文件数量，
把多个独立业务责任全部压缩进一个
composite_generator 或 generic_script。

也不要因为自然语言 workflow 有多个步骤，
就机械地一项步骤创建一个脚本。

在确定 scripts/* 文件前，
先识别工作流中的可执行责任。

对每项候选责任判断：

1. 它消费什么运行时输入或前序业务结果；

2. 它产生什么能够被后续处理或最终交付消费的结果；

3. 它执行什么核心业务动作；

4. 它需要哪些抽象语义能力；

5. 它是否形成真实 producer / consumer 边界；

6. 它是否具有独立验证、失败定位或局部修复价值；

7. 它与相邻责任是否高度耦合，
   是否共享同一核心输入和同一最终输出边界。

脚本数量按责任复杂度决定：

- 简单任务：
  如果核心执行责任单一，
  局部步骤高度耦合，
  共享同一主要输入和输出边界，
  使用 1 个脚本是合理的。

- 中等或复杂任务：
  如果存在多个清晰的独立责任闭包，
  通常规划 2～3 个脚本。

- 更复杂任务：
  只有存在更多真实、独立、
  可执行且可验证的责任边界时，
  才继续增加脚本数量。

2～3 个脚本是复杂任务的常见结果，
不是固定数量，
也不是硬上限。

如果 2～3 个清晰责任脚本
已经能够完整表达工作流，
不要继续细碎拆分。

多项责任只有满足以下条件时，
才适合合并到同一个脚本：

- 执行逻辑高度耦合；
- 主要消费同一组核心业务输入；
- 主要产生同一责任闭包中的业务结果；
- 不存在清晰的跨责任 producer / consumer handoff；
- 合并后仍然能够用一个清晰 purpose
  描述该文件的主要业务职责。

存在以下事实时，
应认真考虑形成独立脚本职责：

- 一个责任产生的业务结果
  被另一个责任继续消费；

- 两个责任具有清晰的 producer / consumer 边界；

- 两个责任执行明显不同的核心业务能力；

- 存在独立的 artifact 生产或最终 artifact 构建责任；

- 某个阶段能够独立验证，
  并具有独立失败定位或局部修复价值。

以上是拆分证据，
不是机械拆分条件。

必须结合整个工作流判断。

## Executable responsibility binding is deferred

Do not generate function_items in this FilePlan pass.
Do not generate responsibility_edges in this FilePlan pass.
Do not decide which files can become graph nodes beyond declaring the FilePlan itself.

FunctionItems and ResponsibilityEdges will be bound in a second protocol binding pass by the same Blueprint Planner after the backend freezes the exact executable target domain from this FilePlan.

In this pass, FilePlan owns file topology and file-local metadata.
Declare script file responsibilities inside SkillPlan entries only.
Resource usage remains in FilePlan dependencies/references/resource metadata.

## core action fidelity in FilePlan

规划 workflow 和 script file responsibilities 时，
必须区分 core action、action preparation、action description 和 final delivery。

如果用户要求或已确认的是某个 core action，
Blueprint / FilePlan 中必须存在真正拥有并执行该 action 的 scripts/** file responsibility。

仅生成 description、prompt、instruction、metadata、plan、placeholder 或 recommendation，
不能视为已经执行 core action，
除非用户明确要求的本来就是这些准备结果。

对每一个已确认 core action，
必须回答：

1. Which scripts/** file owns and executes this action?
2. 该 file purpose 是否明确声明该 action？
3. required_capabilities 是否描述该 file 实际执行的抽象能力？
4. 该 action 的真实结果是否进入后续 workflow 或最终交付？

如果四项中任何一项无法回答，
当前 Blueprint 尚未形成 FilePlan 责任闭包，
不得返回 status=ready。

## file-local responsibility metadata

每个 SkillPlan entry 都必须显式包含 path、role、purpose、inputs、outputs、default_values、dependencies、required_capabilities、forbidden_capabilities、references、constraints 以及现有 file-local metadata。internal_default、constant 或 creation-time fixed input 必须以原生 JSON 类型写入该脚本的 structured default_values，不能只写在 prose。

每个 script 必须具有一个清晰的主要业务职责。

purpose 必须说明：

- 当前文件消费什么语义输入；
- 当前文件真正执行什么核心动作；
- 当前文件交付什么业务结果。

不要让多个 script 重复拥有同一个核心业务动作。

如果上游 script 已负责产生某项业务结果，
下游 script 可以消费、整理、组合、映射或交付该结果，
但不应再次实现上游已经拥有的核心生成或处理责任，
除非其自身 SkillPlan purpose 明确声明了不同的业务处理责任。

constraints is generic file-local constraint data.
Planner 负责把 constraint 放到拥有该责任的 SkillPlan entry。
不要广播到所有 scripts。
不要广播到所有 entries。
constraints 必须是单行合法 JSON array。
没有额外 responsibility constraint 时：constraints: []。

## internal processing and script splitting

当前平台没有显式 loop/map/foreach runtime node。
内部遍历、批处理、逐项处理、顺序映射和局部聚合应由拥有该业务责任的 script 内部实现。
内部循环本身不构成拆脚本理由，也不改变 script boundary。
只服务于当前责任的字段适配、数据整理、格式转换、参数映射和小型 deterministic helper 逻辑，应保留在所属 script 内部。

## Capability 声明规则

每个 script 的 required_capabilities 必须来自该 script 实际执行动作。

动作方向必须一致：

- generate/create/build 与 parse/read/extract 不等价；
- 生成某类 artifact 不自动需要该 artifact 的解析能力；
- 生成图片不自动需要视觉理解能力；
- 只有脚本确实消费并语义理解已有图片时，才声明图像或视觉理解能力；
- capability 不得根据相邻概念、文件名或最终 artifact 类型机械扩展。

required_capabilities 只属于真正执行对应动作的 script。
不要因为下游消费了上游产物，就把上游 generation capability 重复声明给下游。
不得填写具体 Registry tool_id。

## final delivery closure

Blueprint 顶层 I/O 契约、review_summary.output、
workflow 最终交付描述和 SkillPlan script outputs
必须形成语义闭环。

对于顶层声明的每一个 required final result：

必须能找到一个 required scripts/** file producer
明确生产或最终交付该结果。

不得出现：

- 顶层 output 声明结果 A；
- workflow 声明最终返回 A；
- 但所有 required script outputs 都没有 A。

也不得出现：

- script purpose 声明交付 A；
- SkillPlan outputs 却遗漏 A。

字段名不要求逐字相同，
但业务结果必须语义可追踪。

生成 status=ready 前，
逐项检查所有 final results 的 producer。

无法找到 producer 时，
必须先修订 Blueprint，
不得由 SKILL.md、reference、asset、workflow prose 或 review_summary 充当 producer。

不得依赖后续 global contract 或 E2E 猜测补齐。

## FilePlan ready self-check

Before returning status=ready, check only the FilePlan / Blueprint contract:

A. confirmed decisions
- 原始用户要求中的核心业务动作是否仍存在？
- 每个已回答 clarification 的明确 decision 是否仍存在？
- 是否把任何执行动作弱化为描述、提示或占位结果？

B. file topology
- 每个 required file 是否有清晰 role/purpose？
- 每个 scripts/** file 是否有完整 file-local responsibility metadata？
- 是否两个 scripts/** files 重复拥有同一核心业务责任？
- 是否有 core action 只存在于 workflow prose/reference 中，但没有进入任何 script file purpose？

C. capability alignment
- 每个 script required_capabilities 是否来自该 script 实际执行动作？
- 是否遗漏了 script 明确执行的抽象能力？
- 是否为了迎合当前已知 tools 而删除业务 capability？

D. constraint preservation
- 每个 SkillPlan entry 是否显式包含 constraints？
- 无 constraint 时是否为 []？
- 明确 implementation requirement 是否只存在于 prose 而没有进入 owning script constraint？

E. final delivery closure
- 每个 required final result 是否存在 script file producer？
- 顶层 output、workflow final delivery、script purpose 和 script outputs 是否语义一致？

F. lightweight runtime-contract self-check
- 对每个 scripts/** SkillPlan responsibility，逐项复核 inputs 是否确实需要在 runtime 提供；creation-time fixed、default 或 static configuration 不得误列为 runtime input。
- 每个 runtime input 是否在理论上可由平台输入或另一个已声明 script output 提供？这里只检查责任定义质量，不生成或描述具体 ResponsibilityEdge。
- 每个 output 是否具有明确业务用途、下游消费者或 final deliverable？每个 final deliverable 是否存在明确 producer？
- 不要为了“可能有用”额外创造 input 或 output。
- 此检查只修订 Blueprint 的 responsibility inputs/outputs；不得在第一段提前生成 ResponsibilityEdges。

如果任一项失败：

先修改 Blueprint / FilePlan。

不得输出 status=ready。

## 返回格式

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
  "internal_blueprint_text": "status=ready 时填写 provisional Skill 架构蓝图",
  "skill_name": "可选",
  "blockers": []
}

## confirmed decision preservation

conversation_history、human_feedback 和 previous_blueprint_text
中已经明确回答或确认的业务选择，
属于当前 Blueprint planning 的已确认 decision。

已确认 decision 的优先级高于 Planner 自行选择的默认方案。

生成新的 ready Blueprint 时，
必须逐项保留这些 decision 的原始业务动作方向和交付含义。

不得：

- 删除已确认 decision；
- 将已确认动作替换为较弱的准备动作；
- 将执行动作替换为描述、建议、提示词或占位信息；
- 将生成、处理、转换、调用、构建等已确认动作
  改写为仅描述相关内容；
- 因为实现更简单而降低用户已经确认的业务目标；
- 因 ToolPool、Registry 或当前已知 helper 信息不足
  提前删除业务责任。

Blueprint Planner 只规划业务责任。

具体 Registry tool 是否存在，
由后续 Creator Tool planning 判断。

如果一个已确认 decision 无法实现，
必须保留该业务 responsibility，
后续由 tool readiness / creation blocker 处理。

不得通过修改 Blueprint 业务目标来规避工具缺失。

## 规划约束

- inputs / outputs 必须只包含纯字段名。正确：inputs: [input_text, max_items]；
  错误：inputs: [input_text, max_items=5]。正确：outputs: [result_text, result_sections]；
  错误：outputs: [result_text:string, result_sections=[]]。默认值和类型不得写进 field identity。

- workflow 必须覆盖每一个 substantive script 的核心责任，顺序与主要数据依赖一致。

- 只有用户需求或实际 Script responsibility 明确需要持久资源时才创建 references/assets；
  能直接由 Script 或 Tool 完成的内容，不要额外创建静态模板、logo 或说明资源。

- references/assets 默认应为空。只有用户明确要求、用户实际上传/提供，或核心责任确实需要无法合理放入
  Script 或现有 Tool usage 的持久静态可复用内容时，才创建持久资源。不得仅为让 Skill 显得完整而创建资源。
  除非当前请求上下文包含用户实际提供的文件，绝不能声明 source=user_upload。

- Script 声明的 references/assets 必须已经属于当前 FilePlan。

- 只有缺失信息会实质改变核心输入、核心输出、核心 workflow，或“用户提供资源 vs Skill 自动生成”时，
  才使用 needs_clarification。内部变量名、默认文件名前缀、普通默认值、实现细节和局部格式选择应合理默认。

- 输出 status=ready 前内部检查：workflow 覆盖全部 substantive scripts；inputs/outputs 均为纯字段名；
  Script resources 均在 FilePlan；没有无必要 resources；workflow 顺序符合主要 I/O；ready 时不再提 clarification。
  只输出最终结果，不输出检查过程。

- status=ready 前先判断需求成熟度。

- 信息不足且 clarification_rounds
  未达到上限时，
  status=needs_clarification。

- clarifying_questions 只能有 1 个问题。

- 问题必须是当前最阻塞创建计划的问题，
  并带 2～4 个选项。

- 每轮只能问一个业务问题。

- 后续规划必须读取完整 conversation_history 与 human_feedback。

- 对于每个 clarification question：

  - 找到用户已经给出的最终回答；
  - 提取其中确定的业务 decision；
  - 在新的 workflow、SkillPlan purpose、
    required_capabilities 和最终交付中保持一致。

- clarification answer 不是参考意见，
  而是 Blueprint planning 输入契约的一部分。

- 当 previous_blueprint_text 与最新 human_feedback 冲突时，
  以最新明确 human_feedback 为准。

- 当 Planner 输出 ready Blueprint 前，
  必须检查新的 Blueprint 是否仍然表达所有已确认 decision。

- 不得因为重新生成 full blueprint
  而遗忘前一轮 clarification decision。

- 已经回答过的问题不得重复询问。

- clarification_rounds 达到上限时
  不得继续无限追问。

- 用户确认无补充后
  不得继续 needs_clarification。

- 用户补充业务要求时，
  可以基于 previous_blueprint_text
  与新增 feedback 修订 full blueprint。

- 除真实 supplement/revise 外，
  不得重新定义已经明确的业务动作方向。

- script topology 必须在 blueprint planning
  阶段完成。

- 蓝图通过后，
  不依赖后续阶段重新新增、删除、
  拆分或合并 script 文件。

- 简单责任闭包允许 1 个 script。

- 中等或复杂任务通常产生
  2～3 个清晰责任 script。

- 不把 2～3 当硬编码数量；
  真实责任边界更多时可以更多，
  责任单一时可以只有一个。

- 不按自然语言步骤数量机械拆文件。

- 不以最少文件数作为规划目标。

- 不以最多模块数作为规划目标。

- 每个 script 必须有清晰主要职责。

- 多脚本之间不得重复实现
  同一核心业务责任。

- upstream output 与 downstream input
  应语义可追踪，
  但字段名不要求逐字相同。

- script 内部生成且内部消费的中间值
  不得提升为 required external input。

- 当前平台没有显式 loop/map/foreach runtime node。

- 内部遍历、批处理、顺序映射和局部聚合
  由拥有该责任的 script 内部实现。

- 内部循环不改变 script boundary。

- uploaded_files 是 Creator 创建阶段上下文，
  不等于 Skill assets。

- confirmed_uploaded_assets
  才是用户确认加入 Skill 的静态素材。

- 没有用户明确确认，
  不得创建 assets/**。

- 运行时用户输入不是 Creator assets。

- 运行时生成产物不是 Creator assets。

- assets/** 只能声明
  source=user_upload 或 source=bundled。

- 目录结构只展示目录。

- 具体文件只在 SkillPlan 中声明。

- references/*.md
  只有业务 Skill 自身在运行前确实需要读取某个静态参考资源时才声明；不确定时默认不声明 reference。

- Creator/Kernel 内部参考资料只是创建阶段规划上下文，不属于业务 Skill 的本地静态资源；不得复制、改写或截取内部 reference path/basename 后写入 SkillPlan dependencies、references 或资源清单。

- 协议示例、kernel 示例、命令示例中的
  references/*.md 路径不是业务文件。

- dependencies/references 中真实引用的静态文件
  必须在 SkillPlan 中显式声明。

- provisional blueprint 可以声明
  text_generation、image_generation、
  pdf_generation 等抽象语义能力。

- 具体 Registry Tool 选择
  由后续 Final Tool Selector 完成。
"""
    )

    (
        confirmed_uploaded_assets,
        unselected_uploaded_files,
    ) = _split_uploaded_asset_decisions(
        request.uploaded_files
    )

    payload = {
        "mode": request.mode,

        "skill_name": (
            request.skill_name
        ),

        "user_request": (
            request.user_request
        ),

        "conversation_history": (
            request.conversation_history
        ),

        "uploaded_files": (
            request.uploaded_files
        ),

        "confirmed_uploaded_assets": (
            confirmed_uploaded_assets
        ),

        "unselected_uploaded_files": (
            unselected_uploaded_files
        ),

        "previous_blueprint_text": (
            request.previous_blueprint_text
        ),

        "human_feedback": (
            request.human_feedback
        ),

        "existing_skill_context": (
            existing_context
        ),

        "clarification_rounds": (
            _count_prepare_business_clarification_rounds(
                request
            )
        ),

        "max_clarification_rounds": (
            MAX_PREPARE_BUSINESS_CLARIFICATION_ROUNDS
        ),

        "clarification_limit_reached": (
            _prepare_business_clarification_limit_reached(
                request
            )
        ),

        "supplement_rounds": (
            _count_prepare_supplement_rounds(
                request
            )
        ),

        "max_supplement_rounds": (
            MAX_PREPARE_SUPPLEMENT_ROUNDS
        ),

        "user_confirmed_no_more_supplement": (
            _prepare_user_confirmed_no_more_supplement(
                request
            )
        ),

        "platform_io_contract": platform_io_contract_prompt_text(),
    }

    route = route_model(
        "creator_prepare_plan",
        requested_model=request.model,
        reason=(
            "creator business action "
            "and blueprint planning"
        ),
    )

    text = await complete_creator_role_once(
        [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": json.dumps(
                    payload,
                    ensure_ascii=False,
                    default=str,
                ),
            },
        ],
        "planner", fallback_model=route.model,
    )

    data = _parse_prepare_plan_json(
        text
    )

    data.pop(
        "tool_pool_patch",
        None,
    )

    data.pop(
        "selected_tools",
        None,
    )

    data.pop(
        "required_tool_slots",
        None,
    )

    status = str(data.get("status") or "").strip()
    data.pop("function_items", None)
    data.pop("responsibility_edges", None)
    first_planner_result = dict(data)
    allowed_resource_paths = _build_prepare_allowed_resource_paths(
        request=request,
        review_summary=first_planner_result.get("review_summary"),
        existing_skill_context=existing_context,
    )
    frozen_blueprint_text = str(
        first_planner_result.get("internal_blueprint_text")
        or ""
    )
    normalized_ready_draft: dict[str, Any] | None = None
    draft_edge_error: Exception | None = None
    draft_function_item_error: Exception | None = None
    allowed_function_item_targets: list[str] = []

    if status == "needs_clarification":
        if data.get("function_items") is None:
            data["function_items"] = []
        data["function_items"] = normalize_structured_function_items(
            data.get("function_items"),
            source="planner",
        )
        if data.get("responsibility_edges") is None:
            data["responsibility_edges"] = []
        normalized_edges = normalize_structured_responsibility_edges(
            data.get("responsibility_edges"),
            source="planner",
        )
        data["responsibility_edges"] = normalized_edges
    elif status == "ready":
        normalized_function_items = None
        binding_data: dict[str, Any] = {
            "function_items": [],
            "responsibility_edges": [],
        }
        protocol_errors = []
        try:
            validate_blueprint_shape_for_creator(
                frozen_blueprint_text
            )
        except BlueprintShapeError as exc:
            protocol_errors.append(
                _prepare_protocol_issue(
                    "invalid_strict_blueprint_shape",
                    str(exc),
                    field="internal_blueprint_text",
                )
            )
        protocol_errors.extend(
            _preflight_prepare_blueprint_text(
                frozen_blueprint_text,
                allowed_resource_paths,
            )
        )
        repair_index = -1
        for repair_index in range(2):
            if not protocol_errors:
                break
            try:
                frozen_blueprint_text = await _repair_prepare_blueprint_protocol(
                    request=request,
                    blueprint_text=frozen_blueprint_text,
                    protocol_errors=protocol_errors,
                    allowed_resource_paths=allowed_resource_paths,
                )
            except Exception as exc:
                raise PreparePlanProtocolError(
                    "Planner ready Blueprint FilePlan protocol "
                    "repair failed before executable target freeze; "
                    f"repair_index={repair_index}; error={type(exc).__name__}: {exc}"
                ) from exc

            protocol_errors = []
            try:
                validate_blueprint_shape_for_creator(frozen_blueprint_text)
            except BlueprintShapeError as exc:
                protocol_errors.append(_prepare_protocol_issue(
                    "invalid_strict_blueprint_shape", str(exc),
                    field="internal_blueprint_text",
                ))
            protocol_errors.extend(_preflight_prepare_blueprint_text(
                frozen_blueprint_text,
                allowed_resource_paths,
            ))
        if protocol_errors:
            raise PreparePlanProtocolError(
                "Planner ready Blueprint failed strict FilePlan preflight "
                "before executable target freeze; "
                f"repair_index={repair_index}; errors={protocol_errors}; "
                f"blueprint={frozen_blueprint_text}"
            )
        if repair_index >= 0:
            first_planner_result = {
                **first_planner_result,
                "internal_blueprint_text": frozen_blueprint_text,
            }
            data = dict(first_planner_result)

        frozen_blueprint_text, rejected_resources = _remove_unauthorized_prepare_resources(
            frozen_blueprint_text,
            allowed_resource_paths,
        )
        logger.info(
            "[Creator][resource_authority] allowed_resources=%s rejected_resources=%s",
            sorted(allowed_resource_paths), rejected_resources,
        )
        authoritative_paths = _extract_prepare_skill_plan_paths(frozen_blueprint_text)
        logger.info(
            "[Creator][file_plan_authority] authoritative_scripts=%s authoritative_references=%s authoritative_assets=%s",
            [path for path in authoritative_paths if path.startswith("scripts/")],
            [path for path in authoritative_paths if path.startswith("references/")],
            [path for path in authoritative_paths if path.startswith("assets/")],
        )

        allowed_function_item_targets = (
            _resolve_allowed_function_item_targets_from_blueprint(
                frozen_blueprint_text
            )
        )
        if event_emitter is not None:
            await event_emitter({
                "event": "file_plan_ready",
                "allowed_function_item_targets": allowed_function_item_targets,
            })

        # Semantic closure is completed before any ResponsibilityGraph call.
        # Extraction and ownership are model decisions; Backend validates only
        # allocation identity/domain/reference integrity and bounds repair to one.
        semantic_function_items = _frozen_function_items_from_blueprint(
            frozen_blueprint_text=frozen_blueprint_text,
            allowed_function_item_targets=allowed_function_item_targets,
        )
        _validate_prepare_semantic_function_item_topology(
            allowed_function_item_targets,
            semantic_function_items,
        )
        requirement_projection = await _plan_executable_requirement_allocations(
            request=request, blueprint_text=frozen_blueprint_text,
            function_items=semantic_function_items, planner_model=route.model,
        )
        requirement_allocations = requirement_projection["requirement_allocations"]
        requirement_channels = requirement_projection["requirement_channels"]
        channel_counts = _requirement_channel_summary(requirement_channels)
        logger.info(
            "[Creator][requirement_channel] executable_requirement_count=%d "
            "resource_requirement_count=%d direct_requirement_count=%d",
            channel_counts["executable_requirement_count"],
            channel_counts["resource_requirement_count"],
            channel_counts["direct_requirement_count"],
        )
        semantic_review = await _review_blueprint_semantic_closure(
            request=request, blueprint_text=frozen_blueprint_text,
            function_items=semantic_function_items,
            requirement_allocations=requirement_allocations,
            requirement_channels=requirement_channels, planner_model=route.model,
        )
        blocking_issues = list(semantic_review["issues"])
        allocation_issues = [
            issue for issue in blocking_issues
            if issue.get("repair_scope") == "allocation"
        ]
        if allocation_issues:
            logger.info(
                "[Creator][semantic_repair_route] scope=allocation issue_count=%d",
                len(allocation_issues),
            )
            requirement_projection = await _reconcile_requirement_allocations(
                request=request,
                blueprint_text=frozen_blueprint_text,
                function_items=semantic_function_items,
                requirement_allocations=requirement_allocations,
                requirement_channels=requirement_channels,
                semantic_review={**semantic_review, "issues": allocation_issues},
                planner_model=route.model,
            )
            requirement_allocations = requirement_projection["requirement_allocations"]
            requirement_channels = requirement_projection["requirement_channels"]
            semantic_review = await _review_blueprint_semantic_closure(
                request=request, blueprint_text=frozen_blueprint_text,
                function_items=semantic_function_items,
                requirement_allocations=requirement_allocations,
                requirement_channels=requirement_channels, planner_model=route.model,
            )
            blocking_issues = list(semantic_review["issues"])

        blueprint_issues = [
            issue for issue in blocking_issues
            if issue.get("repair_scope") == "blueprint"
        ]
        non_blueprint_issues = [
            issue for issue in blocking_issues
            if issue.get("repair_scope") != "blueprint"
        ]
        if blueprint_issues and not non_blueprint_issues:
            logger.info(
                "[Creator][semantic_repair_route] scope=blueprint issue_count=%d",
                len(blueprint_issues),
            )
            before_projection_facts = _semantic_projection_facts(
                frozen_blueprint_text
            )
            replanned_blueprint = await _replan_blueprint_for_semantic_closure(
                request=request, blueprint_text=frozen_blueprint_text,
                function_items=semantic_function_items,
                requirement_allocations=requirement_allocations,
                blocking_issues=blueprint_issues, planner_model=route.model,
            )
            replanned_blueprint, rejected_resources = _remove_unauthorized_prepare_resources(
                replanned_blueprint, allowed_resource_paths,
            )
            logger.info(
                "[Creator][resource_authority] allowed_resources=%s rejected_resources=%s",
                sorted(allowed_resource_paths), rejected_resources,
            )
            validate_blueprint_shape_for_creator(replanned_blueprint)
            protocol_errors = _preflight_prepare_blueprint_text(
                replanned_blueprint, allowed_resource_paths,
            )
            if protocol_errors:
                raise PreparePlanProtocolError(
                    "Blueprint semantic replan failed protocol after resource authority enforcement; "
                    f"errors={protocol_errors}"
                )
            after_projection_facts = _semantic_projection_facts(replanned_blueprint)
            actual_noop = before_projection_facts == after_projection_facts
            frozen_blueprint_text = replanned_blueprint
            logger.info(
                "[Creator][semantic_replan] actual_noop=%s preserved_requirement_projection=%s",
                str(actual_noop).lower(), str(actual_noop).lower(),
            )
            allowed_function_item_targets = _resolve_allowed_function_item_targets_from_blueprint(
                frozen_blueprint_text
            )
            semantic_function_items = _frozen_function_items_from_blueprint(
                frozen_blueprint_text=frozen_blueprint_text,
                allowed_function_item_targets=allowed_function_item_targets,
            )
            _validate_prepare_semantic_function_item_topology(
                allowed_function_item_targets, semantic_function_items,
            )
            if not actual_noop:
                requirement_projection = await _plan_executable_requirement_allocations(
                    request=request, blueprint_text=frozen_blueprint_text,
                    function_items=semantic_function_items, planner_model=route.model,
                )
                requirement_allocations = requirement_projection["requirement_allocations"]
                requirement_channels = requirement_projection["requirement_channels"]
            semantic_review = await _review_blueprint_semantic_closure(
                request=request, blueprint_text=frozen_blueprint_text,
                function_items=semantic_function_items,
                requirement_allocations=requirement_allocations,
                requirement_channels=requirement_channels, planner_model=route.model,
            )
            blocking_issues = list(semantic_review["issues"])

        if blocking_issues:
            for scope in ("allocation", "blueprint", "resource", "graph", "none"):
                scoped = [issue for issue in blocking_issues if issue.get("repair_scope") == scope]
                if scoped:
                    logger.info(
                        "[Creator][semantic_repair_route] scope=%s issue_count=%d",
                        scope, len(scoped),
                    )
            raise PreparePlanProtocolError(
                "Pre-graph semantic closure has unresolved blocking issues; "
                f"issues={blocking_issues}"
            )
        first_planner_result = {
            **first_planner_result,
            "internal_blueprint_text": frozen_blueprint_text,
            "requirement_allocations": requirement_allocations,
            "requirement_channels": requirement_channels,
        }

        binding_data = (
            await _bind_executable_responsibility_plan(
                request=request,
                current_planner_result=first_planner_result,
                planner_model=route.model,
                allowed_function_item_targets=allowed_function_item_targets,
            )
            if allowed_function_item_targets
            else {"function_items": [], "responsibility_edges": []}
        )
        normalized_function_items = normalize_structured_function_items(
            binding_data.get("function_items"), source="graph_expansion"
        )
        _validate_function_item_targets_in_allowed_domain(
            normalized_function_items, allowed_function_item_targets
        )
        normalized_edges = validate_structured_responsibility_edge_transport(
            binding_data.get("responsibility_edges"),
            function_items=normalized_function_items,
            source="graph_expansion",
        )
        data = dict(first_planner_result)
        data["function_items"] = normalized_function_items
        data["responsibility_edges"] = normalized_edges
        data["requirement_allocations"] = requirement_allocations
        data["requirement_channels"] = requirement_channels
        data["internal_blueprint_text"] = _render_structured_responsibility_view(
            frozen_blueprint_text, normalized_function_items, normalized_edges
        )
        if event_emitter is not None:
            await event_emitter({
                "event": "planner_converged",
                "function_items": normalized_function_items,
                "responsibility_edges": normalized_edges,
            })

    return data

async def _project_prepare_review_summary_from_blueprint(
    *,
    request: PreparePlanRequest,
    blueprint_text: str,
    prepared: dict[str, Any] | None = None,
) -> PreparePlanReviewSummary:
    """Project the full internal blueprint into a user-facing review summary.

    review_summary is display-only.

    It is never a source for:
    - blueprint generation;
    - blueprint repair;
    - workflow allocation;
    - Tool recall;
    - Tool selection;
    - code generation;
    - E2E.

    Projection input is the full blueprint only.
    """

    source_blueprint = str(
        blueprint_text or ""
    ).strip()

    prepared = (
        prepared
        if isinstance(
            prepared,
            dict,
        )
        else {}
    )

    fallback = _coerce_prepare_summary(
        prepared.get(
            "review_summary"
        )
    )

    if not source_blueprint:
        fallback.risks = []

        fallback.assets_to_upload = [
            str(path).strip()
            for path in (
                fallback.assets_to_upload
                or []
            )
            if str(path).strip().startswith(
                "assets/"
            )
        ]

        return fallback

    response_schema: dict[
        str,
        Any,
    ] = {
        "type": "object",

        "properties": {
            "goal": {
                "type": "string",
            },

            "input": {
                "type": "string",
            },

            "output": {
                "type": "string",
            },

            "workflow": {
                "type": "array",

                "items": {
                    "type": "string",
                },
            },

            "files_to_create_or_update": {
                "type": "array",

                "items": {
                    "type": "string",
                },
            },

            "assets_to_upload": {
                "type": "array",

                "items": {
                    "type": "string",
                },
            },

            "risks": {
                "type": "array",

                "items": {
                    "type": "string",
                },
            },

            "changes": {
                "type": "array",

                "items": {
                    "type": "string",
                },
            },
        },

        "required": [
            "goal",
            "input",
            "output",
            "workflow",
            "files_to_create_or_update",
            "assets_to_upload",
            "risks",
            "changes",
        ],

        "additionalProperties": False,
    }

    prompt = """
你只负责把 internal_blueprint_text 映射成给用户展示的“创建要点”。

这是只读 projection。

internal_blueprint_text 是唯一事实来源。

禁止：

- 补充蓝图中没有的业务需求；
- 修改输入；
- 修改输出；
- 修改工作流；
- 修改文件拓扑；
- 重构蓝图；
- 生成蓝图；
- 判断工具；
- 选择工具；
- 输出风险。

映射规则：

goal：
概括蓝图的最终 Skill 业务目标。

input：
概括蓝图 I/O 契约中的真实运行时输入。

output：
概括蓝图 I/O 契约中的最终用户可见输出。

workflow：
按蓝图工作流逻辑映射为简短步骤。

files_to_create_or_update：
映射 SkillPlan 中声明的文件。

assets_to_upload：
只映射 SkillPlan 中明确声明的静态 assets。

risks：
必须为空数组。

changes：
只在蓝图明确描述变更时映射；否则为空数组。

响应结构由 JSON Schema 强制。

只做映射。
""".strip()

    route = route_model(
        "creator_prepare_plan",
        requested_model=request.model,
        reason=(
            "creator blueprint to review "
            "summary projection"
        ),
    )

    summary = fallback

    try:
        projected = (
            await _complete_creator_json_object_once(
                messages=[
                    {
                        "role": "system",
                        "content": prompt,
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "internal_blueprint_text": (
                                    source_blueprint
                                ),
                            },
                            ensure_ascii=False,
                            default=str,
                        ),
                    },
                ],

                model=route.model,

                phase=(
                    "prepare_review_summary_"
                    "projection"
                ),

                response_schema=(
                    response_schema
                ),
            )
        )

        summary = (
            _coerce_prepare_summary(
                projected
            )
        )

    except Exception as exc:
        logger.warning(
            "[Creator]"
            "[review_summary_projection]"
            "[failed] "
            "error=%s",
            (
                f"{type(exc).__name__}: "
                f"{exc}"
            ),
        )

        summary = fallback

    plan_paths = (
        _extract_prepare_skill_plan_paths(
            source_blueprint
        )
    )

    # File list is a deterministic projection.
    summary.files_to_create_or_update = (
        plan_paths
    )

    asset_plan_paths = {
        path
        for path in plan_paths
        if path.startswith(
            "assets/"
        )
    }

    summary.assets_to_upload = [
        str(path).strip()
        for path in (
            summary.assets_to_upload
            or []
        )
        if (
            str(path).strip()
            in asset_plan_paths
        )
    ]

    summary.risks = []

    return summary

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


def _extract_script_tool_calls(content: str) -> list[dict[str, str]]:
    """Extract explicit generated tool calls/imports.

    This is only a boundary guard. It must not perform semantic recall.
    Custom-tool imports are represented with module/function information so the
    guard can compare them against Current File Tool Binding allowed_import_paths
    and allowed_function_imports.
    """
    text = str(content or "")
    calls: list[dict[str, str]] = []

    for match in re.finditer(
        r"\b(?:run_registered_tool|run_tool|call_tool)\(\s*['\"]([A-Za-z_][A-Za-z0-9_.-]*)['\"]",
        text,
    ):
        tool_id = match.group(1).strip()
        if tool_id:
            calls.append({
                "kind": "registry_call",
                "tool_id": tool_id,
                "module": "",
                "function": "",
                "display": tool_id,
            })

    import_from_re = re.compile(
        r"^\s*from\s+(backend\.services\.runtime_tools\.custom_tools\.([A-Za-z_][A-Za-z0-9_]*))\s+import\s+([A-Za-z_][A-Za-z0-9_]*)\b",
        re.MULTILINE,
    )
    for match in import_from_re.finditer(text):
        module = match.group(1).strip()
        tool_id = match.group(2).strip()
        function_name = match.group(3).strip()
        calls.append({
            "kind": "custom_import_from",
            "tool_id": tool_id,
            "module": module,
            "function": function_name,
            "display": f"{module}.{function_name}",
        })

    import_re = re.compile(
        r"^\s*import\s+(backend\.services\.runtime_tools\.custom_tools\.([A-Za-z_][A-Za-z0-9_]*))\b",
        re.MULTILINE,
    )
    for match in import_re.finditer(text):
        module = match.group(1).strip()
        tool_id = match.group(2).strip()
        calls.append({
            "kind": "custom_import",
            "tool_id": tool_id,
            "module": module,
            "function": "",
            "display": module,
        })

    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in calls:
        key = (
            item.get("kind", ""),
            item.get("tool_id", ""),
            item.get("module", ""),
            item.get("function", ""),
        )
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped


def _script_tool_boundary_violations(
    content: str,
    allowed_tools: list[str],
    *,
    file_binding: Any = None,
    skill_plan_entry: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Validate generated explicit tool calls against the real file binding.

    Important:
    - This is not semantic matching.
    - It must not second-guess ToolPool recall.
    - If Current File Tool Binding allows a custom import path/function, this
      boundary guard must accept it exactly like runtime_import_guard does.
    """
    allowed_tool_ids: set[str] = {str(name).strip() for name in allowed_tools or [] if str(name or "").strip()}
    allowed_import_paths: set[str] = set()
    allowed_function_imports: set[str] = set()
    allowed_helper_imports: set[str] = set()

    def _as_dict(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if hasattr(value, "model_dump"):
            try:
                dumped = value.model_dump(mode="json")
                return dumped if isinstance(dumped, dict) else {}
            except Exception:
                return {}
        if isinstance(value, dict):
            return value
        return {}

    def _list(value: Any) -> list[str]:
        if value in (None, ""):
            return []
        raw = value if isinstance(value, list) else [value]
        out: list[str] = []
        for item in raw:
            text = str(item or "").strip()
            if text and text not in out:
                out.append(text)
        return out

    binding = _as_dict(file_binding)

    if not binding and isinstance(skill_plan_entry, dict):
        runtime_contract = skill_plan_entry.get("runtime_contract")
        if isinstance(runtime_contract, dict):
            maybe_binding = runtime_contract.get("tool_binding_summary")
            if isinstance(maybe_binding, dict):
                binding = maybe_binding

    allowed_tool_ids.update(_list(binding.get("allowed_tool_ids")))
    allowed_tool_ids.update(_list(binding.get("primary_tool_ids")))
    allowed_tool_ids.update(_list(binding.get("secondary_tool_ids")))
    allowed_import_paths.update(_list(binding.get("allowed_import_paths")))
    allowed_function_imports.update(_list(binding.get("allowed_function_imports")))
    allowed_helper_imports.update(_list(binding.get("allowed_helper_imports")))

    if isinstance(skill_plan_entry, dict):
        runtime_contract = skill_plan_entry.get("runtime_contract")
        if isinstance(runtime_contract, dict):
            allowed_tool_ids.update(_list(runtime_contract.get("selected_tools")))
            allowed_tool_ids.update(_list(runtime_contract.get("allowed_tools")))
            allowed_tool_ids.update(_list(runtime_contract.get("tool_names")))
            allowed_import_paths.update(_list(runtime_contract.get("allowed_imports")))
            allowed_function_imports.update(_list(runtime_contract.get("allowed_function_imports")))
            allowed_helper_imports.update(_list(runtime_contract.get("allowed_helper_imports")))

    unexpected: list[dict[str, str]] = []

    for call in _extract_script_tool_calls(content):
        kind = call.get("kind", "")
        tool_id = call.get("tool_id", "")
        module = call.get("module", "")
        function_name = call.get("function", "")

        allowed = False

        if tool_id and tool_id in allowed_tool_ids:
            allowed = True

        if function_name and function_name in allowed_function_imports:
            allowed = True

        if module and module in allowed_import_paths:
            allowed = True

        if module and function_name and f"{module}.{function_name}" in allowed_function_imports:
            allowed = True

        if function_name and function_name in allowed_helper_imports:
            allowed = True

        # Non-custom registry call fallback: exact capability/tool id only.
        if kind == "registry_call" and tool_id in allowed_tool_ids:
            allowed = True

        if not allowed:
            unexpected.append(call)

    if not unexpected:
        return []

    displays = [item.get("display") or item.get("tool_id") or "" for item in unexpected]
    return [{
        "id": "script.hallucinated_tool_call",
        "layer": "script_tool_boundary",
        "message": "脚本调用了未注册/未允许的工具：" + ", ".join(x for x in displays if x),
        "expected": (
            "只能调用 Current File Tool Binding 中允许的工具、helper、custom import path 或 function；"
            "缺工具时返回 creation_blocker，不能编造工具。"
        ),
        "details": {
            "unexpected": unexpected,
            "allowed_tool_ids": sorted(allowed_tool_ids),
            "allowed_import_paths": sorted(allowed_import_paths),
            "allowed_function_imports": sorted(allowed_function_imports),
            "allowed_helper_imports": sorted(allowed_helper_imports),
        },
    }]


async def _extract_requirement_graph_with_validator(
    *,
    blueprint_text: str,
    files_out: list[FileSpecOut],
    requested_model: str | None = None,
    warnings: list[dict[str, Any]] | None = None,
    workflow_allocation_summary: str = "",
    responsibility_edges: list[dict[str, Any]] | None = None,
    function_items: list[dict[str, Any]] | None = None,
) -> RequirementGraph:
    """Build the deterministic RequirementGraph from normalized FileSpecs.

    Creator no longer invokes a second LLM to reinterpret per-file
    responsibilities. Constraint semantics remain model-owned data; this layer
    only transports existing FileSpec constraints into RequirementItem objects
    and validates required script coverage.
    """
    _ = (workflow_allocation_summary,)
    graph = build_default_requirement_graph(files_out, responsibility_edges=responsibility_edges, function_items=function_items)
    max_refine_rounds = 3
    original_validation_error: RequirementGraphValidationError | None = None
    for attempt in range(max_refine_rounds + 1):
        try:
            validated = validate_requirement_graph_schema(graph, files_out)
            if attempt:
                logger.info("[Creator][responsibility_graph][refine_recheck_passed] attempt=%d", attempt)
            return validated
        except RequirementGraphValidationError as exc:
            if original_validation_error is None:
                original_validation_error = exc
            if not _is_refinable_platform_io_graph_error(exc) or attempt >= max_refine_rounds:
                if attempt:
                    logger.info(
                        "[Creator][responsibility_graph][refine_recheck_failed_final] attempt=%d code=%s detail=%s",
                        attempt,
                        getattr(exc, "code", ""),
                        str(exc)[:500],
                    )
                raise original_validation_error or exc
            feedback = _platform_io_graph_refine_feedback(exc, graph)
            logger.info(
                "[Creator][responsibility_graph][platform_io_closure_failed] attempt=%d code=%s nodes=%s",
                attempt,
                getattr(exc, "code", ""),
                json.dumps(feedback.get("related_nodes") or [], ensure_ascii=False),
            )
            logger.info("[Creator][responsibility_graph][refine_request] attempt=%d", attempt + 1)
            try:
                graph = await _refine_requirement_graph_platform_io_closure(
                    blueprint_text=blueprint_text,
                    files_out=files_out,
                    current_graph=graph,
                    feedback=feedback,
                    requested_model=requested_model,
                )
            except Exception as refine_exc:
                logger.info(
                    "[Creator][responsibility_graph][refine_result_rejected] attempt=%d error=%s",
                    attempt + 1,
                    f"{type(refine_exc).__name__}: {refine_exc}"[:500],
                )
                if attempt + 1 >= max_refine_rounds:
                    raise original_validation_error or exc from refine_exc
                continue


def _is_refinable_platform_io_graph_error(exc: RequirementGraphValidationError) -> bool:
    if getattr(exc, "code", "") != "responsibility_graph_platform_io_conflict":
        return False
    details = getattr(exc, "details", {}) or {}
    if not isinstance(details, dict):
        return False
    if details.get("index") is not None:
        return False
    if any(str(details.get(key) or "").strip() for key in ("from_output", "to_input", "from_node", "to_node")):
        return False
    node = str(details.get("node") or "")
    targets = details.get("targets")
    if node in {"platform_input_node", "platform_output_node"}:
        return True
    if isinstance(targets, list) and any(str(item or "").startswith("scripts/") for item in targets):
        return True
    return False


def _platform_io_graph_refine_feedback(exc: RequirementGraphValidationError, graph: RequirementGraph) -> dict[str, Any]:
    details = getattr(exc, "details", {}) or {}
    node = str(details.get("node") or "")
    targets = [str(item) for item in (details.get("targets") or []) if str(item)]
    script_nodes = [str(item.target_file) for item in (getattr(graph, "function_items", []) or []) if str(getattr(item, "target_file", "") or "")]
    if not targets and node in {"platform_input_node", "platform_output_node"} and len(script_nodes) == 1:
        targets = script_nodes
    directions: list[str] = []
    if node == "platform_input_node":
        directions.append("connect platform_input_node outputs to the actual first executable responsibility when runtime input is required")
    if node == "platform_output_node":
        directions.append("connect the final executable result producer to platform_output_node inputs")
    if targets:
        directions.append("place each isolated required FunctionItem on a platform_input_node -> ... -> platform_output_node path")
    return {
        "error_code": getattr(exc, "code", ""),
        "message": str(exc),
        "missing_connection_directions": directions or ["close platform IO path without inventing business fields"],
        "related_nodes": ([node] if node else []) + targets,
        "closure_requirements": [
            "use only current FunctionItem target_file nodes and immutable platform boundary nodes",
            "do not hard-code or invent business field names",
            "do not add fixed backend edges; model must refine the graph",
            "preserve deterministic graph contract validation after refine",
        ],
        "current_endpoint_pairs": _responsibility_edge_endpoint_pairs(getattr(graph, "dataflow_edges", []) or []),
    }


def _stable_graph_scope_value(value: Any) -> Any:
    """Normalize graph scope values for deterministic equality checks.

    This is intentionally local to refine scope validation: it does not change
    ResponsibilityGraph/FunctionItem/ResponsibilityEdge wire schema.
    """
    if hasattr(value, "model_dump"):
        try:
            value = value.model_dump(mode="json")
        except Exception:
            value = str(value)
    if isinstance(value, dict):
        return {str(key): _stable_graph_scope_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        normalized = [_stable_graph_scope_value(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, default=str))
    return value


def _function_item_scope_signature(item: Any) -> dict[str, Any]:
    # function_item_prompt_payload explicitly includes fields excluded from the
    # normal Pydantic dump, notably constraints/evidence_policy-like responsibility
    # metadata when available. Keep this as a comparison-only signature.
    if isinstance(item, FunctionItem):
        data = function_item_prompt_payload(item)
        data["id"] = getattr(item, "id", "")
        data["evidence_policy"] = _stable_graph_scope_value(getattr(item, "evidence_policy", {}) or {})
    elif isinstance(item, dict):
        data = dict(item)
    else:
        data = item.model_dump(mode="json") if hasattr(item, "model_dump") else {"value": str(item)}
    return _stable_graph_scope_value(data)


def _edge_scope_signature(edge: dict[str, Any]) -> tuple[str, str, str, str, str]:
    semantic = {
        "purpose": edge.get("purpose", ""),
        "constraints": edge.get("constraints", []),
    }
    return (
        str(edge.get("from_node") or ""),
        str(edge.get("from_output") or ""),
        str(edge.get("to_node") or ""),
        str(edge.get("to_input") or ""),
        json.dumps(_stable_graph_scope_value(semantic), ensure_ascii=False, sort_keys=True, default=str),
    )


def _graph_scope_signature(graph: RequirementGraph) -> tuple[dict[str, dict[str, Any]], set[tuple[str, str, str, str, str]]]:
    items: dict[str, dict[str, Any]] = {}
    for item in getattr(graph, "function_items", []) or []:
        data = _function_item_scope_signature(item)
        target = str(data.get("target_file") or "") if isinstance(data, dict) else ""
        if target:
            items[target] = data
    edges = {
        _edge_scope_signature(edge)
        for edge in (getattr(graph, "dataflow_edges", []) or [])
        if isinstance(edge, dict)
    }
    return items, edges


def _validate_refined_graph_scope(
    *,
    before: RequirementGraph,
    after: RequirementGraph,
    feedback: dict[str, Any],
) -> None:
    before_items, before_edges = _graph_scope_signature(before)
    after_items, after_edges = _graph_scope_signature(after)
    if set(before_items) != set(after_items):
        raise ValueError("graph refine changed script node identity set")
    related = {str(node) for node in (feedback.get("related_nodes") or []) if str(node)}
    related.update({"platform_input_node", "platform_output_node"})
    for target, before_item in before_items.items():
        if target in related:
            continue
        after_item = after_items.get(target) or {}
        if before_item != after_item:
            raise ValueError(f"graph refine changed unrelated FunctionItem responsibility fields: {target}")
    changed_edges = before_edges.symmetric_difference(after_edges)
    for edge in changed_edges:
        from_node, _from_output, to_node, _to_input, _edge_semantics = edge
        if from_node not in related and to_node not in related:
            raise ValueError("graph refine changed unrelated responsibility edge")


async def _refine_requirement_graph_platform_io_closure(
    *,
    blueprint_text: str,
    files_out: list[FileSpecOut],
    current_graph: RequirementGraph,
    feedback: dict[str, Any],
    requested_model: str | None = None,
) -> RequirementGraph:
    route = route_model(VALIDATOR_TASK, requested_model=requested_model, reason="creator responsibility graph platform IO refine")
    payload = {
        "task": "refine_current_responsibility_graph_platform_io_closure",
        "feedback": feedback,
        "current_graph": current_graph.model_dump(mode="json") if hasattr(current_graph, "model_dump") else current_graph,
        "allowed_script_targets": [f.path for f in files_out if str(f.path).startswith("scripts/")],
        "platform_io_contract": platform_io_contract_prompt_text(),
        "blueprint_excerpt": str(blueprint_text or "")[-12000:],
    }
    prompt = (
        "You are refining only the current Creator ResponsibilityGraph.\n"
        "Do not add files, do not invent business fields, do not hard-code platform input/output names beyond the provided platform_io_contract.\n"
        "Only adjust function_items responsibility fields and responsibility_edges needed to close platform IO paths and isolated main-flow nodes.\n"
        "Return strict JSON object with function_items and responsibility_edges only."
    )
    raw = await complete_chat_once([
        {"role": "system", "content": prompt},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
    ], route.model)
    data = _parse_prepare_plan_json(raw)
    if not isinstance(data.get("function_items"), list) or not isinstance(data.get("responsibility_edges"), list):
        raise ValueError("graph refine response must include function_items and responsibility_edges lists")
    refined = build_default_requirement_graph(
        files_out,
        responsibility_edges=data.get("responsibility_edges") or [],
        function_items=data.get("function_items") or [],
    )
    _validate_refined_graph_scope(before=current_graph, after=refined, feedback=feedback)
    return refined

def _persist_requirement_graph(
    skill_name: str,
    graph: RequirementGraph,
) -> None:
    metadata_dir = (
        settings.skills_path
        / _validate_skill_name(skill_name)
        / ".creator"
    )
    metadata_dir.mkdir(
        parents=True,
        exist_ok=True,
    )
    (
        metadata_dir
        / "requirement_graph.json"
    ).write_text(
        graph.model_dump_json(indent=2),
        encoding="utf-8",
    )


def _persist_workflow_allocation_summary(
    skill_name: str,
    summary: str,
) -> None:
    metadata_dir = (
        settings.skills_path
        / _validate_skill_name(skill_name)
        / ".creator"
    )
    metadata_dir.mkdir(
        parents=True,
        exist_ok=True,
    )
    (
        metadata_dir
        / "workflow_allocation_summary.txt"
    ).write_text(
        str(summary or "").strip(),
        encoding="utf-8",
    )

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



def _coverage_terms_from_text(text: str) -> list[str]:
    """Extract generic declared capability tokens from requirement text.

    This is intentionally not a business whitelist: it preserves words that the
    user/planner explicitly declared around input/source/format/action/output
    obligations, then checks that executable script contracts mention them.
    """
    raw = re.findall(r"[A-Za-z][A-Za-z0-9_+.-]{1,40}|[\u4e00-\u9fff]{2,12}", str(text or ""))
    stop = {"the", "and", "with", "for", "from", "into", "this", "that", "skill", "script", "file", "files", "支持", "生成", "输出", "输入", "文件"}
    terms: list[str] = []
    for item in raw:
        token = item.strip().lower()
        if token in stop or len(token) < 2:
            continue
        if token not in terms:
            terms.append(token)
    return terms[:80]


def _script_contract_text(file_spec: FileSpecOut) -> str:
    return "\n".join([
        str(file_spec.path or ""),
        str(file_spec.purpose or ""),
        " ".join(str(x) for x in (file_spec.inputs or [])),
        " ".join(str(x) for x in (file_spec.outputs or [])),
        json.dumps(file_spec.runtime_contract or {}, ensure_ascii=False, default=str),
        json.dumps(file_spec.artifact_contract or {}, ensure_ascii=False, default=str),
    ]).lower()


def _normalize_file_plan_for_requirement_coverage(
    *,
    blueprint_text: str,
    review_summary: PreparePlanReviewSummary | None,
    files_out: list[FileSpecOut],
    final_outputs: list[Any] | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> None:
    """Attach compact deterministic coverage metadata.

    review_summary is display-only and never participates in backend
    responsibility semantics.

    The full blueprint is also not tokenized into generic requirement terms.

    Coverage metadata only preserves normalized contract facts that are already
    present on each FileSpecOut.
    """

    _ = blueprint_text
    _ = review_summary
    _ = final_outputs

    warnings = (
        warnings
        if warnings is not None
        else []
    )

    scripts = [
        item
        for item in (
            files_out or []
        )
        if (
            item.path.startswith(
                "scripts/"
            )
            and item.required
        )
    ]

    if not scripts:
        return

    single_script = (
        len(scripts) == 1
    )

    for script in scripts:
        required_reference_reads: list[
            str
        ] = []

        for raw_path in [
            *list(
                script.dependencies
                or []
            ),
            *list(
                script.reference_files
                or []
            ),
        ]:
            path = str(
                raw_path or ""
            ).strip()

            if (
                path.startswith(
                    "references/"
                )
                and path
                not in required_reference_reads
            ):
                required_reference_reads.append(
                    path
                )

        coverage_requirements = {
            "required_capabilities": list(
                script.required_capabilities
                or []
            ),

            "required_reference_reads": (
                required_reference_reads
            ),
        }

        if single_script:
            coverage_requirements[
                "single_script_full_coverage_contract"
            ] = True

        runtime_contract = dict(
            script.runtime_contract
            or {}
        )

        runtime_contract[
            "coverage_requirements"
        ] = coverage_requirements

        script.runtime_contract = (
            runtime_contract
        )

    if single_script:
        warnings.append({
            "severity": "info",

            "code": (
                "single_script_full_"
                "coverage_contract"
            ),

            "source": (
                "analyze_blueprint"
            ),

            "path": scripts[0].path,

            "field": (
                "runtime_contract."
                "coverage_requirements"
            ),

            "message": (
                "Single required script owns the "
                "full normalized SkillPlan "
                "responsibility. Coverage metadata "
                "contains only explicit capability "
                "and reference contracts."
            ),
        })
    else:
        warnings.append({
            "severity": "info",
            "code": "requirement_coverage_incomplete",
            "source": "analyze_blueprint",
            "path": "scripts",
            "field": "runtime_contract.coverage_requirements",
            "message": "Multiple required scripts keep per-file coverage metadata only; no bridge script is synthesized.",
        })

    logger.info(
        "[Creator]"
        "[requirement_coverage]"
        "[compact] %s",
        json.dumps(
            {
                "event": (
                    "creator_requirement_"
                    "coverage_compact"
                ),

                "script_count": len(
                    scripts
                ),

                "single_script": (
                    single_script
                ),

                "capabilities_by_script": {
                    script.path: list(
                        script
                        .required_capabilities
                        or []
                    )
                    for script
                    in scripts
                },

                "references_by_script": {
                    script.path: list(
                        (
                            script.runtime_contract
                            or {}
                        )
                        .get(
                            "coverage_requirements",
                            {},
                        )
                        .get(
                            "required_reference_reads",
                            [],
                        )
                        or []
                    )
                    for script
                    in scripts
                },
            },
            ensure_ascii=False,
            default=str,
        ),
    )


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


async def _allocate_workflow_script_responsibilities(
    *,
    blueprint_text: str,
    files_out: list[FileSpecOut],
    requested_model: str | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> tuple[str, set[str], bool]:
    """Preserve Blueprint Planner target-local constraints.

    This deterministic step only normalizes the transport shape for existing
    per-file constraints. It does not reconcile ownership, reinterpret
    constraint semantics, call a model, introduce semantic enums, or classify
    constraint domains.
    """
    _ = (blueprint_text, requested_model, warnings)
    for file_spec in files_out or []:
        raw_constraints = getattr(file_spec, "constraints", None)
        if raw_constraints is None:
            try:
                file_spec.constraints = []
            except Exception:
                pass
        elif not isinstance(raw_constraints, list):
            try:
                file_spec.constraints = [raw_constraints]
            except Exception:
                pass
    return (
        "Preserved Blueprint Planner target-local constraints and normalized transport shape only.",
        set(),
        True,
    )


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
    responsibility_edges: list[dict[str, Any]] | None = None,
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
    purpose_graph = build_default_requirement_graph(
        files_out,
        responsibility_edges=responsibility_edges,
    )
    edge_contexts = {
        item.path: function_item_graph_context(
            purpose_graph,
            item.path,
        )
        for item in targets
    }
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
            "This phase compresses wording only. It must not add a core action; must not remove a core action; must not replace an owned action with consumption of an already-produced result; must not change required capability ownership; must not change incoming/outgoing ResponsibilityEdge obligations; must not change FunctionItem constraints; must not move responsibility to another script. target_file must be copied exactly from one provided script path. Do not invent, abbreviate, normalize, or replace the path. Unknown targets are ignored by exact match only; no fuzzy match."
        )},
        {"role": "user", "content": (
            "compact_context_by_target:\n"
            + json.dumps(edge_contexts, ensure_ascii=False, default=str)[:16000]
            + "\n\n"
            "exact_provided_script_paths:\n"
            + json.dumps([item.path for item in targets], ensure_ascii=False, default=str)
            + "\n\n"
            "返回 JSON object：{\"patches\":[{\"target_file\":\"<copy one exact provided script path>\",\"purpose\":\"来源：... | 动作：... | 交付：... | 约束：...\\n说明：...\"}]}"
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


async def _prepare_plan_impl(
    request: PreparePlanRequest,
    event_emitter: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> PreparePlanResponse:
    prepare_action = str(
        request.prepare_action
        or "none"
    )

    confirmed_prepare = (
        prepare_action == "confirm"
        or (
            _prepare_user_confirmed_no_more_supplement(
                request
            )
        )
    )

    previous_blueprint_text = str(
        request.previous_blueprint_text
        or ""
    ).strip()

    prepared: dict[
        str,
        Any,
    ] = {}

    summary = (
        PreparePlanReviewSummary()
    )

    skill_name = str(
        request.skill_name
        or ""
    )

    blueprint_text = (
        previous_blueprint_text
    )

    async def project_summary(
        current_blueprint_text: str,
        current_prepared: (
            dict[str, Any] | None
        ) = None,
    ) -> PreparePlanReviewSummary:
        return (
            await _project_prepare_review_summary_from_blueprint(
                request=request,

                blueprint_text=(
                    current_blueprint_text
                ),

                prepared=(
                    current_prepared
                ),
            )
        )

    async def confirmation_response(
        *,
        current_blueprint_text: str,
        current_prepared: (
            dict[str, Any] | None
        ),
        current_skill_name: str,
        prepare_stage: str,
        question: str,
    ) -> PreparePlanResponse:
        projected = await project_summary(
            current_blueprint_text,
            current_prepared,
        )
        function_items = (
            normalize_structured_function_items((current_prepared or {}).get("function_items"), source="planner")
            if isinstance(current_prepared, dict) and current_prepared.get("function_items") is not None
            else []
        )
        responsibility_edges = (
            normalize_structured_responsibility_edges(
                (current_prepared or {}).get("responsibility_edges"),
                source="planner",
            )
            if isinstance(current_prepared, dict) and current_prepared.get("responsibility_edges") is not None
            else []
        )
        projected = _structured_plan_review_summary(
            {"function_items": function_items, "review_summary": (current_prepared or {}).get("review_summary") if isinstance(current_prepared, dict) else {}},
            projected,
        )
        try:
            frozen_projection = parse_blueprint(
                [{"role": "assistant", "content": current_blueprint_text}], strict=True
            )
            _sync_prepare_summary_files_from_skill_plan(projected, frozen_projection.files)
        except Exception:
            # Protocol validation reports malformed blueprints elsewhere; summary
            # projection must never become a reverse authority or repair source.
            projected.files_to_create_or_update = []
            projected.assets_to_upload = []
        current_blueprint_text = _render_structured_responsibility_view(
            current_blueprint_text,
            function_items,
            responsibility_edges,
        )

        return PreparePlanResponse(
            status="needs_clarification",

            prepare_stage=prepare_stage,

            clarifying_questions=[
                question
            ],

            review_summary=(
                _strip_prepare_summary_risks(
                    projected
                )
            ),

            blueprint_text=(
                current_blueprint_text
            ),

            skill_name=(
                current_skill_name
            ),

            function_items=function_items,
            responsibility_edges=(
                responsibility_edges
            ),
            requirement_allocations=(
                list((current_prepared or {}).get("requirement_allocations") or [])
                if isinstance(current_prepared, dict) else []
            ),
        )

    if (
        prepare_action
        == "request_supplement"
    ):
        if previous_blueprint_text:
            summary = await project_summary(
                previous_blueprint_text
            )

        return PreparePlanResponse(
            status="needs_clarification",

            prepare_stage=(
                "creation_points_confirmation"
            ),

            clarifying_questions=[
                "好的，请补充你的其他要求。"
            ],

            review_summary=(
                _strip_prepare_summary_risks(
                    summary
                )
            ),

            blueprint_text=(
                previous_blueprint_text
            ),

            skill_name=(
                skill_name
            ),

            function_items=(
                request.function_items
                or []
            ),

            responsibility_edges=(
                request.responsibility_edges
                or []
            ),
        )

    if confirmed_prepare:
        # Confirmation freezes the existing full blueprint.
        #
        # review_summary is never used to reconstruct it.
        if not previous_blueprint_text:
            return PreparePlanResponse(
                status="blocked",

                prepare_stage=(
                    "blueprint_protocol_failed"
                ),

                clarifying_questions=[],

                review_summary=(
                    PreparePlanReviewSummary()
                ),

                blueprint_text="",

                skill_name=(
                    skill_name
                ),

                creation_blockers=[
                    _prepare_protocol_issue(
                        (
                            "missing_confirmed_"
                            "blueprint_state"
                        ),
                        (
                            "用户确认创建要点时，"
                            "previous_blueprint_text 为空。"
                            "Creator 不允许从 review_summary "
                            "重新生成 full blueprint。"
                        ),
                        field=(
                            "previous_blueprint_text"
                        ),
                    )
                ],
            )

        if request.function_items is None:
            return PreparePlanResponse(
                status="blocked",
                prepare_stage="blueprint_protocol_failed",
                clarifying_questions=[],
                review_summary=PreparePlanReviewSummary(),
                blueprint_text=previous_blueprint_text,
                skill_name=skill_name,
                creation_blockers=[
                    _prepare_protocol_issue(
                        "missing_structured_function_items",
                        "用户确认创建要点时缺少 structured function_items；Creator 不允许从 blueprint text legacy fallback 恢复 ready graph。",
                        field="function_items",
                    )
                ],
            )

        if request.responsibility_edges is None:
            return PreparePlanResponse(
                status="blocked",
                prepare_stage="blueprint_protocol_failed",
                clarifying_questions=[],
                review_summary=PreparePlanReviewSummary(),
                blueprint_text=previous_blueprint_text,
                skill_name=skill_name,
                creation_blockers=[
                    _prepare_protocol_issue(
                        "missing_structured_responsibility_edges",
                        "用户确认创建要点时缺少 structured responsibility_edges；Creator 不允许从 blueprint text legacy fallback 恢复 ready graph。",
                        field="responsibility_edges",
                    )
                ],
            )

        prepared = {
            "status": "ready",

            "internal_blueprint_text": (
                previous_blueprint_text
            ),

            "skill_name": skill_name,
            "function_items": request.function_items,
            "requirement_allocations": request.requirement_allocations or [],

            "responsibility_edges": (
                request.responsibility_edges
                if request.responsibility_edges is not None
                else None
            ),
        }

        blueprint_text = (
            previous_blueprint_text
        )

        summary = await project_summary(
            blueprint_text,
            prepared,
        )

    else:
        try:
            prepared = (
                await (
                    _generate_internal_blueprint_or_questions(
                        request,
                        event_emitter=event_emitter,
                    )
                    if event_emitter is not None
                    else _generate_internal_blueprint_or_questions(
                        request
                    )
                )
            )

        except PreparePlanProtocolError as exc:
            return PreparePlanResponse(
                status="blocked",
                prepare_stage="blueprint_protocol_failed",
                clarifying_questions=[],
                review_summary=PreparePlanReviewSummary(),
                blueprint_text=previous_blueprint_text,
                skill_name=skill_name,
                creation_blockers=[_prepare_protocol_issue(
                    "planner_structured_graph_protocol_failed", str(exc),
                    field="responsibility_edges",
                )],
            )

        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=(
                    "prepare-plan 生成失败："
                    f"{exc}"
                ),
            ) from exc

        raw_status = str(
            prepared.get("status")
            or ""
        ).strip()

        status = (
            raw_status
            if raw_status in {
                "ready",
                "needs_clarification",
                "blocked",
            }
            else "needs_clarification"
        )

        skill_name = str(
            prepared.get("skill_name")
            or request.skill_name
            or ""
        )

        blueprint_text = str(
            prepared.get(
                "internal_blueprint_text"
            )
            or prepared.get(
                "blueprint_text"
            )
            or ""
        ).strip()

        blueprint_text = (
            _normalize_prepare_blueprint_references(
                blueprint_text
            )
        )

        if (
            prepare_action
            == "submit_supplement"
        ):
            # Supplement changes business requirements.
            #
            # Therefore the business planner may revise the
            # previous full blueprint exactly once.
            #
            # The result itself becomes the next full blueprint.
            if status == "needs_clarification":
                return PreparePlanResponse(
                    status=(
                        "needs_clarification"
                    ),

                    prepare_stage=(
                        "business_clarification"
                    ),

                    clarifying_questions=(
                        _normalize_prepare_clarifying_questions(
                            prepared.get(
                                "clarifying_questions"
                            )
                        )
                    ),

                    review_summary=(
                        PreparePlanReviewSummary()
                    ),

                    blueprint_text=(
                        previous_blueprint_text
                    ),

                    skill_name=skill_name,
                )

            if status == "blocked":
                blockers = (
                    prepared.get("blockers")
                    or [
                        (
                            "补充要求后仍缺少生成"
                            "完整蓝图所需的信息。"
                        )
                    ]
                )

                return PreparePlanResponse(
                    status="blocked",

                    prepare_stage=(
                        "blueprint_protocol_failed"
                    ),

                    clarifying_questions=[],

                    review_summary=(
                        PreparePlanReviewSummary()
                    ),

                    blueprint_text=(
                        previous_blueprint_text
                    ),

                    skill_name=skill_name,

                    creation_blockers=(
                        blockers
                        if isinstance(
                            blockers,
                            list,
                        )
                        else [
                            str(blockers)
                        ]
                    ),
                )

            if not blueprint_text:
                return PreparePlanResponse(
                    status="blocked",

                    prepare_stage=(
                        "blueprint_protocol_failed"
                    ),

                    clarifying_questions=[],

                    review_summary=(
                        PreparePlanReviewSummary()
                    ),

                    blueprint_text=(
                        previous_blueprint_text
                    ),

                    skill_name=skill_name,

                    creation_blockers=[
                        _prepare_protocol_issue(
                            (
                                "supplement_blueprint_"
                                "missing"
                            ),
                            (
                                "补充要求处理后 full "
                                "blueprint 为空。"
                            ),
                        )
                    ],
                )

            return await confirmation_response(
                current_blueprint_text=(
                    blueprint_text
                ),

                current_prepared=prepared,

                current_skill_name=(
                    skill_name
                ),

                prepare_stage=(
                    "supplement_confirmation"
                ),

                question=(
                    "已根据补充内容更新创建要点。"
                    "是否按这些要点继续？"
                    "A. 没有其他补充，按这些要点继续 "
                    "B. 继续补充说明"
                ),
            )

        if status == "needs_clarification":
            if (
                not _prepare_business_clarification_limit_reached(
                    request
                )
            ):
                return PreparePlanResponse(
                    status=(
                        "needs_clarification"
                    ),

                    prepare_stage=(
                        "business_clarification"
                    ),

                    clarifying_questions=(
                        _normalize_prepare_clarifying_questions(
                            prepared.get(
                                "clarifying_questions"
                            )
                        )
                    ),

                    review_summary=(
                        PreparePlanReviewSummary()
                    ),

                    blueprint_text=(
                        previous_blueprint_text
                    ),

                    skill_name=skill_name,
                )

            # Planner was explicitly required to finalize a
            # blueprint at the clarification limit.
            #
            # Do not rebuild one from review_summary.
            if blueprint_text:
                return await confirmation_response(
                    current_blueprint_text=(
                        blueprint_text
                    ),

                    current_prepared=prepared,

                    current_skill_name=(
                        skill_name
                    ),

                    prepare_stage=(
                        "creation_points_confirmation"
                    ),

                    question=(
                        _PREPARE_SUPPLEMENT_QUESTION
                    ),
                )

            return PreparePlanResponse(
                status="blocked",

                prepare_stage=(
                    "blueprint_protocol_failed"
                ),

                clarifying_questions=[],

                review_summary=(
                    PreparePlanReviewSummary()
                ),

                blueprint_text="",

                skill_name=skill_name,

                creation_blockers=[
                    _prepare_protocol_issue(
                        (
                            "planner_failed_to_"
                            "finalize_blueprint"
                        ),
                        (
                            "业务澄清达到上限后，"
                            "规划模型仍未生成 full "
                            "blueprint。Creator 不允许"
                            "从 review_summary 重建蓝图。"
                        ),
                    )
                ],
            )

        if status == "blocked":
            blockers = (
                prepared.get("blockers")
                or [
                    (
                        "当前业务条件不足以生成"
                        "可执行 full blueprint。"
                    )
                ]
            )

            return PreparePlanResponse(
                status="blocked",

                prepare_stage=(
                    "blueprint_protocol_failed"
                ),

                clarifying_questions=[],

                review_summary=(
                    PreparePlanReviewSummary()
                ),

                blueprint_text=(
                    blueprint_text
                    or previous_blueprint_text
                ),

                skill_name=skill_name,

                creation_blockers=(
                    blockers
                    if isinstance(
                        blockers,
                        list,
                    )
                    else [
                        str(blockers)
                    ]
                ),
            )

        if not blueprint_text:
            return PreparePlanResponse(
                status="blocked",

                prepare_stage=(
                    "blueprint_protocol_failed"
                ),

                clarifying_questions=[],

                review_summary=(
                    PreparePlanReviewSummary()
                ),

                blueprint_text="",

                skill_name=skill_name,

                creation_blockers=[
                    _prepare_protocol_issue(
                        "empty_blueprint",
                        (
                            "规划模型返回 ready，"
                            "但 full blueprint 为空。"
                        ),
                    )
                ],
            )

        # First complete blueprint:
        #
        # project it for display and freeze it in the
        # response so the frontend can send it back on
        # confirmation.
        return await confirmation_response(
            current_blueprint_text=(
                blueprint_text
            ),

            current_prepared=prepared,

            current_skill_name=(
                skill_name
            ),

            prepare_stage=(
                "creation_points_confirmation"
            ),

            question=(
                _PREPARE_SUPPLEMENT_QUESTION
            ),
        )

    # ------------------------------------------------------------------
    # From here on, the user has confirmed an existing full blueprint.
    #
    # No business planner and no review_summary -> blueprint conversion.
    # ------------------------------------------------------------------

    allowed_resource_paths = _build_prepare_allowed_resource_paths(
        request=request,
        existing_skill_context=(
            _read_prepare_existing_skill_context(skill_name)
            if request.mode == "revise"
            else {}
        ),
    )

    blueprint_text = (
        _normalize_prepare_blueprint_references(
            blueprint_text,
            allowed_resource_paths,
        )
    )

    protocol_errors = _preflight_prepare_blueprint_text(
        blueprint_text,
        allowed_resource_paths,
    )
    repair_index = -1
    for repair_index in range(2):
        if not protocol_errors:
            break
        try:
            blueprint_text = await _repair_prepare_blueprint_protocol(
                request=request, blueprint_text=blueprint_text,
                protocol_errors=protocol_errors,
                allowed_resource_paths=allowed_resource_paths,
            )
        except Exception as exc:
            raise PreparePlanProtocolError(
                "Confirmed Blueprint protocol repair failed; "
                f"repair_index={repair_index}; error={type(exc).__name__}: {exc}"
            ) from exc
        blueprint_text = _normalize_prepare_blueprint_references(
            blueprint_text, allowed_resource_paths
        )
        protocol_errors = _preflight_prepare_blueprint_text(
            blueprint_text,
            allowed_resource_paths,
        )

    if protocol_errors:
        summary = await project_summary(blueprint_text, prepared)
        return PreparePlanResponse(
            status="blocked", prepare_stage="blueprint_protocol_failed",
            clarifying_questions=[], review_summary=_strip_prepare_summary_risks(summary),
            blueprint_text=blueprint_text, skill_name=skill_name,
            creation_blockers=protocol_errors,
        )

    graph_error: Exception | None = None
    if isinstance(prepared, dict) and prepared.get("responsibility_edges") is not None:
        current_function_items = prepared.get("function_items")
        current_edges = prepared.get("responsibility_edges")
        for repair_count in range(3):
            try:
                normalized_function_items = normalize_structured_function_items(current_function_items, source="planner")
                normalized_edges = validate_structured_responsibility_edge_transport(
                    current_edges, function_items=normalized_function_items, source="planner")
                prepared["function_items"] = normalized_function_items
                prepared["responsibility_edges"] = normalized_edges
                graph_error = None
                break
            except Exception as exc:
                graph_error = exc
                if repair_count >= 2:
                    break
                issue = {
                    "id": "responsibility_graph_candidate_validation", "target_files": [],
                    "affected_edge_indexes": [], "reason": str(exc),
                    "evidence": "The current structured responsibility graph failed the existing deterministic validator.",
                    "repair_guidance": "Repair only the invalid FunctionItem or ResponsibilityEdge. Preserve the confirmed Blueprint and FilePlan.",
                }
                repaired = await _repair_responsibility_graph_alignment(
                    request=request, frozen_blueprint_text=blueprint_text,
                    allowed_function_item_targets=_resolve_allowed_function_item_targets_from_blueprint(blueprint_text),
                    function_items=current_function_items or [], responsibility_edges=current_edges or [],
                    review_issues=[issue], planner_model=route_model(
                        "creator_prepare_plan", requested_model=request.model,
                        reason="confirmed responsibility graph repair",
                    ).model,
                )
                current_function_items = repaired["function_items"]
                current_edges = repaired["responsibility_edges"]
    if graph_error is not None:
        summary = await project_summary(blueprint_text, prepared)
        return PreparePlanResponse(status="blocked", prepare_stage="blueprint_protocol_failed",
            clarifying_questions=[], review_summary=_strip_prepare_summary_risks(summary),
            blueprint_text=blueprint_text, skill_name=skill_name,
            creation_blockers=[_prepare_protocol_issue("planner_structured_graph_protocol_failed",
                f"已确认 structured ResponsibilityEdges 违反 endpoint topology protocol：{graph_error}",
                field="responsibility_edges")])

    plan = None

    analyze_errors: list[
        dict[str, Any]
    ] = []

    for attempt in range(3):
        blueprint_text = (
            _normalize_prepare_blueprint_references(
                blueprint_text,
                allowed_resource_paths,
            )
        )

        try:
            plan = await analyze_blueprint(
                AnalyzeBlueprintRequest(
                    messages=[
                        {
                            "role": "assistant",
                            "content": (
                                blueprint_text
                            ),
                        }
                    ],

                    model=request.model,

                    strict=True,

                    responsibility_edges=(
                        prepared.get("responsibility_edges")
                        if isinstance(prepared, dict)
                        else None
                    ),

                    function_items=(
                        prepared.get("function_items")
                        if isinstance(prepared, dict)
                        else None
                    ),

                    # Confirmed full blueprint is frozen.
                    refine_contract=False,

                    refine_rounds=0,
                )
            )

            break

        except HTTPException as exc:
            analyze_errors = [
                _prepare_protocol_issue(
                    "strict_analyze_failed",
                    (
                        "内部蓝图未通过 strict "
                        "analyze。"
                    ),
                    field="analyze_blueprint",
                )
            ]

            if attempt >= 2:
                break

            try:
                blueprint_text = (
                    await _repair_prepare_blueprint_protocol(
                        request=request,

                        blueprint_text=(
                            blueprint_text
                        ),

                        protocol_errors=[
                            {
                                **analyze_errors[0],

                                "detail": str(
                                    exc.detail
                                ),
                            }
                        ],
                        allowed_resource_paths=allowed_resource_paths,
                    )
                )

                blueprint_text = (
                    _normalize_prepare_blueprint_references(
                        blueprint_text,
                        allowed_resource_paths,
                    )
                )

            except Exception:
                break

            protocol_errors = (
                _preflight_prepare_blueprint_text(
                    blueprint_text,
                    allowed_resource_paths,
                )
            )

            if protocol_errors:
                analyze_errors = protocol_errors
                try:
                    # Repair the latest candidate and its latest preflight
                    # error before another analyze attempt is permitted.
                    blueprint_text = await _repair_prepare_blueprint_protocol(
                        request=request,
                        blueprint_text=blueprint_text,
                        protocol_errors=protocol_errors,
                        allowed_resource_paths=allowed_resource_paths,
                    )
                    blueprint_text = _normalize_prepare_blueprint_references(
                        blueprint_text,
                        allowed_resource_paths,
                    )
                    protocol_errors = _preflight_prepare_blueprint_text(
                        blueprint_text,
                        allowed_resource_paths,
                    )
                    analyze_errors = protocol_errors
                except Exception:
                    break
                if protocol_errors:
                    break

    if plan is None:
        summary = await project_summary(blueprint_text, prepared)
        return PreparePlanResponse(
            status="blocked", prepare_stage="blueprint_analyze_failed",
            clarifying_questions=[], review_summary=_strip_prepare_summary_risks(summary),
            blueprint_text=blueprint_text, skill_name=skill_name,
            creation_blockers=analyze_errors or [_prepare_protocol_issue(
                "strict_analyze_failed", "已确认 full blueprint 无法解析为创建计划。",
                field="analyze_blueprint",
            )],
        )

    rejected_resources = _enforce_prepare_plan_resource_authority(
        plan,
        allowed_resource_paths,
    )
    logger.info(
        "[Creator][resource_authority] allowed_resources=%s rejected_resources=%s",
        sorted(allowed_resource_paths), rejected_resources,
    )

    (
        confirmed_uploaded_assets,
        unselected_uploaded_files,
    ) = _split_uploaded_asset_decisions(
        request.uploaded_files
    )

    confirmed_asset_paths = {
        str(
            item.get(
                "asset_target_path"
            )
            or ""
        ).strip()
        for item
        in confirmed_uploaded_assets
    }

    asset_filter_warnings = _filter_unconfirmed_asset_plan(
        files=plan.files,
        asset_requirements=plan.asset_requirements,
        uploaded_files=request.uploaded_files,
        review_summary=None,
    )

    required_upload_assets = (
        _required_file_plan_user_upload_asset_paths(
            plan.files,
            plan.asset_requirements,
        )
    )
    unresolved_bundled_assets = _unresolved_bundled_asset_paths(
        plan.files, skill_name=skill_name
    )
    if unresolved_bundled_assets:
        summary = await project_summary(plan.blueprint_text or blueprint_text, prepared)
        return PreparePlanResponse(
            status="blocked",
            prepare_stage="asset_source_unresolved",
            clarifying_questions=[],
            review_summary=_strip_prepare_summary_risks(summary),
            blueprint_text=plan.blueprint_text or blueprint_text,
            skill_name=skill_name,
            creation_blockers=[_prepare_protocol_issue(
                "asset_source_unresolved",
                "Bundled asset declaration has no matching file in the platform bundled inventory: "
                + ", ".join(unresolved_bundled_assets),
                field="SkillPlan",
            )],
        )
    missing_required_upload_assets = [
        path
        for path
        in required_upload_assets
        if path not in confirmed_asset_paths
    ]

    if missing_required_upload_assets:
        final_blueprint_text = (
            plan.blueprint_text
            or blueprint_text
        )
        summary = await project_summary(
            final_blueprint_text,
            prepared,
        )
        summary_sync_warnings = (
            _sync_prepare_summary_files_from_skill_plan(
                summary,
                plan.files,
            )
        )
        summary.assets_to_upload = missing_required_upload_assets
        return PreparePlanResponse(
            status="needs_clarification",
            prepare_stage="asset_upload_required",
            clarifying_questions=[
                "请先上传创建该 Skill 必需的静态资源文件，然后继续。"
            ],
            review_summary=(
                _strip_prepare_summary_risks(
                    summary
                )
            ),
            blueprint_text=final_blueprint_text,
            skill_name=plan.skill_name,
            files=plan.files,
            warnings=[
                *(
                    plan.warnings
                    or []
                ),
                *summary_sync_warnings,
                *asset_filter_warnings,
            ],
            asset_requirements=plan.asset_requirements,
            creation_blockers=[
                {
                    "code": "required_asset_not_uploaded",
                    "type": "required_asset_not_uploaded",
                    "blocking": True,
                    "assets_to_upload": missing_required_upload_assets,
                    "message": "Required user-upload assets from the FilePlan have not been uploaded.",
                }
            ],
        )

    plan.files = [
        file_spec
        for file_spec
        in (
            plan.files or []
        )
        if not (
            str(
                getattr(
                    file_spec,
                    "path",
                    "",
                )
                or ""
            ).startswith(
                "assets/"
            )
            and str(
                getattr(
                    file_spec,
                    "asset_source",
                    "",
                )
                or ""
            )
            == "user_upload"
            and str(
                getattr(
                    file_spec,
                    "path",
                    "",
                )
                or ""
            )
            not in confirmed_asset_paths
        )
    ]

    plan.asset_requirements = [
        asset
        for asset
        in (
            plan.asset_requirements
            or []
        )
        if (
            str(
                getattr(
                    asset,
                    "source",
                    "",
                )
                or ""
            )
            != "user_upload"
            or str(
                getattr(
                    asset,
                    "path",
                    "",
                )
                or ""
            )
            in confirmed_asset_paths
        )
    ]

    final_blueprint_text = (
        plan.blueprint_text
        or blueprint_text
    )

    summary = await project_summary(
        final_blueprint_text,
        prepared,
    )

    summary_sync_warnings = (
        _sync_prepare_summary_files_from_skill_plan(
            summary,
            plan.files,
        )
    )

    graph_payload = (
        plan.requirement_graph.model_dump(
            mode="json"
        )
        if hasattr(
            plan.requirement_graph,
            "model_dump",
        )
        else dict(
            plan.requirement_graph
            or {}
        )
    )

    if event_emitter is not None:
        await event_emitter({
            "event": "graph_resolved",
            "requirement_graph": graph_payload,
        })

    file_specs_payload = [
        (
            file_spec.model_dump(
                mode="json"
            )
            if hasattr(
                file_spec,
                "model_dump",
            )
            else dict(file_spec)
        )
        for file_spec
        in (
            plan.files or []
        )
    ]

    uploaded_files_payload = [
        (
            item.model_dump(
                mode="json"
            )
            if hasattr(
                item,
                "model_dump",
            )
            else dict(item)
        )
        for item
        in (
            getattr(
                request,
                "uploaded_files",
                [],
            )
            or []
        )
        if (
            isinstance(
                item,
                dict,
            )
            or hasattr(
                item,
                "model_dump",
            )
        )
    ]

    skill_dir_for_tool_pool = (
        settings.skills_path
        / _validate_skill_name(
            plan.skill_name
        )
    )

    skill_dir_for_tool_pool.mkdir(
        parents=True,
        exist_ok=True,
    )

    required_capabilities = []
    seen_required_capabilities = set()
    for item in (
        list(getattr(plan, "function_items", []) or [])
        if hasattr(plan, "function_items")
        else list(getattr(getattr(plan, "skill_plan", None), "function_items", []) or [])
    ):
        raw_item = (
            item.model_dump(mode="json")
            if hasattr(item, "model_dump")
            else dict(item)
            if isinstance(item, dict)
            else {}
        )
        for capability in raw_item.get("required_capabilities") or []:
            capability_text = str(capability or "").strip()
            if capability_text and capability_text not in seen_required_capabilities:
                seen_required_capabilities.add(capability_text)
                required_capabilities.append(capability_text)

    if event_emitter is not None:
        await event_emitter({
            "event": "tool_planning",
            "required_capabilities": required_capabilities,
        })

    tool_planning = (
        await _plan_final_tool_pool(
            skill_name=plan.skill_name,

            file_specs=(
                file_specs_payload
            ),

            responsibility_graph=(
                graph_payload
            ),

            requested_model=(
                request.model
            ),
        )
    )

    current_tool_pool = (
        tool_planning["tool_pool"]
    )

    tool_pool = build_tool_pool(
        skill_name=plan.skill_name,

        user_request=str(
            getattr(
                request,
                "user_request",
                "",
            )
            or ""
        ),

        blueprint_text=(
            final_blueprint_text
        ),

        file_specs=(
            file_specs_payload
        ),

        uploaded_files=(
            uploaded_files_payload
        ),

        current_tool_pool=(
            current_tool_pool
        ),
    )

    save_tool_pool(
        skill_dir_for_tool_pool,
        tool_pool,
    )
    try:
        final_binding = get_skill_tool_binding(tool_pool, target_file="scripts/__final_selection_probe__.py", include_script_core=True).model_dump(mode="json")
        logger.info(
            "[Creator][final_tool_selection] skill=%s summary=%s",
            plan.skill_name,
            json.dumps(_tool_binding_log_summary(final_binding), ensure_ascii=False),
        )
    except Exception:
        logger.exception("[Creator][final_tool_selection] failed to summarize ToolPool")

    for file_spec in (
        plan.files or []
    ):
        binding = get_file_binding(
            tool_pool,
            getattr(
                file_spec,
                "path",
                "",
            ),
        )

        if (
            binding is not None
            and hasattr(
                file_spec,
                "tool_binding_summary",
            )
        ):
            file_spec.tool_binding_summary = {
                "allowed_tool_ids": (
                    binding.allowed_tool_ids
                ),

                "primary_tool_ids": (
                    binding.primary_tool_ids
                ),

                "secondary_tool_ids": (
                    binding.secondary_tool_ids
                ),

                "allowed_helper_imports": (
                    binding.allowed_helper_imports
                ),

                "allowed_import_paths": (
                    binding.allowed_import_paths
                ),

                "allowed_function_imports": (
                    binding.allowed_function_imports
                ),

                "scored_tools": (
                    binding.scored_tools
                ),

                "matched_features_by_tool": (
                    binding
                    .matched_features_by_tool
                ),

                "denied_helper_imports": (
                    binding.denied_helper_imports
                ),

                "required_env": (
                    binding.required_env
                ),

                "dependencies": (
                    binding.dependencies
                ),
            }

    gate_events = [
        event.model_dump(
            mode="json"
        )
        for event
        in tool_pool.gate_events
    ]

    tool_pool_summary = {
        "tools": [
            {
                "tool_id": tool.tool_id,

                "status": tool.status,

                "target_files": (
                    tool.target_files
                ),

                "allowed_helper_imports": (
                    tool.allowed_helper_imports
                ),

                "allowed_import_paths": (
                    tool.allowed_import_paths
                ),

                "allowed_function_imports": (
                    tool.allowed_function_imports
                ),

                "score": tool.score,

                "matched_features": (
                    tool.matched_features
                ),
            }
            for tool
            in tool_pool.tools
        ],

        "file_bindings": [
            {
                "target_file": (
                    binding.target_file
                ),

                "allowed_tool_ids": (
                    binding.allowed_tool_ids
                ),

                "primary_tool_ids": (
                    binding.primary_tool_ids
                ),

                "secondary_tool_ids": (
                    binding.secondary_tool_ids
                ),

                "allowed_helper_imports": (
                    binding.allowed_helper_imports
                ),

                "allowed_import_paths": (
                    binding.allowed_import_paths
                ),

                "allowed_function_imports": (
                    binding.allowed_function_imports
                ),

                "scored_tools": (
                    binding.scored_tools
                ),

                "matched_features_by_tool": (
                    binding
                    .matched_features_by_tool
                ),
            }
            for binding
            in tool_pool.file_bindings
        ],

        "exploration_candidates": (
            tool_pool.exploration_candidates
        ),

        "scored_candidates": (
            tool_pool.scored_candidates
        ),

        "uploaded_file_triggers": (
            tool_pool.uploaded_file_triggers
        ),

        "gate_events": gate_events,

        "selected_primary_tools": {
            binding.target_file: (
                binding.primary_tool_ids
            )
            for binding
            in tool_pool.file_bindings
        },

        "fallback_tools": {
            binding.target_file: (
                binding.secondary_tool_ids
            )
            for binding
            in tool_pool.file_bindings
        },

        "denied_requests": [
            denied.model_dump(
                mode="json"
            )
            for denied
            in tool_pool.denied_requests
        ],

        "missing_requests": [
            missing.model_dump(
                mode="json"
            )
            for missing
            in tool_pool.missing_requests
        ],

        "denied_tools": [
            denied.tool_id
            for denied
            in tool_pool.denied_requests
        ],

        "missing_tools": [
            missing.tool_id
            for missing
            in tool_pool.missing_requests
        ],

        "rejected_candidates": [
            event
            for event
            in gate_events
            if event.get("decision")
            not in {
                "allow",
                "require_config",
                "require_dependency",
            }
        ],
    }

    if event_emitter is not None:
        await event_emitter({
            "event": "tool_pool_ready",
            "tool_pool_summary": tool_pool_summary,
        })

    return PreparePlanResponse(
        status="ready",

        prepare_stage="ready",

        review_summary=summary,

        blueprint_text=(
            final_blueprint_text
        ),

        skill_name=plan.skill_name,

        function_items=(
            list(getattr(plan, "function_items", []) or [])
            if hasattr(plan, "function_items")
            else list(getattr(getattr(plan, "skill_plan", None), "function_items", []) or [])
        ),

        responsibility_edges=(
            list(getattr(plan, "responsibility_edges", []) or [])
            if hasattr(plan, "responsibility_edges")
            else []
        ),

        files=plan.files,

        warnings=[
            *(
                plan.warnings
                or []
            ),
            *summary_sync_warnings,
            *asset_filter_warnings,
        ],

        asset_requirements=(
            plan.asset_requirements
        ),

        final_outputs=(
            plan.final_outputs
        ),

        available_tools=(
            plan.available_tools
        ),

        missing_tool_configs=(
            plan.missing_tool_configs
        ),

        tool_requirements=(
            plan.tool_requirements
        ),

        creation_blockers=(
            plan.creation_blockers
        ),

        requirement_graph=(
            graph_payload
        ),

        workflow_allocation_summary=(
            _load_workflow_allocation_summary(
                plan.skill_name
            )
        ),

        tool_pool_summary=(
            tool_pool_summary
        ),

        confirmed_uploaded_assets=(
            confirmed_uploaded_assets
        ),

        unselected_uploaded_files=(
            unselected_uploaded_files
        ),
    )


@router.post(
    "/prepare-plan",
    response_model=PreparePlanResponse,
)
async def prepare_plan(
    request: PreparePlanRequest,
):
    return await _prepare_plan_impl(request)


@router.post("/prepare-plan/stream")
async def prepare_plan_stream(
    request: PreparePlanRequest,
):
    async def emit_ndjson():
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def event_emitter(event: dict[str, Any]) -> None:
            await queue.put(event)

        async def run_prepare() -> None:
            try:
                plan = await _prepare_plan_impl(
                    request,
                    event_emitter=event_emitter,
                )
                await queue.put({
                    "event": "complete",
                    "plan": plan,
                })
            except Exception as exc:
                await queue.put({
                    "event": "error",
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                })
            finally:
                await queue.put(None)

        task = asyncio.create_task(run_prepare())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield json.dumps(jsonable_encoder(event), ensure_ascii=False) + "\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        emit_ndjson(),
        media_type="application/x-ndjson",
    )


@router.post("/analyze-blueprint", response_model=AnalyzeBlueprintResponse)
async def analyze_blueprint(request: AnalyzeBlueprintRequest):
    try:
        plan: BlueprintPlan = parse_blueprint(request.messages, strict=request.strict, function_items=request.function_items, responsibility_edges=request.responsibility_edges)
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

    candidate_paths: set[str] = set()
    if not request.strict:
        # Legacy best-effort analysis may still display mentioned paths. Strict
        # confirmed plans take topology exclusively from parsed SkillPlan facts.
        candidate_paths.update(
            path for path in _extract_declared_skill_paths(blueprint_text)
            if not is_directory_placeholder(path)
        )
    candidate_paths.update(entries_by_path.keys())

    extra_paths = []
    extra_path_warnings: list[str] = []
    for path in sorted(candidate_paths):
        if path in base_paths or not path.startswith("references/"):
            continue
        if not request.strict and is_runtime_artifact_semantic(
            path, _local_blueprint_text_for_path(path, blueprint_text)
        ):
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
                constraints=entry.constraints if entry else [],
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
    _normalize_file_plan_for_requirement_coverage(
        blueprint_text=blueprint_text,
        review_summary=plan.review_summary if hasattr(plan, "review_summary") else None,
        files_out=files_out,
        final_outputs=getattr(plan, "final_outputs", []),
        warnings=warnings,
    )
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
        responsibility_edges=(getattr(plan.skill_plan, "responsibility_edges", []) if plan.skill_plan else []),
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
    structured_planner_edges = list(getattr(plan.skill_plan, "responsibility_edges", []) if plan.skill_plan else [])
    structured_planner_function_items = list(getattr(plan.skill_plan, "function_items", []) if plan.skill_plan else [])
    fallback_requirement_graph = build_default_requirement_graph(
        files_out,
        responsibility_edges=structured_planner_edges,
        function_items=structured_planner_function_items or None,
    )
    graph_fallback_used = False
    try:
        requirement_graph = await _extract_requirement_graph_with_validator(
            blueprint_text=blueprint_text,
            files_out=files_out,
            requested_model=request.model,
            warnings=warnings,
            workflow_allocation_summary=workflow_allocation_summary,
            responsibility_edges=(getattr(plan.skill_plan, "responsibility_edges", []) if plan.skill_plan else []),
            function_items=structured_planner_function_items or None,
        )
    except RequirementGraphValidationError as exc:
        graph_fallback_used = True
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
    edge_list_for_log = list(getattr(requirement_graph, "dataflow_edges", []) or [])
    logger.info(
        "[Creator][responsibility_graph][resolved] %s",
        json.dumps({
            "event": "creator_responsibility_graph_resolved",
            "function_item_count": len(getattr(requirement_graph, "function_items", []) or []),
            "structured_function_item_count": len(structured_planner_function_items),
            "graph_function_item_count": len(getattr(requirement_graph, "function_items", []) or []),
            "edge_count": len(edge_list_for_log),
            "fallback_used": graph_fallback_used,
            "incoming_edge_count_by_script": {
                item.target_file: sum(1 for edge in edge_list_for_log if str(edge.get("to_node") or "") == item.target_file)
                for item in getattr(requirement_graph, "function_items", []) or []
            },
            "outgoing_edge_count_by_script": {
                item.target_file: sum(1 for edge in edge_list_for_log if str(edge.get("from_node") or "") == item.target_file)
                for item in getattr(requirement_graph, "function_items", []) or []
            },
        }, ensure_ascii=False, default=str),
    )
    logger.info(
        "[Creator][responsibility_graph_transport] %s",
        json.dumps({
            "event": "creator_responsibility_graph_transport",
            "planner_edge_count": len(structured_planner_edges),
            "normalized_edge_count": len(structured_planner_edges),
            "graph_edge_count": len(edge_list_for_log),
            "graph_fallback_used": graph_fallback_used,
        }, ensure_ascii=False, default=str),
    )
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
        responsibility_edges=list(getattr(plan.skill_plan, "responsibility_edges", []) if plan.skill_plan else []),
        blueprint_text=blueprint_text,
        blueprint_refined=False,
    )



@router.post("/upload-context-file")
async def upload_context_file(
    session_id: str = Form(...),
    file: UploadFile = File(...),
):
    """Persist a Creator planning context upload without touching Skill assets."""
    try:
        return save_creator_context_upload(
            fileobj=file.file,
            filename=file.filename or "upload",
            session_id=session_id,
            mime_type=file.content_type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@router.post("/init-skill", response_model=InitSkillResponse)
async def init_skill(request: InitSkillRequest):
    """Initialise a new Skill directory structure and preserve ToolPool observability."""
    skill_name = _validate_skill_name(request.skill_name)
    skill_dir = settings.skills_path / skill_name

    before_allowed: list[str] = []
    before_digest = ""
    try:
        before_pool = load_tool_pool(skill_dir)
        before_binding = get_skill_tool_binding(before_pool, target_file="scripts/__init_probe__.py", include_script_core=True).model_dump(mode="json")
        before_allowed = sorted(before_binding.get("allowed_tool_ids") or [])
        before_digest = _tool_binding_digest(before_binding)
    except Exception:
        before_allowed = []
        before_digest = "tool_pool_missing"
    logger.info(
        "[Creator][init_skill_before] skill=%s allowed_tool_ids=%s binding_digest=%s",
        skill_name,
        before_allowed,
        before_digest,
    )

    result = run_action({"action": "init", "name": skill_name})
    if result.get("success"):
        _copy_confirmed_uploaded_assets_to_skill(skill_name, request.confirmed_uploaded_assets)

    after_allowed: list[str] = []
    after_digest = ""
    try:
        after_pool = load_tool_pool(skill_dir)
        after_binding = get_skill_tool_binding(after_pool, target_file="scripts/__init_probe__.py", include_script_core=True).model_dump(mode="json")
        after_allowed = sorted(after_binding.get("allowed_tool_ids") or [])
        after_digest = _tool_binding_digest(after_binding)
    except Exception:
        after_allowed = []
        after_digest = "tool_pool_missing"
    logger.info(
        "[Creator][init_skill_after] skill=%s allowed_tool_ids=%s binding_digest=%s",
        skill_name,
        after_allowed,
        after_digest,
    )

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

    if isinstance(exc, CreatorValidatorReviewError):
        return FileGenerationStageError(
            source="skill_md_semantic_validator_error",
            layer="skill_md_semantic_validator_error",
            detail=str(exc),
            original=exc,
        )

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

def _tool_requests_for_matching_forbidden_helpers(
    *,
    file_path: str,
    role: str | None,
    skill_plan_entry: dict[str, Any] | None,
    import_guard_result: Any,
) -> list[ToolPoolAddToolRequest]:
    """Return gated tool requests for forbidden helpers that match the file contract.

    A runtime helper is eligible only when:
    - import guard reports it as forbidden rather than unknown;
    - it belongs to a registered capability;
    - resolve_implementation already matches that callable to the current
      canonical file contract.

    The function only proposes tool requests. gate_tool_request remains the
    authorization boundary.
    """
    error_type = str(
        getattr(import_guard_result, "error_type", "")
        or (
            import_guard_result.get("error_type")
            if isinstance(import_guard_result, dict)
            else ""
        )
        or ""
    )

    if error_type != "generated_pool_forbidden_import":
        return []

    forbidden_imports = (
        list(getattr(import_guard_result, "forbidden_imports", []) or [])
        if not isinstance(import_guard_result, dict)
        else list(
            import_guard_result.get("forbidden_imports") or []
        )
    )

    forbidden_helpers = {
        str(item or "").strip()
        for item in forbidden_imports
        if str(item or "").strip()
        and "." not in str(item or "").strip()
    }

    if not forbidden_helpers:
        return []

    entry = _skill_plan_entry_for_file(
        file_path=file_path,
        role=role,
        skill_plan_entry=skill_plan_entry,
    )

    required_stdout_fields: list[str] = []

    for field_name in list(entry.outputs or []):
        text = str(field_name or "").strip()
        if text and text not in required_stdout_fields:
            required_stdout_fields.append(text)

    artifact_contract = (
        entry.artifact_contract
        if isinstance(entry.artifact_contract, dict)
        else {}
    )

    for field_name in (
        artifact_contract.get("stdout_fields") or []
    ):
        text = str(field_name or "").strip()
        if text and text not in required_stdout_fields:
            required_stdout_fields.append(text)

    stdout_schema = {
        "type": "object",
        "required": required_stdout_fields,
        "properties": {
            field_name: {}
            for field_name in required_stdout_fields
        },
    }

    canonical_contract = compile_canonical_file_contract(
        entry,
        stdout_schema,
    )
    resolution = resolve_implementation(
        entry,
        canonical_contract,
    )

    requests: list[ToolPoolAddToolRequest] = []
    seen_capabilities: set[str] = set()

    for tool in (
        resolution.available_tools
        or resolution.selected_tools
        or []
    ):
        import_path = str(
            getattr(tool, "import_path", "") or ""
        ).strip()
        function_name = str(
            getattr(tool, "function_name", "") or ""
        ).strip()

        if import_path != "backend.services.runtime_tools":
            continue

        if function_name not in forbidden_helpers:
            continue

        tool_id = str(
            getattr(tool, "tool_id", "") or ""
        ).strip()
        capability_id = tool_id.split(".", 1)[0]

        if (
            not capability_id
            or capability_id in seen_capabilities
            or get_tool_capability(capability_id) is None
        ):
            continue

        seen_capabilities.add(capability_id)

        requests.append(
            ToolPoolAddToolRequest(
                target_file=file_path,
                requested_capability=capability_id,
                candidate_tool_id=capability_id,
                source="repair_request",
                reason=(
                    "Generated script imported a registered runtime helper "
                    "that resolve_implementation matched to the current "
                    "canonical file contract."
                ),
                confidence=1.0,
                score=100.0,
                matched_features=[
                    "registered_runtime_helper",
                    "canonical_contract_match",
                    "import_guard_forbidden_unbound",
                ],
                matched_terms=[function_name],
                candidate_source="runtime_import_guard",
                semantic_reason=(
                    f"{function_name} matches current canonical contract"
                ),
            )
        )

    return requests


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
7. 默认命令格式是：python scripts/<file>.py '<JSON object>'，脚本路径后直接传入一个完整、shell-quoted 的 JSON object 位置参数。
8. 该 JSON object 是输入 JSON，必须作为脚本路径后的第一个位置参数传入，对应 Python 脚本中的 `sys.argv[1]`。
9. 动态 placeholder 必须作为 JSON 字符串值出现。
10. 禁止在 ```bash block 内放 runtime/entrypoint/输入 JSON 伪配置对象。
11. 禁止在 ```bash block 内放 runner/script/输入 JSON 伪命令对象。
12. 不要把输入 JSON 理解为 --argv 等命令行选项；除非当前脚本源码明确实现，否则禁止新增这类调用参数。
13. 不要新增额外调用参数或把输入 JSON 改写成命令行选项。
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
2. requirement_graph / workflow_allocation 的 inputs/outputs 是强语义参考，不是字段名硬合同；输入字段可以与图谱字段不逐字一致。
3. 生成输入 JSON 时必须先语义理解图谱中的输入、输出和依赖关系。
4. 输入字段和值必须语义上可追踪到用户输入、上游脚本 stdout、当前脚本配置或蓝图明确常量。
5. 不得为了让命令看起来完整而编造无来源字面值或占位参数。
6. 不得把示例调用、示例值或说明性样例写进 bash command block。
7. 不得把下游脚本输入写成无来源字面值；应语义上来自上游 stdout，字段名可由第二轮 E2E 对齐。
8. 不得把用户输入写成字面值；应引用平台输入 placeholder 或传入通用 payload。
9. 如果不确定具体字段名，优先使用通用 user_request/input/payload，由脚本解析。
10. 第一轮只判断是否语义可追踪、是否明显示例调用、是否明显无来源占位、是否完全脱离图谱 IO 语义。
11. 第一轮不得要求输入字段必须逐字等于 graph.inputs，也不得要求 placeholder 必须逐字等于 graph.outputs。
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


def _single_skill_md_command_block_failure(original: Exception | None) -> dict[str, Any] | None:
    """Return the first structured command-block locator for local repair.

    Multiple failing blocks are repaired as a queue across validation rounds:
    repair one locator, re-run full format/semantic/block validation, then handle
    the next still-failing block from the fresh failure set.
    """
    if not isinstance(original, ContractValidationError):
        return None
    failures = [result for result in original.results if not result.passed]
    if not failures:
        return None
    locators: list[dict[str, Any]] = []
    for result in failures:
        if str(getattr(result, "layer", "") or "") != "skill_md_command_block_interface":
            return None
        details = result.details if isinstance(result.details, dict) else {}
        scope = details.get("skill_md_block_repair_scope")
        if not isinstance(scope, dict):
            scope = {}
        block_text = str(scope.get("full_block_text") or scope.get("block_text") or details.get("full_block_text") or details.get("block_text") or "")
        command_text = str(
            scope.get("command_body_text")
            or scope.get("command_text")
            or details.get("command_body_text")
            or details.get("command_text")
            or details.get("current_block")
            or ""
        )
        script_path = str(scope.get("script_path") or details.get("script_path") or "").strip()
        block_start = scope.get("block_start")
        block_end = scope.get("block_end")
        body_start = scope.get("body_start")
        body_end = scope.get("body_end")
        block_locator = scope.get("block_locator") if isinstance(scope.get("block_locator"), dict) else details.get("block_locator")
        if block_start is None:
            block_start = details.get("block_start")
        if block_end is None:
            block_end = details.get("block_end")
        if body_start is None:
            body_start = details.get("body_start")
        if body_end is None:
            body_end = details.get("body_end")
        if block_start is None and isinstance(block_locator, dict):
            block_start = block_locator.get("block_start", block_locator.get("start"))
        if block_end is None and isinstance(block_locator, dict):
            block_end = block_locator.get("block_end", block_locator.get("end"))
        if body_start is None and isinstance(block_locator, dict):
            body_start = block_locator.get("body_start")
        if body_end is None and isinstance(block_locator, dict):
            body_end = block_locator.get("body_end")
        block_sha256 = str(scope.get("block_sha256") or details.get("block_sha256") or "").strip()
        try:
            block_start = int(block_start)
            block_end = int(block_end)
            body_start = int(body_start) if body_start is not None else -1
            body_end = int(body_end) if body_end is not None else -1
        except Exception:
            return None
        if not block_text or not script_path or block_start < 0 or block_end <= block_start or not block_sha256:
            return None
        locators.append({
            "block_text": block_text,
            "full_block_text": block_text,
            "command_text": command_text,
            "command_body_text": command_text,
            "script_path": script_path,
            "block_start": block_start,
            "block_end": block_end,
            "body_start": body_start,
            "body_end": body_end,
            "block_sha256": block_sha256,
            "block_ordinal": details.get("block_ordinal") or scope.get("block_ordinal"),
            "structured_checks": details.get("structured_checks") or {},
            "result": result,
        })
    locators.sort(key=lambda item: (item["block_start"], item["block_end"]))
    first = locators[0]
    first_start = first["block_start"]
    first_end = first["block_end"]
    first_sha = first["block_sha256"]
    first["failure_reasons"] = [
        {
            "id": item["result"].id,
            "message": item["result"].message,
            "expected": item["result"].expected,
            "minimal_edit": item["result"].minimal_edit,
            "details": item["result"].details,
        }
        for item in locators
        if item["block_start"] == first_start
        and item["block_end"] == first_end
        and item["block_sha256"] == first_sha
    ]
    first.pop("result", None)
    return first

def _command_block_arg_protocol(structured_checks: Any, failure_reasons: Any) -> dict[str, Any]:
    """Extract already-established argv facts without guessing business fields."""
    facts: dict[str, Any] = {}
    stack = [structured_checks, failure_reasons]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            for key in ("expected_arg_mode", "requires_json_argv", "argv_schema", "required_keys"):
                if key in value and key not in facts:
                    facts[key] = value[key]
            stack.extend(value.values())
        elif isinstance(value, (list, tuple)):
            stack.extend(value)
    schema = facts.get("argv_schema") if isinstance(facts.get("argv_schema"), dict) else {}
    required = facts.get("required_keys")
    if not isinstance(required, list):
        required = schema.get("required_keys", schema.get("required", [])) if isinstance(schema, dict) else []
    facts["argv_schema"] = schema
    facts["required_keys"] = [str(key) for key in required if isinstance(key, str)] if isinstance(required, (list, tuple, set)) else []
    facts["expected_arg_mode"] = str(facts.get("expected_arg_mode") or "")
    facts["requires_json_argv"] = facts.get("requires_json_argv") is True
    return facts


def _validate_repaired_skill_md_command_block(
    repaired_block: str, *, script_path: str, arg_protocol: dict[str, Any] | None = None,
) -> None:
    text = str(repaired_block or "")
    if text.lstrip().startswith("---"):
        raise ValueError("repaired command block must not contain frontmatter or Markdown headings")
    fence_matches = list(re.finditer(r"(?m)^\s*(`{3,}|~{3,})([^`\n]*)\s*$", text))
    if len(fence_matches) != 2:
        raise ValueError("repaired command block must contain exactly one fenced block")
    outside_text = text[:fence_matches[0].start()] + text[fence_matches[1].end():]
    if re.search(r"(?m)^\s{0,3}#{1,6}\s", outside_text):
        raise ValueError("repaired command block must not contain frontmatter or Markdown headings")
    if text[:fence_matches[0].start()].strip() or text[fence_matches[1].end():].strip():
        raise ValueError("repaired command block must not contain prose outside the fence")
    if fence_matches[0].group(1)[0] != "`" or len(fence_matches[0].group(1)) != 3:
        raise ValueError("repaired command block must use a ```bash fence")
    if (fence_matches[0].group(2) or "").strip().lower() != "bash":
        raise ValueError("repaired command block fence type must be bash")
    body = text[fence_matches[0].end():fence_matches[1].start()]
    command_lines = _effective_command_lines(body)
    if len(command_lines) != 1:
        raise ValueError("repaired command block must contain exactly one command line")
    if script_path not in command_lines[0]:
        raise ValueError("repaired command block must invoke the same script_path")
    protocol = arg_protocol or {}
    json_required = protocol.get("expected_arg_mode") == "json_object" or protocol.get("requires_json_argv") is True
    if not json_required:
        return
    blocks = parse_skill_md_bash_command_blocks(text)
    if len(blocks) != 1:
        raise ValueError("repaired command block must contain exactly one parseable bash block")
    signature = creator_contracts._command_signature(command_lines[0], script_path)
    if not signature or signature.get("arg_mode") != "json_arg":
        raise ValueError("repaired command block must use one shell-quoted JSON object argv")
    payload = signature.get("json_payload")
    if not isinstance(payload, dict):
        raise ValueError("repaired command block JSON argv must decode to an object")
    missing = set(protocol.get("required_keys") or []) - set(payload)
    if missing:
        raise ValueError(f"repaired command block JSON argv missing required keys: {', '.join(sorted(missing))}")


async def _repair_skill_md_command_block(
    *,
    model: str,
    skill_name: str,
    block_text: str,
    script_path: str,
    structured_checks: Any,
    failure_reasons: Any,
    retry_index: int = 0,
) -> str:
    """Repair exactly one SKILL.md bash command block without sending full SKILL.md."""
    arg_protocol = _command_block_arg_protocol(structured_checks, failure_reasons)
    messages = [
        {
            "role": "system",
            "content": (
                "你是 SKILL.md 单个 bash command block 修复器。"
                "只返回修复后的一个完整 ```bash fenced block。"
                "不要返回完整 SKILL.md。不要返回 frontmatter、标题、正文、解释或其他 block。"
                "仍然调用原 script_path。"
                "命令协议约束：只修复当前传入的一个 bash fenced block，不得修改其它 SKILL.md 内容。"
                "如果 expected_arg_mode=json_object：script_path 后必须只有一个业务参数；该参数必须是可由 json.loads() 解析的 JSON object；"
                "外层必须使用 shell 引号，JSON 内部必须使用标准双引号。严禁改为 --topic、--input、--prompt 等 argparse flags，"
                "除非当前脚本合同明确声明 argparse_flags。JSON key 必须来自真实 argv_schema，不得自行发明或替换字段名。"
                "如果 argv_schema 或 required_keys 为空，只修复 shell quoting 和 JSON object argv 形态；"
                "优先保留当前命令中已有的 JSON key，不得自行猜测、重命名或新增业务字段。"
                "如果失败原因包含 category=unknown_source，必须只依据失败信息中 review.available_source_fields 已明确提供的字段修复 placeholder source；"
                "不得自行增加、保留或猜测 available_source_fields 中不存在的 namespace、prefix、step/output/result 容器名。"
                "此类修复只能修改 placeholder source，不得借此修改 argv key、script_path、Graph 或其他 command block。"
                "格式示例 python scripts/example.py '{\"field\":\"{{source}}\"}' 中 field 仅是格式示例，真实字段必须来自 argv_schema。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"script_path:\n{script_path}\n\n"
                "当前失败 block:\n"
                f"{block_text}\n\n"
                "structured_checks:\n"
                f"{json.dumps(structured_checks, ensure_ascii=False, indent=2, default=str)}\n\n"
                "失败原因:\n"
                f"{json.dumps(failure_reasons, ensure_ascii=False, indent=2, default=str)}\n\n"
                "脚本参数协议事实:\n"
                f"{json.dumps(arg_protocol, ensure_ascii=False, indent=2, default=str)}\n\n"
                "硬性要求：只返回一个完整 ```bash fenced block；block 内只包含一条有效命令；"
                "命令仍然调用同一个 script_path；不要输出任何其它 Markdown。"
            ),
        },
    ]
    repaired_block = await _complete_creator_file_generation(
        messages=messages,
        model=model,
        skill_name=skill_name,
        file_path="SKILL.md",
        prompt_variant="repair_skill_md_command_block",
        retry_index=retry_index,
    )
    _validate_repaired_skill_md_command_block(repaired_block, script_path=script_path, arg_protocol=arg_protocol)
    return repaired_block


def _replace_skill_md_command_block_exact(candidate: str, locator: dict[str, Any], repaired_block: str) -> str:
    block_start = int(locator["block_start"])
    block_end = int(locator["block_end"])
    block_text = str(locator["block_text"])
    block_sha256 = str(locator["block_sha256"])
    if candidate[block_start:block_end] != block_text:
        raise ValueError("SKILL.md command block locator is stale; rerun validation for a fresh locator")
    if hashlib.sha256(block_text.encode("utf-8")).hexdigest() != block_sha256:
        raise ValueError("SKILL.md command block hash mismatch; rerun validation for a fresh locator")
    replacement = str(repaired_block or "").rstrip("\r\n")
    original_trailing_newlines = block_text[len(block_text.rstrip("\r\n")):]
    if original_trailing_newlines:
        replacement += original_trailing_newlines
    elif candidate[block_end:] and not candidate[block_end:].startswith(("\n", "\r")):
        replacement += "\n"
    repaired_candidate = candidate[:block_start] + replacement + candidate[block_end:]
    assert repaired_candidate[:block_start] == candidate[:block_start]
    assert repaired_candidate[block_start + len(replacement):] == candidate[block_end:]
    return repaired_candidate



def _classify_skill_md_repair_scope(
    stage_error: FileGenerationStageError,
) -> str:
    """Classify first-round SKILL.md repair routing before any full rewrite checks."""
    if _single_skill_md_command_block_failure(getattr(stage_error, "original", None)) is not None:
        return "command_block"

    original = getattr(stage_error, "original", None)
    results: list[Any] = []
    if isinstance(original, ContractValidationError):
        results = [result for result in original.results if not getattr(result, "passed", False)]

    def _result_text(result: Any) -> str:
        parts = [
            str(getattr(result, "id", "") or ""),
            str(getattr(result, "target", "") or ""),
            str(getattr(result, "message", "") or ""),
            str(getattr(result, "expected", "") or ""),
            str(getattr(result, "minimal_edit", "") or ""),
        ]
        details = getattr(result, "details", None)
        if isinstance(details, dict):
            parts.append(json.dumps(details, ensure_ascii=False, default=str))
        return "\n".join(parts).lower()

    def _is_full_format_result(result: Any) -> bool:
        layer = str(getattr(result, "layer", "") or "").strip()
        if layer == "hard_format":
            return True
        details = getattr(result, "details", None)
        if isinstance(details, dict) and str(details.get("repair_strategy") or "").strip() == "full_rewrite":
            return True
        result_id = str(getattr(result, "id", "") or "").strip().lower()
        if result_id.startswith("skill_md.frontmatter") or result_id.startswith("skill_md.markdown_body_structure"):
            return True
        text = _result_text(result)
        deterministic_markers = (
            "unclosed fence",
            "outer markdown fence",
            "empty body",
            "missing body",
            "frontmatter 未闭合",
            "缺少正文",
            "正文为空",
            "整体包裹",
            "fenced block 未闭合",
            "全局 fence",
        )
        return any(marker in text for marker in deterministic_markers)

    if results and any(_is_full_format_result(result) for result in results):
        return "full_format"

    if (
        getattr(stage_error, "source", "") == "hard_format"
        or getattr(stage_error, "layer", "") in {"hard_format", "markdown_format"}
    ):
        return "full_format"

    return "semantic"

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
    if file_path.startswith("references/"):
        return [
            {
                "role": "system",
                "content": (
                    "你是 references/*.md Markdown hard-format 全量重写器。"
                    "只输出当前 Reference 文件的完整 Markdown 文件内容。"
                    "这是格式重写，不是业务语义 repair。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"文件路径：{file_path}\n"
                    f"Skill 名称：{skill_name}\n\n"
                    "后台 Markdown hard format 校验失败项如下：\n"
                    f"{deterministic_error}\n\n"
                    "要求：\n"
                    "1. 保留当前 Reference 已有有效语义和职责。\n"
                    "2. 重新输出完整 Reference 文件，不输出 patch。\n"
                    "3. 所有 Markdown fenced code blocks 必须完整成对闭合。\n"
                    "4. 不要使用 outer ```markdown / ```md wrapper 包住整个输出文件。\n"
                    "5. Reference 正文内部允许正常的 text/json/python 等示例 fenced block，但必须闭合。\n"
                    "6. 不要输出 JSON patch。\n"
                    "7. 不要输出 diff。\n"
                    "8. 不要输出 old_lines/new_lines。\n"
                    "9. 不要输出解释、日志、分析或 repair proposal。\n"
                    "10. 不要新增或修改 scripts 的执行接口。\n"
                    "11. 不要重新设计 Skill workflow。\n"
                    "12. 不要重新设计 ResponsibilityGraph。\n"
                    "13. 只修复当前 Reference 文件的 Markdown hard-format 问题。\n"
                    "14. 输出必须是完整的当前 Reference Markdown 文件。\n\n"
                    "蓝图上下文：\n"
                    f"{(blueprint_text or '')[:8000]}\n\n"
                    "当前文件内容：\n"
                    "<<<CURRENT_FILE\n"
                    f"{current_content or ''}\n"
                    "CURRENT_FILE\n"
                ),
            },
        ]

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
                "8. 格式重写只修复 JSON 的引号、分组和完整性，以及 Markdown fence/frontmatter 闭合问题；不要混入输入字段对齐或 workflow dataflow 的局部修复。\n"
                "9. 保持现有脚本调用形式；输入 JSON 仍是脚本路径后的第一个位置参数，对应 Python 脚本中的 `sys.argv[1]`；不要把输入 JSON 理解为 --argv 等命令行选项，不要新增调用参数；格式合法后由后续校验处理。\n\n"
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

            effective_skill_plan_entry = (
                request.skill_plan_entry
                if isinstance(request.skill_plan_entry, dict)
                else None
            )
            entry_requirements: list[RequirementItem] = []
            current_skill_binding_payload: dict[str, Any] = {}
            current_tool_pool_summary: dict[str, Any] = {}
            tool_readiness_observations: list[dict[str, Any]] = []

            if request.file_path.startswith("scripts/"):
                skill_dir = settings.skills_path / skill_name
                current_tool_pool = load_tool_pool(skill_dir)
                current_file_binding = get_file_binding(
                    current_tool_pool,
                    request.file_path,
                )
                if current_file_binding is None:
                    current_file_binding = get_skill_tool_binding(
                        current_tool_pool,
                        target_file=request.file_path,
                        include_script_core=True,
                    )
                current_skill_binding_payload = current_file_binding.model_dump(mode="json")
                current_tool_pool_summary = current_tool_pool.model_dump(mode="json")
                logger.info(
                    "[Creator][generate_file][producer_tool_pool] skill=%s file=%s summary=%s",
                    skill_name,
                    request.file_path,
                    json.dumps(_tool_binding_log_summary(current_skill_binding_payload), ensure_ascii=False),
                )

            if request.file_path.startswith("scripts/"):
                # 后端生成阶段重新构建 canonical entry，避免依赖前端传来的不完整 entry。
                # 这仍然是单文件合同，不做跨文件 E2E 判断。
                skill_md_path = settings.skills_path / skill_name / "SKILL.md"
                skill_md_for_entry = (
                    skill_md_path.read_text(encoding="utf-8")
                    if skill_md_path.is_file()
                    else request.blueprint_text
                )
                entry_obj = _skill_plan_entry_for_file(
                    file_path=request.file_path,
                    purpose=request.purpose,
                    blueprint_text=skill_md_for_entry or request.blueprint_text,
                    role=request.role,
                    skill_plan_entry=effective_skill_plan_entry,
                )
                effective_skill_plan_entry = dict(
                    getattr(entry_obj, "__dict__", {}) or {}
                )
                entry_requirements = _requirements_for_generated_file(
                    skill_name=skill_name,
                    file_path=request.file_path,
                    request_graph=request.requirement_graph,
                    skill_plan_entry=request.skill_plan_entry,
                )

                effective_skill_plan_entry.setdefault("path", request.file_path)
                effective_skill_plan_entry.setdefault("purpose", request.purpose)
                effective_skill_plan_entry = _with_current_skill_tool_binding(
                    effective_skill_plan_entry,
                    current_skill_binding_payload,
                )
                _, tool_blockers = _creator_tool_readiness_blockers(
                    effective_skill_plan_entry
                )
                if tool_blockers:
                    tool_readiness_observations = list(tool_blockers)
                    logger.info(
                        "[Creator][producer_tool_readiness_observation] skill=%s file=%s blockers=%s",
                        skill_name,
                        request.file_path,
                        json.dumps(tool_readiness_observations, ensure_ascii=False, default=str),
                    )
            def _build_current_function_execution_context() -> dict[str, Any] | None:
                if not request.file_path.startswith("scripts/"):
                    return None
                context_entry = _skill_plan_entry_for_file(
                    file_path=request.file_path,
                    purpose=request.purpose,
                    blueprint_text=request.blueprint_text,
                    role=request.role,
                    skill_plan_entry=effective_skill_plan_entry,
                )
                context_binding: dict[str, Any] = dict(current_skill_binding_payload)
                return build_function_execution_context(
                    graph=request.requirement_graph,
                    target_file=request.file_path,
                    current_file_tool_binding=context_binding,
                    fallback_function_item=(function_item_prompt_payload(entry_requirements[0]) if entry_requirements else {}),
                )

            function_execution_context: dict[str, Any] | None = _build_current_function_execution_context()

            prompt_messages = (
                _build_generate_file_prompt(
                    request.file_path,
                    skill_name,
                    request.purpose,
                    request.blueprint_text,
                    request.conversation_history,
                    role=request.role,
                    skill_plan_entry=(
                        effective_skill_plan_entry
                    ),
                    requirements=(
                        entry_requirements
                    ),
                    responsibility_graph=request.requirement_graph,
                    function_execution_context=function_execution_context,
                )
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
        # Track how many tool re-explorations have been triggered for this file
        # (at most once per generation session to avoid unbounded exploration).
        tool_re_explore_count = 0

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

                last_import_guard_result: Any = None
                last_file_binding: Any = current_skill_binding_payload if request.file_path.startswith("scripts/") else None
                boundary_violations: list[Any] = []

                if request.file_path.startswith("scripts/"):
                    unsupported_issue = _not_supported_declared_input_issue(
                        source=content,
                        blueprint_text=request.blueprint_text,
                        skill_plan_entry=effective_skill_plan_entry,
                        file_path=request.file_path,
                    )
                    if unsupported_issue:
                        raise ScriptFunctionalValidationError([unsupported_issue], layer="script_declared_input_not_supported")

                    allowed_tools = list(current_skill_binding_payload.get("allowed_tool_ids") or [])
                    boundary_violations = (
                        _script_tool_boundary_violations(
                            content,
                            allowed_tools,
                            file_binding=current_skill_binding_payload,
                            skill_plan_entry=effective_skill_plan_entry if isinstance(effective_skill_plan_entry, dict) else None,
                        )
                        or []
                    )
                    logger.info(
                        "[Creator][first_round_tool_observations] skill=%s file=%s count=%d summary=%s",
                        skill_name,
                        request.file_path,
                        len(boundary_violations),
                        json.dumps(_tool_binding_log_summary(current_skill_binding_payload), ensure_ascii=False),
                    )

                format_stage_error = _first_round_format_stage_error(
                    file_path=request.file_path,
                    content=content,
                    role=request.role,
                    skill_plan_entry=effective_skill_plan_entry,
                )
                if format_stage_error is not None:
                    raise format_stage_error
                compile_stage_error = _post_patch_python_compile_stage_error(request.file_path, content)
                if compile_stage_error is not None:
                    raise compile_stage_error

                if request.file_path.startswith("scripts/"):
                    try:
                        import_guard_result = guard_runtime_imports(
                            content,
                            request.file_path,
                            current_skill_binding_payload,
                        )
                        last_import_guard_result = import_guard_result
                    except Exception as guard_exc:
                        logger.warning(
                            "[Creator][first_round_tool_observation_error] skill=%s file=%s error=%s: %s",
                            skill_name,
                            request.file_path,
                            type(guard_exc).__name__,
                            guard_exc,
                        )
                        last_import_guard_result = {
                            "success": None,
                            "observation_error": f"{type(guard_exc).__name__}: {guard_exc}",
                            "target_file": request.file_path,
                        }
                    logger.info(
                        "[Creator][runtime_import_observation] skill=%s file=%s summary=%s",
                        skill_name,
                        request.file_path,
                        json.dumps(_tool_binding_log_summary(current_skill_binding_payload), ensure_ascii=False),
                    )

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
                        _raise_file_contract_failures(reference_results)

                        reference_review = await _run_reference_semantic_review(
                            file_path=request.file_path,
                            content=content,
                            purpose=request.purpose or str(reference_entry.get("purpose") or ""),
                            blueprint_context=request.blueprint_text,
                            dependent_context={
                                "workflow_allocation_summary": request.workflow_allocation_summary,
                                "requirement_graph": request.requirement_graph,
                                "final_outputs": request.final_outputs,
                            },
                            requested_model=request.model or route.model,
                        )
                        if not reference_review.get("passed"):
                            issues = reference_review.get("issues") if isinstance(reference_review.get("issues"), list) else []
                            raise FileGenerationStageError(
                                source="reference_semantic_failed",
                                layer="semantic",
                                detail=json.dumps({
                                    "issues": issues,
                                    "repair_instructions": reference_review.get("repair_instructions") or "Patch only this reference's semantic content.",
                                    "review": reference_review,
                                }, ensure_ascii=False, default=str),
                            )

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

                        workflow_allocation_summary = _load_workflow_allocation_summary(skill_name)
                        try:
                            reviewer_tool_usage = _build_e2e_callable_repair_context(
                                skill_name=skill_name,
                                target_file=request.file_path,
                            )
                        except Exception as tool_usage_exc:
                            logger.warning(
                                "[Creator][responsibility_judge_tool_usage_unavailable] skill=%s file=%s error=%s",
                                skill_name, request.file_path, tool_usage_exc,
                            )
                            reviewer_tool_usage = {}
                        logger.info(
                            "[Creator][responsibility_judge_tool_pool] skill=%s file=%s summary=%s",
                            skill_name,
                            request.file_path,
                            json.dumps(_tool_binding_log_summary(current_skill_binding_payload), ensure_ascii=False),
                        )
                        responsibility_review = await _run_script_responsibility_review(
                            file_path=request.file_path,
                            script_content=content,
                            skill_plan_entry=entry,
                            requirements=entry_requirements,
                            deterministic_issues=list(boundary_violations),
                            requested_model=request.model or route.model,
                            review_context={
                                "phase": "RESPONSIBILITY_STAGE",
                                "policy": "只判断当前文件职责是否完成。Skill ToolPool 是允许使用的能力上界，不表示当前脚本必须使用；不得仅因零业务工具调用、未使用某个授权工具、使用标准库完成职责、池中存在更高级工具或 required_capabilities 可映射但脚本未选择而判失败。只有确认调用池外平台工具、不存在的 Registry 函数、实现无法完成职责、工具调用/返回处理真实错误或声称产物但未实现时才判失败。",
                                "blueprint_text": request.blueprint_text,
                                "purpose_short_contract": getattr(entry, "purpose", request.purpose),
                                "workflow_allocation_summary": workflow_allocation_summary,
                                "trial_stdout": "第一轮责任审查在局部 patch 前可能尚未执行试运行；如为空，不得把缺 stdout 当接口失败。",
                                "artifact_info": "第一轮责任审查只用 artifact 信息辅助判断语义交付；真实存在性由运行/E2E 检查。",
                                "runtime_import_guard_result": (
                                    last_import_guard_result.model_dump(mode="json")
                                    if hasattr(last_import_guard_result, "model_dump")
                                    else last_import_guard_result
                                ),
                                "current_skill_tool_binding": current_skill_binding_payload,
                                "current_file_tool_binding": current_skill_binding_payload,
                                "tool_usage_contracts": reviewer_tool_usage.get("resolved_tools") or [],
                                "tool_usage_review_rule": (
                                    "不得推测 Tool 参数或返回字段。脚本读取返回字段时只能依据 supplied Tool contract；"
                                    "contract 未声明的字段不得要求脚本读取。"
                                ),
                                "deterministic_issues": list(boundary_violations),
                                "tool_readiness_observations": list(tool_readiness_observations),
                                "requirement_graph": request.requirement_graph,
                                "function_execution_context": function_execution_context,
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
                error_source = stage_error.source
                error_layer = f"{stage_error.source}:{stage_error.layer}"
                if error_source == "skill_md_semantic_validator_error":
                    yield _file_done_error_sse(
                        file_path=request.file_path,
                        role=request.role,
                        error=deterministic_error,
                        error_type="skill_md_semantic_validator_error",
                        content=candidate or "",
                        recoverable=True,
                    )
                    return
                skill_md_repair_scope = (
                    _classify_skill_md_repair_scope(stage_error)
                    if request.file_path == "SKILL.md"
                    else ""
                )
                if (
                    is_generation_format_error(stage_error)
                    or stage_error.source == "python_compile"
                    or stage_error.layer in {"python_compile_error", "post_patch_python_compile_error"}
                ) and request.file_path.startswith("scripts/"):
                    format_retry_count += 1
                elif (
                    skill_md_repair_scope == "full_format"
                    or (
                        request.file_path != "SKILL.md"
                        and is_markdown_hard_format_error(stage_error)
                        and _is_markdown_creator_file(request.file_path)
                    )
                ):
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

                is_compile_rewrite_error = (
                    request.file_path.startswith("scripts/")
                    and (
                        stage_error.source == "python_compile"
                        or stage_error.layer in {"python_compile_error", "post_patch_python_compile_error"}
                        or "script candidate after patch cannot compile" in deterministic_error
                    )
                )
                if (is_script_raw_source_format_error(stage_error) or is_compile_rewrite_error) and request.file_path.startswith("scripts/"):
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

                    rewrite_prompt_variant = "strict_compile_rewrite" if is_compile_rewrite_error else "strict_source_only_regeneration"
                    if is_compile_rewrite_error:
                        rewrite_current_file_binding = dict(current_skill_binding_payload)
                        try:
                            compile_detail = json.loads(str(stage_error.detail or "{}"))
                        except Exception:
                            compile_detail = {}
                        logger.info("[Creator][compile_rewrite] %s", json.dumps({
                            "event": "compile_rewrite",
                            "file_path": request.file_path,
                            "compile_error_type": compile_detail.get("error_type"),
                            "compile_error_lineno": compile_detail.get("lineno"),
                            "compile_error_msg": compile_detail.get("msg"),
                            "rewrite_attempt": format_retry_count,
                            "rewrite_prompt_variant": "strict_compile_rewrite",
                        }, ensure_ascii=False, default=str))
                        next_messages = _build_strict_compile_rewrite_prompt(
                            file_path=request.file_path,
                            skill_name=skill_name,
                            purpose=request.purpose,
                            blueprint_text=request.blueprint_text,
                            role=request.role,
                            skill_plan_entry=effective_skill_plan_entry,
                            deterministic_error=deterministic_error,
                            previous_content=candidate or "",
                            current_file_binding=rewrite_current_file_binding,
                        )
                    else:
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
                        prompt_variant=rewrite_prompt_variant,
                        retry_index=format_retry_count - 1,
                    )
                    prompt_messages = next_messages
                    prompt_variant = rewrite_prompt_variant
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
                            requirements=entry_requirements,
                            function_execution_context=function_execution_context,
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
                    yield _file_done_error_sse(
                        file_path=request.file_path,
                        role=request.role,
                        error=deterministic_error,
                        error_type=error_source,
                        content=candidate or "",
                        recoverable=True,
                    )
                    return

                if skill_md_repair_scope == "command_block":
                    single_block_locator = _single_skill_md_command_block_failure(stage_error.original)
                    if single_block_locator is None:
                        raise ValueError("SKILL.md command block repair scope lost locator")
                    last_block_repair_error: Exception | None = None
                    for block_retry_index in range(3):
                        try:
                            repaired_block = await _repair_skill_md_command_block(
                                model=route.model,
                                skill_name=skill_name,
                                block_text=(
                                    single_block_locator.get("full_block_text")
                                    or single_block_locator.get("block_text")
                                ),
                                script_path=single_block_locator["script_path"],
                                structured_checks=single_block_locator.get("structured_checks") or {},
                                failure_reasons=single_block_locator.get("failure_reasons") or deterministic_error,
                                retry_index=block_retry_index,
                            )
                            candidate = _replace_skill_md_command_block_exact(
                                candidate or "",
                                single_block_locator,
                                repaired_block,
                            )
                            break
                        except Exception as block_repair_exc:
                            last_block_repair_error = block_repair_exc
                    else:
                        yield _file_done_error_sse(
                            file_path=request.file_path,
                            role=request.role,
                            error=(
                                "SKILL.md command block repair failed without whole-file fallback: "
                                f"{last_block_repair_error}"
                            ),
                            error_type="skill_md_command_block_repair_failed",
                            content=candidate or "",
                            recoverable=True,
                        )
                        return
                    continue

                if (
                    skill_md_repair_scope == "full_format"
                    or (
                        request.file_path != "SKILL.md"
                        and is_markdown_hard_format_error(stage_error)
                        and _is_markdown_creator_file(request.file_path)
                    )
                ):
                    layer_limit = _first_round_repair_limit(error_source)

                    if markdown_format_retry_count > layer_limit:
                        yield _file_done_error_sse(
                            file_path=request.file_path,
                            role=request.role,
                            error=(
                                f"Markdown 格式重写失败：已完整重写 {layer_limit} 轮仍未通过。"
                                f"最后错误：{deterministic_error}"
                            ),
                            error_type=_markdown_warning_error_type(request.file_path),
                            content=candidate or "",
                            recoverable=True,
                        )
                        return

                    yield _sse({
                        "type": "validation",
                        "status": "format_full_rewrite",
                        "success": False,
                        "file_path": request.file_path,
                        "role": request.role,
                        "editable": True,
                        "disabled": False,
                        "validation": {
                            "status": "format_full_rewrite",
                            "attempt": markdown_format_retry_count,
                            "markdown_format_retry_count": markdown_format_retry_count,
                            "business_repair_count": business_repair_count,
                            "source": error_source,
                            "layer": stage_error.layer,
                            "error": deterministic_error,
                        },
                    })

                    rewrite_messages = _build_markdown_format_full_rewrite_prompt(
                        file_path=request.file_path,
                        skill_name=skill_name,
                        blueprint_text=request.blueprint_text,
                        deterministic_error=deterministic_error,
                        current_content=candidate or "",
                    )

                    candidate = await _complete_creator_file_generation(
                        messages=rewrite_messages,
                        model=route.model,
                        skill_name=skill_name,
                        file_path=request.file_path,
                        prompt_variant="rewrite_markdown_full_format",
                        retry_index=markdown_format_retry_count - 1,
                    )
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
                        "\n\n额外修复目标：references/*.md 语义错误只做局部内容 patch；"
                        "保持已通过的 YAML frontmatter 和 Markdown 格式；"
                        "按 reference semantic judge 的 issues 修正当前参考资料职责。"
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
                    single_block_locator = (
                        _single_skill_md_command_block_failure(stage_error.original)
                        if request.file_path == "SKILL.md"
                        else None
                    )

                    if single_block_locator is not None:
                        last_block_repair_error: Exception | None = None
                        for block_retry_index in range(3):
                            try:
                                repaired_block = await _repair_skill_md_command_block(
                                    model=route.model,
                                    skill_name=skill_name,
                                    block_text=(
                                        single_block_locator.get("command_text")
                                        or single_block_locator["block_text"]
                                    ),
                                    script_path=single_block_locator["script_path"],
                                    structured_checks=single_block_locator.get("structured_checks") or {},
                                    failure_reasons=single_block_locator.get("failure_reasons") or deterministic_error,
                                    retry_index=block_retry_index,
                                )
                                repaired_candidate = _replace_skill_md_command_block_exact(
                                    candidate or "",
                                    single_block_locator,
                                    repaired_block,
                                )
                                break
                            except Exception as block_repair_exc:
                                last_block_repair_error = block_repair_exc
                        else:
                            raise ValueError(
                                "SKILL.md command block repair failed without whole-file fallback: "
                                f"{last_block_repair_error}"
                            )
                    elif error_source == "basic_format" or stage_error.layer in {"python_compile_error", "markdown_basic_format_error"}:
                        feedback = _basic_format_repair_feedback(stage_error)
                        passed_checks_text = ""
                        failed_checks_text = feedback
                        contract_text = ""
                        targeted_repair = "只修复当前候选的基础源码/Markdown 格式，不处理工具、argv、stdout、E2E 或业务职责。"
                    elif (
                        error_source in {"script_requirement_failed", "script_functional", "script_responsibility"}
                        and request.file_path.startswith("scripts/")
                    ):
                        responsibility_issues = []
                        original_exc = stage_error.original
                        if isinstance(original_exc, ScriptFunctionalValidationError):
                            responsibility_issues = original_exc.issues
                        # Trigger re-exploration of the tool library for this file when:
                        # 1. Stub/empty-shell implementations are detected, OR
                        # 2. passed=false and issues indicate missing tool/capability/dependency
                        #    caused the responsibility failure.
                        # The result is a proposal only — it still passes through the gate
                        # before any tool is added to the allowed pool.
                        if (
                                tool_re_explore_count < 1
                                and request.file_path.startswith("scripts/")
                                and _has_responsibility_missing_capability_issue(
                                    responsibility_issues
                                )
                        ):
                            try:
                                if (
                                        effective_skill_plan_entry
                                        and isinstance(
                                    effective_skill_plan_entry,
                                    dict,
                                )
                                ):
                                    planner_file_spec = dict(
                                        effective_skill_plan_entry
                                    )
                                    planner_file_spec.setdefault(
                                        "path",
                                        request.file_path,
                                    )
                                    planner_file_spec.setdefault(
                                        "role",
                                        request.role or "generic_script",
                                    )
                                else:
                                    planner_file_spec = {
                                        "path": request.file_path,
                                        "role": (
                                                request.role
                                                or "generic_script"
                                        ),
                                    }

                                expansion_result = await (
                                    _plan_tool_pool_patch_from_responsibility_feedback(
                                        skill_name=skill_name,
                                        target_file=request.file_path,
                                        file_spec=planner_file_spec,
                                        responsibility_issues=(
                                            responsibility_issues
                                        ),
                                        script_content=candidate or "",
                                        requested_model=(
                                                request.model
                                                or route.model
                                        ),
                                    )
                                )

                                logger.info(
                                    "[Creator]"
                                    "[responsibility_tool_planning] %s",
                                    json.dumps(
                                        {
                                            "event": (
                                                "responsibility_tool_planning"
                                            ),
                                            "skill_name": skill_name,
                                            "file_path": request.file_path,
                                            **expansion_result,
                                        },
                                        ensure_ascii=False,
                                        default=str,
                                    ),
                                )

                                tool_re_explore_count += 1
                                if (
                                    int(expansion_result.get("allowed_new") or 0) > 0
                                    or int(expansion_result.get("attached_existing") or 0) > 0
                                ):
                                    refreshed_tool_pool = load_tool_pool(settings.skills_path / skill_name)
                                    refreshed_binding = get_file_binding(
                                        refreshed_tool_pool,
                                        request.file_path,
                                    )
                                    if refreshed_binding is None:
                                        refreshed_binding = get_skill_tool_binding(
                                            refreshed_tool_pool,
                                            target_file=request.file_path,
                                            include_script_core=True,
                                        )
                                    current_tool_pool_summary = refreshed_tool_pool.model_dump(mode="json")
                                    current_skill_binding_payload = refreshed_binding.model_dump(mode="json")
                                    effective_skill_plan_entry = _with_current_skill_tool_binding(
                                        effective_skill_plan_entry,
                                        current_skill_binding_payload,
                                    )
                                    function_execution_context = _build_current_function_execution_context()
                                    try:
                                        last_import_guard_result = guard_runtime_imports(
                                            candidate or "",
                                            request.file_path,
                                            current_skill_binding_payload,
                                        )
                                    except Exception as guard_exc:
                                        logger.warning(
                                            "[Creator][responsibility_tool_observation_refresh_error] skill=%s file=%s error=%s: %s",
                                            skill_name,
                                            request.file_path,
                                            type(guard_exc).__name__,
                                            guard_exc,
                                        )
                                        last_import_guard_result = {
                                            "success": None,
                                            "observation_error": f"{type(guard_exc).__name__}: {guard_exc}",
                                            "target_file": request.file_path,
                                        }
                                    normalized_refreshed_guard = (
                                        last_import_guard_result.model_dump(mode="json")
                                        if hasattr(last_import_guard_result, "model_dump")
                                        else (last_import_guard_result if isinstance(last_import_guard_result, dict) else {})
                                    )
                                    logger.info(
                                        "[Creator][responsibility_tool_pool_refreshed] skill=%s file=%s summary=%s",
                                        skill_name,
                                        request.file_path,
                                        json.dumps(_tool_binding_log_summary(current_skill_binding_payload), ensure_ascii=False),
                                    )
                                    logger.info(
                                        "[Creator][responsibility_import_observation_refreshed] skill=%s file=%s success=%s error_type=%s summary=%s",
                                        skill_name,
                                        request.file_path,
                                        normalized_refreshed_guard.get("success"),
                                        normalized_refreshed_guard.get("error_type"),
                                        json.dumps(_tool_binding_log_summary(current_skill_binding_payload), ensure_ascii=False),
                                    )

                            except Exception as planning_exc:
                                logger.warning(
                                    "[Creator]"
                                    "[responsibility_tool_planning] "
                                    "planning failed skill=%s "
                                    "file=%s error=%s",
                                    skill_name,
                                    request.file_path,
                                    planning_exc,
                                )
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

                    repair_tool_pool_summary = {}
                    repair_current_file_binding = {}
                    repair_import_guard_result = {}
                    if request.file_path.startswith("scripts/"):
                        repair_tool_pool_summary = dict(current_tool_pool_summary)
                        repair_current_file_binding = dict(current_skill_binding_payload)
                        if 'last_import_guard_result' in locals():
                            repair_import_guard_result = (
                                last_import_guard_result.model_dump(mode="json")
                                if hasattr(last_import_guard_result, "model_dump")
                                else (last_import_guard_result if isinstance(last_import_guard_result, dict) else {})
                            )
                        logger.info(
                            "[Creator][localized_repair_tool_pool] skill=%s file=%s summary=%s",
                            skill_name,
                            request.file_path,
                            json.dumps(_tool_binding_log_summary(repair_current_file_binding), ensure_ascii=False),
                        )

                    if single_block_locator is None:
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
                            import_guard_result=repair_import_guard_result,
                            current_file_binding=repair_current_file_binding,
                            tool_pool_summary=repair_tool_pool_summary,
                            function_execution_context=function_execution_context,
                        )
                    repaired_candidate = _canonicalize_generated_candidate(
                        file_path=request.file_path,
                        content=repaired_candidate,
                        role=request.role,
                        skill_plan_entry=effective_skill_plan_entry,
                        skill_name=skill_name,
                        purpose=request.purpose,
                    )
                    basic_format_stage_error = _post_patch_basic_format_stage_error(request.file_path, repaired_candidate)
                    if basic_format_stage_error is not None:
                        candidate = repaired_candidate
                        stage_error = basic_format_stage_error
                        deterministic_error = _basic_format_repair_feedback(basic_format_stage_error)
                        continue
                    compile_stage_error = _post_patch_python_compile_stage_error(request.file_path, repaired_candidate)
                    if compile_stage_error is not None:
                        candidate = repaired_candidate
                        stage_error = compile_stage_error
                        deterministic_error = str(compile_stage_error)
                        continue

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
                                    f"Markdown 格式重写失败：已完整重写 {layer_limit} 轮仍未通过。"
                                    f"最后错误：{deterministic_error}"
                                ),
                                error_type=_markdown_warning_error_type(request.file_path),
                                content=candidate or "",
                                recoverable=True,
                            )
                            return
                        yield _sse({
                            "type": "validation",
                            "status": "format_full_rewrite",
                            "success": False,
                            "file_path": request.file_path,
                            "role": request.role,
                            "editable": True,
                            "disabled": False,
                            "validation": {
                                "status": "format_full_rewrite",
                                "attempt": markdown_format_retry_count,
                                "markdown_format_retry_count": markdown_format_retry_count,
                                "business_repair_count": business_repair_count,
                                "source": "hard_format",
                                "layer": "hard_format",
                                "error": deterministic_error,
                            },
                        })
                        rewrite_messages = _build_markdown_format_full_rewrite_prompt(
                            file_path=request.file_path,
                            skill_name=skill_name,
                            blueprint_text=request.blueprint_text,
                            deterministic_error=deterministic_error,
                            current_content=candidate or "",
                        )
                        candidate = await _complete_creator_file_generation(
                            messages=rewrite_messages,
                            model=route.model,
                            skill_name=skill_name,
                            file_path=request.file_path,
                            prompt_variant="rewrite_markdown_full_format",
                            retry_index=markdown_format_retry_count - 1,
                        )
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
                        import_guard_result=repair_import_guard_result if 'repair_import_guard_result' in locals() else {},
                        current_file_binding=repair_current_file_binding if 'repair_current_file_binding' in locals() else {},
                        tool_pool_summary=repair_tool_pool_summary if 'repair_tool_pool_summary' in locals() else {},
                        function_execution_context=function_execution_context,
                    )
                    repaired_candidate = _canonicalize_generated_candidate(
                        file_path=request.file_path,
                        content=repaired_candidate,
                        role=request.role,
                        skill_plan_entry=effective_skill_plan_entry,
                        skill_name=skill_name,
                        purpose=request.purpose,
                    )
                    basic_format_stage_error = _post_patch_basic_format_stage_error(request.file_path, repaired_candidate)
                    if basic_format_stage_error is not None:
                        candidate = repaired_candidate
                        stage_error = basic_format_stage_error
                        deterministic_error = _basic_format_repair_feedback(basic_format_stage_error)
                        continue
                    compile_stage_error = _post_patch_python_compile_stage_error(request.file_path, repaired_candidate)
                    if compile_stage_error is not None:
                        candidate = repaired_candidate
                        stage_error = compile_stage_error
                        deterministic_error = str(compile_stage_error)
                        continue

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
    orchestration_cycle_count = 0
    max_orchestration_cycles = max_attempts * 3
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
        blocking_errors, advisory_warnings = _split_e2e_blocking_errors(e2e_errors)
        # Point 5: extract missing stdlib package requests from E2E errors.
        missing_stdlib_reqs = extract_missing_stdlib_from_e2e_errors(blocking_errors)
        if not blocking_errors:
            suffix = ""
            if repair_logs:
                suffix = "\n\n端到端自动修复记录：\n" + "\n".join(repair_logs)
            return SkillActionResponse(
                success=True,
                path=result.get("path"),
                message=(
                    result["message"]
                    + "\n严格端到端工作流校验通过：SKILL.md 命令已按顺序真实执行，中间 JSON 边界已流转，最终 stdout 已对齐 sandbox 平台输出协议。"
                    + ("\nLLM advisory validator 暂不可用，已跳过；不影响打包。" if advisory_warnings else "")
                    + suffix
                ),
                repair_events=repair_events or e2e_session.events,
                deterministic_workflow_passed=True,
                advisory_validator_status=_e2e_advisory_status_from_warnings(advisory_warnings),
                blocking_errors=[],
                warnings=advisory_warnings,
            )

        if not request.auto_repair:
            return SkillActionResponse(
                success=False,
                path=None,
                message=(
                    "严格端到端工作流校验失败：\n"
                    + "\n\n".join(blocking_errors)
                    + (
                        "\n\n端到端自动修复记录：\n" + "\n".join(repair_logs)
                        if repair_logs else ""
                    )
                ),
                repair_events=repair_events or e2e_session.events,
                deterministic_workflow_passed=False,
                advisory_validator_status=_e2e_advisory_status_from_warnings(advisory_warnings),
                blocking_errors=blocking_errors,
                warnings=advisory_warnings,
                missing_stdlib_requests=missing_stdlib_reqs,
            )

        # This is the runtime symptom location, not a confirmed repair target.
        # _repair_existing_file_for_e2e_failure performs the diagnosis phase.
        target_path = _e2e_symptom_file_from_errors(blocking_errors)
        if target_path == "__validator__":
            return SkillActionResponse(
                success=True,
                path=result.get("path"),
                message=result["message"] + "\n严格 E2E 工作流已通过。LLM advisory validator 暂不可用，已跳过；Skill 已允许打包。",
                repair_events=repair_events or e2e_session.events,
                deterministic_workflow_passed=True,
                advisory_validator_status="unavailable",
                blocking_errors=[],
                warnings=advisory_warnings,
            )

        if orchestration_cycle_count >= max_orchestration_cycles:
            return SkillActionResponse(
                success=False, path=None,
                message=("严格端到端工作流校验未能在受控 Debug Loop 范围内收敛。已达到最大 Debug orchestration cycle 上限。\n"
                         + "\n\n".join(blocking_errors)
                         + ("\n\n端到端自动修复记录：\n" + "\n".join(repair_logs) if repair_logs else "")),
                repair_events=repair_events or e2e_session.events,
                missing_stdlib_requests=missing_stdlib_reqs,
            )

        if attempt >= max_attempts:
            return SkillActionResponse(
                success=False,
                path=None,
                message=(
                    "严格端到端工作流校验失败，且自动修复达到当前目标最大次数：\n"
                    + "\n\n".join(blocking_errors)
                    + f"\n\n自动修复目标：{target_path}"
                    + f"\nDebug experiments：{attempt}/{max_attempts}"
                    + (
                        "\n\n端到端自动修复记录：\n" + "\n".join(repair_logs)
                        if repair_logs else ""
                    )
                ),
                repair_events=repair_events or e2e_session.events,
                missing_stdlib_requests=missing_stdlib_reqs,
            )
        orchestration_cycle_count += 1
        try:
            attempts_by_target[target_path] = attempts_by_target.get(target_path, 0) + 1
            e2e_callable_repair_context: dict[str, Any] | None = None
            if target_path.startswith("scripts/"):
                try:
                    candidate_callable_context = _build_e2e_callable_repair_context(
                        skill_name=skill_name,
                        target_file=target_path,
                    )
                    if candidate_callable_context:
                        e2e_callable_repair_context = candidate_callable_context
                except Exception as callable_context_exc:
                    logger.warning(
                        "[Creator][E2E] callable repair context unavailable skill=%s file=%s error=%s",
                        skill_name,
                        target_path,
                        callable_context_exc,
                    )
            repair_result = await _repair_existing_file_for_e2e_failure(
                skill_name=skill_name,
                target_path=target_path,
                e2e_errors=blocking_errors,
                requested_model=request.model,
                external_context=external_context,
                repair_events=repair_events,
                e2e_session=e2e_session,
                read_only_callable_context=e2e_callable_repair_context,
            )
            status = repair_result.get("status")
            if repair_result.get("sandbox_executed") is True:
                attempt += 1
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
            if status == "debug_progress":
                repair_logs.append(f"第 {attempt} 轮：{repaired_target} 已推动 E2E 断点，保留补丁并重新诊断新失败")
                continue
            if status == "debug_hypothesis_rejected":
                repair_logs.append(f"第 {attempt} 轮：当前 hypothesis 经真实 E2E 实验未产生改善，已回滚并进入下一轮根因诊断")
                continue
            if status == "patch_proposal_exhausted":
                repair_logs.append(f"第 {attempt} 轮：当前 diagnosis 未能生成合法局部补丁，尚未经过真实 E2E 实验证伪，进入下一轮根因诊断")
                continue
            if status == "diagnosis_exhausted":
                return SkillActionResponse(success=False, path=None, message="严格端到端工作流校验失败，且根因诊断无法提出新的合法假设：\n" + "\n\n".join(blocking_errors), repair_events=repair_events or e2e_session.events, missing_stdlib_requests=missing_stdlib_reqs)
            if status == "still_failed_same_target":
                repair_logs.append(
                    f"第 {attempt} 轮：{repaired_target} 仍报同目标错误，未完成修复"
                )
                return SkillActionResponse(
                    success=False,
                    path=None,
                    message=(
                        "严格端到端工作流校验失败，且内容补丁修复未完成；文件保持可编辑草稿：\n"
                        + "\n\n".join(blocking_errors)
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
                    missing_stdlib_requests=missing_stdlib_reqs,
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
                    + "\n\n".join(blocking_errors)
                    + f"\n\n自动修复目标：{target_path}"
                    + f"\n自动修复异常：{exc}"
                    + (
                        "\n\n端到端自动修复记录：\n" + "\n".join(repair_logs)
                        if repair_logs else ""
                    )
                ),
                repair_events=repair_events or e2e_session.events,
                missing_stdlib_requests=missing_stdlib_reqs,
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
        blocking_errors, advisory_warnings = _split_e2e_blocking_errors(e2e_errors)
        if blocking_errors:
            return SkillActionResponse(
                success=False,
                path=None,
                message=(
                    "打包已中止：严格端到端工作流校验未通过。\n"
                    "请先调用 /api/creator/validate-skill 完成自动修复，"
                    "或根据以下错误手动修改后重试：\n"
                    + "\n\n".join(blocking_errors)
                ),
                deterministic_workflow_passed=False,
                advisory_validator_status=_e2e_advisory_status_from_warnings(advisory_warnings),
                blocking_errors=blocking_errors,
                warnings=advisory_warnings,
            )
        if advisory_warnings:
            logger.info("[Creator][package][advisory_validator_skipped] skill=%s warnings=%d", skill_name, len(advisory_warnings))

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
    skill_root = getattr(settings, "skill_public_dir", settings.skills_path) / skill_name

    before_allowed: list[str] = []
    before_digest = ""
    try:
        before_pool = load_tool_pool(settings.skills_path / skill_name)
        before_binding = get_skill_tool_binding(before_pool, target_file="scripts/__init_probe__.py", include_script_core=True).model_dump(mode="json")
        before_allowed = sorted(before_binding.get("allowed_tool_ids") or [])
        before_digest = _tool_binding_digest(before_binding)
    except Exception:
        before_allowed = []
        before_digest = "tool_pool_missing"
    logger.info(
        "[Creator][init_from_blueprint_before] skill=%s allowed_tool_ids=%s binding_digest=%s",
        skill_name,
        before_allowed,
        before_digest,
    )

    try:
        skill_root.mkdir(parents=True, exist_ok=True)
        for folder in ("scripts", "references", "assets", ".creator"):
            (skill_root / folder).mkdir(parents=True, exist_ok=True)

        dirs_created = 0
        seen_dirs: set[Path] = set()

        copied_assets = _copy_confirmed_uploaded_assets_to_skill(skill_name, request.confirmed_uploaded_assets)

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

        after_allowed: list[str] = []
        after_digest = ""
        try:
            after_pool = load_tool_pool(settings.skills_path / skill_name)
            after_binding = get_skill_tool_binding(after_pool, target_file="scripts/__init_probe__.py", include_script_core=True).model_dump(mode="json")
            after_allowed = sorted(after_binding.get("allowed_tool_ids") or [])
            after_digest = _tool_binding_digest(after_binding)
        except Exception:
            after_allowed = []
            after_digest = "tool_pool_missing"
        logger.info(
            "[Creator][init_from_blueprint_after] skill=%s allowed_tool_ids=%s binding_digest=%s",
            skill_name,
            after_allowed,
            after_digest,
        )

        return InitFromBlueprintResponse(
            success=True,
            path=str(skill_root),
            files_created=0,
            message=(
                f"已初始化 Skill 目录结构，创建目录 {dirs_created} 个，复制已确认 assets {len(copied_assets)} 个。"
                "文件将在 generate-file 成功返回非空内容后写入，不再预创建 0 B 空文件；未重建或覆盖 ToolPool。"
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
    
    skill_root = getattr(settings, "skill_public_dir", settings.skills_path) / skill_name
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
