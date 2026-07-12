"""E2E workflow validation, script static checks, and trial-run helpers."""

import hashlib
import uuid

from .common import *  # noqa: F403
from .contracts import *  # noqa: F403
from .command_normalizer import canonicalize_skill_md_runtime_commands
from .tool_pool_store import load_tool_pool, get_file_binding
from .runtime_import_guard import guard_runtime_imports
from .basic_format import check_patch_candidate_basic_format




_COMMAND_FORMAT_ERROR_LAYERS = {
    "invalid_json_arg",
    "command_parse_failed",
    "bash_fence_invalid",
    "runtime_command_invalid",
    "placeholder_json_invalid",
    "command_parse",
    "command_json_parse",
    "command_json_type",
    "command_argv_missing",
    "command_argv_extra",
    "command_protocol",
    "command_block_multiple",
}


def _is_skill_md_command_format_error(errors: list[str]) -> bool:
    for error in errors or []:
        if "E2E_REPAIR_TARGET=SKILL.md" not in error:
            continue
        layer = ""
        match = re.search(r"^E2E_LAYER=([^\n]+)", error, re.M)
        if match:
            layer = match.group(1).strip()
        lowered = error.lower()
        if layer in _COMMAND_FORMAT_ERROR_LAYERS or any(code in lowered for code in _COMMAND_FORMAT_ERROR_LAYERS):
            return True
    return False


def _command_normalizer_blocked_payload(*, target_file: str, issues: list[Any]) -> dict[str, Any]:
    return {
        "target_file": target_file,
        "error_code": "command_normalizer_blocked",
        "script_path": next((getattr(issue, "script_path", None) for issue in issues if getattr(issue, "script_path", None)), None),
        "issues": [getattr(issue, "__dict__", {}) for issue in issues],
        "missing_keys": [key for issue in issues for key in (getattr(issue, "detail", {}) or {}).get("missing_keys", [])],
        "available_contract_sources": [source for issue in issues for source in (getattr(issue, "detail", {}) or {}).get("available_contract_sources", [])],
    }


def _is_python_stdlib_module(name: str) -> bool:
    """Return True if *name* is a Python standard-library module.

    Uses ``sys.stdlib_module_names`` (Python 3.10+) and ``sys.builtin_module_names``
    (all versions).  Standard-library modules ship with the interpreter and cannot
    be installed via pip; surfacing them as install requests would always fail.
    """
    import sys as _sys
    stdlib_names: frozenset[str] = getattr(_sys, "stdlib_module_names", frozenset())
    builtin_names: frozenset[str] = frozenset(getattr(_sys, "builtin_module_names", ()))
    return name in stdlib_names or name in builtin_names


def extract_missing_stdlib_from_e2e_errors(errors: list[str]) -> list[dict[str, str]]:
    """Extract missing third-party package requests from E2E execution errors.

    Parses ``ModuleNotFoundError`` and ``ImportError`` lines in stderr/stdout
    sections of E2E error messages and returns structured install requests.
    These are surfaced to the caller so the backend can add the packages to the
    environment rather than treating them as code bugs.

    Python standard-library modules are automatically excluded: they ship with
    the interpreter and cannot be installed via pip, so attempting to install
    them would always fail.
    """
    import re as _re
    requests: list[dict[str, str]] = []
    seen: set[str] = set()
    # Patterns: "No module named 'X'" or "No module named X"
    module_pattern = _re.compile(
        r"(?:ModuleNotFoundError|ImportError)[^\n]*No module named ['\"]?([A-Za-z0-9_.\-]+)['\"]?",
        _re.IGNORECASE,
    )
    for error in errors or []:
        for match in module_pattern.finditer(error):
            pkg = match.group(1).split(".")[0]  # top-level package name
            if pkg and pkg not in seen and not _is_python_stdlib_module(pkg):
                seen.add(pkg)
                requests.append({
                    "package": pkg,
                    "reason": "E2E execution failed with ModuleNotFoundError; package must be added to the environment library.",
                    "source": "e2e_missing_stdlib",
                })
    return requests


def _skill_md_command_normalizer_context(
    *,
    skill_dir: Path,
    skill_md: str,
) -> tuple[list[Any], dict[str, Any], Any | None]:
    """Collect existing E2E contract context for command normalization.

    The key addition is script_argv_schema extracted from the generated script's
    strict_json_argv_guard.  This lets SKILL.md command normalization prefer the
    script's actual first-round argv interface without inferring argument names
    from SkillPlan/RequirementGraph field names.
    """
    script_files = (
        sorted((skill_dir / "scripts").glob("*.py"))
        if (skill_dir / "scripts").is_dir()
        else []
    )

    entries: list[Any] = []
    runtime_specs: dict[str, Any] = {}

    for script_file in script_files:
        rel = script_file.relative_to(skill_dir).as_posix()

        try:
            entry = _skill_plan_entry_for_file(file_path=rel, blueprint_text=skill_md)
        except Exception:
            entry = None

        if entry is not None:
            entries.append(entry)

        spec: dict[str, Any] = {}

        command_template = str(getattr(entry, "command_template", "") or "") if entry is not None else ""
        if command_template:
            spec["command_template"] = command_template

        try:
            script_content = script_file.read_text(encoding="utf-8", errors="replace")
            argv_schema = extract_python_strict_argv_schema(script_content)
        except Exception:
            argv_schema = {}

        if isinstance(argv_schema, dict) and argv_schema:
            allowed_keys = argv_schema.get("allowed_keys")
            required_keys = argv_schema.get("required_keys")
            optional_keys = argv_schema.get("optional_keys")
            expected_types = argv_schema.get("expected_types")

            has_useful_schema = any(
                isinstance(value, (list, dict)) and bool(value)
                for value in (allowed_keys, required_keys, optional_keys, expected_types)
            )

            if has_useful_schema:
                spec["script_argv_schema"] = argv_schema

        if spec:
            runtime_specs[rel] = spec

    try:
        requirement_graph = _load_requirement_graph_for_e2e(skill_dir)
    except Exception:
        requirement_graph = None

    return entries, runtime_specs, requirement_graph


def _normalize_skill_md_runtime_commands_for_e2e(
    *,
    skill_name: str,
    skill_dir: Path,
    skill_md: str,
):
    files, runtime_specs, requirement_graph = _skill_md_command_normalizer_context(
        skill_dir=skill_dir,
        skill_md=skill_md,
    )
    return canonicalize_skill_md_runtime_commands(
        skill_name=skill_name,
        skill_md=skill_md,
        files=files,
        requirement_graph=requirement_graph,
        runtime_specs=runtime_specs,
    )

def _platform_io_repair_summary() -> str:
    return (
        platform_io_contract_prompt_text()
        + "\nE2E repair prohibitions: do not use os.path.join(OUTPUT_DIR, \"outputs\"), "
        + "do not use os.path.join(output_dir, \"outputs\"), do not replace(\"/tmp/\", \"outputs/\"), "
        + "do not use filename=full_path, and do not manually rewrite helper-returned pdf_path/file_outputs. "
        + "If a helper already returns pdf_path/file_outputs, prefer return result or forward those fields unchanged. "
        + "If a stdout-declared path resolves under trial_skill_dir/outputs or trial_skill_dir/assets/generated and exists, it is legal."
    )

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
    details: dict[str, Any] = field(default_factory=dict)

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
            "details": self.details,
        }


@dataclass(frozen=True)
class E2ETypedInputSpec:
    name: str
    shape: str = "string"
    item_shape: str = ""
    required: bool = True
    source: str = "placeholder"
    target_file: str = ""
    confidence: str = "low"
    properties: dict[str, str] = field(default_factory=dict)


def _format_e2e_failure(failure: E2EFailure) -> str:
    return (
        f"E2E_REPAIR_TARGET={failure.target_file}\n"
        f"E2E_LAYER={failure.layer}\n"
        "E2E_STRUCTURED_FAILURE="
        + json.dumps(failure.to_dict(), ensure_ascii=False, sort_keys=True)
    )


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

_SANDBOX_TERMINAL_OUTPUT_KEYS = set(
    build_platform_io_contract().get("platform_skill_boundary", {}).get("final_output_fields", [])
)


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
    expr = _normalize_e2e_placeholder_expr(expr)
    if not expr:
        return ""
    return re.split(r"[.\[]", expr, maxsplit=1)[0].strip()


def _normalize_e2e_placeholder_expr(expr: str) -> str:
    value = str(expr or "").strip()
    value = re.sub(r"\[([^\]]+)\]", r".\1", value)
    return value


def _canonical_e2e_shape(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    text = text.replace("array", "list").replace("path", "file_path")
    if not text:
        return "string"
    if "list" in text and ("file_path" in text or "file" in text):
        return "list[file_path]"
    if "list" in text and ("object" in text or "dict" in text):
        return "list[object]"
    if "list" in text and ("str" in text or "string" in text or "text" in text):
        return "list[string]"
    if text in {"list", "array"} or "list" in text:
        return "list"
    if "file_path" in text or text in {"file", "filepath"}:
        return "file_path"
    if text in {"int", "integer"}:
        return "integer"
    if text in {"float", "number"}:
        return "number"
    if text in {"bool", "boolean"}:
        return "boolean"
    if text in {"dict", "object", "json"}:
        return "object"
    if text in {"str", "string", "text", "scalar"}:
        return "string"
    return text


def _shape_item_shape(shape: str) -> str:
    match = re.fullmatch(r"list\[(.+)\]", str(shape or "").strip())
    return match.group(1) if match else ""


def _parse_typed_name(raw: Any) -> tuple[str, str]:
    text = str(raw or "").strip()
    if not text:
        return "", ""
    match = re.match(r"^([A-Za-z_][\w.-]*)\s*(?::|\=|\()?\s*([A-Za-z_][\w\[\]-]*(?:\[[^\]]+\])?)?", text)
    if not match:
        return text, ""
    name = (match.group(1) or "").strip()
    type_text = (match.group(2) or "").strip()
    if type_text == name:
        type_text = ""
    return name, _canonical_e2e_shape(type_text)


def _put_typed_spec(specs: dict[str, E2ETypedInputSpec], spec: E2ETypedInputSpec) -> None:
    if not spec.name:
        return
    priority = {"requirement_graph": 5, "skill_plan_entry": 4, "argv_schema": 3, "placeholder": 2, "external_context": 1}
    old = specs.get(spec.name)
    if old is None or priority.get(spec.source, 0) > priority.get(old.source, 0):
        specs[spec.name] = spec


def _whole_e2e_placeholder_expr(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\{\{\s*([^{}]+?)\s*\}\}", value.strip())
    if not match:
        return None
    return _normalize_e2e_placeholder_expr(match.group(1))


def _e2e_placeholder_uses_index(expr: str) -> bool:
    return bool(re.search(r"(?:^|\.)\d+(?:\.|$)", _normalize_e2e_placeholder_expr(expr)))


def _list_shape_for_item_shape(shape: str) -> str:
    canonical = _canonical_e2e_shape(shape)
    if canonical.startswith("list"):
        return canonical
    return f"list[{canonical or 'string'}]"


def _collect_e2e_typed_inputs_from_graph(
    *,
    commands: list[E2EWorkflowCommand],
    requirements_by_file: dict[str, list[RequirementItem]],
    skill_plan_entries: dict[str, SkillPlanEntry] | None,
    skill_dir: Path | None,
) -> list[E2ETypedInputSpec]:
    specs: dict[str, E2ETypedInputSpec] = {}
    for target_file, reqs in (requirements_by_file or {}).items():
        for req in reqs or []:
            for raw in getattr(req, "inputs", []) or []:
                name, shape = _parse_typed_name(raw)
                if name:
                    _put_typed_spec(specs, E2ETypedInputSpec(name=name, shape=shape or "string", item_shape=_shape_item_shape(shape), required=True, source="requirement_graph", target_file=target_file, confidence="high"))

    for target_file, entry in (skill_plan_entries or {}).items():
        for raw in (getattr(entry, "inputs", []) or []) + (getattr(entry, "outputs", []) or []):
            name, shape = _parse_typed_name(raw)
            if name:
                _put_typed_spec(specs, E2ETypedInputSpec(name=name, shape=shape or "string", item_shape=_shape_item_shape(shape), required=True, source="skill_plan_entry", target_file=target_file, confidence="medium"))
        artifact_contract = getattr(entry, "artifact_contract", None)
        if isinstance(artifact_contract, dict):
            for name, raw_shape in artifact_contract.items():
                _put_typed_spec(specs, E2ETypedInputSpec(name=str(name), shape=_canonical_e2e_shape(raw_shape), item_shape=_shape_item_shape(_canonical_e2e_shape(raw_shape)), required=True, source="skill_plan_entry", target_file=target_file, confidence="medium"))

    for command in commands:
        command_expected_types: dict[str, str] = {}
        if skill_dir is not None and command.script_path.endswith(".py"):
            script_file = skill_dir / command.script_path
            if script_file.is_file():
                try:
                    schema = extract_python_strict_argv_schema(script_file.read_text(encoding="utf-8"))
                except Exception:
                    schema = {}
                expected_types = schema.get("expected_types") if isinstance(schema, dict) else {}
                if isinstance(expected_types, dict):
                    for name, raw_shape in expected_types.items():
                        shape = _canonical_e2e_shape(raw_shape)
                        command_expected_types[str(name)] = shape
                        _put_typed_spec(specs, E2ETypedInputSpec(name=str(name), shape=shape, item_shape=_shape_item_shape(shape), required=True, source="argv_schema", target_file=command.script_path, confidence="high"))

        for argv_key, argv_value in (command.argv_template or {}).items():
            expr = _whole_e2e_placeholder_expr(argv_value)
            if not expr:
                continue
            root = _placeholder_root(expr)
            if not root:
                continue
            argv_key_text = str(argv_key)
            expected_shape = command_expected_types.get(argv_key_text)
            if not expected_shape and argv_key_text in specs:
                expected_shape = specs[argv_key_text].shape
            expected_shape = _canonical_e2e_shape(expected_shape or "")
            uses_index = _e2e_placeholder_uses_index(expr)
            if uses_index:
                root_shape = _list_shape_for_item_shape(expected_shape or "string")
            elif expected_shape.startswith("list"):
                root_shape = expected_shape
            else:
                continue
            _put_typed_spec(specs, E2ETypedInputSpec(name=root, shape=root_shape, item_shape=_shape_item_shape(root_shape), required=True, source="argv_schema" if command_expected_types.get(argv_key_text) else "placeholder", target_file=command.script_path, confidence="high" if command_expected_types.get(argv_key_text) else "medium"))

        for expr in _placeholder_exprs_from_value(command.argv_template):
            normalized = _normalize_e2e_placeholder_expr(expr)
            root = _placeholder_root(normalized)
            if root:
                shape = "list" if _e2e_placeholder_uses_index(normalized) else "string"
                _put_typed_spec(specs, E2ETypedInputSpec(name=root, shape=shape, item_shape=_shape_item_shape(shape), required=True, source="placeholder", target_file=command.script_path, confidence="low"))
            if normalized.startswith("fields."):
                parts = normalized.split(".")
                if len(parts) >= 2 and parts[1]:
                    argv_spec = specs.get(parts[1])
                    shape = argv_spec.shape if argv_spec else "string"
                    _put_typed_spec(specs, E2ETypedInputSpec(name=f"fields.{parts[1]}", shape=shape, item_shape=_shape_item_shape(shape), required=True, source=(argv_spec.source if argv_spec else "placeholder"), target_file=command.script_path, confidence=(argv_spec.confidence if argv_spec else "low")))
    return list(specs.values())


def _read_e2e_skill_md_for_samples(skill_dir: Path | None) -> str:
    if skill_dir is None:
        return ""
    path = skill_dir / "SKILL.md"
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _read_e2e_script_for_samples(skill_dir: Path | None, target_file: str = "") -> str:
    if skill_dir is None or not target_file:
        return ""
    path = skill_dir / target_file
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _e2e_file_kind_from_text(text: str) -> list[str]:
    lowered = str(text or "").lower()
    kinds: list[str] = []

    # Keep this generic: infer file formats from extensions / common MIME words,
    # not business-specific skill names.
    checks = [
        ("pdf", ("pdf", ".pdf", "application/pdf")),
        ("docx", ("docx", ".docx", "word document", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")),
        ("txt", ("txt", ".txt", "plain text", "text/plain")),
        ("md", ("markdown", ".md", "text/markdown")),
        ("csv", ("csv", ".csv", "text/csv")),
        ("json", ("json", ".json", "application/json")),
        ("html", ("html", ".html", ".htm", "text/html")),
        ("png", ("png", ".png", "image/png")),
        ("jpg", ("jpg", ".jpg", ".jpeg", "image/jpeg")),
    ]

    for kind, needles in checks:
        if any(needle in lowered for needle in needles):
            kinds.append(kind)

    return list(dict.fromkeys(kinds))


def _infer_e2e_file_sample_kinds(
    *,
    name: str = "",
    shape: str = "",
    target_file: str = "",
    skill_md: str = "",
    script_content: str = "",
    max_count: int = 3,
) -> list[str]:
    """Infer representative sample file kinds for Creator E2E.

    This deliberately uses generic evidence:
    - argv key / spec name
    - expected shape
    - target script path
    - SKILL.md
    - script source

    It does not hardcode business skill names.
    """
    evidence = "\n".join([
        str(name or ""),
        str(shape or ""),
        str(target_file or ""),
        str(skill_md or "")[:16000],
        str(script_content or "")[:16000],
    ])

    key_kinds = _e2e_file_kind_from_text(" ".join([str(name or ""), str(shape or ""), str(target_file or "")]))
    context_kinds = _e2e_file_kind_from_text(evidence)

    ordered: list[str] = []

    # If the argv key itself says pdf/docx/csv/etc., obey it first.
    ordered.extend(key_kinds)

    # Otherwise infer from SKILL.md / script content.
    ordered.extend(context_kinds)

    # For generic "document/file/path" inputs, prefer document formats when declared.
    # This helps catch fake txt-only implementations for a skill that claims PDF/DOCX support.
    priority = ["pdf", "docx", "txt", "md", "csv", "json", "html", "png", "jpg"]

    deduped = [kind for kind in priority if kind in set(ordered)]

    if not deduped:
        deduped = ["txt"]

    return deduped[: max(1, max_count)]


def _pdf_escape_text(text: str) -> str:
    return (
        str(text or "")
        .replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _write_minimal_pdf(path: Path, text: str) -> None:
    """Write a tiny valid PDF using only stdlib bytes.

    It is enough for most PDF text extractors to open the file and see a page.
    """
    stream = f"BT /F1 12 Tf 72 720 Td ({_pdf_escape_text(text)}) Tj ET".encode("latin-1", errors="replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
    ]

    data = bytearray()
    data.extend(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for idx, obj in enumerate(objects, start=1):
        offsets.append(len(data))
        data.extend(f"{idx} 0 obj\n".encode("ascii"))
        data.extend(obj)
        data.extend(b"\nendobj\n")

    xref_offset = len(data)
    data.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    data.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode("ascii"))

    data.extend(
        (
            "trailer\n"
            f"<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            "startxref\n"
            f"{xref_offset}\n"
            "%%EOF\n"
        ).encode("ascii")
    )
    path.write_bytes(bytes(data))


def _write_minimal_docx(path: Path, text: str) -> None:
    """Write a minimal valid DOCX using only stdlib zipfile."""
    import zipfile
    from xml.sax.saxutils import escape

    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        "<w:p><w:r><w:t>"
        + escape(str(text or "Creator E2E sample DOCX"))
        + "</w:t></w:r></w:p>"
        '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>'
        "</w:body>"
        "</w:document>"
    )

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )

    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document_xml)


def _write_minimal_png(path: Path) -> None:
    # 1x1 transparent PNG.
    path.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c636000000200015d0b2a0000000049454e44ae426082"
    ))


def _write_minimal_jpg(path: Path) -> None:
    # 1x1 JPEG.
    path.write_bytes(bytes.fromhex(
        "ffd8ffe000104a46494600010101006000600000ffdb00430003020203020203"
        "030303040304050805050404050a070706080c0a0c0c0b0a0b0b0d0e12100d0e"
        "110e0b0b1016101113141515150c0f171816141812141514ffc0000b08000100"
        "0101011100ffc4001400010000000000000000000000000000000000000000ff"
        "da0008010100003f00d2cf20ffd9"
    ))


def _e2e_sample_suffix_for_kind(kind: str) -> str:
    return {
        "pdf": ".pdf",
        "docx": ".docx",
        "txt": ".txt",
        "md": ".md",
        "csv": ".csv",
        "json": ".json",
        "html": ".html",
        "png": ".png",
        "jpg": ".jpg",
        "jpeg": ".jpg",
    }.get(str(kind or "").lower(), ".txt")


def _write_e2e_sample_file_by_kind(path: Path, *, kind: str, name: str, index: int) -> None:
    kind = str(kind or "txt").lower()
    sample_text = (
        f"Creator E2E sample content for {name or 'input'} #{index}.\n"
        "This file is generated deterministically to validate file input handling, "
        "script execution, stdout JSON, and final workflow closure.\n"
    )

    if kind == "pdf":
        _write_minimal_pdf(path, sample_text)
    elif kind == "docx":
        _write_minimal_docx(path, sample_text)
    elif kind == "md":
        path.write_text("# Creator E2E Sample\n\n" + sample_text, encoding="utf-8")
    elif kind == "csv":
        path.write_text("title,content\nCreator E2E Sample," + sample_text.replace("\n", " ") + "\n", encoding="utf-8")
    elif kind == "json":
        path.write_text(json.dumps({"title": "Creator E2E Sample", "content": sample_text}, ensure_ascii=False), encoding="utf-8")
    elif kind == "html":
        path.write_text(f"<html><body><h1>Creator E2E Sample</h1><p>{sample_text}</p></body></html>", encoding="utf-8")
    elif kind == "png":
        _write_minimal_png(path)
    elif kind in {"jpg", "jpeg"}:
        _write_minimal_jpg(path)
    else:
        path.write_text(sample_text, encoding="utf-8")


def _e2e_sample_file(
    skill_dir: Path | None,
    name: str,
    index: int = 1,
    *,
    kind: str | None = None,
    suffix: str | None = None,
) -> str:
    base = (skill_dir / ".creator_e2e" / "samples") if skill_dir is not None else Path(tempfile.mkdtemp(prefix="creator-e2e-samples-"))
    base.mkdir(parents=True, exist_ok=True)

    sample_kind = str(kind or "").strip().lower() or "txt"
    sample_suffix = suffix or _e2e_sample_suffix_for_kind(sample_kind)

    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name or "input").strip("._") or "input"
    path = base / f"{safe}_{index}{sample_suffix}"

    _write_e2e_sample_file_by_kind(path, kind=sample_kind, name=name, index=index)
    return str(path)


def _materialize_e2e_sample_value(
    spec: E2ETypedInputSpec,
    *,
    skill_dir: Path | None,
) -> Any:
    shape = _canonical_e2e_shape(spec.shape)
    skill_md = _read_e2e_skill_md_for_samples(skill_dir)
    script_content = _read_e2e_script_for_samples(skill_dir, spec.target_file)

    if shape in {"number", "integer"}:
        return 1

    if shape == "boolean":
        return True

    if shape == "object":
        return {
            key: _materialize_e2e_sample_value(
                E2ETypedInputSpec(
                    name=key,
                    shape=value,
                    target_file=spec.target_file,
                    source=spec.source,
                    confidence=spec.confidence,
                ),
                skill_dir=skill_dir,
            )
            for key, value in (spec.properties or {}).items()
        } or {"value": "sample value"}

    if shape == "file_path":
        kinds = _infer_e2e_file_sample_kinds(
            name=spec.name,
            shape=shape,
            target_file=spec.target_file,
            skill_md=skill_md,
            script_content=script_content,
            max_count=1,
        )
        return _e2e_sample_file(skill_dir, spec.name, 1, kind=kinds[0])

    if shape.startswith("list"):
        item_shape = _shape_item_shape(shape) or spec.item_shape or "string"

        if item_shape == "file_path":
            kinds = _infer_e2e_file_sample_kinds(
                name=spec.name,
                shape=shape,
                target_file=spec.target_file,
                skill_md=skill_md,
                script_content=script_content,
                max_count=3,
            )
            return [
                _e2e_sample_file(skill_dir, spec.name, index + 1, kind=kind)
                for index, kind in enumerate(kinds)
            ]

        if item_shape == "object":
            return [{"value": "sample item 1"}, {"value": "sample item 2"}]

        if item_shape in {"number", "integer"}:
            return [1, 2]

        if item_shape == "boolean":
            return [True, False]

        return ["sample item 1", "sample item 2"]

    return "sample value"


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
    missing_details: list[dict[str, Any]] | None = None,
    typed_specs: dict[str, E2ETypedInputSpec] | None = None,
) -> Any:
    """Resolve placeholder expression against current runtime payload.

    Supports:
    - {{text_content}}
    - {{image_paths.0}}
    - {{foo.bar.0}}
    """
    original_expr = str(expr or "").strip()
    expr = _normalize_e2e_placeholder_expr(original_expr)

    def mark_missing(reason: str, root: str = "") -> None:
        missing.append(original_expr or expr)
        if missing_details is not None:
            root_value = payload.get(root) if root else None
            spec = (typed_specs or {}).get(root) if root else None
            missing_details.append({
                "expr": original_expr,
                "normalized_expr": expr,
                "root": root,
                "reason": reason,
                "root_shape": _json_shape(root_value) if root in payload else "missing",
                "expected_shape_from_graph": spec.shape if spec else "",
                "available_payload_shape": _json_object_shape(payload),
            })

    if not expr:
        mark_missing("empty_expr")
        return ""

    parts = expr.split(".")
    root = parts[0].strip()

    if root not in payload:
        mark_missing("root_missing", root)
        return ""

    value: Any = payload[root]

    for part in parts[1:]:
        part = part.strip()
        if isinstance(value, list):
            try:
                index = int(part)
            except ValueError:
                mark_missing("index_not_integer", root)
                return ""
            if index < 0 or index >= len(value):
                mark_missing("index_out_of_range", root)
                return ""
            value = value[index]
            continue

        if isinstance(value, dict):
            if part not in value:
                mark_missing("key_missing", root)
                return ""
            value = value[part]
            continue

        mark_missing("type_mismatch", root)
        return ""

    return value


def _e2e_command_placeholders(command: E2EWorkflowCommand) -> list[str]:
    return _placeholder_exprs_from_value(command.argv_template)

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
    missing_details: list[dict[str, Any]] | None = None,
    typed_specs: dict[str, E2ETypedInputSpec] | None = None,
) -> Any:
    if isinstance(value, str):
        whole = re.fullmatch(r"\{\{\s*([^{}]+?)\s*\}\}", value.strip())
        if whole:
            return _resolve_e2e_payload_expr(
                whole.group(1),
                payload=payload,
                missing=missing,
                missing_details=missing_details,
                typed_specs=typed_specs,
            )

        def replace_match(match: re.Match[str]) -> str:
            rendered = _resolve_e2e_payload_expr(
                match.group(1),
                payload=payload,
                missing=missing,
                missing_details=missing_details,
                typed_specs=typed_specs,
            )
            if isinstance(rendered, (dict, list)):
                return json.dumps(rendered, ensure_ascii=False)
            return str(rendered)

        return _E2E_PLACEHOLDER_RE.sub(replace_match, value)

    if isinstance(value, dict):
        return {
            str(key): _render_e2e_template_value(item, payload=payload, missing=missing, missing_details=missing_details, typed_specs=typed_specs)
            for key, item in value.items()
        }

    if isinstance(value, list):
        return [
            _render_e2e_template_value(item, payload=payload, missing=missing, missing_details=missing_details, typed_specs=typed_specs)
            for item in value
        ]

    return value


def _render_e2e_command_payload(
    command: E2EWorkflowCommand,
    *,
    payload: dict[str, Any],
    traces: list[E2EStepTrace] | None = None,
    typed_input_specs: list[E2ETypedInputSpec] | None = None,
) -> dict[str, Any]:
    missing: list[str] = []
    missing_details: list[dict[str, Any]] = []
    typed_specs_by_name = {spec.name: spec for spec in (typed_input_specs or [])}

    rendered = {
        str(key): _render_e2e_template_value(value, payload=payload, missing=missing, missing_details=missing_details, typed_specs=typed_specs_by_name)
        for key, value in command.argv_template.items()
    }

    if missing:
        unique_missing = sorted(set(missing))
        available = sorted(payload.keys())
        source_lines = [
            f"step {trace.ordinal} {trace.script_path}: stdout_keys={trace.stdout_keys} new_keys={trace.new_keys}"
            for trace in (traces or [])
        ]
        logger.info("[Creator][E2E][interface_repair_risk] %s", json.dumps({
            "event": "e2e_interface_repair_risk",
            "script_path": command.script_path,
            "ordinal": command.ordinal,
            "missing_placeholders": unique_missing,
            "available_keys": available,
            "source_path": command.source_path,
            "missing_placeholder_details": missing_details,
        }, ensure_ascii=False, default=str))

        raise ValueError(
            _e2e_error(
                target=command.source_path,
                layer="external_input_missing" if command.ordinal == 1 else "e2e_dataflow_missing",
                message=(
                    ("missing_placeholder: 平台外部输入缺失，第一条命令不能引用 guaranteed envelope 中不存在的字段；可改为传 user_request/input/payload/envelope 并由入口脚本内部解析和默认化可选项。" if command.ordinal == 1 else "missing_placeholder: Skill 内部 dataflow 缺失，后续命令只能引用已有 context 或前序 stdout 字段。")
                    + "\n"
                    + f"第 {command.ordinal} 步 {command.script_path} 的命令模板引用了当前 payload 中不存在的字段："
                    f"{', '.join(unique_missing)}。\n"
                    f"当前可用字段：{', '.join(available) or '(无)'}。\n"
                    "missing_placeholder_details="
                    f"{json.dumps(missing_details, ensure_ascii=False, sort_keys=True, default=str)}\n"
                    "当前可用字段来源：\n"
                    f"{chr(10).join(source_lines) if source_lines else '(仅外部输入或无前序 stdout 来源记录)'}\n"
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



_E2E_RUNTIME_INPUT_LITERAL_TOKENS = {
    "__RUNTIME_INPUT_FILE__",
    "__RUNTIME_INPUT_FILES__",
    "__RUNTIME_INPUT_TEXT__",
    "__RUNTIME_INPUT__",
    "__USER_INPUT__",
}


def _is_e2e_runtime_input_literal(value: Any) -> bool:
    return isinstance(value, str) and value.strip() in _E2E_RUNTIME_INPUT_LITERAL_TOKENS


def _e2e_runtime_literal_token(value: Any) -> str:
    return str(value or "").strip() if isinstance(value, str) else ""


def _e2e_text_sample_value() -> str:
    return (
        "Creator E2E runtime input sample text. "
        "这是一段用于验证纯文本输入的中文内容，包含 English words 和标点。"
        "它用于测试参数传递、脚本消费、stdout JSON 和最终输出闭环。"
    )


def _materialize_e2e_runtime_literal_scalar(
    *,
    key: str,
    value: str,
    skill_dir: Path | None,
    target_file: str = "",
    skill_md: str = "",
    script_content: str = "",
    index: int = 1,
) -> Any:
    """Materialize Creator runtime sentinel literals for deterministic E2E.

    Real platform execution replaces these sentinels before invoking a Skill.
    Creator E2E must do the same in the trial workspace; otherwise generated
    scripts receive strings such as ``__RUNTIME_INPUT_FILE__`` and fail with a
    misleading FileNotFoundError that gets incorrectly attributed to the script.
    """
    token = _e2e_runtime_literal_token(value)

    if token == "__RUNTIME_INPUT_FILES__":
        kinds = _infer_e2e_file_sample_kinds(
            name=key,
            shape="list[file_path]",
            target_file=target_file,
            skill_md=skill_md,
            script_content=script_content,
            max_count=3,
        )
        return [
            _e2e_sample_file(skill_dir, key or "runtime_input_file", idx + 1, kind=kind)
            for idx, kind in enumerate(kinds)
        ]

    if token == "__RUNTIME_INPUT_FILE__":
        kinds = _infer_e2e_file_sample_kinds(
            name=key,
            shape="file_path",
            target_file=target_file,
            skill_md=skill_md,
            script_content=script_content,
            max_count=1,
        )
        return _e2e_sample_file(skill_dir, key or "runtime_input_file", index, kind=kinds[0])

    if token in {"__RUNTIME_INPUT_TEXT__", "__RUNTIME_INPUT__", "__USER_INPUT__"}:
        return _e2e_text_sample_value()

    return value


def _materialize_e2e_runtime_input_literals(
    value: Any,
    *,
    skill_dir: Path | None,
    key: str = "",
    target_file: str = "",
    skill_md: str = "",
    script_content: str = "",
) -> tuple[Any, list[dict[str, Any]]]:
    """Replace runtime sentinel literals in a rendered E2E payload.

    This function is deliberately deterministic and does not write back to
    SKILL.md or generated scripts. It only prepares realistic sandbox inputs.
    """
    events: list[dict[str, Any]] = []

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for child_key, child_value in value.items():
            materialized, child_events = _materialize_e2e_runtime_input_literals(
                child_value,
                skill_dir=skill_dir,
                key=str(child_key),
                target_file=target_file,
                skill_md=skill_md,
                script_content=script_content,
            )
            out[str(child_key)] = materialized
            events.extend(child_events)
        return out, events

    if isinstance(value, list):
        out_list: list[Any] = []
        for item in value:
            materialized, child_events = _materialize_e2e_runtime_input_literals(
                item,
                skill_dir=skill_dir,
                key=key,
                target_file=target_file,
                skill_md=skill_md,
                script_content=script_content,
            )
            out_list.append(materialized)
            events.extend(child_events)
        return out_list, events

    if _is_e2e_runtime_input_literal(value):
        token = _e2e_runtime_literal_token(value)
        materialized = _materialize_e2e_runtime_literal_scalar(
            key=key,
            value=token,
            skill_dir=skill_dir,
            target_file=target_file,
            skill_md=skill_md,
            script_content=script_content,
        )
        events.append({
            "event": "runtime_input_literal_materialized",
            "key": key,
            "token": token,
            "target_file": target_file,
            "materialized_shape": _json_shape(materialized),
            "materialized_value": materialized if isinstance(materialized, str) else str(materialized),
        })
        return materialized, events

    return value, events


def _materialize_rendered_e2e_payload_runtime_literals(
    rendered_payload: dict[str, Any],
    *,
    skill_dir: Path | None,
    target_file: str = "",
    skill_md: str = "",
    script_content: str = "",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Materialize literal runtime input sentinels before executing a step."""
    if not isinstance(rendered_payload, dict):
        return {}, []

    materialized, events = _materialize_e2e_runtime_input_literals(
        rendered_payload,
        skill_dir=skill_dir,
        key="",
        target_file=target_file,
        skill_md=skill_md,
        script_content=script_content,
    )

    if not isinstance(materialized, dict):
        return rendered_payload, events

    return materialized, events


def _seed_initial_e2e_payload(
    commands: list[E2EWorkflowCommand],
    *,
    external_context: dict[str, Any] | None = None,
    skill_dir: Path | None = None,
    requirements_by_file: dict[str, list[RequirementItem]] | None = None,
    skill_plan_entries: dict[str, SkillPlanEntry] | None = None,
) -> dict[str, Any]:
    """Seed Creator E2E with a non-empty generic external input envelope.

    第二轮 E2E 需要真实跑 workflow。若 validate-skill 没传用户消息，
    也必须给 {{user_request}} / {{input}} / {{text}} / {{payload}}
    一个非空通用测试值，否则第一步会收到空字符串，导致参数接入审查误判。

    fields.<key> 占位符按第一步脚本 strict_json_argv_guard schema 补 typed E2E
    测试值；这些值只存在于 runtime payload，不写回 SKILL.md。
    """
    base_context = external_context
    if not isinstance(base_context, dict):
        base_context = build_creator_external_input_context(messages=[])

    payload: dict[str, Any] = dict(base_context or {})

    seed_value = ""
    for key in ("user_request", "input", "text", "payload"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            seed_value = value.strip()
            break

    if not seed_value:
        seed_value = (
            "Creator E2E 验证输入：请根据这个请求完成当前 Skill 的主要任务，"
            "内容包含中文、English words 和标点，用于验证参数传递、脚本消费和输出闭环。"
        )

    for key in ("user_request", "input", "text", "payload"):
        if not _json_value_non_empty(payload.get(key)):
            payload[key] = seed_value

    if not isinstance(payload.get("fields"), dict):
        payload["fields"] = {}

    if not isinstance(payload.get("options"), dict):
        payload["options"] = {}

    if not isinstance(payload.get("input_files"), list):
        payload["input_files"] = []

    if not isinstance(payload.get("files"), list):
        payload["files"] = list(payload.get("input_files") or [])

    typed_specs = _collect_e2e_typed_inputs_from_graph(
        commands=commands,
        requirements_by_file=requirements_by_file or {},
        skill_plan_entries=skill_plan_entries,
        skill_dir=skill_dir,
    )
    platform_roots = {"user_request", "input", "text", "payload", "fields", "options", "input_files", "files", "resources"}
    for spec in typed_specs:
        if spec.name in {"fields", "options"} and isinstance(payload.get(spec.name), dict):
            continue
        if "." in spec.name:
            root, child = spec.name.split(".", 1)
            if root not in {"fields", "options"}:
                continue
            container = payload.get(root)
            if isinstance(container, dict) and not _json_value_non_empty(container.get(child)):
                container[child] = _materialize_e2e_sample_value(spec, skill_dir=skill_dir)
            continue
        if spec.name in platform_roots and not _json_value_non_empty(payload.get(spec.name)):
            payload[spec.name] = _materialize_e2e_sample_value(spec, skill_dir=skill_dir)

    if (
        isinstance(payload.get("input_files"), list)
        and payload.get("input_files")
        and (not isinstance(payload.get("files"), list) or not payload.get("files"))
    ):
        payload["files"] = list(payload["input_files"])
    if (
        isinstance(payload.get("files"), list)
        and payload.get("files")
        and (not isinstance(payload.get("input_files"), list) or not payload.get("input_files"))
    ):
        payload["input_files"] = list(payload["files"])

    if commands:
        first = commands[0]
        schema: dict[str, Any] = {"expected_types": {}}
        if skill_dir is not None and first.script_path.endswith(".py"):
            script_file = skill_dir / first.script_path
            if script_file.is_file():
                try:
                    schema = extract_python_strict_argv_schema(script_file.read_text(encoding="utf-8"))
                except Exception:
                    schema = {"expected_types": {}}
        expected_types = schema.get("expected_types") if isinstance(schema, dict) else {}
        if not isinstance(expected_types, dict):
            expected_types = {}
        placeholders = _placeholder_exprs_from_value(first.argv_template)
        fields = payload.get("fields")
        if isinstance(fields, dict):
            for expr in placeholders:
                normalized = _normalize_e2e_placeholder_expr(expr)
                parts = normalized.split(".")
                if len(parts) < 2 or parts[0] != "fields" or not parts[1]:
                    continue
                key = parts[1]
                if _json_value_non_empty(fields.get(key)):
                    continue
                value_type = str(expected_types.get(key) or "").lower()
                shape = _canonical_e2e_shape(value_type)
                fields[key] = _materialize_e2e_sample_value(
                    E2ETypedInputSpec(name=key, shape=shape, item_shape=_shape_item_shape(shape), source="argv_schema", target_file=first.script_path, confidence="high"),
                    skill_dir=skill_dir,
                )

    return payload


def _e2e_repair_target_from_errors(errors: list[str]) -> str:
    for error in errors:
        match = re.search(r"^E2E_REPAIR_TARGET=([^\n]+)", error)
        if match:
            target = match.group(1).strip()
            if target:
                return target
    return "SKILL.md"


def _copy_skill_dir_for_e2e(
    skill_name: str,
    *,
    source_skill_dir: Path | None = None,
) -> tuple[tempfile.TemporaryDirectory, Path]:
    source_dir = (source_skill_dir or (settings.skills_path / skill_name)).resolve()

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




@dataclass
class CreatorE2ESession:
    e2e_session_id: str
    skill_name: str
    workspace_dir: Path
    venv_path: Path
    outputs_dir: Path
    deps_signature: str = ""
    command_plan_signature: str = ""
    current_revision: int = 0
    installed_deps_signature: str = ""
    temp_handle: tempfile.TemporaryDirectory | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    resolved_failures: list[dict[str, Any]] = field(default_factory=list)
    repair_attempt_counts: dict[str, int] = field(default_factory=dict)

    def to_event_base(self) -> dict[str, Any]:
        return {
            "phase": "e2e_repair",
            "e2e_session_id": self.e2e_session_id,
            "skill_name": self.skill_name,
            "workspace_dir": str(self.workspace_dir),
            "venv_path": str(self.venv_path),
            "outputs_dir": str(self.outputs_dir),
            "deps_signature": self.deps_signature,
            "command_plan_signature": self.command_plan_signature,
            "workspace_revision": self.current_revision,
        }


def _stable_json_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _file_sha256(path: Path) -> str:
    if not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _command_plan_signature(commands: list[E2EWorkflowCommand]) -> str:
    return _stable_json_hash([
        {
            "ordinal": command.ordinal,
            "script_path": command.script_path,
            "runner": command.runner,
            "argv_template": command.argv_template,
            "raw_command": command.raw_command,
        }
        for command in commands
    ])


def _deps_signature_for_commands(skill_dir: Path, skill_md: str, commands: list[E2EWorkflowCommand]) -> str:
    deps: list[str] = []
    for command in commands:
        if not command.script_path.endswith(".py"):
            continue
        try:
            entry = _skill_plan_entry_for_file(file_path=command.script_path, blueprint_text=skill_md)
            deps.extend(str(item) for item in (entry.required_capabilities or []))
            refined_contract, resolution = _contract_resolution_for_trial(command.script_path, skill_md, None, None)
            deps.extend(str(item) for item in (refined_contract.declared_dependencies or []))
            deps.extend(str(item) for item in (resolution.declared_dependencies or []))
        except Exception as exc:
            deps.append(f"unresolved:{command.script_path}:{type(exc).__name__}:{exc}")
    return _stable_json_hash(sorted(set(deps)))


def _create_e2e_session(skill_name: str, *, source_skill_dir: Path | None = None) -> CreatorE2ESession:
    skill_name = _validate_skill_name(skill_name)
    tmp = tempfile.TemporaryDirectory(prefix="creator-e2e-session-")
    root = Path(tmp.name)
    source_dir = (source_skill_dir or (settings.skills_path / skill_name)).resolve()
    workspace_dir = root / skill_name
    shutil.copytree(
        source_dir,
        workspace_dir,
        ignore=shutil.ignore_patterns(".venv", "__pycache__", "*.pyc", ".pytest_cache"),
    )
    outputs_dir = workspace_dir / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    session = CreatorE2ESession(
        e2e_session_id=f"e2e-{uuid.uuid4().hex[:12]}",
        skill_name=skill_name,
        workspace_dir=workspace_dir,
        venv_path=workspace_dir / ".venv",
        outputs_dir=outputs_dir,
        temp_handle=tmp,
    )
    session.events.append({**session.to_event_base(), "event": "session_created", "reused_venv": False})
    return session


def _e2e_repair_key(*, target_path: str, structured_failure: dict[str, Any], phase: str = "workflow_e2e") -> str:
    layer = str(
        structured_failure.get("failure_layer")
        or structured_failure.get("layer")
        or structured_failure.get("failure_category")
        or structured_failure.get("error_code")
        or "e2e"
    )
    return "|".join([target_path, layer, phase])



def _full_file_rewrite_target_rule_for_e2e(target_path: str) -> str:
    """Return full-file rewrite rules that do not reuse localized patch rules.

    Full-file rewrite must not inherit target_rule text that says
    old_lines/new_lines, exact_replace patch, or "do not output full source".
    """
    base = (
        "你正在重写完整目标文件，不是做局部 patch。\n"
        "输出完整文件内容。\n"
        "不要输出解释、Markdown 包裹、diff、old_lines/new_lines 或 JSON patch。\n"
        "不要根据具体错误文本逐字修补，也不要围绕某个报错行做小修。\n"
        "请基于目标文件职责、runtime_contract、coverage_requirements、当前内容、之前版本、上下游合同整体重写。\n"
    )

    if target_path == "SKILL.md":
        return base + (
            "\nSKILL.md 重写要求：\n"
            "- 输出完整 SKILL.md。\n"
            "- 保留合法 YAML frontmatter。\n"
            "- 保留 name / description。\n"
            "- 不要新增 runner/script/argv 伪协议对象。\n"
            "- 不要把 references/assets 写成执行步骤。\n"
            "- bash block 内只能是一条真实 shell 命令。\n"
            "- 如果脚本使用 JSON argv，命令必须是：python scripts/x.py '{\"key\":\"value\"}'。\n"
            "- 不要把 JSON 拆成多个 CLI 参数。\n"
            "- 不要用 --key value 风格替代 JSON argv。\n"
            "- 保持 workflow 说明和真实 scripts/*.py 文件一致。\n"
        )

    if target_path.startswith("scripts/"):
        return base + (
            "\nscripts/*.py 重写要求：\n"
            "- 输出完整 Python 文件。\n"
            "- 保留 strict_json_argv_guard。\n"
            "- 保持 SKILL.md command argv key 对齐。\n"
            "- stdout 必须是 JSON object。\n"
            "- stdout 字段必须满足 runtime/output/artifact 合同。\n"
            "- 不得 placeholder / TODO / fake output。\n"
            "- 不得调用未注册 helper。\n"
            "- 可以使用本轮重新发现的候选工具。\n"
            "- 如果没有合适工具，使用本地确定性实现。\n"
            "- 不要为了绕过校验删除核心输入、核心输出或核心职责。\n"
        )

    return base + (
        "\n通用重写要求：\n"
        "- 输出完整目标文件内容。\n"
        "- 保持该文件原有职责。\n"
        "- 不要引入新的伪协议或伪执行步骤。\n"
    )


def _full_file_rewrite_context_for_e2e(
    *,
    skill_name: str,
    target_path: str,
    skill_md: str,
    current_content: str,
    previous_content: str,
    all_file_summaries: list[str],
    e2e_entry_context: Any,
    clean_tool_context: str,
) -> str:
    """Build a clean full-file rewrite context.

    Important:
    Do NOT include current E2E error text, structured failure objects,
    remaining_failed_checks, targeted repair hints, last_failure, or candidate
    rejection summaries. Format/location diagnostics can point to the wrong
    line and should not drive full-file rewrite.
    """
    return "\n".join([
        f"Skill 名称：{skill_name}",
        f"目标文件：{target_path}",
        "",
        "重写依据说明：",
        "- 下面上下文只包含职责、合同、当前内容、之前版本、上下游摘要和现有只读工具上下文。",
        "- 不包含当前失败文本、结构化失败对象、remaining_failed_checks、targeted repair hint 或上一轮候选失败摘要。",
        "- 请依据整体职责和工作流合同重写完整文件，不要围绕某个报错行小修。",
        "",
        "sandbox IO 前置协议：",
        _sandbox_io_contract_text_for_creator(),
        "",
        "目标文件职责 / SkillPlanEntry / runtime_contract / coverage_requirements / command argv contract：",
        json.dumps(e2e_entry_context or {}, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        "",
        "当前 SKILL.md：",
        skill_md[-12000:],
        "",
        "其它相关文件摘要：",
        "".join(all_file_summaries)[-20000:],
        "",
        "当前只读 Tool Registry / ToolPool 上下文：",
        clean_tool_context or "无可用已授权工具上下文；E2E repair 不得发现、请求或新增工具。",
        "",
        "当前目标文件内容：",
        current_content,
        "",
        "previous content / repair 前快照：",
        previous_content,
    ])




async def _request_full_file_rewrite_for_e2e(
    *,
    model: str,
    target_path: str,
    current_content: str,
    previous_content: str,
    rewrite_context: str,
    rewrite_target_rule: str,
) -> str:
    """Ask the model for a complete replacement file for repeated E2E failures.

    Full rewrite is responsibility/contract/previous-version driven, not
    current-failure driven. The caller must pass a clean rewrite_context that
    excludes raw E2E error text, structured failure, remaining_failed_checks,
    targeted repair hints, and prior candidate failure summaries.
    """
    messages = [
        {
            "role": "system",
            "content": (
                "你是 superskills Creator 的整文件重写代码模型。\n"
                "你只能输出目标文件的完整内容。\n"
                "不能输出解释、Markdown 包裹、diff、old_lines/new_lines 或 JSON patch。\n"
                "不要根据具体错误文本逐字修补；整文件重写必须基于职责、合同、当前内容、之前版本和上下游关系。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"目标文件：{target_path}\n\n"
                "本轮模式：full_file_rewrite。\n"
                "你正在重写目标文件，不是做局部 patch。\n"
                "请基于该文件职责、runtime_contract、coverage_requirements、上下游合同、当前内容、之前版本，重新生成完整目标文件。\n"
                "不要围绕某个报错行做小修。\n"
                "目标是让文件整体重新满足职责和工作流合同。\n\n"
                "完整重写规则：\n"
                f"{rewrite_target_rule}\n\n"
                "职责、合同与上下文：\n"
                f"{rewrite_context[-26000:]}\n\n"
                "再次强调：只输出完整目标文件内容；不要输出解释、diff、old_lines/new_lines、JSON patch 或 Markdown 包裹。"
            ),
        },
    ]

    text = await complete_chat_once(messages, model)
    text = str(text or "").strip()

    fence = re.match(r"^```[a-zA-Z0-9_-]*\s*\n(?P<body>.*)\n```\s*$", text, re.S)
    return fence.group("body").strip() if fence else text


def _checkpoint_dir(session: CreatorE2ESession) -> Path:
    path = session.workspace_dir / ".creator_e2e" / "checkpoints"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _checkpoint_path(session: CreatorE2ESession, step_index: int) -> Path:
    return _checkpoint_dir(session) / f"step_{step_index:03d}.json"


def _shape_for_hash(obj: dict[str, Any]) -> dict[str, str]:
    return _json_object_shape(obj if isinstance(obj, dict) else {})


def _write_step_checkpoint(
    session: CreatorE2ESession,
    *,
    command: E2EWorkflowCommand,
    rendered_payload: dict[str, Any],
    stdout_json: dict[str, Any],
    context_before: dict[str, Any],
    context_after: dict[str, Any],
    new_keys: list[str],
    artifact_paths: list[str],
    proc: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    checkpoint = {
        "e2e_session_id": session.e2e_session_id,
        "skill_name": session.skill_name,
        "command_plan_signature": session.command_plan_signature,
        "step_index": command.ordinal,
        "script_path": command.script_path,
        "argv_json": rendered_payload,
        "argv_shape": _shape_for_hash(rendered_payload),
        "stdout_json": stdout_json,
        "stdout_shape": _shape_for_hash(stdout_json),
        "context_before": context_before,
        "context_after": context_after,
        "new_keys": new_keys,
        "artifact_paths": artifact_paths,
        "file_outputs": artifact_paths,
        "exit_code": getattr(proc, "returncode", 0),
        "stderr_excerpt": str(getattr(proc, "stderr", "") or "")[-2000:],
        "stdout_excerpt": str(getattr(proc, "stdout", "") or "")[-2000:],
        "script_hash": _file_sha256(session.workspace_dir / command.script_path),
        "argv_hash": _stable_json_hash(rendered_payload),
        "context_hash": _stable_json_hash(context_before),
        "workspace_revision": session.current_revision,
        "passed": True,
    }
    _checkpoint_path(session, command.ordinal).write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return checkpoint


def _load_valid_checkpoint(session: CreatorE2ESession, step_index: int) -> dict[str, Any] | None:
    path = _checkpoint_path(session, step_index)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if data.get("e2e_session_id") != session.e2e_session_id:
        return None
    if data.get("skill_name") != session.skill_name:
        return None
    if data.get("command_plan_signature") != session.command_plan_signature:
        return None
    script_path = str(data.get("script_path") or "")
    if script_path and data.get("script_hash") != _file_sha256(session.workspace_dir / script_path):
        return None
    if not data.get("passed"):
        return None
    return data


def _invalidate_checkpoints_from(session: CreatorE2ESession, step_index: int) -> list[int]:
    invalidated: list[int] = []
    for path in _checkpoint_dir(session).glob("step_*.json"):
        match = re.search(r"step_(\d+)\.json$", path.name)
        if not match:
            continue
        index = int(match.group(1))
        if index >= step_index:
            invalidated.append(index)
            path.unlink(missing_ok=True)
    return sorted(invalidated)


def _earliest_invalid_step(
    *,
    changed_file: str,
    commands: list[E2EWorkflowCommand],
    old_command_plan_signature: str,
    new_command_plan_signature: str,
) -> int | None:
    changed_file = str(changed_file or "").replace("\\", "/").removeprefix("a/").removeprefix("b/")
    if changed_file == "SKILL.md":
        if old_command_plan_signature != new_command_plan_signature:
            return 1
        return None
    for command in commands:
        if str(command.script_path or "").replace("\\", "/").removeprefix("a/").removeprefix("b/") == changed_file:
            return command.ordinal
    if changed_file.startswith(("references/", "config/", "assets/")):
        return 1
    return None


def _failure_signature_from_error(error: str) -> str:
    structured = _structured_failure_from_errors([error])
    if structured:
        basis = {
            "failure_kind": structured.get("failure_kind") or structured.get("layer"),
            "target_file": structured.get("target_file"),
            "step_index": structured.get("failed_step_index"),
            "return_code": structured.get("return_code"),
            "stderr_hash": hashlib.sha256(str(structured.get("stderr") or "").encode("utf-8")).hexdigest()[:16],
            "layer": structured.get("layer"),
        }
    else:
        basis = {"error_hash": hashlib.sha256(str(error or "").encode("utf-8")).hexdigest()[:16]}
    return _stable_json_hash(basis)


def _e2e_repair_state_from_errors(errors: list[str], *, resolved_failures: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    failed_checks = [error for error in (errors or []) if str(error or "").strip()]
    resolved = list(resolved_failures or [])
    resolved_signatures = {item.get("failure_signature") for item in resolved}
    remaining = [error for error in failed_checks if _failure_signature_from_error(error) not in resolved_signatures]
    return {
        "full_e2e_passed": not remaining,
        "passed_checks": [] if remaining else ["workflow_e2e"],
        "failed_checks": failed_checks,
        "advisory_notes": [],
        "resolved_failures": resolved,
        "remaining_failed_checks": remaining,
        "current_target_file": _e2e_repair_target_from_errors(remaining) if remaining else "none",
        "current_resume_step": None,
    }

def _runner_matches_command_runtime(command: E2EWorkflowCommand, entry: SkillPlanEntry) -> bool:
    runner = Path(command.runner or "").name
    if entry.runtime == "python":
        return runner.startswith("python")
    if entry.runtime == "node":
        return runner == "node"
    if entry.runtime in {"bash", "shell"}:
        return runner in {"bash", "sh"}
    return True

def _run_e2e_step_argument_effect_review(
    *,
    command: E2EWorkflowCommand,
    script_content: str,
    skill_plan_entry: SkillPlanEntry,
    rendered_payload: dict[str, Any],
    stdout_json: dict[str, Any],
    artifact_paths: list[str],
    trace: E2EStepTrace,
    previous_traces: list[E2EStepTrace],
    requested_model: str | None = None,
    requirements: Any = None,
) -> dict[str, Any]:
    """Second-round E2E step interface + argument-effect review.

    第二轮只检查真实运行链路，不做第一轮责任审查：

    1. SKILL.md 当前 command 渲染后的 argv 是否包含当前 step 所需输入/控制参数；
    2. 当前脚本是否实际读取这些 argv 参数；
    3. stdout_json 是否能被后续步骤 placeholder 或最终平台输出使用；
    4. artifact_paths / 最终产物是否按链路生成；
    5. 如果失败，先最小归因为参数没传上、脚本没读到、上游没产出、或当前输出不符合链路需要。

    平台 IO 是来源/出口层，不是 script argv key 白名单；只审查真实运行链路，不判断推荐字段是否“正确”。
    """

    rendered_payload = rendered_payload if isinstance(rendered_payload, dict) else {}
    stdout_json = stdout_json if isinstance(stdout_json, dict) else {}
    artifact_paths = artifact_paths if isinstance(artifact_paths, list) else []

    declared_inputs = [
        str(item).strip()
        for item in (getattr(skill_plan_entry, "inputs", []) or [])
        if str(item).strip()
    ]
    declared_outputs = [
        str(item).strip()
        for item in (getattr(skill_plan_entry, "outputs", []) or [])
        if str(item).strip()
    ]

    placeholders = sorted(_e2e_command_placeholders(command))
    argv_template = command.argv_template if isinstance(command.argv_template, dict) else {}

    req_items = coerce_requirement_items(requirements) or coerce_requirement_items(getattr(skill_plan_entry, "requirements", []))
    artifact_contract = getattr(skill_plan_entry, "artifact_contract", {}) or {}
    substantive_step = bool(rendered_payload or argv_template or placeholders or declared_inputs or artifact_contract or any(getattr(r, "required", False) for r in req_items))
    # 只有完全没有 argv、没有 placeholder、没有声明输入、没有 required requirements/产物合同时，才跳过。
    if not substantive_step:
        return {
            "passed": True,
            "model": None,
            "advisory_notes": [
                "当前 step 没有声明输入、没有 argv_template、没有 placeholder，视为 no-input deterministic step，跳过接口审查。"
            ],
        }

    route = route_model(
        VALIDATOR_TASK,
        requested_model=requested_model,
        reason=f"creator e2e step interface/argument review: {command.script_path}",
    )

    local_block_payload = {
        "raw_command": command.raw_command,
        "argv_template": command.argv_template,
        "placeholders": placeholders,
    }

    messages = [
        {
            "role": "system",
            "content": (
                "你是 Creator 第二轮 E2E 当前 step 接口对齐审查模型，只输出严格 JSON object。\n\n"

                "你只审查真实运行链路：SKILL.md command argv template -> rendered_payload -> script strict_json_argv_guard -> script run/main 实际读取 -> stdout_json -> 后续 placeholder 或最终平台输出。\n"
                "SkillPlan inputs/outputs、RequirementGraph inputs/outputs、local_contract inputs/outputs、SKILL.md action schema inputs/outputs 都是共同推荐字段，不是 hard validation。不要因为推荐字段未使用、字段改名、字段不完全一致而失败。\n"
                "只有实际出现在 command/rendered_payload/guard/run/stdout/后续 placeholder 中的字段需要判断链路是否闭合。\n\n"

                "重要边界：\n"
                "- 不做第一轮脚本职责审查；脚本功能是否完整由 _run_script_responsibility_review 负责。\n"
                "- 不判断完整 SKILL.md 写得好不好。\n"
                "- 不判断最终产物审美质量。\n"
                "- 平台 IO 是来源/出口层，不是 script argv key 白名单；不要求所有 argv key 来自平台字段，不要求用上所有平台 fields。\n"
                "- literal/default/config/reference/assets/runtime constants 可以存在；第一条命令只要动态来源能从平台 envelope 解析即可，后续命令只要动态来源能从当前 payload 或前序 stdout 解析即可。\n"
                "- 最后一步 stdout 至少有一个平台 final output field 即可；不要因为 recommended 字段不一致而 target script。\n"
                "- E2E 阶段以已经生成的 script 为主要接口事实；SKILL.md command block 是 orchestration 描述。\n"
                "- 当 SKILL.md command argv 与 script 入口接口不一致时，优先修改 SKILL.md 当前 command JSON argv 去对齐 script。\n"
                "- 只有 script 自身语法错误、入口/JSON argv 读取错误、guard 与 run/main 实际读取不一致、没消费已传正确参数、stdout/artifact 输出错误时，才 target_file=当前脚本。\n"
                "- 上游 stdout key 和下游 placeholder 不一致，应失败，并优先 target_file=SKILL.md；通过默认值绕过真实传参，应失败。\n"
                "- 允许脚本通过 payload、input、fields、options、统一对象、别名字段或等价结构接收参数，但必须能证明实际传入的 key 被读取并影响输出。\n"
                "- 如果 rendered_payload 缺少当前 step 必需信息或 SKILL.md 传入 key 与自洽 script 接口不一致，failure_kind=missing_payload，target_file=SKILL.md。\n"
                "- 如果 SKILL.md 已传对但脚本没有读取、读取了不同 key、或被默认值覆盖，failure_kind=script_not_consuming_payload，target_file=当前脚本。\n"
                "- 如果当前 step 输出了内容，但后续 placeholder/字段映射接不上，failure_kind=output_mapping_mismatch，target_file=SKILL.md。\n"
                "- 如果没有接口问题，返回 passed=true。\n\n"

                "返回 JSON：\n"
                "{\n"
                "  \"passed\": true|false,\n"
                "  \"target_file\": \"SKILL.md 或 当前脚本路径\",\n"
                "  \"failure_kind\": \"missing_payload|script_not_consuming_payload|output_mapping_mismatch|none\",\n"
                "  \"problem\": \"具体问题\",\n"
                "  \"evidence\": \"从 command/rendered_payload/script/stdout/trace 中引用证据\",\n"
                "  \"repair_instruction\": \"只修改目标文件的最小区域\",\n"
                "  \"advisory_notes\": []\n"
                "}\n"
            ),
        },
        {
            "role": "user",
            "content": (
                f"当前 step：{command.ordinal}\n"
                f"当前脚本：{command.script_path}\n\n"

                "当前 SKILL.md workflow block 局部信息：\n"
                f"{json.dumps(local_block_payload, ensure_ascii=False, default=str)[:8000]}\n\n"

                "当前脚本 SkillPlanEntry：\n"
                f"{json.dumps(skill_plan_entry.__dict__, ensure_ascii=False, default=str)[:8000]}\n\n"

                "declared_inputs（语义参考，不是字段名硬约束）：\n"
                f"{json.dumps(declared_inputs, ensure_ascii=False, default=str)}\n\n"

                "declared_outputs（语义参考，不是字段名硬约束）：\n"
                f"{json.dumps(declared_outputs, ensure_ascii=False, default=str)}\n\n"

                "E2E rendered_payload：\n"
                f"{json.dumps(rendered_payload, ensure_ascii=False, default=str)[:10000]}\n\n"

                "当前 step stdout_json：\n"
                f"{json.dumps(stdout_json, ensure_ascii=False, default=str)[:10000]}\n\n"

                "当前 step artifact_paths：\n"
                f"{json.dumps(artifact_paths, ensure_ascii=False, default=str)}\n\n"

                "当前 step trace：\n"
                f"{_e2e_trace_line(trace)}\n\n"

                "前序成功 trace 摘要：\n"
                f"{_format_e2e_trace(previous_traces)[-5000:]}\n\n"

                "当前脚本源码（带行号）：\n"
                f"{_numbered_source(script_content)[-18000:]}\n\n"

                "审查要求：\n"
                "1. 先做最小归因：参数没传上、脚本没读到、上游没产出、还是当前输出不符合链路需要。\n"
                "2. 检查 SKILL.md 当前 command 渲染后的 argv 是否把当前 step 必要输入/控制参数传进 rendered_payload。\n"
                "3. 检查脚本是否真实读取并使用同一组传入 key；不能靠默认值绕过真实传参。\n"
                "4. 检查 stdout key 是否能被后续 placeholder 或最终输出使用。\n"
                "5. 不要求平台统一字段名，但已选择的 workflow 字段名必须严格对齐。\n"
                "6. 如果是 SKILL.md 没传对或上下游 placeholder 不一致，target_file=SKILL.md。\n"
                "7. 如果是脚本没接住传入 key 或使用默认值绕过，target_file=当前脚本。\n"
                "8. 如果没有真实链路问题，passed=true。\n"
            ),
        },
    ]

    last_text = ""
    data: dict[str, Any] | None = None
    for review_attempt in range(2):
        try:
            active_messages = messages if review_attempt == 0 else [
                *messages,
                {
                    "role": "user",
                    "content": (
                        "上一轮 E2E requirement validator 没有返回合法 JSON object。\n"
                        "请只重试 JSON 格式；不要要求修改 SKILL.md 或脚本。\n"
                        f"上一轮输出片段：{last_text[:1200]}"
                    ),
                },
            ]
            text = _complete_chat_once_sync_for_e2e(active_messages, route.model)
            last_text = str(text or "")
        except Exception as exc:
            if review_attempt == 0:
                last_text = f"validator unavailable: {type(exc).__name__}: {exc}"
                continue
            logger.warning(
                "[Creator][E2E][argument_effect_review_unavailable] step=%s script=%s error=%s",
                command.ordinal,
                command.script_path,
                exc,
            )
            return {
                "passed": False,
                "failure_type": "e2e_requirement_validator_error",
                "target_file": command.script_path if rendered_payload else "SKILL.md",
                "layer": "e2e_requirement_validator_error",
                "problem": f"E2E requirement validator unavailable: {type(exc).__name__}: {exc}",
                "evidence": "validator unavailable after format retry; not a business-file failure",
                "repair_instruction": "Retry validator or return recoverable validator failure; do not patch business files solely for this.",
                "model": route.model,
            }

        parsed = _parse_validator_json_object(last_text)
        if isinstance(parsed, dict) and parsed:
            data = parsed
            break
        if review_attempt == 0:
            continue
        logger.warning(
            "[Creator][E2E][argument_effect_review_invalid_json] step=%s script=%s raw=%s",
            command.ordinal,
            command.script_path,
            last_text[:1000],
        )
        return {
            "passed": False,
            "failure_type": "e2e_requirement_validator_error",
            "target_file": command.script_path if rendered_payload else "SKILL.md",
            "layer": "e2e_requirement_validator_error",
            "problem": "E2E requirement validator did not return valid JSON after format retry.",
            "evidence": last_text[:1000],
            "repair_instruction": "Retry validator or return recoverable validator failure; do not patch business files solely for this.",
            "model": route.model,
        }

    data = data or {}

    if data.get("passed") is True:
        return {
            "passed": True,
            "model": route.model,
            "advisory_notes": data.get("advisory_notes") if isinstance(data.get("advisory_notes"), list) else [],
        }

    failure_kind = str(data.get("failure_kind") or "").strip()
    if failure_kind not in {
        "missing_payload",
        "script_not_consuming_payload",
        "output_mapping_mismatch",
        "none",
    }:
        failure_kind = "script_not_consuming_payload"

    target_file = str(data.get("target_file") or "").strip()

    if failure_kind in {"missing_payload", "output_mapping_mismatch"}:
        target_file = "SKILL.md"
    elif failure_kind == "script_not_consuming_payload":
        target_file = command.script_path
    elif target_file not in {"SKILL.md", command.script_path}:
        target_file = command.script_path

    layer = (
        "e2e_step_argument_mapping"
        if target_file == "SKILL.md"
        else "e2e_step_argument_effect"
    )

    problem = str(
        data.get("problem")
        or data.get("reason")
        or data.get("message")
        or "当前 step 接口没有闭环，或 rendered_payload 没有真实影响当前脚本输出。"
    )

    evidence = str(
        data.get("evidence")
        or data.get("details")
        or "模型未提供 evidence。"
    )

    repair_instruction = str(
        data.get("repair_instruction")
        or data.get("minimal_edit")
        or data.get("fix")
        or (
            f"只修改 {target_file} 中当前 E2E step 接口映射/参数消费相关的最小区域，"
            "不要修改其它文件或已通过步骤。"
        )
    )

    return {
        "passed": False,
        "target_file": target_file,
        "layer": layer,
        "failure_kind": failure_kind,
        "problem": problem,
        "evidence": evidence,
        "repair_instruction": repair_instruction,
        "model": route.model,
        "raw_review": data,
    }

def _e2e_requirement_mapping_failure(**kwargs) -> str:
    return _e2e_argument_effect_failure(**kwargs)

def _e2e_requirement_effect_failure(**kwargs) -> str:
    return _e2e_argument_effect_failure(**kwargs)


def _load_requirement_graph_for_e2e(skill_dir: Path) -> RequirementGraph:
    path = skill_dir / ".creator" / "requirement_graph.json"
    if not path.is_file():
        return RequirementGraph()
    try:
        return normalize_requirement_graph(parse_requirement_graph_result(path.read_text(encoding="utf-8")))
    except RequirementGraphValidationError:
        raise
    except Exception as exc:
        raise RequirementGraphValidationError(
            f"E2E requirement graph metadata is unreadable: {type(exc).__name__}: {exc}",
            code="validator_error",
            details={"path": str(path)},
        ) from exc


def _attach_requirements_to_entry(entry: SkillPlanEntry, requirements: list[RequirementItem]) -> SkillPlanEntry:
    try:
        setattr(entry, "requirements", requirements)
    except Exception:
        pass
    return entry


def _constraint_matches_metadata(constraint: RequirementConstraint, metadata: dict[str, Any]) -> bool:
    name = str(constraint.name or "").strip()
    value = constraint.value
    kind = str(constraint.kind or "").strip()
    buckets = []
    for key in ("styles", "options", "constraint_values", "layout_options"):
        if isinstance(metadata.get(key), dict):
            buckets.append(metadata[key])
    for bucket in buckets:
        if name and name in bucket:
            if value in (None, ""):
                return True
            return bucket.get(name) == value or str(bucket.get(name)) == str(value)
        if value not in (None, "") and any(v == value or str(v) == str(value) for v in bucket.values()):
            return True
    if kind in {"media_property", "media"} and metadata.get("media_items"):
        return True
    if kind in {"layout_style", "layout"} and metadata.get("layout_options"):
        return True
    if kind == "quantity":
        for key in ("block_count",):
            if metadata.get(key) == value or str(metadata.get(key)) == str(value):
                return True
    return False


def _structured_requirement_metadata_missing(requirement: RequirementItem, metadata: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    policy = requirement.evidence_policy if isinstance(requirement.evidence_policy, dict) else {}
    metadata_paths = policy.get("metadata_paths") if isinstance(policy.get("metadata_paths"), list) else []
    for dotted in metadata_paths:
        cursor: Any = metadata
        for part in str(dotted).split("."):
            if isinstance(cursor, dict) and part in cursor:
                cursor = cursor[part]
            else:
                missing.append(f"metadata.{dotted}")
                break
    component_types = metadata.get("component_types") if isinstance(metadata.get("component_types"), list) else []
    policy_components = policy.get("component_types") if isinstance(policy.get("component_types"), list) else []
    for component in policy_components:
        if component not in component_types:
            missing.append(f"component_types contains {component}")
    for constraint in requirement.constraints or []:
        if (
            getattr(constraint, "required", True)
            and str(getattr(constraint, "source", "") or "") in {"user_explicit", "blueprint", "inferred"}
            and not _constraint_matches_metadata(constraint, metadata)
        ):
            missing.append(f"constraint:{constraint.name or constraint.kind}")
    return missing

def _run_e2e_requirement_flow_review(
    *,
    command: E2EWorkflowCommand,
    script_content: str,
    skill_plan_entry: SkillPlanEntry,
    rendered_payload: dict[str, Any],
    stdout_json: dict[str, Any],
    artifact_paths: list[str],
    trace: E2EStepTrace,
    previous_traces: list[E2EStepTrace],
    requested_model: str | None = None,
    requirements: Any = None,
) -> dict[str, Any]:
    reqs = coerce_requirement_items(requirements) or coerce_requirement_items(getattr(skill_plan_entry, "requirements", []))
    review = _run_e2e_step_argument_effect_review(
        command=command, script_content=script_content, skill_plan_entry=skill_plan_entry,
        rendered_payload=rendered_payload, stdout_json=stdout_json, artifact_paths=artifact_paths,
        trace=trace, previous_traces=previous_traces, requested_model=requested_model, requirements=reqs,
    )
    if not review.get("passed"):
        return review
    required = [r for r in reqs if getattr(r, "required", False)]
    metadata = stdout_json.get("artifact_metadata") if isinstance(stdout_json.get("artifact_metadata"), dict) else {}
    if required and not rendered_payload and any(r.semantic_inputs for r in required):
        r = next((x for x in required if x.semantic_inputs), required[0])
        return {"passed": False, "target_file": "SKILL.md", "layer": "e2e_requirement_mapping_failed", "failure_kind": "missing_payload", "problem": "Required semantic input was not delivered to the step payload.", "evidence": "rendered_payload is empty while requirement declares semantic_inputs", "requirement_id": r.id, "missing_evidence": ["semantic input in rendered_payload"], "repair_instruction": "Pass the required semantic input from user input or previous stdout into this command."}
    if required and artifact_paths and metadata:
        for r in required:
            missing = _structured_requirement_metadata_missing(r, metadata)
            if missing:
                return {"passed": False, "target_file": command.script_path, "layer": "runtime_metadata_requirement_failed", "failure_kind": "runtime_metadata_missing_evidence", "problem": "Runtime metadata is missing structured evidence for a required requirement.", "evidence": json.dumps(metadata, ensure_ascii=False, default=str)[:1000], "requirement_id": r.id, "missing_evidence": missing, "repair_instruction": "Ensure stdout or runtime helper metadata exposes structured evidence for the required component/constraint."}
    return review

def _e2e_argument_effect_failure(
    *,
    command: E2EWorkflowCommand,
    review: dict[str, Any],
    rendered_payload: dict[str, Any],
    stdout_json: dict[str, Any],
    artifact_paths: list[str],
    traces: list[E2EStepTrace],
) -> str:
    layer = str(review.get("layer") or "e2e_step_argument_effect")
    target_file = "__validator__" if layer in {"e2e_requirement_validator_error", "e2e_requirement_validator_incomplete"} else str(review.get("target_file") or command.script_path)
    problem = str(review.get("problem") or "当前 E2E step 参数有效性审查失败。")
    evidence = str(review.get("evidence") or "")
    repair_instruction = str(
        review.get("repair_instruction")
        or f"只修改 {target_file} 中与当前 step 实际字段传输、参数消费或上下游连接相关的最小区域。"
    )

    return _format_e2e_failure(E2EFailure(
        failed_step_index=command.ordinal,
        target_file=target_file,
        target_region="workflow block" if target_file == "SKILL.md" else "run()",
        failed_command=command.raw_command,
        input_payload=rendered_payload,
        rendered_payload=rendered_payload,
        stdout=json.dumps(stdout_json, ensure_ascii=False, default=str)[-4000:],
        stderr=(
            f"{problem}\n\n"
            f"evidence:\n{evidence}\n\n"
            "已成功执行的前序边界 trace：\n"
            f"{_format_e2e_trace(traces)}"
        ),
        return_code=0,
        expected=(
            "最终 SKILL.md 当前 workflow block 传入的核心业务参数，"
            "必须被当前脚本按同一字段链路真实消费，并影响当前 step 的 stdout_json、后续 placeholder 或 artifact。"
        ),
        actual=problem,
        repair_instruction=repair_instruction,
        layer=layer,
        artifact_paths=artifact_paths,
    ))

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


def _extract_failed_argv_keys(text: str) -> list[str]:
    keys: set[str] = set()
    for bracketed in re.findall(r"\[([^\]]+)\]", text):
        keys.update(key for key in re.findall(r"['\"]([^'\"]+)['\"]", bracketed) if key)
    for pattern in (
        r"(?:unknown|unexpected|extra)(?:\s+argv)?\s+keys?\s*[:=]\s*([A-Za-z_][\w.-]*)",
        r"missing(?:\s+required)?(?:\s+argv)?\s+keys?\s*[:=]\s*([A-Za-z_][\w.-]*)",
        r"empty(?:\s+required)?(?:\s+argv)?\s+(?:value|key)\s*[:=]\s*([A-Za-z_][\w.-]*)",
        r"invalid(?:\s+argv)?\s+type(?:\s+for)?\s*[:=]\s*([A-Za-z_][\w.-]*)",
    ):
        keys.update(str(match) for match in re.findall(pattern, text, flags=re.I))
    return sorted(keys)


def _argv_schema_error_kind(stderr: str, stdout: str) -> str | None:
    text = f"{stderr}\n{stdout}".lower()
    if "argv json must be an object" in text or "json argv must be an object" in text:
        return "non_object_argv"
    if "missing json argv" in text:
        return "missing_json_argv"
    if "unknown key" in text or "unknown argv" in text or "unexpected key" in text or "extra key" in text:
        return "unknown_key"
    if "missing required" in text or "missing key" in text:
        return "missing_required"
    if "empty required" in text or "empty argv" in text:
        return "empty_required"
    if "invalid type" in text or "argv type" in text:
        return "invalid_type"
    return None


def _argv_value_shape(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        return "empty_string" if value == "" else "string"
    if isinstance(value, list):
        return "empty_list" if not value else "list"
    if isinstance(value, dict):
        return "empty_object" if not value else "object"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__




def _python_run_main_read_keys(content: str) -> set[str]:
    """Compatibility wrapper returning required run(args) reads only."""
    return set(_python_run_args_analysis(content).get("required_read_keys") or [])

def _classify_argv_schema_failure(
    *,
    command: E2EWorkflowCommand,
    content: str,
    entry: SkillPlanEntry,
    rendered_payload: dict[str, Any],
    stdout: str,
    stderr: str,
) -> dict[str, Any]:
    kind = _argv_schema_error_kind(stderr, stdout)
    if not kind:
        return {}
    schema = extract_python_strict_argv_schema(content) if entry.runtime == "python" else {"allowed_keys": None, "required_keys": None, "expected_types": {}}
    allowed = schema.get("allowed_keys")
    required = schema.get("required_keys")
    optional = schema.get("optional_keys")
    defaulted = schema.get("defaulted_keys")
    expected_types = schema.get("expected_types") or {}
    has_guard = "strict_json_argv_guard" in (content or "") or allowed is not None or required is not None
    failed_keys = _extract_failed_argv_keys(f"{stderr}\n{stdout}")
    received_keys = sorted(str(key) for key in (rendered_payload or {}).keys())
    command_argv_keys = sorted(str(key) for key in (command.argv_template or {}).keys())
    semantic_inputs = sorted(str(key) for key in (getattr(entry, "inputs", []) or []) if str(key or "").strip())
    run_analysis = _python_run_args_analysis(content) if entry.runtime == "python" else {"required_read_keys": [], "optional_read_keys": [], "reads_sys_argv": False}
    run_required_keys = sorted(str(key) for key in (run_analysis.get("required_read_keys") or []))
    run_optional_keys = sorted(str(key) for key in (run_analysis.get("optional_read_keys") or []))
    run_read_keys = sorted(set(run_required_keys) | set(run_optional_keys))
    run_reads_sys_argv = bool(run_analysis.get("reads_sys_argv"))
    allowed_set = set(str(key) for key in (allowed or []) if str(key or "").strip()) if allowed is not None else set()
    required_set = set(str(key) for key in (required or []) if str(key or "").strip())
    run_required_set = set(run_required_keys)
    run_reads_guard_undeclared = bool(allowed is not None and run_required_set - allowed_set)
    guard_required_unconsumed = False
    guard_run_mismatch = run_reads_guard_undeclared or run_reads_sys_argv or not has_guard

    script_reasons: list[str] = []
    if not has_guard:
        script_reasons.append("script has no strict_json_argv_guard call")
    if run_reads_sys_argv:
        script_reasons.append("run(args) reads sys.argv/json argv internally")
    if run_reads_guard_undeclared:
        script_reasons.append("run(args) reads keys that strict_json_argv_guard did not declare")

    default_skill_md_kinds = {"unknown_key", "missing_required", "invalid_type", "empty_required", "non_object_argv", "missing_json_argv"}
    primary_target = "SKILL.md" if kind in default_skill_md_kinds else "SKILL.md"
    target_reason = "SKILL.md command JSON argv does not match the current script entry strict_json_argv_guard spec."
    if kind in {"non_object_argv", "missing_json_argv"}:
        target_reason = "SKILL.md command did not provide exactly one JSON object argv."
    elif kind == "unknown_key":
        target_reason = "SKILL.md command passed argv keys outside the script strict_json_argv_guard spec."
    elif kind == "missing_required":
        target_reason = "SKILL.md command omitted required keys declared by the script strict_json_argv_guard spec."
    elif kind == "invalid_type":
        target_reason = "SKILL.md command rendered argv values whose JSON types do not match the script strict_json_argv_guard spec."
    elif kind == "empty_required":
        target_reason = "SKILL.md command rendered an empty value for a key required by the script strict_json_argv_guard spec."

    if script_reasons:
        primary_target = command.script_path
        target_reason = "; ".join(script_reasons) + "."

    return {
        "argv_schema_error_kind": kind,
        "received_keys": received_keys,
        "allowed_keys": allowed,
        "required_keys": required,
        "optional_keys": optional,
        "defaulted_keys": defaulted,
        "expected_types": expected_types,
        "command_argv_keys": command_argv_keys,
        "skill_plan_inputs": semantic_inputs,
        "advisory_notes": ["Field differences are diagnostics only; strict_json_argv_guard(payload, spec) is the script entry interface fact."],
        "script_run_read_keys": run_read_keys,
        "script_run_required_read_keys": run_required_keys,
        "script_run_optional_read_keys": run_optional_keys,
        "script_run_reads_sys_argv": run_reads_sys_argv,
        "script_has_strict_json_argv_guard": has_guard,
        "script_guard_run_mismatch": guard_run_mismatch,
        "failed_keys": failed_keys,
        "primary_target": primary_target,
        "candidate_targets": [primary_target],
        "target_reason": target_reason,
        "rendered_payload_shape": {str(key): _argv_value_shape(value) for key, value in (rendered_payload or {}).items()},
        "previous_trace_summary": [],
    }


def _argv_schema_repair_instruction(script_path: str, details: dict[str, Any]) -> str:
    primary = str(details.get("primary_target") or "")
    target_reason = str(details.get("target_reason") or "")
    diagnostics = {
        "argv_schema_error_kind": details.get("argv_schema_error_kind"),
        "failed_keys": details.get("failed_keys"),
        "received_keys": details.get("received_keys"),
        "allowed_keys": details.get("allowed_keys"),
        "required_keys": details.get("required_keys"),
        "optional_keys": details.get("optional_keys"),
        "expected_types": details.get("expected_types"),
        "command_argv_keys": details.get("command_argv_keys"),
        "script_run_required_read_keys": details.get("script_run_required_read_keys"),
        "script_run_optional_read_keys": details.get("script_run_optional_read_keys"),
        "script_run_reads_sys_argv": details.get("script_run_reads_sys_argv"),
        "script_has_strict_json_argv_guard": details.get("script_has_strict_json_argv_guard"),
    }
    common = (
        f"argv_schema_error 归因：{target_reason}\n"
        "strict_json_argv_guard(payload, spec) 是当前脚本入口接口事实；"
        "command_argv_keys/script_required_keys 仅作 diagnostics，不作为主提示或新合同。\n"
        f"diagnostics={json.dumps(diagnostics, ensure_ascii=False, sort_keys=True, default=str)}\n"
        "不得新增独立 canonical argv contract；不得因为 SKILL.md block 写错字段而让 script guard 迁就 block；禁止只改 guard schema；不要只修 guard。"
    )
    if primary == "SKILL.md":
        return common + "\nprimary_target=SKILL.md：只修当前失败 command block 的 JSON argv；让 block argv keys 与脚本 strict_json_argv_guard spec 逐字对齐；移除 guard 不接受的 unknown keys，补齐 guard required keys，并修正类型/空值；不得改脚本，不得改 guard，不得改 parse_args/run/main/stdout，不得重写整篇 SKILL.md，不得改其它已通过 command。"
    if primary == script_path:
        return common + f"\nprimary_target={script_path}：只修当前脚本中与失败相关的 parse_args/strict_json_argv_guard/run/main/stdout；确保 guard、run(args)、main() 自洽；run(args) 不得读取 guard 未声明 key，不得重新读取 sys.argv/json argv，guard required key 必须被 run(args) 消费；不得改 SKILL.md。"
    return common + "\nprimary_target 不确定：停止扩大修改；先依据 strict_json_argv_guard spec 与 run(args) AST diagnostics 判断目标，默认只修当前失败 SKILL.md command block。"


def _parse_e2e_stdout_json(
    *,
    command: E2EWorkflowCommand,
    proc: subprocess.CompletedProcess[str],
    trial_skill_dir: Path,
    content: str,
    entry: SkillPlanEntry,
    rendered_payload: dict[str, Any],
    trial_skill_md: str | None = None,
) -> dict[str, Any]:
    """Parse one real E2E subprocess result.

    Second-round E2E only validates actual process execution, argv failures,
    stdout JSON/object shape, declared file outputs, and real workflow closure.

    SkillPlan/RequirementGraph/canonical stdout requirements are first-round
    planning or responsibility facts and must not be re-applied as hard stdout
    field contracts here.
    """
    _ = trial_skill_md

    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "")[-4000:]
        stdout_tail = (proc.stdout or "")[-4000:]

        argv_details = _classify_argv_schema_failure(
            command=command,
            content=content,
            entry=entry,
            rendered_payload=rendered_payload,
            stdout=stdout_tail,
            stderr=stderr_tail,
        )
        is_argv_schema_error = bool(argv_details)

        failure_layer = (
            "argv_schema_error"
            if is_argv_schema_error
            else "script_exit"
        )

        target_file = (
            str(
                argv_details.get("primary_target")
                or command.script_path
            )
            if is_argv_schema_error
            else command.script_path
        )

        target_reason = str(
            argv_details.get("target_reason")
            or ""
        )

        if is_argv_schema_error:
            repair_instruction = _argv_schema_repair_instruction(
                command.script_path,
                argv_details,
            )
            target_region = "command JSON argv"
            expected = (
                "真实 command argv 必须通过当前脚本 strict_json_argv_guard，"
                "并保持 strict_json_argv_guard 与 run(args) 入口接口自洽。"
            )
        else:
            repair_instruction = (
                f"根据 {command.script_path} 本次真实 subprocess "
                "stderr traceback、return_code 和实际报错源码行进行最小修复。"
                "只修改异常直接涉及的代码。"
                "ImportError/ModuleNotFoundError 只修 traceback 直接相关 import；"
                "其它运行异常只修改 traceback 直接涉及的执行区域。"
                "不要检查 ToolPool、allowed_helper_imports、tool binding、"
                "required_capabilities、coverage_requirements 或工具权限。"
                "不要重新判断脚本职责。"
                "不要修改其它文件或已通过步骤。"
            )
            target_region = "runtime traceback"
            expected = (
                "脚本必须在当前真实 E2E runtime 中成功退出，"
                "并向 stdout 输出可解析的 JSON object。"
            )

        raise ValueError(
            _format_e2e_failure(
                E2EFailure(
                    failed_step_index=command.ordinal,
                    target_file=target_file,
                    target_region=target_region,
                    failed_command=command.raw_command,
                    input_payload=rendered_payload,
                    rendered_payload=rendered_payload,
                    stdout=stdout_tail,
                    stderr=stderr_tail,
                    return_code=proc.returncode,
                    expected=expected,
                    actual=(
                        f"return_code={proc.returncode}"
                        + (
                            f"; target_reason={target_reason}"
                            if target_reason
                            else ""
                        )
                    ),
                    repair_instruction=repair_instruction,
                    layer=failure_layer,
                    details=(
                        argv_details
                        if is_argv_schema_error
                        else {}
                    ),
                )
            )
        )

    try:
        _validate_trial_stdout_json(
            stdout=proc.stdout,
            content=content,
            args=[
                json.dumps(
                    rendered_payload,
                    ensure_ascii=False,
                )
            ],
            role=entry.role,
            skill_dir=trial_skill_dir,
            skill_plan_entry=None,
            canonical_contract=None,
        )
    except ValueError as exc:
        raise ValueError(
            _format_e2e_failure(
                E2EFailure(
                    failed_step_index=command.ordinal,
                    target_file=command.script_path,
                    target_region="stdout output logic",
                    failed_command=command.raw_command,
                    input_payload=rendered_payload,
                    rendered_payload=rendered_payload,
                    stdout=(proc.stdout or "")[-4000:],
                    stderr=(proc.stderr or "")[-4000:],
                    return_code=proc.returncode,
                    expected=(
                        "stdout 必须是非空 JSON object，不得包含 error 字段；"
                        "stdout 中实际声明的文件产物路径必须指向真实文件。"
                    ),
                    actual=f"stdout_runtime_error={exc}",
                    repair_instruction=(
                        f"根据当前真实 stdout 校验错误，只修改 "
                        f"{command.script_path} 的 stdout 或实际文件产物返回逻辑。"
                        "不要依据 SkillPlan outputs、canonical output contract、"
                        "RequirementGraph、required_capabilities、ToolPool "
                        "或职责规划重新设计脚本。"
                    ),
                    layer="stdout_contract",
                )
            )
        ) from exc

    try:
        parsed = json.loads(
            (proc.stdout or "").strip()
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            _e2e_error(
                target=command.script_path,
                layer="stdout_json_parse",
                message=(
                    f"第 {command.ordinal} 步 "
                    f"{command.script_path} stdout 不是合法 JSON。\n"
                    f"stdout={(proc.stdout or '')[-4000:]}"
                ),
            )
        ) from exc

    if not isinstance(parsed, dict):
        raise ValueError(
            _e2e_error(
                target=command.script_path,
                layer="stdout_json_type",
                message=(
                    f"第 {command.ordinal} 步 "
                    f"{command.script_path} stdout 必须是 JSON object。"
                ),
            )
        )

    return parsed


def _validate_e2e_script_static_preflight(
    *,
    file_path: str,
    content: str,
    skill_md: str,
) -> None:
    """E2E static preflight for executable workflow boundaries only.

    Second-round E2E validates source syntax, runtime entry shape, JSON argv
    ingestion, and the mandatory argv guard protocol before real execution.

    Tool selection, helper preference, capability fulfillment, and business
    responsibility were handled by first-round Creator validation.
    """
    entry = _skill_plan_entry_for_file(
        file_path=file_path,
        blueprint_text=skill_md,
    )

    if entry.language == "python":
        try:
            ast.parse(content)
        except SyntaxError as exc:
            raise ValueError(
                f"{file_path} 不是合法 Python 源码: {exc.msg}"
            ) from exc

    if not _script_has_main_entry(
        content,
        entry.runtime,
    ):
        raise ValueError(
            f"{file_path} 缺少 runtime={entry.runtime} "
            "的入口或 stdout 输出。"
        )

    commands = _extract_script_command_templates(
        skill_md,
        file_path,
    )
    json_argv_commands = [
        command
        for command in commands
        if _command_uses_json_argv(command)
    ]

    if (
        json_argv_commands
        and not _script_reads_json_argv(
            content,
            entry.runtime,
        )
    ):
        raise ValueError(
            f"{file_path} SKILL.md 命令传入 JSON argv，"
            "但脚本未按 runtime 读取 JSON argv。"
        )

    if entry.runtime == "python" and json_argv_commands:
        try:
            from backend.services.runtime_tools import (
                strict_json_argv_guard as _strict_json_argv_guard,
            )
            _ = _strict_json_argv_guard
        except Exception as exc:
            raise ValueError(
                "mandatory_guard_import_error: "
                "runtime_tools 无法导入 strict_json_argv_guard；"
                "当前 E2E 环境无法执行标准 JSON argv 入口。"
            ) from exc

        try:
            tree = ast.parse(content)
        except SyntaxError as exc:
            raise ValueError(
                f"{file_path} 不是合法 Python 源码: {exc.msg}"
            ) from exc

        imports_guard = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "backend.services.runtime_tools"
            and any(alias.name == "strict_json_argv_guard" for alias in node.names)
            for node in ast.walk(tree)
        )
        calls_guard = any(
            isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "strict_json_argv_guard")
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "strict_json_argv_guard")
            )
            for node in ast.walk(tree)
        )
        if not imports_guard:
            raise ValueError(
                f"{file_path} 缺少 strict_json_argv_guard import。"
            )
        if not calls_guard:
            raise ValueError(
                f"{file_path} 缺少 strict_json_argv_guard call。"
            )
        schema = extract_python_strict_argv_schema(content)
        if schema.get("placeholder_reasons"):
            raise ValueError(
                f"{file_path} strict_json_argv_guard spec 结构不可解析: "
                + "; ".join(str(x) for x in schema.get("placeholder_reasons") or [])
            )

def _validate_e2e_command_static(
    *,
    command: E2EWorkflowCommand,
    trial_skill_dir: Path,
    skill_md: str,
    available_payload_keys: set[str] | None = None,
) -> SkillPlanEntry:
    """Validate only static prerequisites required to launch one E2E step.

    This preflight intentionally does not compare SKILL.md argv field names with
    strict_json_argv_guard keys. The real subprocess must run first; actual guard
    failures are then classified by _classify_argv_schema_failure.
    """
    _ = available_payload_keys

    source_path = (
        trial_skill_dir
        / command.script_path
    )

    if not source_path.is_file():
        raise ValueError(
            _e2e_error(
                target=command.source_path,
                layer="script_missing",
                message=(
                    f"第 {command.ordinal} 步"
                    f"引用的脚本不存在："
                    f"{command.script_path}"
                ),
            )
        )

    entry = _skill_plan_entry_for_file(
        file_path=command.script_path,
        blueprint_text=skill_md,
    )

    if not _runner_matches_command_runtime(
        command,
        entry,
    ):
        raise ValueError(
            _e2e_error(
                target=command.source_path,
                layer="runtime_mismatch",
                message=(
                    f"第 {command.ordinal} 步 "
                    f"{command.script_path} 的命令 "
                    f"runner={command.runner!r} "
                    f"与 SkillPlan.runtime={entry.runtime!r} "
                    "不一致。\n"
                    f"原始命令：{command.raw_command}"
                ),
            )
        )

    content = source_path.read_text(
        encoding="utf-8"
    )

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
                message=(
                    f"第 {command.ordinal} 步 "
                    f"{command.script_path} "
                    f"无法进入真实 E2E 执行：{exc}"
                ),
            )
        ) from exc

    return entry


def _run_skill_workflow_e2e_once(
    skill_name: str,
    *,
    external_context: dict[str, Any] | None = None,
    source_skill_dir: Path | None = None,
    requested_model: str | None = None,
    e2e_session: CreatorE2ESession | None = None,
    resume_from_step: int = 1,
) -> list[str]:
    """Run SKILL.md workflow once.

    第二轮 E2E 职责：
    - 执行最终 SKILL.md 中的 workflow block；
    - 检查上下游 JSON 字段是否串起来；
    - 检查 stdout/artifact/final platform output；
    - 局部检查当前 SKILL.md block 传入参数是否真实影响当前 step 输出。

    不负责：
    - 判断脚本整体业务质量；
    - 判断完整 SKILL.md 写得好不好；
    - 判断 PDF/图片/文本审美质量；
    - 判断其它脚本职责。
    """

    if e2e_session is not None:
        source_skill_dir = e2e_session.workspace_dir.resolve()
    else:
        source_skill_dir = (source_skill_dir or (settings.skills_path / skill_name)).resolve()
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
    normalization = _normalize_skill_md_runtime_commands_for_e2e(
        skill_name=skill_name,
        skill_dir=source_skill_dir,
        skill_md=skill_md,
    )
    if normalization.blocked:
        return [
            _e2e_error(
                target="SKILL.md",
                layer="runtime_command_invalid",
                message=json.dumps(_command_normalizer_blocked_payload(target_file="SKILL.md", issues=normalization.issues), ensure_ascii=False, default=str),
            )
        ]
    if normalization.changed:
        skill_md_path.write_text(normalization.content, encoding="utf-8")
        skill_md = normalization.content

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
        if e2e_session is None:
            tmp_handle, trial_skill_dir = _copy_skill_dir_for_e2e(
                skill_name,
                source_skill_dir=source_skill_dir,
            )
        else:
            trial_skill_dir = e2e_session.workspace_dir
        trial_skill_md = (trial_skill_dir / "SKILL.md").read_text(encoding="utf-8")
        requirement_graph = _load_requirement_graph_for_e2e(trial_skill_dir)
        requirements_by_file: dict[str, list[RequirementItem]] = {}
        for req in requirement_graph.requirements:
            requirements_by_file.setdefault(req.target_file, []).append(req)
        skill_plan_entries: dict[str, SkillPlanEntry] = {}
        for command in commands:
            try:
                skill_plan_entries[command.script_path] = _skill_plan_entry_for_file(file_path=command.script_path, blueprint_text=trial_skill_md)
            except Exception:
                pass

        payload: dict[str, Any] = _seed_initial_e2e_payload(
            commands,
            external_context=external_context,
            skill_dir=trial_skill_dir,
            requirements_by_file=requirements_by_file,
            skill_plan_entries=skill_plan_entries,
        )
        typed_input_specs = _collect_e2e_typed_inputs_from_graph(
            commands=commands,
            requirements_by_file=requirements_by_file,
            skill_plan_entries=skill_plan_entries,
            skill_dir=trial_skill_dir,
        )
        traces: list[E2EStepTrace] = []

        venv_python: Path | None = None

        command_signature = _command_plan_signature(commands)
        deps_signature = _deps_signature_for_commands(trial_skill_dir, trial_skill_md, commands)
        if e2e_session is not None:
            e2e_session.command_plan_signature = command_signature
            e2e_session.deps_signature = deps_signature

        if any(command.script_path.endswith(".py") for command in commands):
            try:
                venv_python = _get_skill_venv_python(trial_skill_dir)

                should_install_deps = e2e_session is None or e2e_session.installed_deps_signature != deps_signature
                if should_install_deps:
                    for command in commands:
                        if not command.script_path.endswith(".py"):
                            continue

                        entry = _attach_requirements_to_entry(_skill_plan_entry_for_file(
                            file_path=command.script_path,
                            blueprint_text=trial_skill_md,
                        ), requirements_by_file.get(command.script_path, []))

                        _install_capability_dependencies(
                            venv_python,
                            entry.required_capabilities,
                        )

                        refined_contract, resolution = _contract_resolution_for_trial(
                            command.script_path,
                            trial_skill_md,
                            None,
                            None,
                        )

                        _install_declared_dependency_packages(
                            venv_python,
                            list(refined_contract.declared_dependencies or [])
                            + list(resolution.declared_dependencies or []),
                            source_label="implementation_resolution",
                        )
                    if e2e_session is not None:
                        e2e_session.installed_deps_signature = deps_signature
                        e2e_session.events.append({**e2e_session.to_event_base(), "event": "dependencies_prepared", "reused_venv": False})
                elif e2e_session is not None:
                    e2e_session.events.append({**e2e_session.to_event_base(), "event": "dependencies_reused", "reused_venv": True})

            except RuntimeError as exc:
                return [
                    _e2e_error(
                        target="scripts",
                        layer="venv_prepare",
                        message=f"端到端试运行环境准备失败：{exc}",
                    )
                ]

        reused_checkpoints: list[int] = []
        if e2e_session is not None and resume_from_step > 1:
            for prior_step in range(1, resume_from_step):
                checkpoint = _load_valid_checkpoint(e2e_session, prior_step)
                if not checkpoint:
                    resume_from_step = prior_step
                    break
                payload = dict(checkpoint.get("context_after") or payload)
                reused_checkpoints.append(prior_step)
                traces.append(E2EStepTrace(
                    ordinal=int(checkpoint.get("step_index") or prior_step),
                    script_path=str(checkpoint.get("script_path") or ""),
                    raw_command="[checkpoint]",
                    placeholders=[],
                    argv_keys=sorted(str(key) for key in (checkpoint.get("argv_json") or {}).keys()),
                    stdout_keys=sorted(str(key) for key in (checkpoint.get("stdout_json") or {}).keys()),
                    new_keys=list(checkpoint.get("new_keys") or []),
                    artifact_paths=list(checkpoint.get("artifact_paths") or []),
                    argv_shape=dict(checkpoint.get("argv_shape") or {}),
                    stdout_shape=dict(checkpoint.get("stdout_shape") or {}),
                ))
            if e2e_session is not None:
                e2e_session.events.append({**e2e_session.to_event_base(), "event": "checkpoints_reused", "resume_from_step": resume_from_step, "reused_checkpoints": reused_checkpoints})

        for index, command in enumerate(commands):
            if command.ordinal < resume_from_step:
                continue
            try:
                entry = _attach_requirements_to_entry(_validate_e2e_command_static(
                    command=command,
                    trial_skill_dir=trial_skill_dir,
                    skill_md=trial_skill_md,
                    available_payload_keys=set(payload.keys()),
                ), requirements_by_file.get(command.script_path, []))

                content = (trial_skill_dir / command.script_path).read_text(encoding="utf-8")

                rendered_payload = _render_e2e_command_payload(
                    command,
                    payload=payload,
                    traces=traces,
                    typed_input_specs=typed_input_specs,
                )

                rendered_payload, runtime_literal_events = _materialize_rendered_e2e_payload_runtime_literals(
                    rendered_payload,
                    skill_dir=trial_skill_dir,
                    target_file=command.script_path,
                    skill_md=trial_skill_md,
                    script_content=content,
                )

                if runtime_literal_events:
                    logger.info("[Creator][E2E][runtime_input_literal_materialized] %s", json.dumps({
                        "event": "runtime_input_literal_materialized",
                        "script_path": command.script_path,
                        "ordinal": command.ordinal,
                        "events": runtime_literal_events,
                    }, ensure_ascii=False, default=str))

                    if e2e_session is not None:
                        e2e_session.events.append({
                            **e2e_session.to_event_base(),
                            "event": "runtime_input_literal_materialized",
                            "phase": "e2e_run",
                            "status": "materialized",
                            "current_step": command.ordinal,
                            "total_steps": len(commands),
                            "target_file": command.script_path,
                            "runtime_literal_events": runtime_literal_events,
                            "rendered_payload_summary": json.dumps(
                                _json_object_shape(rendered_payload),
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                        })

                if e2e_session is not None:
                    e2e_session.events.append({
                        **e2e_session.to_event_base(),
                        "event": "step_started",
                        "phase": "e2e_run",
                        "status": "running",
                        "current_step": command.ordinal,
                        "total_steps": len(commands),
                        "target_file": command.script_path,
                        "rendered_payload_summary": json.dumps(_json_object_shape(rendered_payload), ensure_ascii=False, sort_keys=True),
                        "trace_summary": _format_e2e_trace(traces)[-2000:],
                    })

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

                artifact_paths = _stdout_artifact_paths(stdout_json, entry, None)

                before_keys = set(payload.keys())
                new_keys = sorted(set(stdout_json.keys()) - before_keys)

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

                # Strict E2E is deterministic: once the command renders, the script
                # exits successfully, stdout is a valid JSON object that satisfies
                # the declared stdout/artifact contract, and the final platform
                # output is consumable, the workflow is accepted.  The legacy
                # requirement/argument-effect LLM review is intentionally not run
                # here because validator availability or semantic judgement must
                # not block packaging or trigger business-file repair.

                is_final_step = index == len(commands) - 1
                if is_final_step:
                    _validate_final_platform_output_contract(
                        command=command,
                        stdout_json=stdout_json,
                        traces=traces,
                    )

                context_before = dict(payload)
                logger.info("[Creator][E2E][stdout_key_sources] %s", json.dumps({
                    "event": "e2e_stdout_key_sources",
                    "script_path": command.script_path,
                    "ordinal": command.ordinal,
                    "stdout_keys": sorted(str(key) for key in stdout_json.keys()),
                }, ensure_ascii=False, default=str))
                payload.update(stdout_json)

                if artifact_paths:
                    payload.setdefault("_artifacts", [])
                    if isinstance(payload["_artifacts"], list):
                        payload["_artifacts"].extend(artifact_paths)
                    payload["_last_artifacts"] = artifact_paths

                if e2e_session is not None:
                    _write_step_checkpoint(
                        e2e_session,
                        command=command,
                        rendered_payload=rendered_payload,
                        stdout_json=stdout_json,
                        context_before=context_before,
                        context_after=dict(payload),
                        new_keys=new_keys,
                        artifact_paths=artifact_paths,
                        proc=proc,
                    )
                    e2e_session.events.append({**e2e_session.to_event_base(), "event": "checkpoint_saved", "phase": "e2e_run", "status": "passed", "step_index": command.ordinal, "current_step": command.ordinal, "total_steps": len(commands), "script_path": command.script_path, "target_file": command.script_path})

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
                if e2e_session is not None:
                    structured = _structured_failure_from_errors([message])
                    e2e_session.events.append({
                        **e2e_session.to_event_base(),
                        "event": "step_failed",
                        "phase": "e2e_run",
                        "status": "failed",
                        "current_step": command.ordinal,
                        "total_steps": len(commands),
                        "target_file": structured.get("target_file") or command.script_path,
                        "failure_layer": structured.get("layer") or _failure_layer_from_error_text(message),
                        "failure_summary": (structured.get("actual") or message)[-2000:],
                        "stdout_summary": str(structured.get("stdout") or "")[-1000:],
                        "stderr_summary": str(structured.get("stderr") or "")[-1000:],
                        "rendered_payload_summary": json.dumps(_json_object_shape(structured.get("rendered_payload") or {}), ensure_ascii=False, sort_keys=True),
                        "trace_summary": _format_e2e_trace(traces)[-2000:],
                    })
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
    external_context: dict[str, Any] | None = None,
    repair_events: list[dict[str, Any]] | None = None,
    e2e_session: CreatorE2ESession | None = None,
) -> dict[str, Any]:
    """Repair one real second-round E2E workflow failure.

    第二轮只根据真实 workflow 执行证据做局部修复：
    - failed command / rendered payload；
    - placeholder resolution；
    - subprocess return code / stdout / stderr；
    - strict_json_argv_guard 与 run(args) 的真实入口关系；
    - artifact existence / final platform output；
    - 已成功前序 step trace。

    第一轮已经完成的职责审查、ToolPool、helper 权限、required_capabilities、
    coverage requirements 和工具选择，不在这里重新判断。
    """

    _validate_file_path(target_path)

    if target_path.startswith("references/"):
        logger.info(
            "[Creator][E2E] remap reference repair target to SKILL.md target=%s",
            target_path,
        )
        target_path = "SKILL.md"

    skill_dir = settings.skills_path / skill_name

    if e2e_session is None:
        e2e_session = _create_e2e_session(
            skill_name,
            source_skill_dir=skill_dir,
        )

    target_file = skill_dir / target_path

    if not target_file.is_file():
        raise ValueError(
            f"端到端修复目标不存在：{target_path}"
        )

    skill_md_path = skill_dir / "SKILL.md"
    skill_md = (
        skill_md_path.read_text(
            encoding="utf-8"
        )
        if skill_md_path.is_file()
        else ""
    )

    if (
        target_path == "SKILL.md"
        and _is_skill_md_command_format_error(
            e2e_errors
        )
    ):
        normalizer_attempted = any(
            event.get("type")
            == "command_normalizer_attempt"
            and event.get("target_file")
            == "SKILL.md"
            for event in (
                repair_events or []
            )
        )

        normalization = (
            _normalize_skill_md_runtime_commands_for_e2e(
                skill_name=skill_name,
                skill_dir=skill_dir,
                skill_md=skill_md,
            )
        )

        if repair_events is not None:
            repair_events.append({
                "type": "command_normalizer_attempt",
                "target_file": "SKILL.md",
                "changed": normalization.changed,
                "blocked": normalization.blocked,
                "issues": [
                    getattr(
                        issue,
                        "__dict__",
                        {},
                    )
                    for issue in normalization.issues
                ],
            })

        if normalization.changed:
            skill_md_path.write_text(
                normalization.content,
                encoding="utf-8",
            )

            (
                e2e_session.workspace_dir
                / "SKILL.md"
            ).write_text(
                normalization.content,
                encoding="utf-8",
            )

            return {
                "status": "repaired",
                "repaired_target": "SKILL.md",
                "patch_status": (
                    "deterministic_command_normalized"
                ),
                "diff_stats": {
                    "mode": "command_normalizer"
                },
            }

        payload = (
            _command_normalizer_blocked_payload(
                target_file="SKILL.md",
                issues=normalization.issues,
            )
        )

        if (
            normalization.blocked
            or normalizer_attempted
        ):
            if repair_events is not None:
                repair_events.append({
                    "type": (
                        "command_normalizer_blocked_"
                        "fallback_to_model"
                    ),
                    "target_file": "SKILL.md",
                    "payload": payload,
                })

            e2e_errors = list(
                e2e_errors or []
            ) + [
                _e2e_error(
                    target="SKILL.md",
                    layer=(
                        "command_normalizer_blocked"
                    ),
                    message=json.dumps(
                        payload,
                        ensure_ascii=False,
                        default=str,
                    ),
                )
            ]

    if target_path == "SKILL.md":
        hard_format_failures = (
            detect_markdown_hard_format_failures(
                "SKILL.md",
                skill_md,
                require_frontmatter=True,
            )
        )

        if hard_format_failures:
            if repair_events is not None:
                repair_events.append({
                    "type": (
                        "hard_format_requires_full_rewrite"
                    ),
                    "target_file": "SKILL.md",
                    "failures": hard_format_failures,
                })

            raise ValueError(
                "hard_format_requires_full_rewrite: "
                "E2E localized patch cannot repair "
                "SKILL.md hard Markdown format; "
                + json.dumps(
                    hard_format_failures,
                    ensure_ascii=False,
                    default=str,
                )
            )

    all_file_summaries: list[str] = []
    scripts_dir = skill_dir / "scripts"

    if scripts_dir.is_dir():
        for path in sorted(
            scripts_dir.rglob("*")
        ):
            if not path.is_file():
                continue

            if (
                "__pycache__" in path.parts
                or path.suffix == ".pyc"
            ):
                continue

            if path.suffix.lower() not in {
                ".py",
                ".js",
                ".ts",
                ".sh",
                ".bash",
            }:
                continue

            rel = path.relative_to(
                skill_dir
            ).as_posix()

            if rel == target_path:
                continue

            try:
                text = path.read_text(
                    encoding="utf-8",
                    errors="replace",
                )
            except Exception:
                continue

            all_file_summaries.append(
                f"\n--- RUNTIME FILE {rel} ---\n"
                f"{text[-6000:]}"
            )

    route = route_creator_file_model(
        file_path=target_path,
        purpose=(
            "第二轮 workflow E2E 真实试运行局部修复："
            "只根据 failed command、rendered payload、placeholder、"
            "subprocess return_code、stderr、stdout、artifact 和 final output "
            "真实失败做 single-file local patch；"
            "不重新审查职责、ToolPool、工具权限或工具选择。"
        ),
        requested_model=requested_model,
    )

    model = route.model

    _log_creator_model_usage(
        phase="e2e_repair.route",
        skill_name=skill_name,
        file_path=target_path,
        route=route,
        extra=(
            f"errors={len(e2e_errors)} "
            "mode=exact_replace_patch_sandbox_e2e"
        ),
    )

    deterministic_error = "\n\n".join(
        e2e_errors
    )[-12000:]

    repair_state = (
        _e2e_repair_state_from_errors(
            e2e_errors,
            resolved_failures=(
                e2e_session.resolved_failures
            ),
        )
    )

    structured_failure = (
        _structured_failure_from_errors(
            e2e_errors
        )
    )

    targeted_e2e_hint = (
        _targeted_e2e_repair_hint(
            e2e_errors
        )
    )

    repair_key = _e2e_repair_key(
        target_path=target_path,
        structured_failure=structured_failure,
    )

    scope = CreatorRepairScope(
        phase="workflow_e2e",
        repair_type="runtime_trial_failure",
        target_file=target_path,
        max_changed_lines=220,
        allow_tool_explore=False,
        notes=(
            "第二轮最多 10 轮，始终使用 localized_patch，不会因普通 E2E 失败切 full_file_rewrite。",
            "patch 后会先做 basic format/compile check；通过只代表文件合法，不代表 E2E 通过。",
            "basic format 错误只修格式；sandbox E2E 错误才修 workflow / argv / stdout / artifact 链路。",
            "第二轮只修 workflow / argv / placeholder / stdout / artifact / final sandbox output。",
            "平台 IO 不在 repair 层用词表判断，直接由 sandbox/E2E 试运行判断。",
            "不得重新检查脚本职责、RequirementGraph coverage、required_capabilities、"
            "ToolPool、allowed_helper_imports、tool binding 或 helper permission。",
            "E2E 阶段禁止工具库探索：不得请求 tool_pool_patch.add_tool_requests，"
            "不得探索或扩展工具池。",
            "ImportError/ModuleNotFoundError 只依据真实 stderr traceback 修直接相关 import；"
            "不得根据 ToolPool 或 allowed_helper_imports 判断导入是否合法。",
            "若 E2E 发现缺少第三方依赖，交给 dependency/environment 链路处理，"
            "不得通过重新选工具或改业务职责绕过。",
            "优先输出 edits old_lines/new_lines exact_replace patch，不要输出完整文件。",
        ),
    )

    e2e_entry_context: dict[str, Any] = {}

    if target_path.startswith("scripts/"):
        try:
            e2e_entry = (
                _skill_plan_entry_for_file(
                    file_path=target_path,
                    blueprint_text=skill_md,
                )
            )

            script_content = (
                e2e_session.workspace_dir
                / target_path
            ).read_text(
                encoding="utf-8",
                errors="replace",
            )

            argv_schema: dict[str, Any] = {}
            run_args_analysis: dict[str, Any] = {}

            if target_path.endswith(".py"):
                try:
                    argv_schema = (
                        extract_python_strict_argv_schema(
                            script_content
                        )
                    )
                except Exception:
                    argv_schema = {}

                try:
                    run_args_analysis = (
                        _python_run_args_analysis(
                            script_content
                        )
                    )
                except Exception:
                    run_args_analysis = {}

            e2e_entry_context = {
                "path": target_path,
                "runtime": getattr(
                    e2e_entry,
                    "runtime",
                    "",
                ),
                "script_argv_schema": argv_schema,
                "run_args_analysis": run_args_analysis,
            }

        except Exception:
            e2e_entry_context = {}

    if target_path == "SKILL.md":
        target_rule = (
            "你正在修复 SKILL.md 的 workflow 执行块。\n"
            "第二轮 E2E 的目标是让 workflow 在简单沙盒中真实跑通。\n"
            "E2E 只执行 SKILL.md 中的 bash/sh/shell fenced command block，"
            "references/*.md 不是执行步骤。\n"
            "只修 workflow/cross-step IO/final output/artifact 相关问题，"
            "不修 Markdown 全局格式。\n"
            "修复 command_json_parse/missing_placeholder/argv_schema_error 时，"
            "只依据本轮真实结构化失败中的 failed_command、rendered_payload、"
            "当前 payload keys、placeholder 来源、前序 stdout trace、"
            "当前脚本 strict_json_argv_guard schema 和 run(args) 实际读取关系判断。\n"
            "RequirementGraph / SkillPlanEntry 的 inputs/outputs 只是第一轮语义规划信息，"
            "不得在第二轮作为字段名或字段类型 hard contract。\n"
            "不要因为 placeholder missing 就同时改 argv key 和 placeholder root；"
            "先根据真实 payload/trace 判断 placeholder 来源，"
            "再根据当前脚本 guard/run 接口判断 argv key。\n"
            "如果 guard expected type 明确要求 list，应传整个 collection；"
            "只有当前脚本实际接口要求 scalar/file_path 时才允许从 collection 取单项。\n"
            "如果 script 自身接口自洽而 command argv 不一致，"
            "优先只改 SKILL.md 当前失败 command JSON argv。\n"
            "当 failure layer 是 runtime_command_invalid 或 "
            "command_normalizer_blocked 时，必须把失败命令修成："
            "脚本路径 + 一个单引号包住的 JSON argv 参数。\n"
            "文件输入使用 __RUNTIME_INPUT_FILE__，例如："
            "python scripts/x.py '{\"file_path\":\"__RUNTIME_INPUT_FILE__\"}'。\n"
            "多文件输入使用 __RUNTIME_INPUT_FILES__，例如："
            "python scripts/x.py '{\"file_paths\":\"__RUNTIME_INPUT_FILES__\"}'。\n"
            "纯文本输入使用 __RUNTIME_INPUT_TEXT__ 或 {{text}}，例如："
            "python scripts/x.py '{\"text_content\":\"__RUNTIME_INPUT_TEXT__\"}'。\n"
            "不要把纯文本任务强行改成 file_path；"
            "不要把文件任务强行改成 text_content。\n"
            "禁止未加引号 JSON；禁止把 JSON 拆成多个 CLI 参数；"
            "禁止 --key value 风格；"
            "命令必须通过 exactly one JSON argv object 检查。\n"
            "不得改 YAML frontmatter；不得重写整篇 SKILL.md；"
            "不得改其它已通过 command；不得改 script；"
            "不得新增脚本路径；不得引入 --argv；"
            "不得引入 runtime/entrypoint/argv 伪命令对象。\n"
            "不要重写 SKILL.md 正文。\n"
            "当前 Markdown 格式已经通过；不要修 frontmatter；"
            "不要修 code fence；不要新增/删除 ``` 行。\n"
            "只修改失败命令那一行；old_lines 必须包含完整、真实、"
            "当前文件中的命令行。\n"
            "不要把 ```bash 和 ``` 纳入 old_lines，"
            "除非同时完整包含闭合 fence。\n"
            "不要修改 frontmatter 边界；"
            "不要修改 fenced block 开闭结构。\n"
            "不要在 repair 层重新定义平台 IO；"
            "平台 IO 由 sandbox/E2E 试运行判断。\n"
            "不得检查 ToolPool、allowed_helper_imports、tool binding、"
            "required_capabilities、coverage_requirements 或工具权限。\n"
            "优先输出 edits old_lines/new_lines exact_replace patch。"
            "不要输出完整 SKILL.md。"
        )

    elif target_path.startswith("scripts/"):
        target_rule = (
            "你正在修复一次真实 workflow E2E 试运行失败。\n"
            "只依据本轮结构化失败中的 failed_command、rendered_payload、"
            "stdout、stderr、return_code、失败层和已成功前序 trace 定位问题。\n"
            "修复范围必须直接对应真实失败证据。\n"
            "如果是 argv_schema_error，只核对当前 command JSON argv、"
            "strict_json_argv_guard schema 和 run(args) 实际读取关系。\n"
            "strict_json_argv_guard 是接口不对齐探针，不能通过删除参数降低功能覆盖面。\n"
            "如果是 script_exit，以 raw stderr traceback、异常类型和报错源码行为主；"
            "ImportError 或 ModuleNotFoundError 可以修改直接相关 import，"
            "其它异常只修改 traceback 直接涉及的执行区域。\n"
            "如果是 stdout_contract/stdout_json_parse，"
            "只修改当前 stdout 组织与返回逻辑。\n"
            "如果是 artifact/final output 失败，"
            "只修改当前产物创建、路径返回或最终 stdout 映射。\n"
            "不得重新判断当前脚本职责是否完整，"
            "不得检查 required_capabilities、coverage_requirements、"
            "ToolPool、allowed_helper_imports、tool binding 或 helper permission。\n"
            "不得改其它文件或已通过步骤。\n"
            "优先输出 edits old_lines/new_lines exact_replace patch。"
            "不要输出完整源码。"
        )

    else:
        target_rule = (
            "只修复 E2E_REPAIR_TARGET 指向的文件。\n"
            "只修当前真实 E2E 失败对应的最小接口或运行问题。\n"
            "不得重新审查职责、ToolPool、工具权限或工具选择。\n"
            "优先输出 edits old_lines/new_lines exact_replace patch。"
            "不要输出完整文件。"
        )

    base_task_context = "\n".join([
        f"Skill 名称：{skill_name}",
        "",
        "E2E repair 状态机：",
        json.dumps(
            repair_state,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        ),
        "",
        "本轮真实 E2E 失败：",
        json.dumps(
            structured_failure,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        ),
        "",
        "定向 E2E 修复提示：",
        targeted_e2e_hint or "无",
        "",
        "当前脚本实际运行接口事实：",
        json.dumps(
            e2e_entry_context,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        ),
        "",
        "当前 SKILL.md：",
        skill_md[-12000:],
        "",
        "相关上下游 runtime script 摘要：",
        "".join(all_file_summaries)[-20000:],
        "",
        "第二轮硬性边界：",
        "只根据真实 E2E 运行失败申错改错。",
        "不得重新判断脚本职责、ToolPool、allowed_helper_imports、tool binding、"
        "required_capabilities、coverage_requirements、工具选择或 helper permission。",
    ])

    repair_feedback = "\n\n".join(
        repair_state.get(
            "remaining_failed_checks"
        )
        or e2e_errors
    )[-12000:]

    last_failure = ""
    max_candidate_attempts = 10

    working_content = (
        e2e_session.workspace_dir
        / target_path
    ).read_text(
        encoding="utf-8"
    )

    before_repair_snapshot = working_content
    consecutive_format_regressions = 0

    repair_template_history: dict[
        str,
        list[str],
    ] = {}

    consecutive_argv_schema_noops = 0

    for candidate_attempt in range(
        1,
        max_candidate_attempts + 1,
    ):
        current_content = working_content
        use_full_rewrite = False

        effective_skill_md = (
            current_content
            if target_path == "SKILL.md"
            else skill_md
        )

        effective_task_context = base_task_context

        if target_path == "SKILL.md":
            effective_task_context = (
                base_task_context.replace(
                    skill_md[-12000:],
                    effective_skill_md[-12000:],
                    1,
                )
            )

        current_repair_state = (
            _e2e_repair_state_from_errors(
                repair_feedback.split("\n\n"),
                resolved_failures=(
                    e2e_session.resolved_failures
                ),
            )
        )

        effective_task_context += (
            "\n\n当前 E2E repair 状态机：\n"
            + json.dumps(
                current_repair_state,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                default=str,
            )
            + "\n\n硬性要求：只能修 remaining_failed_checks；"
            "不得再次修改 resolved_failures 对应问题。"
        )

        try:
            if use_full_rewrite:
                clean_rewrite_tool_context = ""

                if target_path.startswith("scripts/"):
                    try:
                        clean_rewrite_entry = (
                            _skill_plan_entry_for_file(
                                file_path=target_path,
                                blueprint_text=skill_md,
                            )
                        )

                        clean_rewrite_tool_context = (
                            _creator_tool_context_for_script(
                                file_path=target_path,
                                skill_plan_entry=(
                                    clean_rewrite_entry
                                ),
                                blueprint_text=skill_md,
                                failure_layer=None,
                                error_text=None,
                                include_snippets=True,
                                rediscover_for_repair=False,
                                repair_context={
                                    "target_file": target_path,
                                    "script_content": (
                                        current_content
                                    ),
                                    "runtime_contract": getattr(
                                        clean_rewrite_entry,
                                        "runtime_contract",
                                        None,
                                    ),
                                    "coverage_requirements": getattr(
                                        clean_rewrite_entry,
                                        "coverage_requirements",
                                        None,
                                    ),
                                    "artifact_contract": getattr(
                                        clean_rewrite_entry,
                                        "artifact_contract",
                                        None,
                                    ),
                                    "command_argv_contract": getattr(
                                        clean_rewrite_entry,
                                        "command_template",
                                        None,
                                    ),
                                },
                            )
                        )
                    except Exception:
                        clean_rewrite_tool_context = ""

                rewrite_context = (
                    _full_file_rewrite_context_for_e2e(
                        skill_name=skill_name,
                        target_path=target_path,
                        skill_md=effective_skill_md,
                        current_content=current_content,
                        previous_content=(
                            before_repair_snapshot
                        ),
                        all_file_summaries=(
                            all_file_summaries
                        ),
                        e2e_entry_context=(
                            e2e_entry_context
                        ),
                        clean_tool_context=(
                            clean_rewrite_tool_context
                        ),
                    )
                )

                candidate_content = await (
                    _request_full_file_rewrite_for_e2e(
                        model=model,
                        target_path=target_path,
                        current_content=current_content,
                        previous_content=(
                            before_repair_snapshot
                        ),
                        rewrite_context=rewrite_context,
                        rewrite_target_rule=(
                            _full_file_rewrite_target_rule_for_e2e(
                                target_path
                            )
                        ),
                    )
                )

                diff_stats = {
                    "mode": "full_file_rewrite",
                    "changed_line_count": abs(
                        len(
                            candidate_content.splitlines()
                        )
                        - len(
                            current_content.splitlines()
                        )
                    ),
                    "generated_diff_excerpt": "",
                    "applied": [{
                        "fallback_type": (
                            "full_file_rewrite"
                        )
                    }],
                }

            else:
                (
                    _proposal,
                    candidate_content,
                    diff_stats,
                ) = await _request_and_apply_repair_patch(
                    model=model,
                    file_path=target_path,
                    current_content=current_content,
                    failure_text=repair_feedback,
                    scope=scope,
                    task_context=(
                        effective_task_context
                        + (
                            "\n\n上一轮候选失败反馈：\n"
                            + last_failure
                            if last_failure
                            else ""
                        )
                    ),
                    target_rule=target_rule,
                    patch_retry_limit=3,
                )

            from .generation import (
                _sanitize_generated_file_content,
            )

            sanitized = (
                _sanitize_generated_file_content(
                    target_path,
                    candidate_content,
                )
            )

            basic_format_failure = (
                check_patch_candidate_basic_format(
                    target_path,
                    sanitized,
                )
            )

            if basic_format_failure is not None:
                last_failure = (
                    "当前失败只表示 patch 后文件基础格式不合法。\n"
                    f"attempt={candidate_attempt}/"
                    f"{max_candidate_attempts}\n"
                    f"{basic_format_failure.to_failure_text()}\n"
                    "不要修改工具选择。不要修改 argv schema。"
                    "不要修改 stdout 字段。不要修改业务职责。"
                    "只把当前候选修成合法源码/合法 Markdown。"
                )

                repair_feedback = (
                    deterministic_error
                    + "\n\n"
                    + last_failure
                )

                e2e_session.events.append({
                    **e2e_session.to_event_base(),
                    "attempt": candidate_attempt,
                    "target_file": target_path,
                    "patch_status": (
                        "basic_format_failed"
                    ),
                    "repair_key": repair_key,
                    "repair_mode": (
                        "localized_patch"
                    ),
                    "status": "patch_failed",
                    "coarse_failure_kind": (
                        basic_format_failure
                        .coarse_failure_kind
                    ),
                    "rejection_reason": (
                        basic_format_failure.message
                    ),
                    "failed_checks": (
                        repair_feedback.split(
                            "\n\n"
                        )[:8]
                    ),
                    "resolved_failures": (
                        e2e_session.resolved_failures
                    ),
                    "rerun_status": "skipped",
                    "writeback_status": (
                        "candidate_only"
                    ),
                })

                e2e_session.repair_attempt_counts[
                    repair_key
                ] = (
                    e2e_session
                    .repair_attempt_counts
                    .get(
                        repair_key,
                        0,
                    )
                    + 1
                )

                consecutive_format_regressions += 1

                if (
                    consecutive_format_regressions
                    >= 3
                ):
                    if repair_events is not None:
                        repair_events.extend(
                            e2e_session.events
                        )

                    return {
                        "status": (
                            "still_failed_same_target"
                        ),
                        "repaired_target": (
                            target_path
                        ),
                        "next_target": None,
                        "next_failure": [
                            last_failure
                        ],
                        "last_failure": (
                            last_failure
                        ),
                        "attempt": (
                            candidate_attempt
                        ),
                        "error_type": (
                            "basic_format_failed"
                        ),
                    }

                continue

            consecutive_format_regressions = 0

            old_command_signature = (
                e2e_session.command_plan_signature
            )

            session_target = (
                e2e_session.workspace_dir
                / target_path
            )

            session_target.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            session_target.write_text(
                sanitized,
                encoding="utf-8",
            )

            e2e_session.current_revision += 1

            session_skill_md = (
                e2e_session.workspace_dir
                / "SKILL.md"
            ).read_text(
                encoding="utf-8"
            )

            try:
                session_commands = (
                    _extract_e2e_workflow_commands(
                        e2e_session.workspace_dir,
                        session_skill_md,
                    )
                )

                new_command_signature = (
                    _command_plan_signature(
                        session_commands
                    )
                )

            except Exception:
                session_commands = []
                new_command_signature = ""

            earliest_step = _earliest_invalid_step(
                changed_file=target_path,
                commands=session_commands,
                old_command_plan_signature=(
                    old_command_signature
                ),
                new_command_plan_signature=(
                    new_command_signature
                ),
            )

            resume_from_step = (
                earliest_step
                or (
                    len(session_commands) + 1
                    if session_commands
                    else 1
                )
            )

            invalidated = (
                _invalidate_checkpoints_from(
                    e2e_session,
                    earliest_step,
                )
                if earliest_step
                else []
            )

            reused = [
                idx
                for idx in range(
                    1,
                    max(
                        1,
                        resume_from_step,
                    ),
                )
                if _load_valid_checkpoint(
                    e2e_session,
                    idx,
                )
            ]

            sandbox_gate = (
                _run_e2e_sandbox_acceptance_gate(
                    skill_name=skill_name,
                    candidate_skill_dir=(
                        e2e_session.workspace_dir
                    ),
                    patched_file=target_path,
                    original_errors=e2e_errors,
                    external_context=external_context,
                    requested_model=requested_model,
                    e2e_session=e2e_session,
                    resume_from_step=(
                        resume_from_step
                    ),
                )
            )

            e2e_session.events.append({
                **e2e_session.to_event_base(),
                "attempt": candidate_attempt,
                "target_file": target_path,
                "resume_from_step": (
                    resume_from_step
                ),
                "reused_venv": True,
                "reused_checkpoints": reused,
                "invalidated_checkpoints": (
                    invalidated
                ),
                "failed_checks": (
                    sandbox_gate.get("errors")
                    or []
                ),
                "resolved_failures": (
                    e2e_session.resolved_failures
                ),
                "patch_mode": "exact_replace",
                "repair_key": repair_key,
                "repair_mode": "localized_patch",
                "fallback_type": (
                    diff_stats.get(
                        "applied"
                    )
                    or [{}]
                )[0].get(
                    "fallback_type",
                    "none",
                ),
                "patch_status": (
                    "e2e_fully_passed"
                    if sandbox_gate.get(
                        "accepted"
                    )
                    else "e2e_still_failed"
                ),
                "status": (
                    "repaired"
                    if sandbox_gate.get(
                        "accepted"
                    )
                    else "same_target_still_failed"
                ),
                "changed_line_count": (
                    diff_stats.get(
                        "changed_line_count"
                    )
                ),
                "diff_excerpt": (
                    diff_stats.get(
                        "generated_diff_excerpt"
                    )
                ),
                "matched_excerpt": (
                    (
                        diff_stats.get(
                            "applied"
                        )
                        or [{}]
                    )[0].get(
                        "matched_excerpt"
                    )
                ),
                "original_model_old_excerpt": (
                    (
                        diff_stats.get(
                            "applied"
                        )
                        or [{}]
                    )[0].get(
                        "original_model_old_excerpt"
                    )
                ),
                "rerun_status": (
                    "passed"
                    if sandbox_gate.get(
                        "accepted"
                    )
                    else "failed"
                ),
                "writeback_status": (
                    "candidate_only"
                ),
            })

            if not sandbox_gate.get(
                "accepted"
            ):
                if not use_full_rewrite:
                    e2e_session.repair_attempt_counts[
                        repair_key
                    ] = (
                        e2e_session
                        .repair_attempt_counts
                        .get(
                            repair_key,
                            0,
                        )
                        + 1
                    )

                gate_errors = (
                    sandbox_gate.get("errors")
                    or []
                )

                failure_signature = (
                    _failure_signature_from_error(
                        (
                            gate_errors
                            or [""]
                        )[0]
                    )
                )

                template_signature = (
                    _stable_json_hash([
                        {
                            "ordinal": c.ordinal,
                            "script_path": (
                                c.script_path
                            ),
                            "argv_template": (
                                c.argv_template
                            ),
                        }
                        for c
                        in (
                            session_commands
                            or []
                        )
                    ])
                )

                history = (
                    repair_template_history
                    .setdefault(
                        failure_signature,
                        [],
                    )
                )

                history.append(
                    template_signature
                )

                if (
                    len(history) >= 3
                    and history[-1]
                    == history[-3]
                ):
                    oscillation_message = (
                        "Detected oscillating E2E repair. "
                        "This indicates missing/ambiguous "
                        "typed sample seeding or placeholder diagnostics. "
                        "Do not continue patching business files."
                    )

                    e2e_session.events.append({
                        **e2e_session.to_event_base(),
                        "attempt": candidate_attempt,
                        "target_file": target_path,
                        "patch_status": (
                            "oscillating_repair_blocked"
                        ),
                        "status": "blocked",
                        "failure_signature": (
                            failure_signature
                        ),
                        "argv_template_history": (
                            history[-4:]
                        ),
                        "rejection_reason": (
                            oscillation_message
                        ),
                        "failed_checks": gate_errors,
                        "rerun_status": "failed",
                        "writeback_status": (
                            "candidate_only"
                        ),
                    })

                    if repair_events is not None:
                        repair_events.extend(
                            e2e_session.events
                        )

                    return {
                        "status": "blocked",
                        "repaired_target": (
                            target_path
                        ),
                        "next_target": None,
                        "next_failure": [
                            oscillation_message
                        ],
                        "attempt": (
                            candidate_attempt
                        ),
                    }

                next_target = (
                    _e2e_repair_target_from_errors(
                        gate_errors
                    )
                )

                has_explicit_next_target = any(
                    "E2E_REPAIR_TARGET="
                    in str(error or "")
                    for error in gate_errors
                )

                if (
                    has_explicit_next_target
                    and next_target
                    and next_target
                    != target_path
                ):
                    for error in e2e_errors:
                        (
                            e2e_session
                            .resolved_failures
                            .append({
                                "failure_signature": (
                                    _failure_signature_from_error(
                                        error
                                    )
                                ),
                                "target_file": target_path,
                                "failure_kind": (
                                    _failure_layer_from_error_text(
                                        error
                                    )
                                    or "e2e"
                                ),
                                "step_index": (
                                    structured_failure.get(
                                        "failed_step_index"
                                    )
                                ),
                                "resolved_by_revision": (
                                    e2e_session
                                    .current_revision
                                ),
                                "verified_by_e2e": True,
                                "handoff_to_target": (
                                    next_target
                                ),
                            })
                        )

                    target_file.parent.mkdir(
                        parents=True,
                        exist_ok=True,
                    )

                    target_file.write_text(
                        sanitized,
                        encoding="utf-8",
                    )

                    handoff_event = {
                        **e2e_session.to_event_base(),
                        "attempt": candidate_attempt,
                        "target_file": target_path,
                        "patch_status": (
                            "partial_success_target_changed"
                        ),
                        "status": "target_changed",
                        "rejection_reason": (
                            "current target failure disappeared; "
                            "remaining failure moved to a different file"
                        ),
                        "next_target": next_target,
                        "next_failure": gate_errors,
                        "remaining_target_file": (
                            next_target
                        ),
                        "failed_checks": gate_errors,
                        "resolved_failures": (
                            e2e_session.resolved_failures
                        ),
                        "rerun_status": "target_handoff",
                        "writeback_status": "written",
                    }

                    e2e_session.events.append(
                        handoff_event
                    )

                    if repair_events is not None:
                        repair_events.extend(
                            e2e_session.events
                        )

                    return {
                        "status": "target_changed",
                        "repaired_target": target_path,
                        "next_target": next_target,
                        "next_failure": gate_errors,
                        "attempt": candidate_attempt,
                    }

                working_content = sanitized

                last_failure = (
                    "SANDBOX_E2E_FAILED：候选 patch 已应用，"
                    "但简单沙盒 E2E 仍失败。\n"
                    f"attempt={candidate_attempt}/"
                    f"{max_candidate_attempts}\n"
                    f"diff_stats="
                    f"{json.dumps(diff_stats, ensure_ascii=False, default=str)[:3000]}\n"
                    f"sandbox_gate="
                    f"{json.dumps(sandbox_gate, ensure_ascii=False, default=str)[:12000]}\n"
                    "请只依据 sandbox_gate.errors 中新的真实运行失败"
                    "继续输出新的 exact_replace patch。"
                )

                repair_feedback = (
                    "\n\n".join(
                        sandbox_gate.get(
                            "errors"
                        )
                        or e2e_errors
                    )[-12000:]
                    + "\n\n"
                    + last_failure
                )

                logger.warning(
                    "[Creator][E2E]"
                    "[repair_candidate_e2e_failed] "
                    "skill=%s file=%s attempt=%d/%d",
                    skill_name,
                    target_path,
                    candidate_attempt,
                    max_candidate_attempts,
                )

                continue

            for error in e2e_errors:
                (
                    e2e_session
                    .resolved_failures
                    .append({
                        "failure_signature": (
                            _failure_signature_from_error(
                                error
                            )
                        ),
                        "target_file": target_path,
                        "failure_kind": (
                            _failure_layer_from_error_text(
                                error
                            )
                            or "e2e"
                        ),
                        "step_index": (
                            structured_failure.get(
                                "failed_step_index"
                            )
                        ),
                        "resolved_by_revision": (
                            e2e_session.current_revision
                        ),
                        "verified_by_e2e": True,
                    })
                )

            target_file.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            target_file.write_text(
                sanitized,
                encoding="utf-8",
            )

            if e2e_session.events:
                e2e_session.events[-1][
                    "writeback_status"
                ] = "written"

            if repair_events is not None:
                repair_events.extend(
                    e2e_session.events
                )

            logger.info(
                "[Creator][E2E][repair_accept] "
                "skill=%s file=%s attempt=%d "
                "diff_stats=%s sandbox=passed",
                skill_name,
                target_path,
                candidate_attempt,
                json.dumps(
                    diff_stats,
                    ensure_ascii=False,
                    default=str,
                )[:3000],
            )

            return {
                "status": "repaired",
                "repaired_target": target_path,
                "next_target": None,
                "next_failure": [],
                "attempt": candidate_attempt,
            }

        except Exception as candidate_exc:
            error_text = str(
                candidate_exc
            )

            is_format_regression_rejection = (
                target_path == "SKILL.md"
                and (
                    "hard_format_regression"
                    in error_text
                    or "PATCH_CANDIDATE_FORMAT_REGRESSED"
                    in error_text
                    or "markdown.fences.unclosed"
                    in error_text
                    or "markdown.fences.bash_unclosed"
                    in error_text
                    or "markdown.frontmatter.unclosed"
                    in error_text
                )
            )

            if is_format_regression_rejection:
                consecutive_format_regressions += 1
            else:
                consecutive_format_regressions = 0

            if (
                "proposal_noop" in error_text
                or "no-op" in error_text
            ):
                patch_status = "noop"

            elif is_format_regression_rejection:
                patch_status = (
                    "hard_format_regression_rejected"
                )

            elif (
                "FORMAT_VIOLATION" in error_text
                or "JSON" in error_text
                or "parse" in error_text
            ):
                patch_status = "parse_failed"

            else:
                patch_status = "rejected"

            e2e_session.events.append({
                **e2e_session.to_event_base(),
                "attempt": candidate_attempt,
                "target_file": target_path,
                "patch_status": (
                    "patch_apply_failed"
                    if patch_status == "rejected"
                    else patch_status
                ),
                "status": "patch_failed",
                "rejection_reason": (
                    error_text[:2000]
                ),
                "last_output_excerpt": getattr(
                    candidate_exc,
                    "last_output_excerpt",
                    "",
                ),
                "parser_error": (
                    getattr(
                        candidate_exc,
                        "parser_error",
                        "",
                    )
                    or (
                        error_text[:1000]
                        if patch_status
                        == "parse_failed"
                        else ""
                    )
                ),
                "diff_extraction_attempted": bool(
                    getattr(
                        candidate_exc,
                        "diff_extraction_attempted",
                        False,
                    )
                ),
                "repair_key": repair_key,
                "repair_mode": "localized_patch",
                "old_lines_new_lines_fallback_attempted": bool(
                    getattr(
                        candidate_exc,
                        "lines_fallback_attempted",
                        False,
                    )
                ),
                "failed_checks": (
                    repair_feedback.split(
                        "\n\n"
                    )[:8]
                ),
                "resolved_failures": (
                    e2e_session.resolved_failures
                ),
                "rerun_status": "skipped",
                "writeback_status": (
                    "candidate_only"
                ),
            })

            if (
                patch_status == "noop"
                and "argv_schema_error"
                in deterministic_error
            ):
                consecutive_argv_schema_noops += 1

                argv_kind = str(
                    structured_failure.get(
                        "details",
                        {},
                    ).get(
                        "argv_schema_error_kind"
                    )
                    or ""
                )

                if (
                    target_path.startswith(
                        "scripts/"
                    )
                    and argv_kind
                    in {
                        "unknown_key",
                        "missing_required",
                        "invalid_type",
                        "empty_required",
                    }
                ):
                    switch_message = (
                        "argv_schema_error no-op on script target; "
                        "for unknown_key/missing_required/"
                        "invalid_type/empty_required the repair target "
                        "is SKILL.md unless script self-inconsistency is proven."
                    )

                    if repair_events is not None:
                        repair_events.extend(
                            e2e_session.events
                        )

                    return {
                        "status": "target_changed",
                        "repaired_target": target_path,
                        "next_target": "SKILL.md",
                        "next_failure": [
                            switch_message,
                            deterministic_error,
                        ],
                        "attempt": candidate_attempt,
                    }

                if (
                    consecutive_argv_schema_noops
                    >= 2
                ):
                    blocked_message = (
                        "argv_schema_error repair produced "
                        "two consecutive no-op patches for the current target; "
                        "stop this target to avoid burning the full retry budget."
                    )

                    if repair_events is not None:
                        repair_events.extend(
                            e2e_session.events
                        )

                    return {
                        "status": "blocked",
                        "repaired_target": target_path,
                        "next_target": None,
                        "next_failure": [
                            blocked_message,
                            deterministic_error,
                        ],
                        "attempt": candidate_attempt,
                        "error_type": (
                            "argv_schema_noop_blocked"
                        ),
                    }

            elif patch_status != "noop":
                consecutive_argv_schema_noops = 0

            if (
                repair_events is not None
                and patch_status
                in {
                    "noop",
                    "parse_failed",
                }
            ):
                repair_events.extend(
                    e2e_session.events
                )

            last_failure = (
                "REPAIR_CANDIDATE_FAILED："
                "候选 patch 生成、解析或应用失败。\n"
                f"attempt={candidate_attempt}/"
                f"{max_candidate_attempts}\n"
                f"error_type="
                f"{type(candidate_exc).__name__}\n"
                f"error={candidate_exc}\n"
                + (
                    "\n原始 Markdown 格式已经通过；"
                    "候选 patch 造成格式回归并已拒绝。"
                    "继续只修 E2E 内容问题："
                    "不要修 frontmatter；不要修 code fence；"
                    "不要新增/删除 ``` 行；"
                    "只修改失败命令那一行；"
                    "old_lines 必须包含当前文件中的完整真实命令行。"
                    if is_format_regression_rejection
                    else (
                        "\n请继续输出新的 exact_replace patch；"
                        "不要重新分析职责、ToolPool 或工具权限。"
                    )
                )
            )

            repair_feedback = (
                deterministic_error
                + "\n\n"
                + last_failure
            )

            if not use_full_rewrite:
                e2e_session.repair_attempt_counts[
                    repair_key
                ] = (
                    e2e_session
                    .repair_attempt_counts
                    .get(
                        repair_key,
                        0,
                    )
                    + 1
                )

            if (
                consecutive_format_regressions
                >= 2
            ):
                if repair_events is not None:
                    repair_events.extend(
                        e2e_session.events
                    )

                return {
                    "status": (
                        "still_failed_same_target"
                    ),
                    "repaired_target": target_path,
                    "next_target": None,
                    "next_failure": (
                        repair_feedback.split(
                            "\n\n"
                        )[:8]
                    ),
                    "last_failure": (
                        last_failure[:12000]
                    ),
                    "attempt": candidate_attempt,
                    "error_type": (
                        "e2e_content_repair_warning"
                    ),
                }

            logger.warning(
                "[Creator][E2E]"
                "[repair_candidate_failed] "
                "skill=%s file=%s attempt=%d/%d error=%s",
                skill_name,
                target_path,
                candidate_attempt,
                max_candidate_attempts,
                candidate_exc,
            )

            continue

    if repair_events is not None:
        repair_events.extend(
            e2e_session.events
        )

    return {
        "status": "still_failed_same_target",
        "repaired_target": target_path,
        "next_target": None,
        "next_failure": (
            repair_feedback.split(
                "\n\n"
            )[:8]
        ),
        "last_failure": last_failure[:12000],
        "attempt": max_candidate_attempts,
    }

def validate_workflow_e2e(
    skill_name: str,
    *,
    external_context: dict[str, Any] | None = None,
    source_skill_dir: Path | None = None,
    requested_model: str | None = None,
    e2e_session: CreatorE2ESession | None = None,
    resume_from_step: int = 1,
) -> list[str]:
    """Second-round Creator validator.

    Strict E2E is a deterministic workflow gate only: command rendering,
    placeholder resolution, script static preflight, real process exit status,
    stdout JSON/object contract, artifact existence, and final sandbox output.
    LLM requirement/argument-effect review is advisory only and is not invoked
    from this blocking path.
    """

    return _run_skill_workflow_e2e_once(
        skill_name,
        external_context=external_context,
        source_skill_dir=source_skill_dir,
        requested_model=requested_model,
        e2e_session=e2e_session,
        resume_from_step=resume_from_step,
    )

def _run_e2e_sandbox_acceptance_gate(
    *,
    skill_name: str,
    candidate_skill_dir: Path,
    patched_file: str,
    original_errors: list[str],
    external_context: dict[str, Any] | None = None,
    requested_model: str | None = None,
    e2e_session: CreatorE2ESession | None = None,
    resume_from_step: int = 1,
) -> dict[str, Any]:
    """Second-round E2E acceptance gate.

    E2E 是否通过，必须由真实沙盒试运行决定。
    不写平台字段词表。
    """

    candidate_skill_dir = candidate_skill_dir.resolve()

    if not candidate_skill_dir.is_dir():
        return {
            "accepted": False,
            "phase": "e2e_sandbox",
            "patched_file": patched_file,
            "reason": f"candidate skill dir 不存在：{candidate_skill_dir}",
            "errors": [f"candidate skill dir 不存在：{candidate_skill_dir}"],
            "original_errors": original_errors,
        }

    errors = _run_skill_workflow_e2e_once(
        skill_name,
        external_context=external_context,
        source_skill_dir=candidate_skill_dir,
        requested_model=requested_model,
        e2e_session=e2e_session,
        resume_from_step=resume_from_step,
    )

    if errors:
        return {
            "accepted": False,
            "phase": "e2e_sandbox",
            "patched_file": patched_file,
            "reason": "candidate 在简单沙盒 E2E 试运行中失败。",
            "errors": errors,
            "original_errors": original_errors,
        }

    return {
        "accepted": True,
        "phase": "e2e_sandbox",
        "patched_file": patched_file,
        "reason": "candidate 已通过现有 sandbox/E2E workflow 试运行。",
        "errors": [],
        "original_errors": original_errors,
    }

def _raise_file_contract_failures(results: list[ContractCheckResult]) -> None:
    failed = [result for result in results if not result.passed]
    if failed:
        raise ContractValidationError(
            "文件级合同校验未通过：\n" + _format_contract_checks(results, passed=False),
            results,
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


def _stage_error_for_script_functional(exc: Exception):
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
    """Return only fields that are semantically artifact/file outputs.

    Creator 中需要严格区分两类字段：

    1. stdout data fields:
       普通 stdout JSON 业务字段，只表示脚本输出的数据结构。
       例如任何业务字段、结构化内容字段、统计字段、描述字段等。
       这些字段只应接受 JSON required/non-empty/type/provenance 校验，
       不能被当作文件路径检查。

    2. artifact/file fields:
       文件产物路径字段，字段值应指向真实生成的文件。
       这些字段才进入 artifact existence/content evidence 校验。

    因此：
    - artifact_fields / file_fields / file_outputs 是显式文件产物字段，可以直接采纳。
    - artifact_outputs 是显式 artifact manifest，也可以采纳。
    - stdout_fields / output_fields 只是普通 stdout 字段集合，不能整体提升为 artifact。
      只有字段名本身具备 runtime artifact 语义时，才可作为 artifact 字段。
    """
    fields: list[str] = []
    contract = contract or {}

    # 显式声明为文件产物的字段，直接采纳。
    for key in ("artifact_fields", "file_fields", "file_outputs"):
        raw = contract.get(key)
        if isinstance(raw, str) and raw.strip():
            fields.append(raw.strip())
        elif isinstance(raw, list):
            fields.extend(str(item).strip() for item in raw if str(item).strip())

    # stdout_fields / output_fields 是 stdout JSON 字段，不等于 artifact 字段。
    # 只有字段名本身具备平台 runtime artifact 语义时，才提升为 artifact。
    for key in ("stdout_fields", "output_fields"):
        raw = contract.get(key)
        candidates: list[str] = []

        if isinstance(raw, str) and raw.strip():
            candidates.append(raw.strip())
        elif isinstance(raw, list):
            candidates.extend(str(item).strip() for item in raw if str(item).strip())

        for field_name in candidates:
            try:
                if is_runtime_artifact_semantic(field_name):
                    fields.append(field_name)
            except Exception:
                # 语义判断 helper 异常时，宁可不把普通 stdout 字段误判为 artifact。
                continue

    # 显式 artifact_outputs manifest 继续采纳。
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
            "当前失败属于 missing_placeholder：命令占位符无法从 payload 或前序 stdout 解析。"
            "先区分 JSON 模板语法错误、placeholder 不存在、argv key 与脚本 schema 不一致、可选参数被误当成必填 placeholder。"
            "裸 placeholder 作为 JSON object value 是合法 workflow template 语义，E2E 会先确定性规范化为字符串占位符；若规范化后仍不在 payload/trace 中，不要继续按 command_json_parse 处理，应改为使用平台 guaranteed input、前序 stdout 字段，或让入口脚本接收 envelope 并内部默认化可选项。"
            "优先修 SKILL.md 当前失败步骤的 JSON argv placeholder，不要改已成功 trace 对应步骤。"
        )

    if layer == "command_json_parse":
        return (
            "当前失败表面是 command_json_parse，但修复前必须分类：JSON 模板语法错误、placeholder 不存在、argv key 与脚本 schema 不一致、可选参数被误当成必填 placeholder。"
            "不要把 JSON object value 位置的裸 placeholder 误诊断为双引号转义问题；E2E 会确定性规范化这类模板。若 placeholder 根不在可用 payload keys 或前序 trace 中，应改为 guaranteed input/envelope，或让入口脚本内部解析并提供默认值。"
        )

    return ""

__all__ = [name for name in globals() if not name.startswith("__")]


E2E_TOOL_POOL_RULES = """Before running generated scripts, Creator must run runtime_import_guard against the current tool_pool file binding; guard failures skip run_script and enter repair/tool_pool_patch + gate."""
