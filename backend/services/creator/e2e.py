"""E2E workflow validation, script static checks, and trial-run helpers."""

import hashlib
import csv
import copy
import uuid
from collections import Counter
from dataclasses import asdict, replace

from .common import *  # noqa: F403
from .contracts import *  # noqa: F403
from .command_normalizer import canonicalize_skill_md_runtime_commands, parse_skill_md_bash_command_blocks
from .basic_format import check_patch_candidate_basic_format
from ..skill_plan import parse_responsibility_edges
from ..skill_dataflow import parse_placeholder_expr
from ..platform_io_contract import (
    build_platform_io_contract,
    get_platform_output_sink,
    normalize_platform_output_sinks,
    parse_runtime_output_mappings,
    project_and_commit_skill_outputs,
    value_matches_platform_schema,
)
from backend.routers.chat_utils import (
    _install_python_import_dependency,
    _scan_and_install_python_deps,
)
from ..llm_proxy import complete_json_object_once


_E2E_PIP_INDEX_URL = (
    "https://pypi.tuna.tsinghua.edu.cn/simple"
)
_E2E_PIP_INSTALL_TIMEOUT_SECONDS = 300
_E2E_PIP_NETWORK_TIMEOUT_SECONDS = 30
_E2E_PIP_RETRIES = 3

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
    payload_before_preview: dict[str, Any] = field(default_factory=dict)
    placeholder_bindings: dict[str, Any] = field(default_factory=dict)
    rendered_argv_preview: dict[str, Any] = field(default_factory=dict)
    stdout_preview: dict[str, Any] = field(default_factory=dict)
    payload_changes: dict[str, Any] = field(default_factory=dict)
    created_files: list[dict[str, Any]] = field(default_factory=list)
    modified_files: list[dict[str, Any]] = field(default_factory=list)
    value_provenance: dict[str, Any] = field(default_factory=dict)


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
    shape: str = ""
    item_shape: str = ""
    required: bool = True
    source: str = "placeholder"
    target_file: str = ""
    confidence: str = "low"
    properties: dict[str, str] = field(default_factory=dict)
    provenance_source: str = ""
    shape_source: str = ""
    argv_schema_shape: str = ""
    platform_io_shape: str = ""
    graph_declared_shape: str = ""
    skill_plan_declared_shape: str = ""
    nullable: bool = False
    default_available: bool = False
    default_value: Any = None
    required_paths: tuple[str, ...] = ()
    optional_paths: tuple[str, ...] = ()
    consumed_paths: tuple[str, ...] = ()
    min_items: int = 0
    max_items: int | None = None
    required_source: str = ""


@dataclass(frozen=True)
class E2EInputCaseSpec:
    """Frozen authority for one root external runtime input."""

    name: str
    provenance_source: str
    runtime_shape: str
    item_shape: str = ""
    required: bool = True
    nullable: bool = False
    default_available: bool = False
    default_value: Any = None
    required_paths: tuple[str, ...] = ()
    optional_paths: tuple[str, ...] = ()
    consumed_paths: tuple[str, ...] = ()
    min_items: int = 0
    max_items: int | None = None
    allowed_formats: tuple[str, ...] = ()
    homogeneous_files: bool | None = None
    file_format_source: str = ""
    target_file: str = ""
    required_source: str = ""


@dataclass(frozen=True)
class E2EInputCasePlan:
    inputs: dict[str, E2EInputCaseSpec]
    digest: str


@dataclass(frozen=True)
class E2EInputCandidate:
    status: str
    fixture: dict[str, Any] | None = None
    reason: str = ""


class E2ECaseInfrastructureError(ValueError):
    """Creator-owned failure before a valid frozen runtime case exists."""

    def __init__(self, reason: str, *, details: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.details = details or {}


def _e2e_case_infrastructure_failure(exc: E2ECaseInfrastructureError) -> str:
    return _e2e_error(
        target="creator_e2e",
        layer="e2e_case_plan",
        message=str(exc),
        failure_code=exc.reason,
        repair_instruction=(
            "This is Creator E2E infrastructure. Do not repair SKILL.md or scripts/*.py; "
            "rebuild or report the input case authority inside Creator E2E."
        ),
        details={
            **exc.details,
            "repair_owner": "creator_e2e",
            "repair_target": "creator_e2e",
            "skill_repair_allowed": False,
        },
    )


@dataclass(frozen=True)
class E2EFileInputSpec:
    source_name: str
    runtime_shape: str
    allowed_formats: tuple[str, ...] = ()
    min_items: int = 1
    max_items: int = 3
    homogeneous: bool | None = None
    format_source: str = "unknown"


def _format_e2e_failure(failure: E2EFailure) -> str:
    return (
        f"E2E_SYMPTOM_FILE={failure.target_file}\n"
        # Legacy consumers may parse this field, but it is never used as a
        # confirmed target by the debug loop.
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


def _e2e_value_hash(value: Any) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        raw = str(value)
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def _preview_value(value: Any, *, max_string: int = 200, max_items: int = 5) -> Any:
    if isinstance(value, str):
        if len(value) <= max_string or Path(value).is_absolute():
            return value
        return {"shape": _json_shape(value), "value_preview": value[:max_string], "value_hash": _e2e_value_hash(value)}
    if isinstance(value, list):
        return {"shape": _json_shape(value), "items_preview": [_preview_value(v) for v in value[:max_items]], "value_hash": _e2e_value_hash(value)}
    if isinstance(value, dict):
        return {"shape": _json_shape(value), "items_preview": {str(k): _preview_value(v) for k, v in list(value.items())[:max_items]}, "value_hash": _e2e_value_hash(value)}
    return value


def _preview_object(obj: Mapping[str, Any] | None) -> dict[str, Any]:
    return {str(k): _preview_value(v) for k, v in (obj or {}).items()}


def _provenance_record(*, step: int, script: str, source_kind: str, value: Any) -> dict[str, Any]:
    return {
        "producer_step": step,
        "producer_script": script,
        "source_kind": source_kind,
        "value_shape": _json_shape(value),
        "value_preview": _preview_value(value),
        "value_hash": _e2e_value_hash(value),
    }


def _runtime_binding_trace(
    *,
    command: E2EWorkflowCommand,
    payload: dict[str, Any],
    rendered_payload: dict[str, Any],
    value_provenance: dict[str, Any],
) -> dict[str, Any]:
    trace: dict[str, Any] = {}
    for target_key, template_value in (command.argv_template or {}).items():
        target = str(target_key)
        expr = _whole_e2e_placeholder_expr(template_value)
        if not expr:
            trace[target] = {
                "placeholder_expr": "",
                "source_root": "",
                "source_value_preview": None,
                "source_provenance": {},
                "rendered_value_preview": _preview_value(rendered_payload.get(target)),
            }
            continue
        source_root = _placeholder_root(expr)
        trace[target] = {
            "placeholder_expr": expr,
            "source_root": source_root,
            "source_value_preview": _preview_value(payload.get(source_root)),
            "source_provenance": value_provenance.get(source_root, {}),
            "rendered_value_preview": _preview_value(rendered_payload.get(target)),
        }
    return trace


def _e2e_runtime_boundary_facts(
    *,
    command: E2EWorkflowCommand,
    payload: dict[str, Any],
    rendered_payload: dict[str, Any],
    script_content: str,
    runtime_binding_trace: dict[str, Any],
) -> dict[str, Any]:

    try:
        schema = (
            extract_python_strict_argv_schema(
                script_content
            )
            if command.script_path.endswith(".py")
            else {}
        )
    except Exception:
        schema = {}

    return {
        "source_payload_shape":
            _json_object_shape(payload),

        "rendered_payload_shape":
            _json_object_shape(
                rendered_payload
            ),

        "runtime_binding_trace":
            runtime_binding_trace,

        "script_argv_schema":
            schema,

        "command_argv_template":
            command.argv_template,
    }


_E2E_RUNTIME_INPUT_LITERAL_RE = re.compile(r"^__RUNTIME_INPUT(?:_[A-Z]+)*(?:_\d+)?__$")


def _validate_e2e_input_fixtures(
    *,
    command: E2EWorkflowCommand,
    payload: dict[str, Any],
    rendered_payload: dict[str, Any],
    typed_input_specs: list[E2ETypedInputSpec],
) -> None:
    """Reject malformed synthetic file fixtures before any business script runs."""
    specs = {spec.name: spec for spec in typed_input_specs}
    used_roots = {
        _placeholder_root(expr)
        for expr in _placeholder_exprs_from_value(command.argv_template)
    }
    issues: list[dict[str, Any]] = []

    def file_value_issues(value: Any, *, name: str, location: str) -> list[dict[str, Any]]:
        values = value if isinstance(value, list) else [value]
        found: list[dict[str, Any]] = []
        for index, path in enumerate(values):
            reason = ""
            if not isinstance(path, str):
                reason = "file_fixture_not_materialized_to_path"
            elif _E2E_RUNTIME_INPUT_LITERAL_RE.fullmatch(path.strip()):
                reason = "runtime_input_sentinel_in_trial_fixture"
            elif not Path(path).is_file():
                reason = "fixture_path_missing"
            if reason:
                found.append({"name": name, "location": location, "index": index,
                              "actual_shape": _json_shape(path), "reason": reason})
        return found

    for root in sorted(used_roots):
        spec = specs.get(root)
        shape = _canonical_e2e_shape(spec.shape if spec else "")
        if shape not in {"file_path", "list[file_path]"}:
            continue
        value = payload.get(root)
        paths = value if shape == "list[file_path]" and isinstance(value, list) else [value]
        if shape == "list[file_path]" and (not isinstance(value, list) or not value):
            issues.append({"name": root, "expected_shape": shape, "actual_shape": _json_shape(value)})
            continue
        issues.extend(file_value_issues(paths, name=root, location="source_payload"))

    for argv_key, template_value in command.argv_template.items():
        roots = {_placeholder_root(expr) for expr in _placeholder_exprs_from_value(template_value)}
        file_roots = [root for root in roots if _canonical_e2e_shape(specs.get(root).shape if specs.get(root) else "") in {"file_path", "list[file_path]"}]
        if file_roots:
            issues.extend(file_value_issues(rendered_payload.get(str(argv_key)), name=file_roots[0], location=f"rendered_argv.{argv_key}"))

    def find_sentinels(value: Any, location: str = "rendered_argv") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                find_sentinels(child, f"{location}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                find_sentinels(child, f"{location}[{index}]")
        elif isinstance(value, str) and _E2E_RUNTIME_INPUT_LITERAL_RE.fullmatch(value.strip()):
            issues.append({"location": location, "reason": "runtime_input_sentinel_in_rendered_argv"})
    find_sentinels(rendered_payload)

    if issues:
        raise ValueError(_e2e_error(
            target="Creator E2E input fixture",
            layer="e2e_input_fixture",
            message=(
                "trial_fixture_invalid: E2E synthetic input failed deterministic pre-execution validation.\n"
                f"issues={json.dumps(issues, ensure_ascii=False, sort_keys=True)}\n"
                "This is an input-construction failure; do not diagnose or repair the Skill script."
            ),
        ))

def _verified_bindings_from_runtime_trace(
    *,
    runtime_binding_trace: dict[str, Any],
    script_content: str,
    script_path: str,
) -> dict[str, str]:
    schema: dict[str, Any] = {}
    run_analysis: dict[str, Any] = {}
    if script_path.endswith(".py"):
        try:
            schema = extract_python_strict_argv_schema(script_content)
        except Exception:
            schema = {}
        try:
            run_analysis = _python_run_args_analysis(script_content)
        except Exception:
            run_analysis = {}
    accepted = set(str(k) for k in (schema.get("allowed_keys") or []) if str(k or "").strip())
    accepted.update(str(k) for k in (run_analysis.get("required_read_keys") or []) if str(k or "").strip())
    accepted.update(str(k) for k in (run_analysis.get("optional_read_keys") or []) if str(k or "").strip())
    verified: dict[str, str] = {}
    for target, item in (runtime_binding_trace or {}).items():
        if not isinstance(item, dict):
            continue
        source_root = str(item.get("source_root") or "").strip()
        source_provenance = item.get("source_provenance") if isinstance(item.get("source_provenance"), dict) else {}
        source_kind = str(source_provenance.get("source_kind") or "")
        source_is_verifiable = source_kind in {"external_context", "stdout"}
        if (
            source_root
            and str(item.get("placeholder_expr") or "").strip()
            and source_is_verifiable
            and str(target) in accepted
        ):
            verified[str(target)] = source_root
    return verified


def snapshot_runtime_files(root: Path) -> dict[str, dict[str, Any]]:
    """Take a lightweight file snapshot for the E2E runtime workspace."""
    base = root.resolve()
    out: dict[str, dict[str, Any]] = {}
    for path in base.rglob("*"):
        if not path.is_file() or any(part in {".venv", "__pycache__", ".pytest_cache", ".git", "node_modules"} for part in path.parts):
            continue
        try:
            stat = path.stat()
            rel = path.relative_to(base).as_posix()
        except Exception:
            continue
        out[rel] = {
            "relative_path": rel,
            "absolute_path": str(path.resolve()),
            "suffix": path.suffix,
            "size": stat.st_size,
            "modified_ns": stat.st_mtime_ns,
        }
    return out


def diff_runtime_files(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    created = [after[k] for k in sorted(set(after) - set(before))]
    deleted = [before[k] for k in sorted(set(before) - set(after))]
    modified = [after[k] for k in sorted(set(before) & set(after)) if before[k].get("size") != after[k].get("size") or before[k].get("modified_ns") != after[k].get("modified_ns")]
    return {"created_files": created, "modified_files": modified, "deleted_files": deleted}


def resolve_reported_artifact_paths(paths: Iterable[str], *, root: Path) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    for raw in paths or []:
        text = str(raw or "").strip()
        if not text:
            continue
        path = Path(text)
        absolute = path if path.is_absolute() else (root / path)
        resolved.append({"raw_path": text, "absolute_path": str(absolute.resolve()), "exists": absolute.exists()})
    return resolved


def _artifact_failure_code(filesystem_trace: dict[str, Any]) -> str:
    resolved = filesystem_trace.get("resolved_reported_paths") or []
    created = filesystem_trace.get("created_files") or []
    if resolved and any(not item.get("exists") for item in resolved):
        return "artifact_return_path_mismatch" if created else "artifact_return_path_missing"
    if not created and not resolved:
        return "artifact_not_created"
    return "artifact_validation_failed"


def _is_artifact_validation_failure(
    *,
    error: Exception | str,
    reported_paths: list[str],
    entry: SkillPlanEntry,
) -> bool:
    error_text = str(error or "").lower()
    validator_says_artifact = any(
        token in error_text
        for token in (
            "artifact",
            "file output",
            "file_outputs",
            "文件产物",
            "产物路径",
            "路径不存在",
        )
    )
    artifact_contract = (
        getattr(entry, "artifact_contract", {})
        if isinstance(getattr(entry, "artifact_contract", {}), dict)
        else {}
    )
    declared_artifact_fields = (
        artifact_contract.get("artifact_fields")
        or artifact_contract.get("file_outputs")
        or artifact_contract.get("path_fields")
        or []
    )
    return bool(reported_paths or validator_says_artifact or declared_artifact_fields)


_ARTIFACT_FAILURE_PROGRESS = {
    "artifact_not_created": 0,
    "artifact_return_path_missing": 1,
    "artifact_return_path_mismatch": 2,
    "artifact_type_mismatch": 3,
    "artifact_validation_failed": 3,
}


def _artifact_runtime_state(filesystem_trace: dict[str, Any] | None) -> dict[str, Any]:
    trace = filesystem_trace or {}
    resolved = trace.get("resolved_reported_paths") or []
    return {
        "created_count": len(trace.get("created_files") or []),
        "modified_count": len(trace.get("modified_files") or []),
        "reported_count": len(trace.get("reported_paths") or []),
        "existing_reported_count": sum(1 for item in resolved if item.get("exists")),
        "missing_reported_count": sum(1 for item in resolved if not item.get("exists")),
        "failure_code": str(trace.get("failure_code") or trace.get("artifact_failure_code") or ""),
    }


def _artifact_runtime_state_improved(old_state: dict[str, Any], new_state: dict[str, Any]) -> bool:
    if not old_state and not new_state:
        return False
    if int(old_state.get("created_count") or 0) == 0 and int(new_state.get("created_count") or 0) > 0:
        return True
    if int(old_state.get("existing_reported_count") or 0) == 0 and int(new_state.get("existing_reported_count") or 0) > 0:
        return True
    if int(new_state.get("missing_reported_count") or 0) < int(old_state.get("missing_reported_count") or 0):
        return True
    old_rank = _ARTIFACT_FAILURE_PROGRESS.get(str(old_state.get("failure_code") or ""), -1)
    new_rank = _ARTIFACT_FAILURE_PROGRESS.get(str(new_state.get("failure_code") or ""), -1)
    return new_rank > old_rank >= 0


def _failure_code_from_structured(structured: dict[str, Any] | None) -> str:
    details = structured.get("details") if isinstance(structured, dict) else {}
    return str((details or {}).get("failure_code") or "")

def _deterministic_repair_authority(
    structured_failure: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return a validator-owned repair decision when one exists.

    A deterministic repair authority means that runtime comparison has
    already established the violated boundary and its owner.

    Repair models may change implementation toward this authority, but
    may not reinterpret or replace the authority itself.
    """

    if not isinstance(structured_failure, dict):
        return {}

    details = structured_failure.get("details")

    if not isinstance(details, dict):
        return {}

    authority = details.get("repair_authority")

    if not isinstance(authority, dict):
        return {}

    if authority.get("mode") != "deterministic":
        return {}

    target_file = str(
        authority.get("target_file") or ""
    ).strip()

    if not target_file:
        return {}

    expected = authority.get("expected")
    observed = authority.get("observed")

    # A deterministic authority must contain an actual comparison.
    # Otherwise it is only a hint and must not bypass diagnosis.
    if expected is None or observed is None:
        return {}

    return dict(authority)

def _allowed_edit_scope_for_failure(
    *,
    target_path: str,
    failure_code: str,
    failure_layer: str,
    is_artifact_failure: bool,
    created_count: int,
    missing_reported_count: int,
) -> list[str]:
    if is_artifact_failure and created_count > 0 and missing_reported_count > 0:
        return ["stdout artifact path mapping", "relative path normalization"]
    if is_artifact_failure and created_count == 0:
        return ["artifact creation", "artifact save path", "stdout artifact return"]
    if target_path == "SKILL.md":
        return ["SKILL.md current failed command line"]
    if failure_code in {"argv_schema_error", "argv_guard"}:
        return ["current script strict_json_argv_guard/run entry alignment"]
    if failure_code in {"script_exit", "timeout"} or failure_layer in {"script_exit", "timeout"}:
        return ["traceback directly involved source region"]
    if failure_code in {"stdout_contract", "stdout_json_parse", "stdout_json_type"}:
        return ["current script stdout serialization and return logic"]
    return ["current failure directly involved source region"]


def _format_json_shape(obj: dict[str, Any]) -> str:
    if not obj:
        return "{}"
    shape = _json_object_shape(obj)
    return json.dumps(shape, ensure_ascii=False, sort_keys=True)

_SANDBOX_OUTPUT_CONTRACT = build_platform_io_contract()
_SANDBOX_TERMINAL_OUTPUT_KEYS = {
    sink["name"] for sink in normalize_platform_output_sinks(_SANDBOX_OUTPUT_CONTRACT)
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
    sink = get_platform_output_sink(_SANDBOX_OUTPUT_CONTRACT, key)
    return json.dumps(sink["value_schema"], ensure_ascii=False, sort_keys=True) if sink else "platform terminal field"

def _terminal_runtime_contract_violation(
    *,
    terminal_edges: list[dict[str, Any]],
    completed_outputs: dict[str, dict[str, Any]],
    platform_contract: dict[str, Any],
) -> dict[str, Any] | None:
    """Locate a terminal producer that violated its frozen runtime contract.

    This compares frozen terminal edges against observed producer stdout.
    Runtime exception names / stderr text are not used to determine ownership.
    """

    for edge in terminal_edges:
        producer = str(edge.get("from_node") or "").strip()
        output_name = str(edge.get("from_output") or "").strip()
        sink_name = str(edge.get("to_input") or "").strip()

        if not producer or not output_name or not sink_name:
            continue

        observed_outputs = completed_outputs.get(producer)

        # If there is no observed producer stdout at all, there is not enough
        # runtime evidence here to blame the producer. Leave it to upstream
        # interface/graph handling.
        if not isinstance(observed_outputs, dict):
            continue

        # The producer ran successfully and emitted stdout, but failed to emit
        # the output port required by the frozen terminal edge.
        if output_name not in observed_outputs:
            return {
                "target_file": producer,
                "output_name": output_name,
                "sink_name": sink_name,
                "expected": {
                    "output_present": True,
                },
                "observed": {
                    "output_present": False,
                    "stdout_keys": sorted(
                        str(key)
                        for key in observed_outputs.keys()
                    ),
                },
            }

        sink = get_platform_output_sink(
            platform_contract,
            sink_name,
        )

        # Invalid / missing sink definitions belong to the frozen interface
        # side, not to the producing script.
        if not isinstance(sink, dict):
            continue

        expected_schema = sink.get("value_schema")

        if not isinstance(expected_schema, dict):
            continue

        observed_value = observed_outputs[output_name]

        if value_matches_platform_schema(
            observed_value,
            expected_schema,
        ):
            continue

        # The declared output exists, but its real runtime representation does
        # not satisfy the frozen downstream sink contract.
        return {
            "target_file": producer,
            "output_name": output_name,
            "sink_name": sink_name,
            "expected": {
                "output_present": True,
                "value_schema": dict(expected_schema),
            },
            "observed": {
                "output_present": True,
                "value_shape": _json_shape(observed_value),
            },
        }

    return None


def _portable_terminal_runtime_contract_violation(
    *, mappings: list[dict[str, str]], completed_outputs: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Attribute only missing artifact-declared stdout ports to a producer."""
    for binding in mappings:
        producer = binding["script"]
        output_name = binding["source"]
        observed = completed_outputs.get(producer)
        if isinstance(observed, dict) and output_name not in observed:
            return {
                "target_file": producer,
                "output_name": output_name,
                "sink_name": binding["target"],
                "expected": {"output_present": True},
                "observed": {
                    "output_present": False,
                    "stdout_keys": sorted(str(key) for key in observed),
                },
            }
    return None

def _valid_terminal_output_value(key: str, value: Any) -> bool:
    sink = get_platform_output_sink(_SANDBOX_OUTPUT_CONTRACT, key)
    return bool(sink and value_matches_platform_schema(value, sink["value_schema"]))


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
    parsed = parse_placeholder_expr(expr)
    return parsed.root if parsed else ""


def _normalize_e2e_placeholder_expr(expr: str) -> str:
    parsed = parse_placeholder_expr(expr)
    return parsed.dotted if parsed else str(expr or "").strip()

def _canonical_e2e_shape(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    text = text.replace("array", "list").replace("path", "file_path")
    if not text:
        return ""
    list_match = re.fullmatch(r"list\s*\[\s*([^\]]+)\s*\]", text)
    if list_match:
        item_shape = _canonical_e2e_shape(list_match.group(1))
        if item_shape in {"string", "number", "integer", "boolean", "object", "file_path"}:
            return f"list[{item_shape}]"
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

def _resolve_e2e_runtime_shape(spec: E2ETypedInputSpec) -> str:
    """
    Resolve runtime input shape.

    Higher confidence sources should override fallback inference.
    """

    candidates = [
        getattr(spec, "argv_schema_shape", None),
        getattr(spec, "platform_io_shape", None),
        getattr(spec, "graph_declared_shape", None),
        getattr(spec, "skill_plan_declared_shape", None),
        spec.shape,
    ]

    for value in candidates:
        if value:
            normalized = _canonical_e2e_shape(value)

            if normalized and normalized != "string":
                return normalized

    return _canonical_e2e_shape(spec.shape or "string")


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
    """Merge identity/provenance independently from runtime type evidence."""
    if not spec.name:
        return
    # Placeholder projections are constraints of their root external input,
    # never additional platform inputs.
    root = _placeholder_root(_normalize_e2e_placeholder_expr(spec.name))
    nested_path = _normalize_e2e_placeholder_expr(spec.name)[len(root):].lstrip(".") if root else ""
    if root and root != spec.name:
        spec = replace(spec, name=root, required_paths=tuple(sorted(set(spec.required_paths + (nested_path,)))))
    provenance_priority = {
        "requirement_graph": 5, "skill_plan_entry": 4,
        "platform_io_contract": 3, "argv_schema": 2, "placeholder": 1,
    }
    shape_priority = {
        "platform_io_contract": 5, "argv_schema": 4,
        "requirement_graph": 3, "skill_plan_entry": 2, "placeholder": 1,
    }
    # Requiredness is an independent contract dimension.  In particular, a
    # RequirementGraph mention must not turn a strict-argv optional key back
    # into a required runtime root.
    required_priority = {
        "argv_schema": 5, "platform_io_contract": 4,
        "requirement_graph": 3, "skill_plan_entry": 2, "placeholder": 1,
    }
    old = specs.get(spec.name)
    if old is None:
        specs[spec.name] = replace(
            spec,
            provenance_source=spec.provenance_source or spec.source,
            shape_source=(spec.shape_source or spec.source) if spec.shape else "",
            argv_schema_shape=spec.shape if spec.source == "argv_schema" else spec.argv_schema_shape,
            platform_io_shape=spec.shape if spec.source == "platform_io_contract" else spec.platform_io_shape,
            graph_declared_shape=spec.shape if spec.source == "requirement_graph" else spec.graph_declared_shape,
            skill_plan_declared_shape=spec.shape if spec.source == "skill_plan_entry" else spec.skill_plan_declared_shape,
            required_source=spec.required_source or spec.source,
        )
        return

    old_provenance = old.provenance_source or old.source
    new_provenance = spec.provenance_source or spec.source
    provenance_source = (
        new_provenance
        if provenance_priority.get(new_provenance, 0) > provenance_priority.get(old_provenance, 0)
        else old_provenance
    )
    old_shape_source = old.shape_source or (old.source if old.shape else "")
    new_shape_source = spec.shape_source or (spec.source if spec.shape else "")
    use_new_shape = bool(spec.shape) and (
        not old.shape
        or shape_priority.get(new_shape_source, 0) > shape_priority.get(old_shape_source, 0)
    )
    old_required_source = old.required_source or old.source
    new_required_source = spec.required_source or spec.source
    use_new_required = required_priority.get(new_required_source, 0) > required_priority.get(old_required_source, 0)
    specs[spec.name] = replace(
        old,
        shape=spec.shape if use_new_shape else old.shape,
        item_shape=spec.item_shape if use_new_shape else old.item_shape,
        source=new_shape_source if use_new_shape else old.source,
        shape_source=new_shape_source if use_new_shape else old_shape_source,
        provenance_source=provenance_source,
        target_file=old.target_file or spec.target_file,
        confidence=spec.confidence if use_new_shape else old.confidence,
        properties=old.properties or spec.properties,
        required=spec.required if use_new_required else old.required,
        required_source=new_required_source if use_new_required else old_required_source,
        nullable=old.nullable or spec.nullable,
        default_available=old.default_available or spec.default_available,
        default_value=old.default_value if old.default_available else spec.default_value,
        required_paths=tuple(sorted(set(old.required_paths + spec.required_paths))),
        optional_paths=tuple(sorted(set(old.optional_paths + spec.optional_paths))),
        consumed_paths=tuple(sorted(set(old.consumed_paths + spec.consumed_paths))),
        min_items=max(old.min_items, spec.min_items),
        max_items=old.max_items if old.max_items is not None else spec.max_items,
        argv_schema_shape=(spec.shape if spec.source == "argv_schema" else "") or old.argv_schema_shape or spec.argv_schema_shape,
        platform_io_shape=(spec.shape if spec.source == "platform_io_contract" else "") or old.platform_io_shape or spec.platform_io_shape,
        graph_declared_shape=(spec.shape if spec.source == "requirement_graph" else "") or old.graph_declared_shape or spec.graph_declared_shape,
        skill_plan_declared_shape=(spec.shape if spec.source == "skill_plan_entry" else "") or old.skill_plan_declared_shape or spec.skill_plan_declared_shape,
    )

def _platform_runtime_file_input_names() -> set[str]:
    """Return platform input roots whose values are runtime file collections.

    This is derived from the canonical Platform IO Contract rather than
    business keywords or Skill-specific field-name heuristics.
    """
    contract = build_platform_io_contract()
    boundary = contract.get("platform_skill_boundary") or {}
    source_semantics = boundary.get("input_source_semantics") or {}
    runtime_files = source_semantics.get("runtime_files") or {}

    names: set[str] = set()

    canonical = str(runtime_files.get("canonical") or "").strip()
    if canonical:
        names.add(canonical)

    for value in runtime_files.get("representations") or []:
        name = str(value or "").strip()
        if name:
            names.add(name)

    return names

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
    # Trial Cases may populate only values that enter the workflow from the
    # platform boundary. A port produced by any workflow member is internal
    # dataflow (or a terminal output), even when a downstream argv schema and
    # RequirementItem also describe it as that member's input.
    command_order = {
        command.script_path: index
        for index, command in enumerate(commands)
    }
    produced_at: dict[str, int] = {}
    for target_file, entry in (skill_plan_entries or {}).items():
        for raw in getattr(entry, "outputs", []) or []:
            name, _shape = _parse_typed_name(raw)
            root = _placeholder_root(_normalize_e2e_placeholder_expr(name))
            if root:
                produced_at[root] = min(
                    produced_at.get(root, len(commands)),
                    command_order.get(target_file, len(commands)),
                )

    consumed_at: dict[str, int] = {}
    for index, command in enumerate(commands):
        for expr in _placeholder_exprs_from_value(command.argv_template):
            root = _placeholder_root(_normalize_e2e_placeholder_expr(expr))
            if root:
                consumed_at[root] = min(consumed_at.get(root, len(commands)), index)

    internal_roots = {
        name
        for name, producer_index in produced_at.items()
        if producer_index < consumed_at.get(name, len(commands))
    }

    for target_file, reqs in (requirements_by_file or {}).items():
        for req in reqs or []:
            for raw in getattr(req, "inputs", []) or []:
                name, shape = _parse_typed_name(raw)

                if name:
                    _put_typed_spec(
                        specs,
                        E2ETypedInputSpec(
                            name=name,
                            shape=shape,
                            item_shape=_shape_item_shape(shape),
                            required=True,
                            source="requirement_graph",
                            target_file=target_file,
                            confidence="high",
                        ),
                    )

    for target_file, entry in (skill_plan_entries or {}).items():
        for raw in getattr(entry, "inputs", []) or []:
            name, shape = _parse_typed_name(raw)

            if name:
                _put_typed_spec(
                    specs,
                    E2ETypedInputSpec(
                        name=name,
                        shape=shape,
                        item_shape=_shape_item_shape(shape),
                        required=True,
                        source="skill_plan_entry",
                        target_file=target_file,
                        confidence="medium",
                    ),
                )
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
                required_keys = set(schema.get("required_keys") or []) if isinstance(schema, dict) else set()
                if isinstance(expected_types, dict):
                    for name, raw_shape in expected_types.items():
                        shape = _canonical_e2e_shape(raw_shape)
                        command_expected_types[str(name)] = shape
                        _put_typed_spec(specs, E2ETypedInputSpec(name=str(name), shape=shape, item_shape=_shape_item_shape(shape), required=str(name) in required_keys, source="argv_schema", target_file=command.script_path, confidence="high"))

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

            # placeholder 只能补充未知输入
            # 不允许覆盖 requirement_graph / contract 已定义的类型
            if root and root not in specs:
                shape = "list" if _e2e_placeholder_uses_index(normalized) else "string"

                _put_typed_spec(
                    specs,
                    E2ETypedInputSpec(
                        name=root,
                        shape=shape,
                        item_shape=_shape_item_shape(shape),
                        required=True,
                        source="placeholder",
                        target_file=command.script_path,
                        confidence="low",
                    ),
                )
            if root:
                path = normalized[len(root):].lstrip(".")
                indexes = [int(value) for value in re.findall(r"(?:^|\.)(\d+)(?:\.|$)", normalized)]
                current = specs.get(root)
                if current and path:
                    _put_typed_spec(specs, replace(
                        current,
                        required_paths=tuple(sorted(set(current.required_paths + (path,)))),
                        consumed_paths=tuple(sorted(set(current.consumed_paths + (path,)))),
                        min_items=max(current.min_items, (max(indexes) + 1) if indexes else 0),
                    ))

    runtime_file_names = _platform_runtime_file_input_names()

    used_roots = {
        _placeholder_root(expr)
        for command in commands
        for expr in _placeholder_exprs_from_value(
            command.argv_template
        )
    }

    for name in runtime_file_names & used_roots:
        existing = specs.get(name)
        _put_typed_spec(
            specs,
            E2ETypedInputSpec(
                name=name,
                shape="list[file_path]",
                item_shape="file_path",
                required=existing.required if existing else True,
                source="platform_io_contract",
                target_file=existing.target_file if existing else "",
                confidence="high",
                properties=existing.properties if existing else {},
            ),
        )

    external_specs = {
        name: spec
        for name, spec in specs.items()
        if name not in internal_roots
    }

    for spec in external_specs.values():
        logger.info(
            "[Creator][E2E][typed_input_resolution] %s",
            json.dumps({
                "name": spec.name,
                "resolved_shape": spec.shape or "unknown",
                "shape_source": spec.shape_source or "unknown",
                "provenance_source": spec.provenance_source or "unknown",
                "target_file": spec.target_file,
                "argv_schema_shape": spec.argv_schema_shape,
                "platform_io_shape": spec.platform_io_shape,
                "graph_declared_shape": spec.graph_declared_shape,
                "skill_plan_declared_shape": spec.skill_plan_declared_shape,
            }, ensure_ascii=False, sort_keys=True),
        )

    if internal_roots & set(specs):
        logger.info(
            "[Creator][E2E][internal_dataflow_inputs_excluded] names=%s",
            sorted(internal_roots & set(specs)),
        )

    return list(external_specs.values())


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

    This is a legacy fallback only.  It deliberately excludes whole SKILL.md
    and script bodies: output artifact descriptions (for example report.pdf)
    are not evidence about an input fixture's format.
    """
    return []


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


@dataclass(frozen=True)
class E2EFileFixtureHandler:
    canonical_format: str
    aliases: tuple[str, ...]
    extension: str
    content_kind: str
    fixture_schema: Any
    validator: Any
    materializer: Any
    semantic_names: tuple[str, ...] = ()
    media_types: tuple[str, ...] = ()


def _text_fixture_schema(fmt: str, content_kind: str) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False,
            "required": ["format", "content_kind", "text"],
            "properties": {"format": {"const": fmt}, "content_kind": {"const": content_kind},
                           "text": {"type": "string", "minLength": 1}}}


def _image_fixture_schema(fmt: str, _kind: str) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False,
            "required": ["format", "content_kind", "width", "height", "mode"],
            "properties": {"format": {"const": fmt}, "content_kind": {"const": "image"},
                           "width": {"type": "integer", "minimum": 1, "maximum": 512},
                           "height": {"type": "integer", "minimum": 1, "maximum": 512},
                           "mode": {"type": "string", "enum": ["RGB", "RGBA", "L"]},
                           "background": {"type": "string"}}}


def _csv_fixture_schema(fmt: str, _kind: str) -> dict[str, Any]:
    scalar = {"type": ["string", "number", "integer", "boolean", "null"]}
    column = {"type": "object", "additionalProperties": False,
              "required": ["name", "type", "nullable"],
              "properties": {"name": {"type": "string", "minLength": 1},
                             "type": {"type": "string", "enum": ["string", "number", "integer", "boolean"]},
                             "nullable": {"type": "boolean"}}}
    return {"type": "object", "additionalProperties": False,
            "required": ["format", "content_kind", "columns", "rows"],
            "properties": {"format": {"const": fmt}, "content_kind": {"const": "tabular"},
                           "columns": {"type": "array", "minItems": 1, "items": column},
                           "rows": {"type": "array", "minItems": 1,
                                    "items": {"type": "object", "additionalProperties": scalar}}}}


def _json_fixture_schema(fmt: str, _kind: str) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False,
            "required": ["format", "content_kind", "value"],
            "properties": {"format": {"const": fmt}, "content_kind": {"const": "json"}, "value": {}}}


def _validate_text_spec(spec: dict[str, Any]) -> bool:
    return isinstance(spec.get("text"), str) and bool(spec["text"].strip())


def _validate_image_spec(spec: dict[str, Any]) -> bool:
    return (spec.get("content_kind") == "image" and isinstance(spec.get("width"), int)
            and isinstance(spec.get("height"), int) and 1 <= spec["width"] <= 512
            and 1 <= spec["height"] <= 512 and spec.get("mode") in {"RGB", "RGBA", "L"})


def _validate_csv_spec(spec: dict[str, Any]) -> bool:
    columns, rows = spec.get("columns"), spec.get("rows")
    if spec.get("content_kind") != "tabular" or not isinstance(columns, list) or not columns or not isinstance(rows, list) or not rows:
        return False
    names = [column.get("name") for column in columns if isinstance(column, dict)]
    if not (len(names) == len(columns) and all(isinstance(name, str) and name for name in names)
            and len(set(names)) == len(names) and all(isinstance(row, dict) and set(row).issubset(names) for row in rows)):
        return False
    types = {column["name"]: column.get("type") for column in columns}
    nullable = {column["name"]: column.get("nullable") is True for column in columns}
    if any(value_type not in {"string", "number", "integer", "boolean"} for value_type in types.values()):
        return False
    for row in rows:
        for name, value_type in types.items():
            value = row.get(name)
            if value is None:
                if not nullable[name]:
                    return False
            elif value_type == "string" and not isinstance(value, str):
                return False
            elif value_type == "boolean" and not isinstance(value, bool):
                return False
            elif value_type == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
                return False
            elif value_type == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
                return False
    return True


def _validate_json_spec(spec: dict[str, Any]) -> bool:
    try:
        json.dumps(spec["value"], allow_nan=False)
    except (KeyError, TypeError, ValueError):
        return False
    return spec.get("content_kind") == "json"


def _materialize_text_fixture(spec: dict[str, Any], path: Path) -> None:
    path.write_text(spec["text"], encoding="utf-8")
    if not path.read_text(encoding="utf-8").strip():
        raise ValueError("empty text fixture")


def _materialize_html_fixture(spec: dict[str, Any], path: Path) -> None:
    text = spec["text"]
    if "<html" not in text.lower() and "<!doctype html" not in text.lower():
        text = ("<!doctype html><html><head><meta charset=\"utf-8\"><title>E2E Fixture</title>"
                f"</head><body><p>{text}</p></body></html>")
    path.write_text(text, encoding="utf-8")
    rendered = path.read_text(encoding="utf-8").lower()
    if "<html" not in rendered and "<!doctype html" not in rendered:
        raise ValueError("invalid HTML fixture")


def _materialize_csv_fixture(spec: dict[str, Any], path: Path) -> None:
    names = [column["name"] for column in spec["columns"]]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=names, extrasaction="raise")
        writer.writeheader()
        for row in spec["rows"]:
            writer.writerow({name: ("true" if value is True else "false" if value is False else "" if value is None else value) for name, value in row.items()})
    with path.open(encoding="utf-8", newline="") as handle:
        assert next(csv.DictReader(handle), None) is not None


def _materialize_json_fixture(spec: dict[str, Any], path: Path) -> None:
    path.write_text(json.dumps(spec["value"], ensure_ascii=False, sort_keys=True), encoding="utf-8")
    json.loads(path.read_text(encoding="utf-8"))


def _materialize_image_fixture(spec: dict[str, Any], path: Path, *, pillow_format: str) -> None:
    from PIL import Image
    mode = spec["mode"]
    if pillow_format == "JPEG" and mode == "RGBA":
        mode = "RGB"
    color = spec.get("background") or ("white" if mode != "RGBA" else "transparent")
    image = Image.new(mode, (spec["width"], spec["height"]), color)
    image.save(path, format=pillow_format)
    with Image.open(path) as check:
        check.verify()


def _document_materializer(writer: Any) -> Any:
    def materialize(spec: dict[str, Any], path: Path) -> None:
        writer(path, spec["text"])
        if writer is _write_minimal_pdf:
            if not path.read_bytes().startswith(b"%PDF-"):
                raise ValueError("invalid PDF fixture")
        else:
            import zipfile
            with zipfile.ZipFile(path) as archive:
                if "word/document.xml" not in archive.namelist():
                    raise ValueError("invalid DOCX fixture")
    return materialize


def _image_materializer(fmt: str) -> Any:
    return lambda spec, path: _materialize_image_fixture(spec, path, pillow_format=fmt)


FILE_FIXTURE_FORMATS: dict[str, E2EFileFixtureHandler] = {}
MAX_E2E_CASE_REPAIR_ATTEMPTS = 2
MAX_E2E_CASE_BUILD_ATTEMPTS = 2


def _register_file_fixture_handler(handler: E2EFileFixtureHandler) -> None:
    for name in (handler.canonical_format, *handler.aliases):
        FILE_FIXTURE_FORMATS[name] = handler


def _register_builtin_file_fixture_handlers() -> None:
    text_handlers = [
        ("txt", (), ".txt", _materialize_text_fixture, (), ("text/plain",)),
        ("md", ("markdown",), ".md", _materialize_text_fixture, ("markdown",), ("text/markdown",)),
        ("html", ("htm",), ".html", _materialize_html_fixture, (), ("text/html",)),
    ]
    for fmt, aliases, extension, materializer, semantic_names, media_types in text_handlers:
        _register_file_fixture_handler(E2EFileFixtureHandler(fmt, aliases, extension, "text", _text_fixture_schema, _validate_text_spec, materializer, semantic_names, media_types))
    _register_file_fixture_handler(E2EFileFixtureHandler("csv", (), ".csv", "tabular", _csv_fixture_schema, _validate_csv_spec, _materialize_csv_fixture, (), ("text/csv",)))
    _register_file_fixture_handler(E2EFileFixtureHandler("json", (), ".json", "json", _json_fixture_schema, _validate_json_spec, _materialize_json_fixture, (), ("application/json",)))
    for fmt, extension, writer, media_type in (("pdf", ".pdf", _write_minimal_pdf, "application/pdf"), ("docx", ".docx", _write_minimal_docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")):
        _register_file_fixture_handler(E2EFileFixtureHandler(fmt, (), extension, "document", _text_fixture_schema, _validate_text_spec, _document_materializer(writer), (), (media_type,)))
    for fmt, aliases, extension, pillow_fmt in (
        ("png", (), ".png", "PNG"), ("jpeg", ("jpg",), ".jpg", "JPEG"),
        ("tiff", ("tif",), ".tiff", "TIFF"), ("webp", (), ".webp", "WEBP"),
        ("bmp", (), ".bmp", "BMP"),
    ):
        _register_file_fixture_handler(E2EFileFixtureHandler(fmt, aliases, extension, "image", _image_fixture_schema, _validate_image_spec, _image_materializer(pillow_fmt), (), (f"image/{fmt}",)))


_register_builtin_file_fixture_handlers()


def _resolve_file_fixture_handler(fmt: str) -> E2EFileFixtureHandler | None:
    return FILE_FIXTURE_FORMATS.get(str(fmt or "").strip().lower())


def _canonical_file_fixture_format(fmt: str) -> str:
    handler = _resolve_file_fixture_handler(fmt)
    return handler.canonical_format if handler else ""


def _file_format_evidence_terms(fmt: str) -> tuple[str, ...]:
    """Derive explicit evidence identifiers from the fixture capability itself."""
    handler = _resolve_file_fixture_handler(fmt)
    if handler is None:
        return ()
    terms = (
        handler.canonical_format,
        *handler.aliases,
        handler.extension,
        *handler.semantic_names,
        *handler.media_types,
    )
    return tuple(dict.fromkeys(term.casefold() for term in terms if term))


def _requirement_quote_explicitly_names_format(quote: str, fmt: str) -> bool:
    """Accept only a finite, explicit format identifier from quoted evidence."""
    text = str(quote or "").casefold()
    for term in _file_format_evidence_terms(fmt):
        escaped = re.escape(term.casefold())
        if re.fullmatch(r"[a-z0-9]+", term, re.I):
            if re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", text):
                return True
        elif term.casefold() in text:
            return True
    return False


def _verified_model_file_format(
    item: dict[str, Any],
    *,
    requirements_by_id: dict[str, str],
) -> str:
    """Verify that a resolved model proposal cites literal, format-specific prose."""
    if item.get("decision") != "resolved":
        return ""
    selected_format = _canonical_file_fixture_format(str(item.get("format") or ""))
    evidence = item.get("evidence")
    if not selected_format or not isinstance(evidence, list) or not evidence:
        return ""
    for citation in evidence:
        if not isinstance(citation, dict):
            continue
        requirement_id = str(citation.get("requirement_id") or "")
        quote = str(citation.get("quote") or "").strip()
        source = requirements_by_id.get(requirement_id, "")
        if quote and quote.casefold() in source.casefold() and _requirement_quote_explicitly_names_format(quote, selected_format):
            return selected_format
    return ""


def _explicit_file_format_mentions(requirements: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    """Project literal format names into a finite set of grounded candidates."""
    mentions: dict[str, list[dict[str, str]]] = {}
    for requirement in requirements:
        requirement_id = str(requirement.get("id") or "")
        text = str(requirement.get("text") or "")
        canonical_formats = sorted({handler.canonical_format for handler in FILE_FIXTURE_FORMATS.values()})
        for fmt in canonical_formats:
            if not _requirement_quote_explicitly_names_format(text, fmt):
                continue
            matches = mentions.setdefault(fmt, [])
            evidence = {"requirement_id": requirement_id, "quote": text}
            if evidence not in matches:
                matches.append(evidence)
    return mentions


def _resolve_e2e_file_input_spec(
    typed_spec: E2ETypedInputSpec,
    requirements: list[RequirementItem],
) -> E2EFileInputSpec:
    """Resolve only explicit structured file constraints.

    Natural-language format selection belongs to the grounded Trial Case
    planner.  This resolver deliberately does not maintain a keyword-to-file
    format table.
    """
    declared_formats: list[str] = []
    for req in requirements:
        for constraint in req.constraints or []:
            constraint_label = " ".join([str(constraint.name or ""), str(constraint.kind or "")]).lower()
            if re.search(r"\b(?:output|result|artifact|export)\b|(?:输出|产物|导出)", constraint_label):
                continue
            raw = constraint.value
            if isinstance(raw, dict):
                raw = raw.get("allowed_formats") or raw.get("formats") or raw.get("format") or []
                values = raw if isinstance(raw, list) else [raw]
                for value in values:
                    canonical = _canonical_file_fixture_format(str(value))
                    if canonical and canonical not in declared_formats:
                        declared_formats.append(canonical)
    allowed = tuple(declared_formats)
    source = "requirement_constraint" if allowed else "unknown"
    shape = _canonical_e2e_shape(typed_spec.shape)
    minimum, maximum = (1, 1) if shape == "file_path" else (1, 3)
    result = E2EFileInputSpec(
        source_name=typed_spec.name, runtime_shape=shape, allowed_formats=allowed,
        min_items=minimum, max_items=maximum,
        homogeneous=True if len(allowed) == 1 else (False if len(allowed) > 1 else None),
        format_source=source,
    )
    logger.info("[Creator][E2E][file_input_resolution] %s", json.dumps({
        "name": result.source_name, "shape": result.runtime_shape,
        "allowed_formats": list(result.allowed_formats), "format_source": result.format_source,
        "cardinality": {"min": result.min_items, "max": result.max_items},
        "collection_format_policy": "homogeneous" if result.homogeneous else "mixed" if result.homogeneous is False else "unknown",
    }, ensure_ascii=False, sort_keys=True))
    return result


def _e2e_sample_suffix_for_kind(kind: str) -> str:
    handler = _resolve_file_fixture_handler(kind)
    return handler.extension if handler else ""


def _write_e2e_sample_file_by_kind(path: Path, *, kind: str, name: str, index: int) -> None:
    handler = _resolve_file_fixture_handler(kind)
    if handler is None:
        raise ValueError(f"unsupported E2E file fixture format: {kind}")
    sample_text = f"Creator E2E sample content for {name or 'input'} #{index}.\n"
    if handler.content_kind == "image":
        spec = {"format": handler.canonical_format, "content_kind": "image", "width": 64, "height": 64, "mode": "RGB"}
    elif handler.canonical_format == "csv":
        spec = {"format": "csv", "content_kind": "tabular",
                "columns": [{"name": "content", "type": "string", "nullable": False}],
                "rows": [{"content": sample_text.strip()}]}
    elif handler.canonical_format == "json":
        spec = {"format": "json", "content_kind": "json", "value": {"content": sample_text.strip()}}
    elif handler.canonical_format == "html":
        spec = {"format": "html", "content_kind": "text", "text": sample_text}
    else:
        spec = {"format": handler.canonical_format, "content_kind": handler.content_kind, "text": sample_text}
    handler.materializer(spec, path)


def _e2e_sample_path(
    skill_dir: Path | None,
    name: str,
    index: int = 1,
    *,
    kind: str | None = None,
    suffix: str | None = None,
) -> Path:
    base = (skill_dir / ".creator_e2e" / "samples") if skill_dir is not None else Path(tempfile.mkdtemp(prefix="creator-e2e-samples-"))
    base.mkdir(parents=True, exist_ok=True)

    sample_kind = str(kind or "").strip().lower()
    if not _resolve_file_fixture_handler(sample_kind):
        raise ValueError(f"unsupported E2E file fixture format: {sample_kind or 'unknown'}")
    sample_suffix = suffix or _e2e_sample_suffix_for_kind(sample_kind)

    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name or "input").strip("._") or "input"
    path = base / f"{safe}_{index}{sample_suffix}"

    return path


def _e2e_sample_file(
    skill_dir: Path | None,
    name: str,
    index: int = 1,
    *,
    kind: str | None = None,
    suffix: str | None = None,
) -> str:
    sample_kind = str(kind or "").strip().lower()
    path = _e2e_sample_path(skill_dir, name, index, kind=sample_kind, suffix=suffix)

    _write_e2e_sample_file_by_kind(path, kind=sample_kind, name=name, index=index)
    return str(path)


def _materialize_e2e_sample_value(
    spec: E2ETypedInputSpec,
    *,
    skill_dir: Path | None,
) -> Any:
    shape = _resolve_e2e_runtime_shape(spec)
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
        if not kinds:
            raise ValueError(f"E2E file format authority is unknown for {spec.name}")
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
            if not kinds:
                raise ValueError(f"E2E file format authority is unknown for {spec.name}")
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


_E2E_TRIAL_FORMATS = set(FILE_FIXTURE_FORMATS)
_E2E_TRIAL_CONTENT_KINDS = {handler.content_kind for handler in FILE_FIXTURE_FORMATS.values()}


def _canonicalize_e2e_trial_case_spec(value: Any) -> Any:
    """Return a copy with fixture-derived metadata made authoritative.

    This boundary deliberately knows only facts that can be mechanically
    derived from the fixture itself.  It never coerces cell values or repairs
    any other part of the model-proposed contract.
    """
    canonical = copy.deepcopy(value)
    if not isinstance(canonical, dict):
        return canonical
    for item in canonical.get("inputs", []) if isinstance(canonical.get("inputs"), list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("fixture"), dict):
            continue
        fixture = item["fixture"]
        files = fixture.get("files") if fixture.get("kind") == "file_list" else [fixture]
        if not isinstance(files, list):
            continue
        for file_spec in files:
            if (
                not isinstance(file_spec, dict)
                or file_spec.get("content_kind") != "tabular"
                or not isinstance(file_spec.get("columns"), list)
                or not isinstance(file_spec.get("rows"), list)
            ):
                continue
            rows = file_spec["rows"]
            if not all(isinstance(row, dict) for row in rows):
                continue
            for column in file_spec["columns"]:
                if not isinstance(column, dict) or not isinstance(column.get("name"), str):
                    continue
                name = column["name"]
                column["nullable"] = any(name not in row or row.get(name) is None for row in rows)
    return canonical


def _case_plan_digest(inputs: dict[str, E2EInputCaseSpec]) -> str:
    return _stable_json_hash({name: asdict(spec) for name, spec in sorted(inputs.items())})


def _build_e2e_input_case_plan(
    typed_specs: list[E2ETypedInputSpec],
    *,
    requirements_by_file: dict[str, list[RequirementItem]] | None = None,
) -> E2EInputCasePlan:
    """Close all input evidence into one immutable, root-only authority."""
    merged: dict[str, E2ETypedInputSpec] = {}
    for typed in typed_specs:
        _put_typed_spec(merged, typed)
    inputs: dict[str, E2EInputCaseSpec] = {}
    for name, typed in sorted(merged.items()):
        shape = _canonical_e2e_shape(typed.shape)
        file_spec = None
        if shape in {"file_path", "list[file_path]"}:
            file_spec = _resolve_e2e_file_input_spec(
                typed, (requirements_by_file or {}).get(typed.target_file, []),
            )
        inputs[name] = E2EInputCaseSpec(
            name=name,
            provenance_source=typed.provenance_source or typed.source,
            runtime_shape=shape,
            item_shape=_shape_item_shape(shape) or typed.item_shape,
            required=typed.required,
            nullable=typed.nullable,
            default_available=typed.default_available,
            default_value=typed.default_value,
            required_paths=tuple(sorted(set(typed.required_paths))),
            optional_paths=tuple(sorted(set(typed.optional_paths))),
            consumed_paths=tuple(sorted(set(typed.consumed_paths))),
            min_items=max(typed.min_items, file_spec.min_items if file_spec else 0),
            max_items=typed.max_items if typed.max_items is not None else (file_spec.max_items if file_spec else None),
            allowed_formats=file_spec.allowed_formats if file_spec else (),
            homogeneous_files=file_spec.homogeneous if file_spec else None,
            file_format_source=file_spec.format_source if file_spec else "",
            target_file=typed.target_file,
            required_source=typed.required_source or typed.source,
        )
    plan = E2EInputCasePlan(inputs=inputs, digest=_case_plan_digest(inputs))
    logger.info("[Creator][E2E][input_case_plan] %s", json.dumps({
        "digest": plan.digest,
        "inputs": {name: asdict(spec) for name, spec in plan.inputs.items()},
    }, ensure_ascii=False, sort_keys=True, default=str))
    return plan


def _plan_unknown_e2e_file_formats(
    plan: E2EInputCasePlan,
    *,
    requirements_by_file: dict[str, list[RequirementItem]],
    requested_model: str | None,
) -> E2EInputCasePlan:
    """Let the model fill unresolved file semantics without changing the contract.

    The materializer registry is exposed only as an execution-capability list;
    it is not used as a natural-language keyword map.  Names, shapes,
    cardinalities, and explicitly declared formats remain frozen.
    """
    unresolved = [
        spec for spec in plan.inputs.values()
        if spec.runtime_shape in {"file_path", "list[file_path]"} and not spec.allowed_formats
    ]
    if not unresolved:
        return plan

    evidence: list[dict[str, Any]] = []
    for spec in unresolved:
        requirements = []
        for req in requirements_by_file.get(spec.target_file, []) or []:
            text = next((
                str(getattr(req, field, "") or "").strip()
                for field in ("requirement", "text", "purpose")
                if str(getattr(req, field, "") or "").strip()
            ), "")
            inputs = [str(value) for value in (req.inputs or []) if str(value).strip()]
            if text or inputs:
                requirements.append({"id": str(req.id or ""), "text": text, "inputs": inputs})
        if requirements:
            evidence.append({
                "name": spec.name,
                "target_file": spec.target_file,
                "shape": spec.runtime_shape,
                "requirements": requirements,
            })

    # With no semantic evidence, guessing would weaken rather than complete the
    # frozen authority.  The caller will report the existing infrastructure
    # failure.
    evidence_names = {item["name"] for item in evidence}
    unresolved = [spec for spec in unresolved if spec.name in evidence_names]
    if not unresolved:
        return plan

    capabilities = sorted({handler.canonical_format for handler in FILE_FIXTURE_FORMATS.values()})
    explicit_mentions_by_input = {
        item["name"]: _explicit_file_format_mentions(item.get("requirements", []))
        for item in evidence
    }
    for item in evidence:
        item["explicit_format_mentions"] = explicit_mentions_by_input[item["name"]]
    variants = []
    for spec in unresolved:
        grounded_formats = sorted(explicit_mentions_by_input.get(spec.name, {}))
        decisions = ["resolved", "ambiguous"] if grounded_formats else ["unknown"]
        selectable_formats = ["", *grounded_formats] if grounded_formats else [""]
        variants.append({
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "decision", "format", "evidence", "reason"],
            "properties": {
                "name": {"const": spec.name},
                "decision": {"type": "string", "enum": decisions},
                "format": {"type": "string", "enum": selectable_formats},
                "evidence": {
                    "type": "array",
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "required": ["requirement_id", "quote"],
                        "properties": {
                            "requirement_id": {"type": "string", "minLength": 1},
                            "quote": {"type": "string", "minLength": 1},
                        },
                    },
                },
                "reason": {"type": "string", "minLength": 1},
            },
        })
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["inputs"],
        "properties": {
            "inputs": {
                "type": "array", "minItems": len(unresolved), "maxItems": len(unresolved),
                "items": {"oneOf": variants},
            },
        },
    }
    try:
        route = route_model(
            VALIDATOR_TASK, requested_model=requested_model,
            reason="creator grounded E2E file semantic planning",
        )
    except Exception as exc:
        logger.warning(
            "[Creator][E2E][file_semantic_plan_failed] stage=route type=%s error=%s",
            type(exc).__name__, exc,
        )
        return plan
    base_messages = [
                {"role": "system", "content": (
                    "Extract explicit file-format evidence for each unresolved runtime file input. "
                    "The supplied names, shapes, targets, and cardinalities are a frozen contract: do not change them. "
                    "Available formats are execution capabilities, not hints that every format is appropriate. "
                    "The supplied explicit_format_mentions are deterministically extracted from a finite format "
                    "ontology. Canonical content-format names count as explicit: for example, 'Markdown file' or "
                    "'Markdown document' explicitly names format=md even when the extension '.md' is omitted. "
                    "When explicit_format_mentions is non-empty, unknown is not valid: select the input-side format "
                    "with decision=resolved, or decision=ambiguous if the input-side evidence truly conflicts. "
                    "Use decision=resolved only when requirement prose explicitly names one available input format; "
                    "quote the exact supporting words and requirement_id in evidence. Use decision=unknown when prose "
                    "only says generic file, document, text, or image. Use decision=ambiguous when multiple explicit "
                    "formats conflict. Never infer a file format from broad compatibility, and never use an output "
                    "artifact format. For unknown or ambiguous decisions use format='' and evidence may be empty. "
                    "Return exactly the bound JSON Schema."
                )},
                {"role": "user", "content": json.dumps({
                    "unresolved_inputs": evidence,
                    "materialization_capabilities": capabilities,
                }, ensure_ascii=False, sort_keys=True)},
    ]
    requirements_by_input: dict[str, dict[str, str]] = {}
    for evidence_item in evidence:
        requirements_by_input[evidence_item["name"]] = {
            str(req.get("id") or ""): str(req.get("text") or "")
            for req in evidence_item.get("requirements", [])
        }
    selected: dict[str, tuple[str, ...]] = {}
    previous_proposal: Any = None
    validation_issues: list[dict[str, str]] = []
    for build_attempt in range(MAX_E2E_CASE_BUILD_ATTEMPTS):
        messages = list(base_messages)
        if validation_issues:
            messages.append({"role": "user", "content": json.dumps({
                "instruction": "Rebuild the file-format plan. Correct every validation issue; do not repeat the rejected proposal.",
                "previous_proposal": previous_proposal,
                "validation_issues": validation_issues,
            }, ensure_ascii=False, sort_keys=True)})
        try:
            proposed = _complete_creator_json_object_once_sync_for_e2e(
                messages=messages, model=route.model,
                phase="creator_e2e_file_semantic_plan",
                response_schema=schema,
            )
        except Exception as exc:
            logger.warning(
                "[Creator][E2E][file_semantic_plan_failed] attempt=%s type=%s error=%s",
                build_attempt + 1, type(exc).__name__, exc,
            )
            previous_proposal = None
            validation_issues = [{"input": "*", "code": "model_call_failed", "message": str(exc)}]
            continue

        previous_proposal = proposed
        validation_issues = []
        items = proposed.get("inputs", []) if isinstance(proposed, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            selected_format = _verified_model_file_format(
                item, requirements_by_id=requirements_by_input.get(name, {}),
            )
            if name in evidence_names and selected_format:
                selected[name] = (selected_format,)
            elif name in evidence_names:
                validation_issues.append({
                    "input": name,
                    "code": "format_evidence_rejected",
                    "message": "The decision, format, or quoted requirement evidence was not verifiable. Rebuild this input using an exact quote from the supplied frozen requirement.",
                })
        missing = sorted(spec.name for spec in unresolved if spec.name not in selected)
        for name in missing:
            if not any(issue["input"] == name for issue in validation_issues):
                validation_issues.append({
                    "input": name, "code": "format_unresolved",
                    "message": "No verified format plan was returned for this input. Rebuild it from the supplied explicit format mentions.",
                })
        if not validation_issues:
            break
        logger.warning(
            "[Creator][E2E][file_semantic_plan_rebuild] attempt=%s issues=%s",
            build_attempt + 1, json.dumps(validation_issues, ensure_ascii=False, sort_keys=True),
        )

    inputs = dict(plan.inputs)
    for name, formats in selected.items():
        inputs[name] = replace(
            inputs[name],
            allowed_formats=formats,
            homogeneous_files=True if len(formats) == 1 else False,
            file_format_source="model_semantic_planner_verified_evidence",
        )
    resolved = E2EInputCasePlan(inputs=inputs, digest=_case_plan_digest(inputs))
    logger.info("[Creator][E2E][file_semantic_plan] %s", json.dumps({
        "previous_digest": plan.digest,
        "digest": resolved.digest,
        "formats": {name: list(formats) for name, formats in selected.items()},
    }, ensure_ascii=False, sort_keys=True))
    return resolved


def _path_tokens(path: str) -> list[str | int]:
    normalized = _normalize_e2e_placeholder_expr(path)
    return [int(part) if part.isdigit() else part for part in normalized.split(".") if part]


def _value_has_case_path(value: Any, path: str) -> bool:
    current = value
    for token in _path_tokens(path):
        if isinstance(token, int):
            if not isinstance(current, list) or token >= len(current):
                return False
            current = current[token]
        else:
            if not isinstance(current, dict) or token not in current:
                return False
            current = current[token]
    return True


def _e2e_input_case_schema(spec: E2EInputCaseSpec) -> dict[str, Any]:
    """Build JSON Schema from the complete case authority, not shape alone."""
    shape = _canonical_e2e_shape(spec.runtime_shape)
    if shape == "object":
        schema: dict[str, Any] = {"type": "object"}
        properties: dict[str, Any] = {}
        required: list[str] = []
        for path in spec.required_paths:
            tokens = _path_tokens(path)
            if tokens and isinstance(tokens[0], str):
                required.append(tokens[0])
                properties.setdefault(tokens[0], {})
        if properties:
            schema["properties"] = properties
            schema["required"] = sorted(set(required))
    elif shape.startswith("list"):
        item_shape = _shape_item_shape(shape)
        item_schema = {
            "string": {"type": "string"}, "number": {"type": "number"},
            "integer": {"type": "integer"}, "boolean": {"type": "boolean"},
            "object": {"type": "object"},
        }.get(item_shape, {})
        schema = {"type": "array", "minItems": spec.min_items, "items": item_schema}
        if spec.max_items is not None:
            schema["maxItems"] = spec.max_items
    else:
        schema = {"type": shape}
    if spec.nullable:
        schema = {"anyOf": [schema, {"type": "null"}]}
    return schema


def _e2e_value_matches_case_spec(value: Any, spec: E2EInputCaseSpec) -> bool:
    if value is None:
        return spec.nullable
    if not _e2e_json_value_matches_shape(value, spec.runtime_shape, allow_empty=True):
        return False
    if isinstance(value, list):
        if len(value) < spec.min_items or (spec.max_items is not None and len(value) > spec.max_items):
            return False
    return all(_value_has_case_path(value, path) for path in spec.required_paths)


def _validate_e2e_trial_inputs(
    value: Any, *, plan: E2EInputCasePlan,
    requirement_ids_by_input: dict[str, set[str]],
) -> dict[str, E2EInputCandidate]:
    """Validate independently so one bad fixture cannot discard its siblings."""
    items = value.get("inputs", []) if isinstance(value, dict) and value.get("version") == 1 else []
    proposed = {str(item.get("name") or ""): item for item in items if isinstance(item, dict)}
    results: dict[str, E2EInputCandidate] = {}
    for name, spec in plan.inputs.items():
        item = proposed.get(name)
        reason = "required_root_missing" if item is None and spec.required and not spec.default_available else "optional_root_omitted"
        if item is not None:
            single = _validate_e2e_trial_case_spec(
                {"version": 1, "inputs": [item]}, input_specs={name: spec},
                requirement_ids_by_input={name: requirement_ids_by_input.get(name, set())},
            )
            if single is not None:
                results[name] = E2EInputCandidate("accepted", item.get("fixture"))
                continue
            reason = "trial_fixture_invalid"
        results[name] = E2EInputCandidate("omitted" if reason == "optional_root_omitted" else "invalid", reason=reason)
        logger.info("[Creator][E2E][trial_input_validation] %s", json.dumps({"name": name, "status": results[name].status, "reason": reason}, sort_keys=True))
    return results


def _assign_case_path(root: Any, path: str, value: Any = "sample") -> None:
    tokens = _path_tokens(path)
    current = root
    for index, token in enumerate(tokens):
        last = index == len(tokens) - 1
        next_token = tokens[index + 1] if not last else None
        if isinstance(token, int):
            while len(current) <= token:
                current.append({} if not isinstance(next_token, int) else [])
            if last:
                current[token] = value
            else:
                current = current[token]
        else:
            if last:
                current[token] = value
            else:
                current = current.setdefault(token, [] if isinstance(next_token, int) else {})


def _minimal_file_fixture(fmt: str) -> dict[str, Any]:
    handler = _resolve_file_fixture_handler(fmt)
    if handler is None:
        raise ValueError(f"unsupported E2E file fixture format: {fmt}")
    if handler.content_kind == "tabular":
        return {"format": handler.canonical_format, "content_kind": "tabular", "columns": [{"name": "value", "type": "string", "nullable": False}], "rows": [{"value": "sample"}]}
    if handler.content_kind == "json":
        return {"format": handler.canonical_format, "content_kind": "json", "value": {}}
    if handler.content_kind == "image":
        return {"format": handler.canonical_format, "content_kind": "image", "width": 64, "height": 64, "mode": "RGB"}
    return {"format": handler.canonical_format, "content_kind": handler.content_kind, "text": "sample\n"}


def _synthesize_e2e_input_fixture(spec: E2EInputCaseSpec) -> dict[str, Any]:
    shape = spec.runtime_shape
    if shape in {"file_path", "list[file_path]"}:
        if not spec.allowed_formats:
            raise ValueError(f"E2E case plan has no file format authority for {spec.name}")
        count = 1 if shape == "file_path" else max(1, spec.min_items)
        formats = list(spec.allowed_formats)
        files = [_minimal_file_fixture(formats[0] if spec.homogeneous_files is not False else formats[index % len(formats)]) for index in range(count)]
        return files[0] if shape == "file_path" else {"kind": "file_list", "files": files}
    if shape in {"string", "integer", "number", "boolean"}:
        return {"kind": "scalar", "value": {"string": "sample", "integer": 1, "number": 1.0, "boolean": True}[shape]}
    if shape == "object":
        value: Any = {}
    else:
        count = max(0, spec.min_items)
        item = {"string": "sample", "integer": 1, "number": 1.0, "boolean": True, "object": {}}.get(spec.item_shape, "sample")
        value = [copy.deepcopy(item) for _ in range(count)]
    for path in spec.required_paths:
        _assign_case_path(value, path)
    return {"kind": "json_value", "value": value}


def _validate_e2e_trial_case_spec(
    value: Any,
    *,
    input_specs: dict[str, E2ETypedInputSpec],
    requirement_ids_by_input: dict[str, set[str]],
) -> dict[str, Any] | None:
    """Validate the deliberately small, model-produced trial fixture format."""
    if not isinstance(value, dict) or value.get("status") == "unsupported" or value.get("version") != 1:
        return None
    inputs = value.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        return None
    seen: set[str] = set()
    for item in inputs:
        if not isinstance(item, dict):
            return None
        name, shape = str(item.get("name") or ""), _canonical_e2e_shape(str(item.get("shape") or ""))
        spec = input_specs.get(name)
        spec_shape = getattr(spec, "runtime_shape", getattr(spec, "shape", "")) if spec is not None else ""
        if spec is None or name in seen or shape != _canonical_e2e_shape(spec_shape):
            return None
        seen.add(name)
        evidence = item.get("evidence_requirement_ids", [])
        relevant_requirement_ids = requirement_ids_by_input.get(name, set())
        if (
            not isinstance(evidence, list)
            or (relevant_requirement_ids and not evidence)
            or any(str(req_id) not in relevant_requirement_ids for req_id in evidence)
        ):
            return None
        fixture = item.get("fixture")
        if not isinstance(fixture, dict):
            return None
        fixture_kind = str(fixture.get("kind") or "")
        scalar_shapes = {"string", "number", "integer", "boolean"}
        json_value_shapes = {
            "object", "list", "list[string]", "list[number]",
            "list[integer]", "list[boolean]", "list[object]",
        }
        if shape in scalar_shapes and fixture_kind != "scalar":
            return None
        if shape in json_value_shapes and fixture_kind != "json_value":
            return None
        if shape == "file_path" and fixture_kind == "scalar":
            return None
        if shape == "list[file_path]" and fixture_kind != "file_list":
            return None
        if fixture.get("kind") == "scalar":
            scalar = fixture.get("value")
            expected = {"string": str, "number": (int, float), "integer": int, "boolean": bool}.get(shape)
            if expected is None or not isinstance(scalar, expected) or (shape in {"number", "integer"} and isinstance(scalar, bool)):
                return None
            continue
        if fixture_kind == "json_value":
            fixture_value = fixture.get("value")

            case_spec = (
                spec
                if isinstance(
                    spec,
                    E2EInputCaseSpec,
                )
                else E2EInputCaseSpec(
                    name=spec.name,
                    provenance_source=(
                            spec.provenance_source
                            or spec.source
                    ),
                    runtime_shape=spec.shape,
                    item_shape=spec.item_shape,
                    required=spec.required,
                    nullable=spec.nullable,
                    default_available=(
                        spec.default_available
                    ),
                    default_value=(
                        spec.default_value
                    ),
                    required_paths=(
                        spec.required_paths
                    ),
                    optional_paths=(
                        spec.optional_paths
                    ),
                    consumed_paths=(
                        spec.consumed_paths
                    ),
                    min_items=spec.min_items,
                    max_items=spec.max_items,
                )
            )

            # Creator-owned deterministic invariant:
            # optional open collections must stay minimal.
            if (
                    not case_spec.required
                    and not case_spec.default_available
            ):
                if (
                        case_spec.runtime_shape == "list"
                        and not case_spec.item_shape
                        and not case_spec.required_paths
                        and fixture_value != []
                ):
                    return None

                if (
                        case_spec.runtime_shape == "object"
                        and not case_spec.required_paths
                        and fixture_value != {}
                ):
                    return None

            if not _e2e_value_matches_case_spec(
                    fixture_value,
                    case_spec,
            ):
                return None

            continue

        files = fixture.get("files") if fixture.get("kind") == "file_list" else [fixture]
        minimum = getattr(spec, "min_items", 1) or 1
        maximum = getattr(spec, "max_items", None) or (1 if shape == "file_path" else 3)
        if not isinstance(files, list) or not minimum <= len(files) <= maximum:
            return None
        if shape == "file_path" and len(files) != 1:
            return None
        for file_spec in files:
            if not isinstance(file_spec, dict):
                return None
            handler = _resolve_file_fixture_handler(str(file_spec.get("format") or ""))
            if handler is None or file_spec.get("content_kind") != handler.content_kind or not handler.validator(file_spec):
                return None
            allowed = tuple(getattr(spec, "allowed_formats", ()) or ())
            if allowed and handler.canonical_format not in allowed:
                return None
    return value


def _materialize_e2e_trial_fixture(item: dict[str, Any], *, skill_dir: Path) -> Any:
    fixture = item["fixture"]
    if fixture.get("kind") == "scalar":
        materialized = fixture["value"]
    elif fixture.get("kind") == "json_value":
        materialized = copy.deepcopy(fixture["value"])
    else:
        materialized = None
    files = fixture["files"] if fixture.get("kind") == "file_list" else ([] if materialized is not None else [fixture])
    paths: list[str] = []
    for index, file_spec in enumerate(files, 1):
        handler = _resolve_file_fixture_handler(file_spec["format"])
        if handler is None or not handler.validator(file_spec):
            raise ValueError(f"unsupported or invalid E2E file fixture: {file_spec.get('format')}")
        path = _e2e_sample_path(skill_dir, item["name"], index, kind=handler.canonical_format)
        handler.materializer(file_spec, path)
        paths.append(str(path))
    if materialized is None:
        materialized = paths if _canonical_e2e_shape(item["shape"]).startswith("list") else paths[0]
    logger.info(
        "[Creator][E2E][trial_fixture_materialized] %s",
        json.dumps({
            "name": item["name"],
            "shape": _canonical_e2e_shape(item["shape"]),
            "fixture_kind": fixture.get("kind", fixture.get("content_kind")),
            "materialized_shape": _json_shape(materialized),
            "paths": paths or None,
            "files": [{"canonical_format": _canonical_file_fixture_format(spec["format"]),
                       "extension": _e2e_sample_suffix_for_kind(spec["format"]),
                       "content_kind": _resolve_file_fixture_handler(spec["format"]).content_kind,
                       "path": path} for spec, path in zip(files, paths)],
        }, ensure_ascii=False, sort_keys=True),
    )
    return materialized


def _build_e2e_trial_case(
    facts: dict[str, Any], *, requested_model: str | None = None,
    rebuild_feedback: dict[str, Any] | None = None,
) -> Any:
    """Make the single narrow model call used to project frozen facts to inputs."""
    route = route_model(VALIDATOR_TASK, requested_model=requested_model, reason="creator grounded E2E trial case")
    schema = _e2e_trial_case_response_schema(facts)
    messages = [{"role": "system", "content": (
        "You are not a Skill planner. You may not modify requirements, interfaces, tools, scripts, or Blueprint. "
        "Construct only the smallest deterministic happy-path input consistent with the supplied frozen facts. "
        "Do not invent platform inputs or requirements, optimize the Skill, or judge implementation quality. "
        "Return exactly the Trial Case object required by the bound JSON Schema."
    )}, {"role": "user", "content": json.dumps(facts, ensure_ascii=False, sort_keys=True)}]
    if rebuild_feedback:
        messages.append({"role": "user", "content": json.dumps({
            "instruction": "Rebuild the Trial Case from scratch. Correct every reported issue while preserving all frozen input facts.",
            **rebuild_feedback,
        }, ensure_ascii=False, sort_keys=True, default=str)})
    return _complete_creator_json_object_once_sync_for_e2e(
        messages=messages,
        model=route.model,
        phase="creator_e2e_trial_case",
        response_schema=schema,
    )

def _review_e2e_trial_case(
    *,
    facts: dict[str, Any],
    trial_case: dict[str, Any],
    requested_model: str | None = None,
) -> dict[str, Any]:
    """
    Semantic review for one already schema-valid Trial Case.

    Structural representation validity belongs to the JSON Schema and
    materializer, not to this reviewer.

    Reviewer only decides whether the proposed Case is a meaningful,
    non-contradictory witness of the frozen requirements.
    """

    route = route_model(
        VALIDATOR_TASK,
        requested_model=requested_model,
        reason="creator E2E trial case semantic review",
    )

    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "passed",
            "issues",
            "repair_instructions",
        ],
        "properties": {
            "passed": {
                "type": "boolean",
            },
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "input",
                        "code",
                        "message",
                    ],
                    "properties": {
                        "input": {
                            "type": "string",
                        },
                        "code": {
                            "type": "string",
                            "enum": [
                                "semantic_witness_invalid",
                                "requirement_contradiction",
                                "requirement_not_exercised",
                            ],
                        },
                        "message": {
                            "type": "string",
                        },
                    },
                },
            },
            "repair_instructions": {
                "type": "string",
            },
        },
    }

    system_prompt = """
你是 Creator E2E Trial Case Semantic Reviewer。

你审查的对象已经通过 Creator 的结构化 JSON Schema。
因此你不能重新定义或否定 Trial Case 的序列化协议。

你的唯一任务是判断：

“这个 Trial Case 在 Creator materialize 成真实 runtime 输入以后，
是否是一个合理、最小、能够有效触发 frozen requirements 的 E2E 测试样本？”

严格遵守以下边界。

一、必须区分 Case representation 与 runtime representation

Trial Case 中：

- scalar fixture:
    {"kind":"scalar","value":...}
  materialize 后才成为 runtime scalar。

- json_value fixture:
    {"kind":"json_value","value":...}
  materialize 后才成为 runtime JSON value。

- file fixture:
    描述 Creator 应创建的真实本地文件内容。

- file_list fixture:
    描述 Creator 应创建的一组真实本地文件。

对于 file_path / list[file_path]：

Trial Case 阶段出现 file fixture / file_list 是正确表示形式。
Reviewer 不得要求 Trial Case 自己直接包含 /tmp/x.csv、
data/x.csv 等现成路径。

Creator 会在 Reviewer 通过以后：

file fixture
→ materialize local file
→ runtime file path

因此禁止仅因为 fixture 不是最终 path string 而判失败。

二、不得重新做 JSON Schema 审查

以下内容已经由 Creator schema 负责：

- root input 名称；
- root shape；
- fixture kind；
- scalar 类型；
- file format；
- file cardinality；
- file fixture 基础结构。

不要因为上述理由重复判失败。

三、open object / open list 不是 closed schema

如果 frozen facts 只声明：

shape = object

且没有明确声明：

- closed properties
- forbidden properties
- additionalProperties=false

则不得认为 object 必须为 {}，
也不得仅因为出现额外字段而判 invalid_input_shape。

required_paths=[] 只表示没有被冻结的必填 nested path，
不表示禁止其它字段。

四、Reviewer 只允许判断语义 witness

只在以下情况下 passed=false：

1. semantic_witness_invalid
   Case 本身虽然结构合法，但内容明显让 E2E 无法测试目标行为。

2. requirement_contradiction
   Case 内容直接违反 frozen requirement。

3. requirement_not_exercised
   Case 提前提供了 requirement 本来要求 Skill 自己计算、
   推断、转换、发现或生成的答案，从而绕过了核心行为。

例如：

Requirement：
“Skill 必须自动识别主键。”

如果 Trial Case 提供：

{"primary_key":"id"}

且该值会使脚本不需要自动识别主键，
可以判：

requirement_not_exercised

但不能判：

invalid_input_shape

因为 object 的结构本身可能完全合法。

五、不要审查 Skill 实现

如果 Case 是合法测试输入，
但你怀疑脚本无法处理，
必须 passed=true。

脚本问题由后续真实 E2E subprocess 判断。

六、倾向让真实 E2E 执行

只有存在明确 frozen-requirement 证据时才能拒绝 Case。

有歧义时优先 passed=true，
让真实 subprocess 暴露实现问题。

不要因为“可能”“通常”“建议”“最好”而拒绝 Case。

只输出 JSON。
""".strip()

    payload = {
        "frozen_input_facts": facts,
        "trial_case": trial_case,

        # Explicit Creator-owned representation semantics.
        "trial_case_representation_contract": {
            "scalar": (
                "fixture.value becomes the runtime scalar"
            ),
            "json_value": (
                "fixture.value becomes the runtime JSON value"
            ),
            "file_path": (
                "file fixture is materialized by Creator "
                "into one real local file path"
            ),
            "list[file_path]": (
                "file_list fixtures are materialized by Creator "
                "into a list of real local file paths"
            ),
        },
    }

    result = (
        _complete_creator_json_object_once_sync_for_e2e(
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ),
                },
            ],
            model=route.model,
            phase="creator_e2e_trial_case_review",
            response_schema=schema,
        )
    )

    return (
        result
        if isinstance(result, dict)
        else {
            "passed": False,
            "issues": [
                {
                    "input": "",
                    "code":
                        "semantic_witness_invalid",
                    "message": (
                        "Trial Case Reviewer "
                        "returned invalid output."
                    ),
                }
            ],
            "repair_instructions": "",
        }
    )

def _complete_creator_json_object_once_sync_for_e2e(**kwargs: Any) -> dict[str, Any]:
    """Synchronously reuse Creator's existing strict JSON-Schema completion."""
    import asyncio
    import concurrent.futures

    async def call() -> dict[str, Any]:
        return await complete_json_object_once(**kwargs)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(call())
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda: asyncio.run(call())).result()

def _e2e_json_value_schema(shape: str) -> dict[str, Any]:
    """Return the narrow JSON Schema used for non-file structured fixtures."""
    canonical = _canonical_e2e_shape(shape)
    if canonical == "object":
        return {"type": "object"}
    item_shape = _shape_item_shape(canonical)
    item_schema: dict[str, Any] = {
        "string": {"type": "string"},
        "number": {"type": "number"},
        "integer": {"type": "integer"},
        "boolean": {"type": "boolean"},
        "object": {"type": "object"},
    }.get(item_shape, {})
    return {"type": "array", "minItems": 0, "items": item_schema}


def _e2e_json_value_matches_shape(value: Any, shape: str, *, allow_empty: bool = True) -> bool:
    """Deterministically validate a model-produced structured input value."""
    canonical = _canonical_e2e_shape(shape)
    if canonical == "string":
        return isinstance(value, str)
    if canonical == "boolean":
        return isinstance(value, bool)
    if canonical == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if canonical == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if canonical == "object":
        return isinstance(value, dict) and (allow_empty or bool(value))
    if not isinstance(value, list) or (not allow_empty and not value):
        return False
    item_shape = _shape_item_shape(canonical)
    if not item_shape:
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError):
            return False
        return True
    expected: Any = {
        "string": str,
        "number": (int, float),
        "integer": int,
        "boolean": bool,
        "object": dict,
    }.get(item_shape)
    if expected is None:
        return False
    for item in value:
        if not isinstance(item, expected):
            return False
        if item_shape in {"number", "integer"} and isinstance(item, bool):
            return False
        if item_shape == "object" and not allow_empty and not item:
            return False
    return True


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
                target="creator_e2e",
                layer="command_contract_projection_failure",
                message=(
                    ("missing_placeholder: 冻结合同的外部输入没有进入 canonical command payload。" if command.ordinal == 1 else "missing_placeholder: 冻结的 Skill 内部 dataflow 无法从前序 stdout 解析。")
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
                    "command block 是冻结合同的只读投影，模型不得修改。"
                    "请重建或报告 Creator command/dataflow projection authority；"
                    "不要修改 SKILL.md 或业务脚本来猜测缺失 binding。"
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
        if not kinds:
            raise ValueError("legacy runtime input sentinel has no frozen file format authority")
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
        if not kinds:
            raise ValueError("legacy runtime input sentinel has no frozen file format authority")
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


def _e2e_trial_case_response_schema(
    facts: dict[str, Any],
) -> dict[str, Any]:
    """
    Build the structural output schema for the Trial Case model.

    The schema enforces serialization/materialization structure only.

    It deliberately does NOT decide semantic questions such as:
    - whether an optional list should be empty;
    - whether an optional object should contain fields;
    - whether a particular sample value is meaningful.

    Those questions belong to _review_e2e_trial_case().
    """

    input_variants: list[
        dict[str, Any]
    ] = []

    for frozen in (
        facts.get("external_inputs")
        or []
    ):
        if not isinstance(frozen, dict):
            continue

        platform_input = (
            frozen.get("platform_input")
            if isinstance(
                frozen.get(
                    "platform_input"
                ),
                dict,
            )
            else {}
        )

        name = str(
            platform_input.get("name")
            or ""
        ).strip()

        shape = _canonical_e2e_shape(
            str(
                platform_input.get("shape")
                or ""
            )
        )

        if not name or not shape:
            continue

        file_contract = (
            platform_input.get(
                "file_contract"
            )
            if isinstance(
                platform_input.get(
                    "file_contract"
                ),
                dict,
            )
            else {}
        )

        allowed_formats = [
            fmt
            for fmt in (
                file_contract.get(
                    "allowed_formats"
                )
                or []
            )
            if _resolve_file_fixture_handler(
                fmt
            )
            is not None
        ]

        evidence_ids = [
            str(item.get("id"))
            for item in (
                frozen.get(
                    "requirements"
                )
                or []
            )
            if (
                isinstance(item, dict)
                and item.get("id")
            )
        ]

        evidence_schema: dict[
            str,
            Any,
        ] = {
            "type": "array",
            "uniqueItems": True,
            "items": (
                {
                    "type": "string",
                    "enum": evidence_ids,
                }
                if evidence_ids
                else {
                    "type": "string"
                }
            ),
            "minItems": (
                1
                if evidence_ids
                else 0
            ),
            "maxItems": len(
                evidence_ids
            ),
        }

        # =====================================================
        # Scalar runtime value
        # =====================================================
        if shape in {
            "string",
            "number",
            "integer",
            "boolean",
        }:
            fixture_schema: dict[
                str,
                Any,
            ] = {
                "type": "object",
                "additionalProperties":
                    False,
                "required": [
                    "kind",
                    "value",
                ],
                "properties": {
                    "kind": {
                        "const": "scalar"
                    },
                    "value": {
                        "type": shape
                    },
                },
            }

        # =====================================================
        # Structured JSON runtime value
        # =====================================================
        elif shape in {
            "object",
            "list",
            "list[string]",
            "list[number]",
            "list[integer]",
            "list[boolean]",
            "list[object]",
        }:
            case_spec = E2EInputCaseSpec(
                name=name,
                provenance_source=str(
                    platform_input.get(
                        "provenance_source"
                    )
                    or "frozen_contract"
                ),
                runtime_shape=shape,
                item_shape=(
                    _canonical_e2e_shape(
                        str(
                            platform_input.get(
                                "item_shape"
                            )
                            or ""
                        )
                    )
                    or _shape_item_shape(
                        shape
                    )
                ),
                required=bool(
                    platform_input.get(
                        "required",
                        True,
                    )
                ),
                nullable=bool(
                    platform_input.get(
                        "nullable",
                        False,
                    )
                ),
                default_available=bool(
                    platform_input.get(
                        "default_available",
                        False,
                    )
                ),
                default_value=(
                    platform_input.get(
                        "default_value"
                    )
                ),
                required_paths=tuple(
                    platform_input.get(
                        "required_paths"
                    )
                    or ()
                ),
                optional_paths=tuple(
                    platform_input.get(
                        "optional_paths"
                    )
                    or ()
                ),
                consumed_paths=tuple(
                    platform_input.get(
                        "consumed_paths"
                    )
                    or ()
                ),
                min_items=int(
                    platform_input.get(
                        "min_items"
                    )
                    or 0
                ),
                max_items=(
                    platform_input.get(
                        "max_items"
                    )
                ),
            )

            value_schema = (
                _e2e_input_case_schema(
                    case_spec
                )
            )

            fixture_schema = {
                "type": "object",
                "additionalProperties":
                    False,
                "required": [
                    "kind",
                    "value",
                ],
                "properties": {
                    "kind": {
                        "const":
                            "json_value"
                    },
                    "value":
                        value_schema,
                },
            }

        # =====================================================
        # One runtime file path
        # =====================================================
        elif shape == "file_path":
            file_variants = []

            for fmt in allowed_formats:
                handler = (
                    _resolve_file_fixture_handler(
                        fmt
                    )
                )

                if handler is None:
                    continue

                file_variants.append(
                    handler.fixture_schema(
                        handler.canonical_format,
                        handler.content_kind,
                    )
                )

            direct_file_schema: dict[
                str,
                Any,
            ] = {
                "oneOf": file_variants
            }

            fixture_schema = {
                "oneOf": [
                    direct_file_schema,
                    {
                        "type": "object",
                        "additionalProperties":
                            False,
                        "required": [
                            "kind",
                            "files",
                        ],
                        "properties": {
                            "kind": {
                                "const":
                                    "file_list"
                            },
                            "files": {
                                "type":
                                    "array",
                                "minItems": 1,
                                "maxItems": 1,
                                "items":
                                    direct_file_schema,
                            },
                        },
                    },
                ],
            }

        # =====================================================
        # Collection of runtime file paths
        # =====================================================
        elif shape == "list[file_path]":
            file_variants = []

            for fmt in allowed_formats:
                handler = (
                    _resolve_file_fixture_handler(
                        fmt
                    )
                )

                if handler is None:
                    continue

                file_variants.append(
                    handler.fixture_schema(
                        handler.canonical_format,
                        handler.content_kind,
                    )
                )

            file_schema = {
                "oneOf": file_variants
            }

            min_items = int(
                file_contract.get(
                    "min_items"
                )
                or platform_input.get(
                    "min_items"
                )
                or 1
            )

            max_items_raw = (
                file_contract.get(
                    "max_items"
                )
            )

            if max_items_raw is None:
                max_items_raw = (
                    platform_input.get(
                        "max_items"
                    )
                )

            max_items = (
                int(max_items_raw)
                if max_items_raw
                is not None
                else max(
                    min_items,
                    3,
                )
            )

            fixture_schema = {
                "type": "object",
                "additionalProperties":
                    False,
                "required": [
                    "kind",
                    "files",
                ],
                "properties": {
                    "kind": {
                        "const":
                            "file_list"
                    },
                    "files": {
                        "type":
                            "array",
                        "minItems":
                            min_items,
                        "maxItems":
                            max_items,
                        "items":
                            file_schema,
                    },
                },
            }

        else:
            # Unknown shapes should not normally reach the Trial Case model.
            # Keep the schema non-inventive rather than guessing a value.
            continue

        input_variants.append({
            "type": "object",
            "additionalProperties": False,
            "required": [
                "name",
                "shape",
                "fixture",
                "evidence_requirement_ids",
            ],
            "properties": {
                "name": {
                    "const": name
                },
                "shape": {
                    "const": shape
                },
                "fixture":
                    fixture_schema,
                "evidence_requirement_ids":
                    evidence_schema,
            },
        })

    trial_case_schema: dict[
        str,
        Any,
    ] = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "version",
            "inputs",
        ],
        "properties": {
            "version": {
                "const": 1
            },
            "inputs": {
                "type": "array",
                "minItems": 1,
                "maxItems": len(
                    input_variants
                ),
                "items": {
                    "oneOf":
                        input_variants
                },
            },
        },
    }

    unsupported_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "status",
        ],
        "properties": {
            "status": {
                "const":
                    "unsupported"
            },
        },
    }

    return {
        "oneOf": [
            trial_case_schema,
            unsupported_schema,
        ],
    }


def _e2e_repair_target_from_errors(errors: list[str]) -> str:
    """Compatibility parser for legacy repair-target diagnostics only."""
    for error in errors:
        match = re.search(r"^E2E_REPAIR_TARGET=([^\n]+)", error)
        if match:
            target = match.group(1).strip()
            if target:
                return target
    return "SKILL.md"


def _e2e_symptom_file_from_errors(errors: list[str]) -> str:
    for error in errors:
        match = re.search(r"^E2E_SYMPTOM_FILE=([^\n]+)", error)
        if match and match.group(1).strip():
            return match.group(1).strip()
    return _e2e_repair_target_from_errors(errors)


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
    # One entry per real hypothesis -> patch -> sandbox experiment.
    debug_attempts: list[dict[str, Any]] = field(default_factory=list)
    verified_bindings_by_script: dict[str, dict[str, str]] = field(default_factory=dict)
    runtime_dependency_attempts: set[str] = field(default_factory=set)
    trial_case: dict[str, Any] | None = None
    trial_case_digest: str = ""
    trial_case_prepared: bool = False
    input_case_plan_digest: str = ""
    input_case_values: dict[str, Any] = field(default_factory=dict)
    input_case_fixture_digests: dict[str, str] = field(default_factory=dict)
    input_case_plan_failure: str = ""
    input_case_plan_failure_details: dict[str, Any] = field(default_factory=dict)

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

def _repair_e2e_trial_case(
    *,
    facts: dict[str, Any],
    previous_case: dict[str, Any],
    review: dict[str, Any],
    requested_model: str | None = None,
) -> dict[str, Any]:
    """
    Regenerate only the Trial Case.

    Frozen facts are immutable.
    Skill files are immutable.
    """

    route = route_model(
        VALIDATOR_TASK,
        requested_model=requested_model,
        reason="creator E2E trial case repair",
    )

    response_schema = (
        _e2e_trial_case_response_schema(
            facts
        )
    )

    messages = [
        {
            "role": "system",
            "content": (
                "You repair only a Creator E2E Trial Case. "
                "Frozen requirements and interfaces are immutable. "
                "Do not modify or reinterpret the Skill. "
                "Return a replacement Trial Case that satisfies "
                "the same JSON Schema and addresses only the "
                "semantic reviewer issues."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "frozen_input_facts":
                        facts,
                    "previous_trial_case":
                        previous_case,
                    "review_issues":
                        review.get(
                            "issues"
                        )
                        or [],
                    "review_repair_instructions":
                        review.get(
                            "repair_instructions"
                        )
                        or "",
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ),
        },
    ]

    return (
        _complete_creator_json_object_once_sync_for_e2e(
            messages=messages,
            model=route.model,
            phase="creator_e2e_trial_case_repair",
            response_schema=response_schema,
        )
    )

def _prepare_e2e_trial_case(
    *,
    typed_specs: list[E2ETypedInputSpec],
    requirements_by_file: dict[str, list[RequirementItem]],
    skill_plan_entries: dict[str, SkillPlanEntry],
    external_context: dict[str, Any] | None,
    requested_model: str | None,
    session: CreatorE2ESession | None,
) -> dict[str, Any] | None:
    """
    Build one frozen E2E Trial Case.

    Authority flow:

        frozen typed inputs
            -> determine which inputs need synthetic E2E values
            -> close E2E InputCasePlan
            -> Trial Case model
            -> Trial Case Reviewer
            -> freeze reviewed Trial Case

    Important rules:

    1. Merely having a key in external_context does NOT mean that a real
       runtime value exists. Empty platform-envelope placeholders such as
       [] / {} / "" must not suppress Trial Case generation.

    2. Only a meaningful external value or a frozen/default value suppresses
       synthetic Trial Case generation.

    3. "No Case needed" is not cached as a frozen prepared Case.

    4. A previously frozen Trial Case may only be reused when its
       InputCasePlan digest is still identical to the current plan.
    """

    external = (
        external_context
        if isinstance(external_context, dict)
        else {}
    )

    supported_shapes = {
        "string",
        "number",
        "integer",
        "boolean",
        "object",
        "list",
        "list[string]",
        "list[number]",
        "list[integer]",
        "list[boolean]",
        "list[object]",
        "file_path",
        "list[file_path]",
    }

    candidates: dict[str, E2ETypedInputSpec] = {}

    # =========================================================
    # 1. Decide which frozen external inputs actually need an
    #    E2E Trial Case.
    #
    # Do NOT use:
    #
    #     if spec.name in external
    #
    # because platform envelopes may contain empty placeholders.
    # =========================================================
    for spec in typed_specs:
        shape = _canonical_e2e_shape(
            spec.shape
        )

        if shape not in supported_shapes:
            continue

        entry = skill_plan_entries.get(
            spec.target_file
        )

        defaults = (
            getattr(
                entry,
                "default_values",
                {},
            )
            or {}
        )

        # Only a meaningful actual external runtime value suppresses
        # synthetic Trial Case generation.
        if _json_value_non_empty(
            external.get(spec.name)
        ):
            continue

        # Explicit frozen/default values are already authoritative.
        if (
            spec.default_available
            or spec.name in defaults
        ):
            continue

        candidates.setdefault(
            spec.name,
            spec,
        )

    # =========================================================
    # 2. No synthetic input is needed.
    #
    # IMPORTANT:
    # Do not mark trial_case_prepared=True here.
    #
    # Otherwise a temporary "no Case needed" decision becomes a
    # permanent cache entry for the whole E2E repair session.
    # =========================================================
    if not candidates:
        if session is not None:
            session.trial_case = None
            session.trial_case_digest = ""
            session.input_case_plan_digest = ""
            session.input_case_fixture_digests = {}
            session.input_case_plan_failure = ""
            session.input_case_plan_failure_details = {}

            # A no-case result is not a frozen Case.
            session.trial_case_prepared = False

        logger.info(
            "[Creator][E2E]"
            "[input_case_no_generation_needed] "
            "typed_inputs=%s external_non_empty=%s",
            sorted(
                spec.name
                for spec in typed_specs
            ),
            sorted(
                str(key)
                for key, value
                in external.items()
                if _json_value_non_empty(value)
            ),
        )

        return None

    # =========================================================
    # 3. Close current frozen InputCasePlan.
    # =========================================================
    plan = _build_e2e_input_case_plan(
        list(candidates.values()),
        requirements_by_file=
            requirements_by_file,
    )
    plan = _plan_unknown_e2e_file_formats(
        plan,
        requirements_by_file=requirements_by_file,
        requested_model=requested_model,
    )

    # =========================================================
    # 4. Session reuse is allowed only for the same frozen plan.
    #
    # This prevents:
    #
    #   first run → no/wrong Case cached
    #   SKILL repair changes command/interface
    #   second run → stale Case blindly reused
    # =========================================================
    if session is not None:
        same_plan = bool(
            session.input_case_plan_digest
            and session.input_case_plan_digest
            == plan.digest
        )

        if (
            same_plan
            and session.input_case_plan_failure
        ):
            logger.info(
                "[Creator][E2E][trial_case_cached_failure_rebuild] plan_digest=%s failure=%s details=%s",
                plan.digest,
                session.input_case_plan_failure,
                json.dumps(session.input_case_plan_failure_details, ensure_ascii=False, sort_keys=True, default=str),
            )
            # A failed derived Trial Case is not frozen authority. Re-enter the
            # bounded model build flow on the next E2E pass instead of making a
            # transient first-wave failure permanent for this session.
            session.trial_case = None
            session.trial_case_digest = ""
            session.trial_case_prepared = False
            session.input_case_fixture_digests = {}
            session.input_case_plan_failure = ""
            session.input_case_plan_failure_details = {}

        if (
            same_plan
            and session.trial_case_prepared
            and isinstance(
                session.trial_case,
                dict,
            )
        ):
            logger.info(
                "[Creator][E2E]"
                "[trial_case_reused] "
                "plan_digest=%s "
                "case_digest=%s",
                plan.digest,
                session.trial_case_digest,
            )

            return session.trial_case

        # Current frozen plan changed.
        # Invalidate only Trial Case cache, not the whole E2E session.
        if not same_plan:
            session.trial_case = None
            session.trial_case_digest = ""
            session.trial_case_prepared = False
            session.input_case_fixture_digests = {}
            session.input_case_plan_failure = ""
            session.input_case_plan_failure_details = {}

        session.input_case_plan_digest = (
            plan.digest
        )

    # =========================================================
    # 5. File inputs require enough frozen information to
    #    materialize a real local fixture.
    # =========================================================
    unknown_file_inputs = sorted(
        name
        for name, spec in plan.inputs.items()
        if (
            spec.runtime_shape
            in {
                "file_path",
                "list[file_path]",
            }
            and not spec.allowed_formats
        )
    )

    if unknown_file_inputs:
        details = {
            "unsupported_inputs":
                unknown_file_inputs,
            "plan_digest":
                plan.digest,
            "repair_owner":
                "creator_e2e",
            "skill_repair_allowed":
                False,
        }

        if session is not None:
            session.trial_case_prepared = False
            session.input_case_plan_failure = (
                "file_format_unknown"
            )
            session.input_case_plan_failure_details = (
                details
            )

        logger.warning(
            "[Creator][E2E]"
            "[case_plan_unsupported] "
            "reason=file_format_unknown "
            "inputs=%s",
            unknown_file_inputs,
        )

        raise E2ECaseInfrastructureError(
            "file_format_unknown",
            details=details,
        )

    # =========================================================
    # 6. Preserve frozen requirement prose for Model + Reviewer.
    # =========================================================
    requirements: list[
        dict[str, str]
    ] = []

    requirements_by_id: dict[
        str,
        RequirementItem,
    ] = {}

    for target_file in {
        spec.target_file
        for spec in candidates.values()
    }:
        for requirement in (
            requirements_by_file.get(
                target_file,
                [],
            )
            or []
        ):
            req_id = str(
                getattr(
                    requirement,
                    "id",
                    "",
                )
                or ""
            ).strip()

            if not req_id:
                continue

            requirements_by_id.setdefault(
                req_id,
                requirement,
            )

    for req_id, requirement in (
        requirements_by_id.items()
    ):
        text = next(
            (
                str(
                    getattr(
                        requirement,
                        field_name,
                        "",
                    )
                    or ""
                ).strip()
                for field_name in (
                    "requirement",
                    "text",
                    "purpose",
                )
                if str(
                    getattr(
                        requirement,
                        field_name,
                        "",
                    )
                    or ""
                ).strip()
            ),
            "",
        )

        if not text:
            text = "; ".join(
                str(value)
                for value in (
                    getattr(
                        requirement,
                        "inputs",
                        [],
                    )
                    or []
                )
            )

        requirements.append({
            "id": req_id,
            "text": text,
        })

    # =========================================================
    # 7. Build the frozen facts visible to Trial Case model.
    # =========================================================
    facts: dict[str, Any] = {
        "version": 1,
        "external_inputs": [],
    }

    for spec in plan.inputs.values():
        target_requirements = (
            requirements_by_file.get(
                spec.target_file,
                [],
            )
            or []
        )

        target_requirement_ids = {
            str(
                getattr(
                    req,
                    "id",
                    "",
                )
                or ""
            )
            for req in target_requirements
            if str(
                getattr(
                    req,
                    "id",
                    "",
                )
                or ""
            ).strip()
        }

        platform_input: dict[
            str,
            Any,
        ] = {
            "name":
                spec.name,
            "shape":
                spec.runtime_shape,
            "item_shape":
                spec.item_shape,
            "provenance_source":
                spec.provenance_source,
            "required":
                spec.required,
            "nullable":
                spec.nullable,
            "default_available":
                spec.default_available,
            "default_value":
                spec.default_value,
            "required_paths":
                list(
                    spec.required_paths
                ),
            "optional_paths":
                list(
                    spec.optional_paths
                ),
            "consumed_paths":
                list(
                    spec.consumed_paths
                ),
            "min_items":
                spec.min_items,
            "max_items":
                spec.max_items,
        }

        if (
            spec.runtime_shape
            in {
                "file_path",
                "list[file_path]",
            }
        ):
            platform_input[
                "file_contract"
            ] = {
                "allowed_formats":
                    list(
                        spec.allowed_formats
                    ),
                "min_items":
                    spec.min_items,
                "max_items":
                    spec.max_items,
                "homogeneous":
                    spec.homogeneous_files,
                "format_source":
                    spec.file_format_source,
            }

        facts[
            "external_inputs"
        ].append({
            "platform_input":
                platform_input,
            "target": {
                "script":
                    spec.target_file,
                "input":
                    spec.name,
            },
            "requirements": [
                item
                for item in requirements
                if item["id"]
                in target_requirement_ids
            ],
        })

    logger.info(
        "[Creator][E2E]"
        "[trial_case_build_start] "
        "plan_digest=%s "
        "typed_input_specs=%s",
        plan.digest,
        json.dumps(
            {
                name:
                    spec.shape
                for name, spec
                in sorted(
                    candidates.items()
                )
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )

    # =========================================================
    # 8. Model generates Trial Case.
    #
    # No deterministic semantic fallback.
    # =========================================================
    generated: Any = None
    generation_issues: list[dict[str, str]] = []
    last_generation_error: Exception | None = None
    for build_attempt in range(MAX_E2E_CASE_BUILD_ATTEMPTS):
        feedback = None
        if generation_issues:
            feedback = {
                "previous_trial_case": generated,
                "validation_issues": generation_issues,
            }
        try:
            generated = _build_e2e_trial_case(
                facts, requested_model=requested_model, rebuild_feedback=feedback,
            )
            last_generation_error = None
        except Exception as exc:
            last_generation_error = exc
            generation_issues = [{
                "code": "generation_exception", "message": f"{type(exc).__name__}: {exc}",
            }]
        else:
            valid_shape = (
                isinstance(generated, dict)
                and generated.get("status") != "unsupported"
                and generated.get("version") == 1
                and isinstance(generated.get("inputs"), list)
            )
            if valid_shape:
                break
            generation_issues = [{
                "code": "unsupported_or_invalid_case",
                "message": "Return a complete version=1 Trial Case with an inputs array matching the bound schema.",
            }]
        logger.warning(
            "[Creator][E2E][trial_case_rebuild] attempt=%s issues=%s",
            build_attempt + 1, json.dumps(generation_issues, ensure_ascii=False, sort_keys=True),
        )

    if generation_issues and not (
        isinstance(generated, dict)
        and generated.get("status") != "unsupported"
        and generated.get("version") == 1
        and isinstance(generated.get("inputs"), list)
    ):
        details = {
            "reason":
                (
                    "model_returned_"
                    "unsupported_or_invalid_case"
                ),
            "generated_shape":
                _json_shape(
                    generated
                ),
            "validation_issues": generation_issues,
            "plan_digest":
                plan.digest,
            "repair_owner":
                "creator_e2e",
            "skill_repair_allowed":
                False,
        }

        if session is not None:
            session.trial_case_prepared = False
            session.input_case_plan_failure = (
                "trial_case_generation_failed"
            )
            session.input_case_plan_failure_details = (
                details
            )

        raise E2ECaseInfrastructureError(
            "trial_case_generation_failed",
            details=details,
        ) from last_generation_error

    accepted = (
        _canonicalize_e2e_trial_case_spec(
            generated
        )
    )

    review: dict[str, Any] = {}

    for case_attempt in range(
            MAX_E2E_CASE_REPAIR_ATTEMPTS + 1
    ):
        review = _review_e2e_trial_case(
            facts=facts,
            trial_case=accepted,
            requested_model=requested_model,
        )

        logger.info(
            "[Creator][E2E]"
            "[trial_case_review] %s",
            json.dumps(
                {
                    "attempt":
                        case_attempt,
                    "passed":
                        bool(
                            review.get(
                                "passed"
                            )
                        ),
                    "issues":
                        (
                                review.get(
                                    "issues"
                                )
                                or []
                        ),
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ),
        )

        if review.get("passed") is True:
            break

        if (
                case_attempt
                >= MAX_E2E_CASE_REPAIR_ATTEMPTS
        ):
            break

        try:
            repaired = (
                _repair_e2e_trial_case(
                    facts=facts,
                    previous_case=accepted,
                    review=review,
                    requested_model=
                    requested_model,
                )
            )
        except Exception as exc:
            logger.warning(
                "[Creator][E2E]"
                "[trial_case_repair_failed] "
                "attempt=%s type=%s error=%s",
                case_attempt + 1,
                type(exc).__name__,
                exc,
            )
            break

        if (
                not isinstance(repaired, dict)
                or repaired.get("version") != 1
                or not isinstance(
            repaired.get("inputs"),
            list,
        )
        ):
            logger.warning(
                "[Creator][E2E]"
                "[trial_case_repair_invalid] "
                "attempt=%s",
                case_attempt + 1,
            )
            break

        next_case = (
            _canonicalize_e2e_trial_case_spec(
                repaired
            )
        )

        # Stop pointless loops.
        if (
                _stable_json_hash(next_case)
                == _stable_json_hash(accepted)
        ):
            logger.warning(
                "[Creator][E2E]"
                "[trial_case_repair_no_change] "
                "attempt=%s",
                case_attempt + 1,
            )
            break

        accepted = next_case

    if review.get("passed") is not True:
        details = {
            "review": review,
            "plan_digest": plan.digest,
            "repair_owner":
                "creator_e2e",
            "skill_repair_allowed":
                False,
        }

        if session is not None:
            session.trial_case_prepared = False
            session.input_case_plan_failure = (
                "trial_case_review_failed"
            )
            session.input_case_plan_failure_details = (
                details
            )

        raise E2ECaseInfrastructureError(
            "trial_case_review_failed",
            details=details,
        )

    # =========================================================
    # 11. Freeze exactly the reviewed Case.
    # =========================================================
    digest = _stable_json_hash(
        accepted
    )

    fixture_digests = {
        str(
            item.get(
                "name"
            )
            or ""
        ):
            _stable_json_hash(
                item.get(
                    "fixture"
                )
            )
        for item in (
            accepted.get(
                "inputs"
            )
            or []
        )
        if (
            isinstance(
                item,
                dict,
            )
            and item.get(
                "name"
            )
        )
    }

    if session is not None:
        session.trial_case = accepted
        session.trial_case_digest = digest
        session.trial_case_prepared = True
        session.input_case_plan_digest = (
            plan.digest
        )
        session.input_case_plan_failure = ""
        session.input_case_plan_failure_details = {}
        session.input_case_fixture_digests = (
            fixture_digests
        )

    logger.info(
        "[Creator][E2E]"
        "[trial_case_frozen] "
        "plan_digest=%s "
        "case_digest=%s "
        "fixtures=%s",
        plan.digest,
        digest,
        json.dumps(
            {
                item["name"]: {
                    "shape":
                        item.get(
                            "shape"
                        ),
                    "fixture_kind":
                        (
                            (
                                item.get(
                                    "fixture"
                                )
                                or {}
                            ).get(
                                "kind"
                            )
                            or (
                                item.get(
                                    "fixture"
                                )
                                or {}
                            ).get(
                                "content_kind"
                            )
                        ),
                }
                for item in (
                    accepted.get(
                        "inputs"
                    )
                    or []
                )
                if (
                    isinstance(
                        item,
                        dict,
                    )
                    and item.get(
                        "name"
                    )
                )
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )

    logger.info(
        "[Creator][E2E]"
        "[input_case_frozen] %s",
        json.dumps(
            {
                "plan_digest":
                    plan.digest,
                "case_digest":
                    digest,
                "fixture_digests":
                    fixture_digests,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )

    return accepted


def _file_sha256(path: Path) -> str:
    if not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _verify_e2e_source_revision(path: Path, expected_digest: str) -> str | None:
    """Return a deterministic mismatch message for an inactive candidate."""
    actual_digest = _file_sha256(path)
    if actual_digest == expected_digest:
        return None
    return (
        "E2E candidate source revision mismatch: "
        f"path={path} expected_digest={expected_digest} "
        f"actual_digest={actual_digest or '(missing)'}"
    )


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
        # Generated source is the final dependency truth.  Including its digest
        # makes a newly added import invalidate dependency preparation.
        deps.append(f"source:{command.script_path}:{_file_sha256(skill_dir / command.script_path)}")
        try:
            entry = _skill_plan_entry_for_file(file_path=command.script_path, blueprint_text=skill_md)
            deps.extend(str(item) for item in (entry.required_capabilities or []))
            refined_contract, resolution = _contract_resolution_for_trial(command.script_path, skill_md, None, None)
            deps.extend(str(item) for item in (refined_contract.declared_dependencies or []))
            deps.extend(str(item) for item in (resolution.declared_dependencies or []))
        except Exception as exc:
            deps.append(f"unresolved:{command.script_path}:{type(exc).__name__}:{exc}")
    return _stable_json_hash(sorted(set(deps)))


_MISSING_PYTHON_MODULE_RE = re.compile(
    r"(?:ModuleNotFoundError|ImportError)[^\n]*No module named ['\"]?([A-Za-z0-9_.-]+)['\"]?",
    re.IGNORECASE,
)


def _missing_python_module(text: str) -> str | None:
    match = _MISSING_PYTHON_MODULE_RE.search(text or "")
    return match.group(1).split(".")[0] if match else None


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
    value_provenance: dict[str, Any] | None = None,
    filesystem_diff: dict[str, Any] | None = None,
    verified_bindings: dict[str, str] | None = None,
    runtime_binding_trace: dict[str, Any] | None = None,
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
        "command_hash": _stable_json_hash(command.raw_command),
        "argv_hash": _stable_json_hash(rendered_payload),
        "context_hash": _stable_json_hash(context_before),
        "workspace_revision": session.current_revision,
        "rendered_argv": rendered_payload,
        "value_provenance": value_provenance or {},
        "filesystem_diff": filesystem_diff or {},
        "verified_bindings": verified_bindings or {},
        "runtime_binding_trace": runtime_binding_trace or {},
        "status": "passed",
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


_E2E_LAYER_RANK = {
    "command_parse": 0,
    "runtime_command_invalid": 0,
    "command_json_parse": 0,
    "placeholder_render": 1,
    "external_input_missing": 1,
    "e2e_dataflow_missing": 1,
    "command_binding": 2,
    "argv_guard": 2,
    "argv_schema_error": 2,
    "script_execution": 3,
    "script_exit": 3,
    "stdout_validation": 4,
    "stdout_contract": 4,
    "stdout_json_parse": 4,
    "stdout_json_type": 4,
    "artifact_validation": 5,
    "artifact": 5,
    "downstream_handoff": 6,
    "final_output": 7,
    "terminal_output_commit": 8,
}


def _e2e_failure_position(error: str) -> tuple[int, int]:
    structured = _structured_failure_from_errors([error])
    step = int(structured.get("failed_step_index") or 0) if structured else 0
    layer = str((structured or {}).get("layer") or _failure_layer_from_error_text(error) or "")
    return (step, _E2E_LAYER_RANK.get(layer, 3))


def _normalize_e2e_failure_text(value: str) -> str:
    """Stabilize volatile runtime values without discarding breakpoint evidence."""
    text = str(value or "")
    text = re.sub(r"/tmp/creator-e2e-session-[^/\s]+", "<SESSION_WORKSPACE>", text)
    text = re.sub(r"(?:[A-Za-z]:)?/[^\s\"']*(?:creator-e2e-session|tmp)[^\s\"']*", lambda m: "<SESSION_WORKSPACE>" + ("/" + m.group(0).rsplit("/", 1)[-1] if "/" in m.group(0) else ""), text)
    text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "<UUID>", text, flags=re.I)
    text = re.sub(r"0x[0-9a-f]+", "<ADDRESS>", text, flags=re.I)
    text = re.sub(r"\b(?:port|PORT)[ =:]\d{2,5}\b", "port=<PORT>", text)
    text = re.sub(r"\b(elapsed|duration)=[0-9]+(?:\.[0-9]+)?\b", r"\1=<DURATION>", text, flags=re.I)
    text = re.sub(r"\bpid=\d+\b", "pid=<PID>", text, flags=re.I)
    text = re.sub(r"\brequest_id=[A-Za-z0-9_-]+\b", "request_id=<REQUEST_ID>", text, flags=re.I)
    text = re.sub(r"^\s*\d{4}-\d\d-\d\d[T ][0-2]\d:[0-5]\d:[0-5]\d(?:[.,]\d+)?(?:Z|[+-]\d\d:?\d\d)?\s*", "", text, flags=re.M)
    return " ".join(text.split())


def _e2e_failure_identity(error: str, *, target_file: str = "") -> dict[str, Any]:
    """Extract a deterministic, structured identity for an E2E breakpoint."""
    structured = _structured_failure_from_errors([error])
    details = structured.get("details") if isinstance(structured.get("details"), dict) else {}
    stderr = str(structured.get("stderr") or "")
    actual = str(structured.get("actual") or "")
    evidence = "\n".join(part for part in (stderr, actual, str(details.get("message") or "")) if part)
    exception = re.findall(r"\b([A-Za-z_][\w.]*(?:Error|Exception))\s*:", evidence)
    traceback_function = ""
    traceback_source_line = ""
    frames = re.findall(r'File "[^"]+", line \d+, in ([^\n]+)\n\s*([^\n]+)', stderr)
    if frames:
        traceback_function, traceback_source_line = frames[-1]
    elif exception:
        # Preserve a concise non-traceback runtime expression where available.
        line = next((line for line in evidence.splitlines() if exception[-1] in line), "")
        traceback_source_line = line
    return {
        "failed_step_index": structured.get("failed_step_index"),
        "target_file": str(structured.get("target_file") or target_file or ""),
        "layer": str(structured.get("layer") or _failure_layer_from_error_text(error) or ""),
        "return_code": structured.get("return_code"),
        "exception_type": exception[-1].split(".")[-1] if exception else "",
        "error_code": str(details.get("failure_code") or structured.get("error_code") or ""),
        "target_region": _normalize_e2e_failure_text(str(structured.get("target_region") or details.get("target_region") or "")),
        "failed_command_digest": _stable_json_hash(_normalize_e2e_failure_text(str(structured.get("failed_command") or ""))),
        "traceback_function": _normalize_e2e_failure_text(traceback_function),
        "traceback_source_line": _normalize_e2e_failure_text(traceback_source_line),
        "normalized_actual": _normalize_e2e_failure_text(actual or stderr),
    }


def _e2e_breakpoint_changed(
    before: dict[str, Any],
    after: dict[str, Any],
) -> bool:
    """
    Return whether runtime evidence moved to a materially different breakpoint.

    A breakpoint is not identified only by:
        step + file + layer + error category.

    Within one execution boundary, multiple independent contractual slots may
    fail sequentially. Repairing one slot and exposing another unresolved slot
    is real progress and must not be rolled back merely because the script,
    workflow step, layer, or error family stayed the same.

    target_region is deterministic failure ownership evidence when present,
    e.g.:
        stdout.<field>
        argv.<field>
        workflow binding region
        artifact return region

    Volatile message/source-text changes alone are not progress.
    """

    stable_boundary_fields = (
        "failed_step_index",
        "target_file",
        "layer",
        "error_code",
        "exception_type",
        "traceback_function",
    )

    same_stable_boundary = all(
        before.get(key)
        == after.get(key)
        for key in stable_boundary_fields
    )

    before_region = str(
        before.get(
            "target_region"
        )
        or ""
    ).strip()

    after_region = str(
        after.get(
            "target_region"
        )
        or ""
    ).strip()

    if same_stable_boundary:
        # Same execution stage does not imply same contractual failure.
        #
        # If deterministic ownership moved from one concrete receiving/output
        # region to another, the previous repair discharged one blocking
        # obligation and revealed the next one. Keep that candidate.
        if (
            before_region
            and after_region
            and before_region
            != after_region
        ):
            return True

        # No structural ownership movement. Changes in traceback/source text
        # under the same boundary are treated as noise or the same unresolved
        # breakpoint.
        return False

    structural_fields = (
        "exception_type",
        "traceback_function",
        "error_code",
        "target_region",
    )

    for key in structural_fields:
        before_value = before.get(
            key
        )
        after_value = after.get(
            key
        )

        if (
            before_value
            and after_value
            and before_value
            != after_value
        ):
            return True

    # normalized_actual is only a fallback when there is no stable
    # structural breakpoint evidence on either side.
    if any(
        before.get(key)
        or after.get(key)
        for key in structural_fields
    ):
        return False

    return bool(
        before.get(
            "normalized_actual"
        )
        and after.get(
            "normalized_actual"
        )
        and before[
            "normalized_actual"
        ]
        != after[
            "normalized_actual"
        ]
    )


def _e2e_behavior_fingerprint(error: str, *, target_file: str) -> str:
    return _stable_json_hash(_e2e_failure_identity(error, target_file=target_file))


def _e2e_diagnosis_family_key(*, repair_target: str, before_failure_identity: dict[str, Any]) -> str:
    return _stable_json_hash({"repair_target": repair_target, "before_failure_identity": before_failure_identity, "target_region": before_failure_identity.get("target_region")})


def _e2e_experiment_key(*, repair_target: str, before_failure_identity: dict[str, Any], patch_digest: str) -> str:
    return _stable_json_hash({"repair_target": repair_target, "before_failure_identity": before_failure_identity, "patch_digest": patch_digest})


def _count_matching_no_progress_attempts(e2e_session: CreatorE2ESession, *, repair_target: str, before_failure_identity: dict[str, Any]) -> int:
    return sum(1 for attempt in e2e_session.debug_attempts if attempt.get("result") == "no_progress" and attempt.get("repair_target") == repair_target and attempt.get("before_failure_identity") == before_failure_identity)


def _e2e_candidate_improved(
    original_errors: list[str],
    new_errors: list[str],
    *,
    target_file: str,
) -> bool:
    """
    Decide whether a localized E2E repair moved the real runtime breakpoint.

    Progress is derived from observed execution state only.

    Legacy pre-execution boundary flags such as fixture_valid /
    argv_shape_valid are intentionally not used here.
    """

    if not new_errors:
        return True

    old_error = (
        original_errors
        or [""]
    )[0]

    new_error = (
        new_errors
        or [""]
    )[0]

    old_structured = (
        _structured_failure_from_errors(
            [old_error]
        )
    )

    new_structured = (
        _structured_failure_from_errors(
            [new_error]
        )
    )

    old_fs = (
        (
            (
                old_structured.get(
                    "details"
                )
                or {}
            ).get(
                "filesystem_trace"
            )
            or {}
        )
        if old_structured
        else {}
    )

    new_fs = (
        (
            (
                new_structured.get(
                    "details"
                )
                or {}
            ).get(
                "filesystem_trace"
            )
            or {}
        )
        if new_structured
        else {}
    )

    old_code = (
        _failure_code_from_structured(
            old_structured
        )
    )

    new_code = (
        _failure_code_from_structured(
            new_structured
        )
    )

    is_artifact_failure = (
        old_code.startswith(
            "artifact_"
        )
        or new_code.startswith(
            "artifact_"
        )
    )

    old_pos = _e2e_failure_position(
        old_error
    )

    new_pos = _e2e_failure_position(
        new_error
    )

    # Moving backwards in the same workflow step is not progress.
    if (
        new_pos[0] == old_pos[0]
        and new_pos[1] < old_pos[1]
    ):
        return False

    if (
        old_pos == new_pos
        and is_artifact_failure
    ):
        return (
            _artifact_runtime_state_improved(
                _artifact_runtime_state(
                    old_fs
                ),
                _artifact_runtime_state(
                    new_fs
                ),
            )
        )

    before = _e2e_failure_identity(
        old_error,
        target_file=target_file,
    )

    after = _e2e_failure_identity(
        new_error,
        target_file=target_file,
    )

    interface_markers = {
        "external_input_missing",
        "missing_placeholder",
        "argv_schema_error",
        "argv_guard",
        "runtime_interface",
        "command_interface",
        "runtime_binding",
    }

    same_interface_boundary = (
        before["target_file"]
        == after["target_file"]
        and (
            before["layer"]
            in interface_markers
            or before["error_code"]
            in interface_markers
        )
        and (
            after["layer"]
            in interface_markers
            or after["error_code"]
            in interface_markers
        )
    )

    # A later real execution layer is progress unless we are merely moving
    # around inside the same unresolved interface boundary.
    if (
        new_pos > old_pos
        and not same_interface_boundary
    ):
        return True

    if (
        (
            before[
                "failed_step_index"
            ],
            before[
                "target_file"
            ],
            before[
                "layer"
            ],
        )
        ==
        (
            after[
                "failed_step_index"
            ],
            after[
                "target_file"
            ],
            after[
                "layer"
            ],
        )
    ):
        return _e2e_breakpoint_changed(
            before,
            after,
        )

    old_target = (
        _e2e_repair_target_from_errors(
            original_errors
            or []
        )
    )

    new_target = (
        _e2e_repair_target_from_errors(
            new_errors
            or []
        )
    )

    return bool(
        old_target == target_file
        and new_target
        and new_target != target_file
    )

_RUNTIME_SENTINEL_RE = re.compile(r"__RUNTIME_INPUT_[A-Z0-9_]*__")


def _e2e_candidate_invariant_veto(
    original_content: str,
    candidate_content: str,
    *,
    original_errors: list[str] | None = None,
    new_errors: list[str] | None = None,
) -> list[str]:
    """
    Reject only genuine frozen-boundary regressions.

    Do not use legacy pre-execution semantic/shape verdict flags here.
    Runtime wiring quality is proven by the next real E2E execution.
    """

    reasons: list[str] = []

    # A localized repair must not introduce synthetic runtime sentinels that
    # were not present in the original implementation.
    before_sentinels = set(
        _RUNTIME_SENTINEL_RE.findall(
            original_content
            or ""
        )
    )

    after_sentinels = set(
        _RUNTIME_SENTINEL_RE.findall(
            candidate_content
            or ""
        )
    )

    if (
        after_sentinels
        - before_sentinels
    ):
        reasons.append(
            "introduced_runtime_sentinel"
        )

    # Model-driven repair may still edit SKILL.md prose, but canonical command
    # blocks are read-only projections of the frozen contract.  Only the
    # deterministic command normalizer may regenerate them.
    before_commands = [
        block.command_body_text.strip()
        for block in parse_skill_md_bash_command_blocks(original_content or "")
    ]
    after_commands = [
        block.command_body_text.strip()
        for block in parse_skill_md_bash_command_blocks(candidate_content or "")
    ]
    if before_commands != after_commands:
        reasons.append("canonical_command_changed_by_model")

    before_structured = (
        _structured_failure_from_errors(
            original_errors
            or []
        )
    )

    after_structured = (
        _structured_failure_from_errors(
            new_errors
            or []
        )
    )

    before_details = (
        before_structured.get(
            "details"
        )
        or {}
    )

    after_details = (
        after_structured.get(
            "details"
        )
        or {}
    )

    def invariant_value(
        source: dict[str, Any],
        *keys: str,
    ) -> Any:
        for key in keys:
            if key in source:
                return source[key]

        frozen = (
            source.get(
                "frozen_invariants"
            )
            or {}
        )

        for key in keys:
            if key in frozen:
                return frozen[key]

        return None

    # Provenance is frozen authority.  A repair cannot silently replace
    # upstream/external data with a literal/default or another producer.
    before_provenance = invariant_value(
        before_details,
        "frozen_provenance",
        "value_provenance",
    )

    after_provenance = invariant_value(
        after_details,
        "frozen_provenance",
        "value_provenance",
    )

    if (
        before_provenance
        is not None
        and after_provenance
        is not None
        and before_provenance
        != after_provenance
    ):
        reasons.append(
            "frozen_provenance_changed"
        )

    # Likewise, an implementation repair cannot rewrite the frozen argv
    # interface merely to make the current candidate pass.
    before_argv = invariant_value(
        before_details,
        "frozen_argv_interface",
        "argv_interface",
    )

    after_argv = invariant_value(
        after_details,
        "frozen_argv_interface",
        "argv_interface",
    )

    if (
        before_argv is not None
        and after_argv is not None
        and before_argv != after_argv
    ):
        reasons.append(
            "frozen_argv_interface_changed"
        )

    return reasons

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
                "- SKILL.md command block 是冻结合同的后台确定性投影，不是模型可修改的接口事实。\n"
                "- 当 canonical command argv 与 script 入口接口不一致时，必须修改当前脚本去对齐合同；禁止修改 command、placeholder 或上游值去迁就脚本。\n"
                "- script 语法、入口/JSON argv、guard 与 run/main、参数消费、stdout/artifact 以及跨步骤字段实现错误，都 target_file=当前脚本。\n"
                "- 上游 stdout key 和下游冻结 binding 不一致时，应修复产生或消费该字段的脚本；不得修改 command binding。通过默认值绕过真实传参，应失败。\n"
                "- 允许脚本通过 payload、input、options、统一对象、别名字段或等价结构接收参数，但必须能证明实际传入的 key 被读取并影响输出。\n"
                "- 如果 rendered_payload 缺少合同要求的信息，报告合同投影基础设施问题；不得提出修改 SKILL.md command。\n"
                "- 如果 SKILL.md 已传对但脚本没有读取、读取了不同 key、或被默认值覆盖，failure_kind=script_not_consuming_payload，target_file=当前脚本。\n"
                "- 如果当前 step 输出了内容但后续冻结字段映射接不上，failure_kind=output_mapping_mismatch，并定位未遵守字段合同的脚本。\n"
                "- 如果没有接口问题，返回 passed=true。\n\n"

                "返回 JSON：\n"
                "{\n"
                "  \"passed\": true|false,\n"
                "  \"target_file\": \"当前脚本路径；合同投影失败时为 creator_e2e\",\n"
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
                "6. command/placeholder 是冻结合同的只读投影，禁止建议修改 SKILL.md；投影缺失时 target_file=creator_e2e。\n"
                "7. 如果脚本没接住传入 key、产出错误字段或使用默认值绕过，target_file=当前脚本。\n"
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
                "target_file": "__validator__",
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
            "target_file": "__validator__",
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

    if failure_kind == "missing_payload":
        target_file = "creator_e2e"
    elif failure_kind in {"script_not_consuming_payload", "output_mapping_mismatch"}:
        target_file = command.script_path
    elif target_file not in {"creator_e2e", command.script_path}:
        target_file = command.script_path

    layer = (
        "e2e_step_argument_mapping"
        if target_file == "creator_e2e"
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
        return {"passed": False, "target_file": "creator_e2e", "layer": "e2e_requirement_mapping_failed", "failure_kind": "missing_payload", "problem": "The canonical command projection did not deliver a required semantic input.", "evidence": "rendered_payload is empty while requirement declares semantic_inputs", "requirement_id": r.id, "missing_evidence": ["semantic input in rendered_payload"], "repair_instruction": "Rebuild or report the frozen command/dataflow projection; do not edit SKILL.md or a business script."}
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
        target_region="frozen command/dataflow projection" if target_file == "creator_e2e" else "run()",
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
        env=_creator_e2e_subprocess_env(trial_skill_dir),
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
        env=_creator_e2e_subprocess_env(trial_skill_dir),
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
        env=_creator_e2e_subprocess_env(trial_skill_dir),
    )


def _creator_e2e_subprocess_env(trial_skill_dir: Path) -> dict[str, str]:
    """Keep side effects in trial mode while allowing isolated local fixtures."""
    return {
        **_build_script_runtime_env(trial_skill_dir),
        "SKILL_WORKDIR": str(trial_skill_dir),
        "SKILL_TRIAL_RUN": "1",
        "CREATOR_E2E_REAL_LOCAL_FIXTURES": "1",
    }


def _extract_failed_argv_keys(text: str) -> list[str]:
    keys: set[str] = set()
    for bracketed in re.findall(r"\[([^\]]+)\]", text):
        keys.update(key for key in re.findall(r"['\"]([^'\"]+)['\"]", bracketed) if key)
    for pattern in (
        r"(?:unknown|unexpected|extra)(?:\s+argv)?\s+keys?\s*[:=]\s*([A-Za-z_][\w.-]*)",
        r"missing(?:\s+required)?(?:\s+argv)?\s+keys?\s*[:=]\s*([A-Za-z_][\w.-]*)",
        r"empty(?:\s+required)?(?:\s+argv)?\s+(?:value|key)\s*[:=]\s*([A-Za-z_][\w.-]*)",
        r"invalid(?:\s+argv)?\s+type(?:\s+for\s+|\s*[:=]\s*)([A-Za-z_][\w.-]*)",
    ):
        keys.update(str(match) for match in re.findall(pattern, text, flags=re.I))
    return sorted(keys)


def _argv_schema_error_kind(stderr: str, stdout: str) -> str | None:
    text = f"{stderr}\n{stdout}".lower()
    if "unexpected keyword argument" in text:
        return None
    if "argv json must be an object" in text or "json argv must be an object" in text:
        return "non_object_argv"
    if "missing json argv" in text:
        return "missing_json_argv"
    if re.search(r"\b(?:unknown|unexpected|extra)\s+(?:argv\s+)?keys?\b", text, re.I):
        return "unknown_key"
    if "missing required" in text or "missing key" in text:
        return "missing_required"
    if "empty required" in text or "empty argv" in text:
        return "empty_required"
    if "invalid type" in text or "argv type" in text:
        return "invalid_type"
    if "argv schema error" in text:
        return "argv_guard"
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


def _strict_argv_type_for_runtime_value(value: Any) -> str:
    """Return the guard type name that accepts an already-rendered JSON value."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if value is None:
        return "null"
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
    runtime_binding_trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    def _same_preview_value(left: Any, right: Any) -> bool:
        if isinstance(left, dict) and isinstance(right, dict):
            left_hash = str(left.get("value_hash") or "")
            right_hash = str(right.get("value_hash") or "")
            if left_hash and right_hash:
                return left_hash == right_hash
        return left == right

    def _binding_forwards_source_unchanged(key: str) -> bool:
        item = (runtime_binding_trace or {}).get(key)
        if not isinstance(item, dict):
            return False

        placeholder_expr = str(item.get("placeholder_expr") or "").strip()
        source_root = str(item.get("source_root") or "").strip()

        if not placeholder_expr or not source_root:
            return False

        source_preview = item.get("source_value_preview")
        rendered_preview = item.get("rendered_value_preview")

        if source_preview is None or rendered_preview is None:
            return False

        return _same_preview_value(
            source_preview,
            rendered_preview,
        )

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

    # Runtime commands are canonical projections of the frozen contract.  A
    # generated script must conform to that projection; E2E must never mutate
    # the command to accommodate a divergent script interface.
    primary_target = command.script_path
    target_reason = (
        "The generated script argv interface does not accept the canonical "
        "command payload projected from the frozen contract."
    )

    failed_runtime_keys = [
        key
        for key in failed_keys
        if key in (rendered_payload or {})
    ]
    canonical_runtime_types = {
        key: _strict_argv_type_for_runtime_value(rendered_payload[key])
        for key in failed_runtime_keys
    }

    failed_command_keys = [
        key
        for key in failed_keys
        if key in (command.argv_template or {})
    ]

    bindings_forwarded_unchanged = bool(
        failed_runtime_keys
        and all(
            _binding_forwards_source_unchanged(key)
            for key in failed_runtime_keys
        )
    )

    if kind in {"non_object_argv", "missing_json_argv"}:
        target_reason = (
            "The generated script did not receive or parse the canonical JSON object argv."
        )

    elif kind == "unknown_key":
        target_reason = (
            "The generated script guard does not declare a key supplied by the "
            "canonical contract command."
        )

    elif kind == "missing_required":
        if failed_keys and not failed_runtime_keys:
            target_reason = (
                "The generated script guard requires a key that is absent from "
                "the canonical contract command payload."
            )
        else:
            target_reason = (
                "The required argv key reached runtime but the script "
                "still reported it as missing; inspect the script argv parser/guard."
            )

    elif kind == "invalid_type":
        if bindings_forwarded_unchanged:
            target_reason = (
                "The canonical command forwarded the upstream runtime value unchanged, "
                "but the generated script strict_json_argv_guard rejected its type."
            )
        else:
            target_reason = (
                "The generated script argv type does not match the rendered canonical "
                "contract payload."
            )

    elif kind == "empty_required":
        if bindings_forwarded_unchanged:
            target_reason = (
                "The canonical command forwarded the upstream runtime value unchanged, "
                "but the generated script strict_json_argv_guard rejected the empty value."
            )
        else:
            target_reason = (
                "The generated script requiredness does not match the rendered canonical "
                "contract payload."
            )
    failed_runtime_keys = [
        key
        for key in failed_keys
        if key in (rendered_payload or {})
    ]

    if (
        kind == "invalid_type"
        and failed_runtime_keys
        and all(
            _binding_forwards_source_unchanged(key)
            for key in failed_runtime_keys
        )
    ):
        primary_target = command.script_path
        target_reason = (
            "The command binding forwarded the upstream runtime value unchanged, "
            "but the generated script strict_json_argv_guard rejected that value."
        )

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
        "canonical_runtime_types": canonical_runtime_types,
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
        "canonical_runtime_types": details.get("canonical_runtime_types"),
        "command_argv_keys": details.get("command_argv_keys"),
        "script_run_required_read_keys": details.get("script_run_required_read_keys"),
        "script_run_optional_read_keys": details.get("script_run_optional_read_keys"),
        "script_run_reads_sys_argv": details.get("script_run_reads_sys_argv"),
        "script_has_strict_json_argv_guard": details.get("script_has_strict_json_argv_guard"),
    }
    common = (
        f"argv_schema_error 归因：{target_reason}\n"
        "SKILL.md command 是冻结合同的后台确定性投影，不是 E2E 模型的可修改接口；"
        "当前生成脚本必须让 argv guard、parse_args 和 run/main 对齐 canonical command payload。"
        "command_argv_keys/script_required_keys 仅作 diagnostics，不作为主提示或新合同。\n"
        f"diagnostics={json.dumps(diagnostics, ensure_ascii=False, sort_keys=True, default=str)}\n"
        "不得修改 SKILL.md command、placeholder 或上游输入值；禁止只改 guard schema，必须同步修复脚本的 guard 与实际消费逻辑。"
        " 对 canonical_runtime_types 中列出的字段，strict_json_argv_guard 必须直接接受该运行时类型；"
        "类型转换只能发生在 guard 成功返回之后，绝不能先用冲突类型的 guard 拒绝该值、再尝试转换。"
    )
    if primary == script_path:
        return common + f"\nprimary_target={script_path}：只修当前脚本中与失败相关的 parse_args/strict_json_argv_guard/run/main/stdout；确保 guard、run(args)、main() 自洽；run(args) 不得读取 guard 未声明 key，不得重新读取 sys.argv/json argv，guard required key 必须被 run(args) 消费；不得改 SKILL.md。"
    return common + f"\nprimary_target={script_path}：command 不可修改；只修当前脚本入口和参数消费逻辑。"


def _parse_e2e_stdout_json(
    *,
    command: E2EWorkflowCommand,
    proc: subprocess.CompletedProcess[str],
    trial_skill_dir: Path,
    content: str,
    entry: SkillPlanEntry,
    rendered_payload: dict[str, Any],
    trial_skill_md: str | None = None,
    value_provenance: dict[str, Any] | None = None,
    filesystem_diff: dict[str, Any] | None = None,
    runtime_binding_trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Parse one real E2E subprocess result.

    Second-round E2E only validates actual process execution, argv failures,
    stdout JSON/object shape, declared file outputs, and real workflow closure.

    SkillPlan/RequirementGraph/canonical stdout requirements are first-round
    planning or responsibility facts and must not be re-applied as hard stdout
    field contracts here.
    """
    _ = trial_skill_md
    parsed_stdout_for_trace: dict[str, Any] = {}
    if proc.returncode == 0:
        try:
            maybe_stdout = json.loads((proc.stdout or "").strip())
            if isinstance(maybe_stdout, dict):
                parsed_stdout_for_trace = maybe_stdout
        except Exception:
            parsed_stdout_for_trace = {}
    reported_paths = _stdout_artifact_paths(parsed_stdout_for_trace, entry, None) if parsed_stdout_for_trace else []
    resolved_reported_paths = resolve_reported_artifact_paths(reported_paths, root=trial_skill_dir)
    created_files = (filesystem_diff or {}).get("created_files") or []
    for item in resolved_reported_paths:
        item["is_created_this_step"] = any(
            str(created.get("absolute_path")) == str(item.get("absolute_path"))
            for created in created_files
        )
    filesystem_trace = {
        "current_working_directory": str((trial_skill_dir / "scripts").resolve()),
        "reported_paths": reported_paths,
        "resolved_reported_paths": resolved_reported_paths,
        "created_files": created_files,
        "modified_files": (filesystem_diff or {}).get("modified_files") or [],
        "deleted_files": (filesystem_diff or {}).get("deleted_files") or [],
    }
    variable_trace = {
        "related_fields": sorted(str(k) for k in (rendered_payload or {}).keys()),
        "binding_chain": [
            {
                "stage": "rendered_argv",
                "target_key": str(k),
                "source_field": str((runtime_binding_trace or {}).get(str(k), {}).get("source_root") or ""),
                "producer_step": (runtime_binding_trace or {}).get(str(k), {}).get("source_provenance", {}).get("producer_step"),
                "producer_script": (runtime_binding_trace or {}).get(str(k), {}).get("source_provenance", {}).get("producer_script"),
            }
            for k in (rendered_payload or {}).keys()
        ],
        "first_bad_step": command.ordinal,
        "runtime_binding_trace": runtime_binding_trace or {},
    }

    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "")[-4000:]
        stdout_tail = (proc.stdout or "")[-4000:]
        missing_module = _missing_python_module(stderr_tail + "\n" + stdout_tail)

        argv_details = _classify_argv_schema_failure(
            command=command,
            content=content,
            entry=entry,
            rendered_payload=rendered_payload,
            stdout=stdout_tail,
            stderr=stderr_tail,
            runtime_binding_trace=runtime_binding_trace,
        )
        is_argv_schema_error = bool(argv_details)

        failure_layer = (
            "argv_schema_error"
            if is_argv_schema_error
            else (
                "runtime_environment_error"
                if missing_module and _is_python_stdlib_module(missing_module)
                else "environment_dependency"
                if missing_module
                else "script_exit"
            )
        )

        target_file = (
            str(
                argv_details.get("primary_target")
                or command.script_path
            )
            if is_argv_schema_error
            else ("runtime_environment" if missing_module else command.script_path)
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
            target_region = "script argv guard and parameter consumption"
            expected = (
                "当前脚本必须接受冻结合同投影出的 canonical command argv，"
                "并保持 strict_json_argv_guard 与 run(args) 入口接口自洽。"
            )
        elif missing_module:
            repair_instruction = (
                "Python module dependency is missing from the runtime environment. "
                "Do not modify the script or remove/guard its import."
            )
            target_region = "python runtime environment"
            expected = "The current skill venv must provide imports used by the final script."
        else:
            repair_instruction = (
                f"根据 {command.script_path} 本次真实 subprocess "
                "stderr traceback、return_code 和实际报错源码行进行最小修复。"
                "只修改异常直接涉及的代码。"
                "ImportError: cannot import name 只能依据真实 callable identity 证据局部修复；"
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
                    details={
                        **(argv_details if is_argv_schema_error else {}),
                        **({
                            "repair_authority": {
                                "mode": "deterministic",
                                "target_file": command.script_path,
                                "expected": {
                                    "canonical_command_payload_shape": {
                                        str(key): _argv_value_shape(value)
                                        for key, value in rendered_payload.items()
                                    },
                                },
                                "observed": {
                                    "script_argv_expected_types": argv_details.get("expected_types") or {},
                                    "argv_schema_error_kind": argv_details.get("argv_schema_error_kind"),
                                },
                                "mutable_scope": [
                                    "script strict_json_argv_guard",
                                    "script parse_args/run/main parameter consumption",
                                ],
                            },
                        } if is_argv_schema_error else {}),
                        "variable_trace": variable_trace,
                        "filesystem_trace": filesystem_trace,
                        "runtime_binding_trace": runtime_binding_trace or {},
                        "failure_code": failure_layer,
                        "missing_module": missing_module,
                        "dependency_kind": (
                            "stdlib_runtime_broken"
                            if missing_module and _is_python_stdlib_module(missing_module)
                            else "third_party_dependency_missing"
                            if missing_module
                            else None
                        ),
                    },
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
        artifact_code = _artifact_failure_code(filesystem_trace)
        is_artifact_validation = _is_artifact_validation_failure(
            error=exc,
            reported_paths=list(filesystem_trace.get("reported_paths") or []),
            entry=entry,
        )
        failure_code = artifact_code if is_artifact_validation else "stdout_contract"
        if failure_code.startswith("artifact_"):
            filesystem_trace["failure_code"] = failure_code
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
                    details={
                        "variable_trace": variable_trace,
                        "filesystem_trace": filesystem_trace,
                        "runtime_binding_trace": runtime_binding_trace or {},
                        "failure_code": failure_code,
                    },
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

    # Do not inspect script source shape here. Creator second-round E2E must
    # prove script validity by launching the real subprocess, then classify
    # failures from return_code/stdout/stderr evidence. First-round generation
    # checks still own source-format, responsibility, and guard-shape review.

    return entry

def _seed_initial_e2e_payload(
    commands: list[E2EWorkflowCommand],
    *,
    external_context: dict[str, Any] | None = None,
    skill_dir: Path | None = None,
    requirements_by_file: dict[str, list[RequirementItem]] | None = None,
    skill_plan_entries: dict[str, SkillPlanEntry] | None = None,
    trial_case: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build the initial E2E runtime payload only from authoritative sources:

    1. real non-empty external_context values;
    2. reviewed/frozen Trial Case fixtures;
    3. frozen/default values.

    Do not invent additional synthetic business values here.
    """

    payload: dict[str, Any] = {}

    # ---------------------------------------------------------
    # 1. Keep actual non-empty external values.
    # Empty platform envelope placeholders such as [] / {}
    # are not treated as authoritative E2E test values.
    # ---------------------------------------------------------
    if isinstance(external_context, dict):
        for key, value in external_context.items():
            if _json_value_non_empty(value):
                payload[str(key)] = copy.deepcopy(value)

    # ---------------------------------------------------------
    # 2. Materialize exactly the reviewed Trial Case.
    # ---------------------------------------------------------
    trial_inputs = {
        str(item.get("name") or ""): item
        for item in (
            (trial_case or {}).get("inputs")
            or []
        )
        if (
            isinstance(item, dict)
            and str(item.get("name") or "").strip()
        )
    }

    if skill_dir is not None:
        for name, item in trial_inputs.items():
            # Real external input wins.
            if _json_value_non_empty(
                payload.get(name)
            ):
                continue

            try:
                payload[name] = (
                    _materialize_e2e_trial_fixture(
                        item,
                        skill_dir=skill_dir,
                    )
                )
            except Exception as exc:
                raise E2ECaseInfrastructureError(
                    "trial_case_materialization_failed",
                    details={
                        "input": name,
                        "error_type":
                            type(exc).__name__,
                        "error": str(exc),
                        "repair_owner":
                            "creator_e2e",
                        "skill_repair_allowed":
                            False,
                    },
                ) from exc

    # ---------------------------------------------------------
    # 3. Fill only frozen/default values.
    # No generic sample fallback.
    # ---------------------------------------------------------
    typed_specs = (
        _collect_e2e_typed_inputs_from_graph(
            commands=commands,
            requirements_by_file=(
                requirements_by_file
                or {}
            ),
            skill_plan_entries=(
                skill_plan_entries
                or {}
            ),
            skill_dir=skill_dir,
        )
    )

    for spec in typed_specs:
        if _json_value_non_empty(
            payload.get(spec.name)
        ):
            continue

        if spec.default_available:
            payload[spec.name] = (
                copy.deepcopy(
                    spec.default_value
                )
            )
            continue

        entry = (
            (skill_plan_entries or {})
            .get(spec.target_file)
        )

        defaults = (
            getattr(
                entry,
                "default_values",
                {},
            )
            or {}
        )

        if spec.name in defaults:
            payload[spec.name] = (
                copy.deepcopy(
                    defaults[spec.name]
                )
            )

    logger.info(
        "[Creator][E2E]"
        "[initial_payload_seeded] %s",
        json.dumps(
            {
                "keys": sorted(
                    payload.keys()
                ),
                "shape":
                    _json_object_shape(
                        payload
                    ),
                "reviewed_trial_inputs":
                    sorted(
                        trial_inputs.keys()
                    ),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ),
    )

    return payload

def _run_skill_workflow_e2e_once(
    skill_name: str,
    *,
    external_context: dict[str, Any] | None = None,
    source_skill_dir: Path | None = None,
    requested_model: str | None = None,
    e2e_session: CreatorE2ESession | None = None,
    resume_from_step: int = 1,
    expected_source_digests: dict[str, str] | None = None,
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

        provided_external_keys = {
            str(key)
            for key in (external_context or {}).keys()
        } if isinstance(external_context, dict) else set()
        typed_input_specs = _collect_e2e_typed_inputs_from_graph(
            commands=commands,
            requirements_by_file=requirements_by_file,
            skill_plan_entries=skill_plan_entries,
            skill_dir=trial_skill_dir,
        )
        try:
            trial_case = _prepare_e2e_trial_case(
                typed_specs=typed_input_specs,
                requirements_by_file=requirements_by_file,
                skill_plan_entries=skill_plan_entries,
                external_context=external_context,
                requested_model=requested_model,
                session=e2e_session,
            )
        except E2ECaseInfrastructureError as exc:
            failure = _e2e_case_infrastructure_failure(exc)
            logger.error("[Creator][E2E][case_plan_failed] %s", failure)
            return [failure]
        payload: dict[str, Any] = _seed_initial_e2e_payload(
            commands,
            external_context=external_context,
            skill_dir=trial_skill_dir,
            requirements_by_file=requirements_by_file,
            skill_plan_entries=skill_plan_entries,
            trial_case=trial_case,
        )
        value_provenance: dict[str, Any] = {
            str(key): _provenance_record(
                step=0,
                script="external_context",
                source_kind="external_context" if str(key) in provided_external_keys else "synthetic_fixture",
                value=value,
            )
            for key, value in payload.items()
        }
        traces: list[E2EStepTrace] = []
        completed_outputs: dict[str, dict[str, Any]] = {}

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
                        scan_result = _scan_and_install_python_deps(
                            trial_skill_dir / command.script_path,
                            venv_python,
                        )
                        if e2e_session is not None:
                            e2e_session.events.append({
                                **e2e_session.to_event_base(),
                                "event": "runtime_dependencies_scanned",
                                "script_path": command.script_path,
                                "details": scan_result,
                            })
                    if e2e_session is not None:
                        e2e_session.installed_deps_signature = deps_signature
                        e2e_session.events.append({**e2e_session.to_event_base(), "event": "dependencies_prepared", "reused_venv": False})
                elif e2e_session is not None:
                    e2e_session.events.append({**e2e_session.to_event_base(), "event": "dependencies_reused", "reused_venv": True})

            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                return [
                    _e2e_error(
                        target="runtime_environment",
                        layer="environment_dependency_prepare_failed",
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
                checkpoint_script = str(checkpoint.get("script_path") or "")
                checkpoint_stdout = checkpoint.get("stdout_json")
                if checkpoint_script and isinstance(checkpoint_stdout, dict):
                    completed_outputs[checkpoint_script] = dict(checkpoint_stdout)
                value_provenance.update(dict(checkpoint.get("value_provenance") or {}))
                if e2e_session is not None:
                    script_key = str(checkpoint.get("script_path") or "")
                    verified = dict(checkpoint.get("verified_bindings") or {})
                    if script_key and verified:
                        e2e_session.verified_bindings_by_script.setdefault(script_key, {}).update({str(k): str(v) for k, v in verified.items()})
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
                    rendered_argv_preview=_preview_object(checkpoint.get("argv_json") or {}),
                    placeholder_bindings=dict(checkpoint.get("runtime_binding_trace") or {}),
                    stdout_preview=_preview_object(checkpoint.get("stdout_json") or {}),
                    value_provenance=dict(checkpoint.get("value_provenance") or {}),
                    created_files=list((checkpoint.get("filesystem_diff") or {}).get("created_files") or []),
                    modified_files=list((checkpoint.get("filesystem_diff") or {}).get("modified_files") or []),
                ))
            if e2e_session is not None:
                e2e_session.events.append({**e2e_session.to_event_base(), "event": "checkpoints_reused", "resume_from_step": resume_from_step, "reused_checkpoints": reused_checkpoints})

        for index, command in enumerate(commands):
            if command.ordinal < resume_from_step:
                continue
            try:
                expected_digest = str(
                    (expected_source_digests or {}).get(command.script_path) or ""
                )
                if expected_digest:
                    revision_error = _verify_e2e_source_revision(
                        trial_skill_dir / command.script_path,
                        expected_digest,
                    )
                    if revision_error:
                        return [
                            _e2e_error(
                                target="creator_e2e",
                                layer="e2e_candidate_revision_mismatch",
                                message=revision_error,
                            )
                        ]
                entry = _attach_requirements_to_entry(_validate_e2e_command_static(
                    command=command,
                    trial_skill_dir=trial_skill_dir,
                    skill_md=trial_skill_md,
                    available_payload_keys=set(payload.keys()),
                ), requirements_by_file.get(command.script_path, []))

                content = (trial_skill_dir / command.script_path).read_text(encoding="utf-8")
                payload_before = dict(payload)

                rendered_payload = _render_e2e_command_payload(
                    command,
                    payload=payload,
                    traces=traces,
                    typed_input_specs=typed_input_specs,
                )

                rendered_payload, runtime_literal_events = (
                    _materialize_rendered_e2e_payload_runtime_literals(
                        rendered_payload,
                        skill_dir=trial_skill_dir,
                        target_file=command.script_path,
                        skill_md=trial_skill_md,
                        script_content=content,
                    )
                )


                logger.info(
                    "[Creator][E2E][rendered_input_validation] %s",
                    json.dumps({
                        "script": command.script_path,
                        "argv_shape": _json_object_shape(rendered_payload),
                    }, ensure_ascii=False, sort_keys=True),
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
                        # Transport-only preview for live Creator review. It does
                        # not participate in execution or validation decisions.
                        "rendered_payload_preview": _preview_object(rendered_payload),
                        "rendered_payload_summary": json.dumps(_json_object_shape(rendered_payload), ensure_ascii=False, sort_keys=True),
                        "trace_summary": _format_e2e_trace(traces)[-2000:],
                    })

                runtime_binding_trace = _runtime_binding_trace(
                    command=command,
                    payload=payload,
                    rendered_payload=rendered_payload,
                    value_provenance=value_provenance,
                )

                boundary_facts = _e2e_runtime_boundary_facts(
                    command=command,
                    payload=payload,
                    rendered_payload=rendered_payload,
                    script_content=content,
                    runtime_binding_trace=runtime_binding_trace,
                )

                logger.info(
                    "[Creator][E2E][runtime_boundary_observation] %s",
                    json.dumps(
                        {
                            "script": command.script_path,
                            "step": command.ordinal,
                            "facts": boundary_facts,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ),
                )
                fs_before = snapshot_runtime_files(trial_skill_dir)

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

                fs_after = snapshot_runtime_files(trial_skill_dir)
                if expected_digest:
                    revision_error = _verify_e2e_source_revision(
                        trial_skill_dir / command.script_path,
                        expected_digest,
                    )
                    if revision_error:
                        return [
                            _e2e_error(
                                target="creator_e2e",
                                layer="e2e_candidate_revision_mismatch",
                                message=revision_error,
                            )
                        ]
                filesystem_diff = diff_runtime_files(fs_before, fs_after)

                stdout_json = _parse_e2e_stdout_json(
                    command=command,
                    proc=proc,
                    trial_skill_dir=trial_skill_dir,
                    trial_skill_md=trial_skill_md,
                    content=content,
                    entry=entry,
                    rendered_payload=rendered_payload,
                    value_provenance=value_provenance,
                    filesystem_diff=filesystem_diff,
                    runtime_binding_trace=runtime_binding_trace,
                )
                completed_outputs[command.script_path] = dict(stdout_json)

                artifact_paths = _stdout_artifact_paths(stdout_json, entry, None)

                before_keys = set(payload.keys())
                new_keys = sorted(set(stdout_json.keys()) - before_keys)
                overwritten: list[dict[str, Any]] = []
                for key, value in stdout_json.items():
                    if key in payload:
                        old_prov = value_provenance.get(str(key), {})
                        overwritten.append({
                            "field": str(key),
                            "old_producer_step": old_prov.get("producer_step"),
                            "new_producer_step": command.ordinal,
                            "old_value_preview": _preview_value(payload.get(key)),
                            "new_value_preview": _preview_value(value),
                        })

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
                    payload_before_preview=_preview_object(payload_before),
                    placeholder_bindings=runtime_binding_trace,
                    rendered_argv_preview=_preview_object(rendered_payload),
                    stdout_preview=_preview_object(stdout_json),
                    payload_changes={
                        "added": _preview_object({key: stdout_json[key] for key in new_keys}),
                        "overwritten": overwritten,
                    },
                    created_files=filesystem_diff.get("created_files") or [],
                    modified_files=filesystem_diff.get("modified_files") or [],
                    value_provenance=dict(value_provenance),
                )
                verified_bindings = _verified_bindings_from_runtime_trace(
                    runtime_binding_trace=runtime_binding_trace,
                    script_content=content,
                    script_path=command.script_path,
                )

                # Strict E2E is deterministic: once the command renders, the script
                # exits successfully, stdout is a valid JSON object that satisfies
                # the declared stdout/artifact contract, and the final platform
                # output is consumable, the workflow is accepted.  The legacy
                # requirement/argument-effect LLM review is intentionally not run
                # here because validator availability or semantic judgement must
                # not block packaging or trigger business-file repair.

                context_before = dict(payload)
                logger.info("[Creator][E2E][stdout_key_sources] %s", json.dumps({
                    "event": "e2e_stdout_key_sources",
                    "script_path": command.script_path,
                    "ordinal": command.ordinal,
                    "stdout_keys": sorted(str(key) for key in stdout_json.keys()),
                }, ensure_ascii=False, default=str))
                payload.update(stdout_json)
                for key, value in stdout_json.items():
                    value_provenance[str(key)] = _provenance_record(step=command.ordinal, script=command.script_path, source_kind="stdout", value=value)

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
                        value_provenance=value_provenance,
                        filesystem_diff=filesystem_diff,
                        verified_bindings=verified_bindings,
                        runtime_binding_trace=runtime_binding_trace,
                    )
                    if verified_bindings:
                        e2e_session.verified_bindings_by_script.setdefault(command.script_path, {}).update(verified_bindings)
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
                structured = _structured_failure_from_errors([message])
                if (
                    e2e_session is not None
                    and venv_python is not None
                    and structured.get("layer") == "environment_dependency"
                ):
                    package = str((structured.get("details") or {}).get("missing_module") or "")
                    attempt_key = f"{venv_python.resolve()}::{package}"
                    if package and attempt_key not in e2e_session.runtime_dependency_attempts:
                        e2e_session.runtime_dependency_attempts.add(attempt_key)
                        e2e_session.events.append({**e2e_session.to_event_base(), "event": "runtime_dependency_missing", "package": package, "step_index": command.ordinal})
                        e2e_session.events.append({**e2e_session.to_event_base(), "event": "runtime_dependency_install_started", "package": package, "python": str(venv_python), "step_index": command.ordinal})
                        try:
                            install_result = _install_python_import_dependency(package, venv_python)
                        except (RuntimeError, subprocess.TimeoutExpired) as install_exc:
                            e2e_session.events.append({**e2e_session.to_event_base(), "event": "runtime_dependency_install_failed", "package": package, "python": str(venv_python), "error": str(install_exc)[-2000:]})
                            errors.append(_e2e_error(target="runtime_environment", layer="environment_dependency_prepare_failed", message=f"package={package}\npython={venv_python}\ninstall_attempted=true\ninstall_result={install_exc}"))
                            break
                        e2e_session.events.append({**e2e_session.to_event_base(), "event": "runtime_dependency_installed", "package": package, "python": str(venv_python), "install_result": install_result, "resume_from_step": command.ordinal})
                        return _run_skill_workflow_e2e_once(
                            skill_name,
                            external_context=external_context,
                            source_skill_dir=trial_skill_dir,
                            requested_model=requested_model,
                            e2e_session=e2e_session,
                            resume_from_step=command.ordinal,
                        )
                if e2e_session is not None:
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

        if not errors:
            portable_output_mappings = parse_runtime_output_mappings(trial_skill_md)
            if portable_output_mappings:
                try:
                    final_platform_payload = project_and_commit_skill_outputs(
                        _SANDBOX_OUTPUT_CONTRACT,
                        trial_skill_md,
                        completed_outputs,
                    )
                    if not final_platform_payload:
                        raise ValueError("terminal output projection produced no platform payload")
                    payload.update(final_platform_payload)
                    if e2e_session is not None:
                        e2e_session.events.append({
                            **e2e_session.to_event_base(),
                            "event": "terminal_outputs_committed",
                            "phase": "e2e_run",
                            "status": "passed",
                            "terminal_edge_count": len(portable_output_mappings),
                            "platform_output_keys": sorted(final_platform_payload),
                            "platform_output_payload": final_platform_payload,
                        })
                except Exception as exc:
                    runtime_violation = _portable_terminal_runtime_contract_violation(
                        mappings=portable_output_mappings,
                        completed_outputs=completed_outputs,
                    )

                    if runtime_violation:
                        errors.append(
                            _e2e_error(
                                target=runtime_violation["target_file"],
                                layer="terminal_output_commit",
                                message=(
                                        str(exc)
                                        + "\nterminal_runtime_contract_violation="
                                        + json.dumps(
                                    runtime_violation,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                    default=str,
                                )
                                ),
                                failed_step_index=len(commands) + 1,
                                failure_code="terminal_producer_contract_violation",
                                target_region=(
                                        "stdout."
                                        + runtime_violation["output_name"]
                                ),
                                repair_instruction=(
                                    "A deterministic runtime contract comparison has already "
                                    "identified the violated boundary and repair owner. "
                                    "Treat details.repair_authority as immutable authority. "
                                    "Modify only the authorized implementation so the observed "
                                    "runtime behavior satisfies expected. "
                                    "Do not rewrite the frozen contract to match the current "
                                    "implementation."
                                ),
                                details={
                                    "repair_authority": {
                                        "mode": "deterministic",
                                        "source": "runtime_contract_comparator",
                                        "target_file": (
                                            runtime_violation["target_file"]
                                        ),
                                        "target_region": (
                                                "stdout."
                                                + runtime_violation["output_name"]
                                        ),
                                        "frozen_boundary": {
                                            "producer": {
                                                "node": (
                                                    runtime_violation[
                                                        "target_file"
                                                    ]
                                                ),
                                                "port": (
                                                    runtime_violation[
                                                        "output_name"
                                                    ]
                                                ),
                                            },
                                            "consumer": {
                                                "node": "platform_output",
                                                "port": (
                                                    runtime_violation[
                                                        "sink_name"
                                                    ]
                                                ),
                                            },
                                        },
                                        "expected": (
                                            runtime_violation["expected"]
                                        ),
                                        "observed": (
                                            runtime_violation["observed"]
                                        ),
                                        "mutable_scope": {
                                            "files": [
                                                runtime_violation[
                                                    "target_file"
                                                ]
                                            ],
                                            "kind": "implementation_only",
                                        },
                                    },
                                },
                            )
                        )
                    else:
                        errors.append(
                            _e2e_error(
                                target="INTERFACE",
                                layer="terminal_output_commit",
                                message=str(exc),
                                failed_step_index=len(commands) + 1,
                                failure_code="upstream_interface_contract_conflict",
                                target_region="frozen terminal binding",
                                repair_instruction=(
                                    "Report the frozen terminal binding and platform sink "
                                    "contract conflict to the upstream Interface owner; "
                                    "E2E must not modify SKILL.md, scripts, or the graph."
                                ),
                            )
                        )

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

def _normalized_debug_hypothesis(value: str) -> str:
    return " ".join(str(value or "").lower().split())


def _is_callable_identity_failure(errors: list[str]) -> bool:
    """Return whether a runtime failure could invite a callable-name change."""
    text = "\n".join(str(error or "") for error in errors or [])
    return bool(
        re.search(r"ImportError[^\n]*cannot import name", text, re.I)
        or re.search(r"NameError:\s*name\s+['\"][^'\"]+['\"]\s+is not defined", text, re.I)
        or re.search(
            r"TypeError:[^\n]*(?:unexpected keyword argument|required positional argument|positional arguments? but|takes .+ argument)",
            text,
            re.I,
        )
    )


def _is_imported_callable_identity_failure(errors: list[str]) -> bool:
    """Return whether ImportError explicitly reports a missing imported name."""
    text = "\n".join(str(error or "") for error in errors or [])
    return bool(re.search(r"ImportError[^\n]*cannot import name", text, re.I))


def _registry_callable_owners(
    context: dict[str, Any] | None,
) -> dict[tuple[str, str], set[str]]:
    """Map explicit Registry callable identities to their selected Tool owners."""
    owners: dict[tuple[str, str], set[str]] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            tool_id = value.get("tool_id")
            import_path = value.get("import_path")
            function_name = value.get("function_name")
            if all(isinstance(item, str) and item.strip() for item in (tool_id, import_path, function_name)):
                identity = (import_path.strip(), function_name.strip())
                owners.setdefault(identity, set()).add(tool_id.strip())
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(context or {})
    return owners


def _registry_callable_identities(context: dict[str, Any] | None) -> set[tuple[str, str]]:
    """Compatibility projection of callable identities from owner-preserving facts."""
    return set(_registry_callable_owners(context))


def _called_identities(tree: ast.AST) -> Counter[tuple[str, str]]:
    """Resolve imported Call identities while retaining occurrence counts."""
    module_aliases: dict[str, str] = {}
    callable_aliases: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.name != "*":
                    callable_aliases[alias.asname or alias.name] = (node.module, alias.name)

    identities: Counter[tuple[str, str]] = Counter()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id in callable_aliases:
            identities[callable_aliases[node.func.id]] += 1
        elif (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in module_aliases
        ):
            identities[(module_aliases[node.func.value.id], node.func.attr)] += 1
    return identities


def _called_registry_tool_context(source: str, context: dict[str, Any] | None) -> dict[str, Any]:
    """Project frozen Registry facts to callables actually used by source."""
    if not isinstance(context, dict):
        return {}
    try:
        called = set(_called_identities(ast.parse(source)))
    except (SyntaxError, ValueError):
        return {}
    owners = _registry_callable_owners(context)
    used_tool_ids = {tool_id for identity in called for tool_id in owners.get(identity, set())}
    resolved = [
        tool for tool in (context.get("resolved_tools") or [])
        if isinstance(tool, dict)
        and (str(tool.get("import_path") or ""), str(tool.get("function_name") or "")) in called
    ]
    if not resolved:
        return {}
    return {
        "authorization_scope": context.get("authorization_scope", "skill"),
        "read_only": True,
        "binding_digest": context.get("binding_digest"),
        # Do not expose Skill-wide alternatives to localized repair.  The owner
        # ids and resolved facts below describe only callables present in this
        # exact source file.
        "selected_tool_ids": sorted(used_tool_ids),
        "used_tool_ids": sorted(used_tool_ids),
        "resolved_tools": resolved,
    }


def _callable_owner_counter(
    calls: Counter[tuple[str, str]],
    owners: dict[tuple[str, str], set[str]],
    selected_tool_ids: set[str],
) -> tuple[Counter[str], Counter[tuple[str, str]], Counter[tuple[str, str]]]:
    """Project calls to unique Tool owners, retaining unresolved/ambiguous facts."""
    owner_counts: Counter[str] = Counter()
    unresolved: Counter[tuple[str, str]] = Counter()
    ambiguous: Counter[tuple[str, str]] = Counter()
    for identity, count in calls.items():
        identity_owners = set(owners.get(identity) or set())
        if identity[1] in selected_tool_ids:
            identity_owners.add(identity[1])
        if not identity_owners:
            unresolved[identity] += count
        elif len(identity_owners) > 1:
            ambiguous[identity] += count
        else:
            owner_counts[next(iter(identity_owners))] += count
    return owner_counts, unresolved, ambiguous


def _unauthorized_callable_identity_change(
    before: str,
    after: str,
    context: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Reject new/replaced Call identities without exact selected Registry evidence."""
    try:
        before_tree = ast.parse(before)
        after_tree = ast.parse(after)
    except (SyntaxError, ValueError):
        return []

    before_calls = _called_identities(before_tree)
    after_calls = _called_identities(after_tree)
    if before_calls == after_calls:
        return []

    owners = _registry_callable_owners(context)
    selected_tool_ids = {
        str(tool_id)
        for tool_id in ((context or {}).get("selected_tool_ids") or [])
        if str(tool_id).strip()
    }

    before_owner_counts, before_unresolved, before_ambiguous = _callable_owner_counter(
        before_calls, owners, selected_tool_ids,
    )
    after_owner_counts, after_unresolved, after_ambiguous = _callable_owner_counter(
        after_calls, owners, selected_tool_ids,
    )
    added = after_calls - before_calls
    removed = before_calls - after_calls
    changed_identities = set(added) | set(removed)

    if any(identity in after_unresolved for identity in added):
        reason = "callable_repair_evidence_missing"
    elif any(identity in before_unresolved for identity in removed):
        reason = "callable_origin_tool_unresolved"
    elif any(identity in before_ambiguous or identity in after_ambiguous for identity in changed_identities):
        reason = "callable_origin_tool_ambiguous"
    elif before_owner_counts != after_owner_counts:
        reason = "callable_tool_identity_changed"
    else:
        return []

    return [{
        "reason": reason,
        "before_tool_owner_counts": dict(sorted(before_owner_counts.items())),
        "after_tool_owner_counts": dict(sorted(after_owner_counts.items())),
        "before_callable_identities": [list(identity) for identity in sorted(removed.elements())],
        "after_callable_identities": [list(identity) for identity in sorted(added.elements())],
    }]


def _is_skill_repair_target(skill_dir: Path, target: str) -> bool:
    if target == "SKILL.md":
        return (skill_dir / target).is_file()
    return bool(re.fullmatch(r"scripts/.+\.py", target or "")) and (skill_dir / target).is_file()


async def _diagnose_e2e_failure_for_repair(*, skill_name: str, skill_dir: Path, e2e_errors: list[str], e2e_session: CreatorE2ESession, requested_model: str | None = None, retry_reason: str = "", read_only_callable_context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Select one new in-skill repair target from the current E2E breakpoint."""
    failure = _structured_failure_from_errors(e2e_errors)
    symptom = str(failure.get("target_file") or _e2e_symptom_file_from_errors(e2e_errors) or "SKILL.md")
    details = failure.get("details") if isinstance(failure.get("details"), dict) else {}
    workspace = e2e_session.workspace_dir
    traces = [event for event in e2e_session.events if event.get("event") in {"step_passed", "checkpoint_saved"}][-8:]
    related = {"SKILL.md": (workspace / "SKILL.md").read_text(encoding="utf-8", errors="replace")[-9000:]}
    scripts_dir = workspace / "scripts"
    if scripts_dir.is_dir():
        for path in sorted(scripts_dir.glob("*.py")):
            rel = path.relative_to(workspace).as_posix()
            related[rel] = path.read_text(encoding="utf-8", errors="replace")[-(12000 if rel == symptom else 4000):]
    skill_text = related["SKILL.md"]
    declared_paths = sorted(set(re.findall(r"(?<![\w.-])((?:references|assets)/[^\s)`'\"]+)", skill_text)))
    resource_facts = [{
        "relative_path": rel,
        "exists_in_workspace": (workspace / rel).exists(),
        "resolved_workspace_absolute_path": str((workspace / rel).resolve()),
    } for rel in declared_paths]
    diagnosed_script = symptom
    if symptom == "SKILL.md":
        failed_command = str(failure.get("failed_command") or "").strip()
        commands = _extract_e2e_workflow_commands(workspace, skill_text)
        matched_command = next(
            (command for command in commands if command.raw_command.strip() == failed_command),
            None,
        )
        if matched_command is not None:
            diagnosed_script = matched_command.script_path
    script_path = workspace / diagnosed_script
    script_schema = {}
    if diagnosed_script.startswith("scripts/") and script_path.is_file():
        try:
            script_schema = extract_python_strict_argv_schema(script_path.read_text(encoding="utf-8"))
        except Exception:
            script_schema = {}
    runtime_filesystem_facts = {
        "workspace_root": str(workspace.resolve()),
        "current_working_directory": str(
            (details.get("filesystem_trace") or {}).get("current_working_directory")
            or details.get("subprocess_cwd")
            or (workspace / "scripts").resolve()
        ),
        "current_script_path": str(script_path.resolve()), "current_script_directory": str(script_path.resolve().parent),
        "declared_dependencies": declared_paths, "declared_reference_paths": [p for p in declared_paths if p.startswith("references/")],
        "declared_asset_paths": [p for p in declared_paths if p.startswith("assets/")], "declared_resources": resource_facts,
    }
    argv_provenance_facts = {
        "current_skill_command_argv": failure.get("failed_command") or "",
        "script_actual_argv_contract": script_schema,
        "available_platform_input_fields": _platform_io_repair_summary(),
        "available_previous_step_stdout_fields": traces,
        "responsibility_graph_provenance": (read_only_callable_context or {}).get("function_execution_context", {}),
        "missing_placeholder_paths": details.get("missing_placeholders") or details.get("missing_placeholder_paths") or [],
        "missing_placeholder_roots": details.get("missing_placeholder_roots") or [],
        "runtime_payload_shape": failure.get("rendered_payload") or details.get("rendered_payload") or {},
    }
    route = route_creator_file_model(file_path=symptom, purpose="E2E failure debug diagnosis only; select one repair target", requested_model=requested_model)
    rejected = [a for a in e2e_session.debug_attempts if a.get("result") == "no_progress"]
    prompt = {"structured_failure": failure, "symptom_file": symptom, "layer": failure.get("layer"), "filesystem_trace": details.get("filesystem_trace", {}), "runtime_filesystem_facts": runtime_filesystem_facts, "argv_interface_provenance_facts": argv_provenance_facts, "runtime_binding_trace": details.get("runtime_binding_trace", {}), "previous_step_traces": traces, "skill_files": related, "platform_io_facts": _platform_io_repair_summary(), "read_only_callable_context": read_only_callable_context or {}, "previous_debug_attempts": rejected, "retry_reason": retry_reason}
    logger.info("[Creator][e2e_diagnosis] source_digest=%s", _stable_json_hash(related))
    callable_boundary = (
        " For the behavior of an already-called Registry callable, read_only_callable_context is authoritative. "
        "Authority order: (1) actual runtime traceback and rendered runtime evidence; (2) the exact "
        "read_only_callable_context of the already-called callable; (3) current source code; (4) model inference "
        "only when the contract is silent. If output_schema, return_contract, or example_return explicitly states "
        "a shape, do not infer a conflicting shape from general knowledge. Do not guess list versus object, field "
        "names, parameter names, return fields, or nested item shape when the callable contract states them. If "
        "actual runtime evidence conflicts with that contract, report an unresolved contract conflict; do not guess "
        "which is correct, switch tools, or make a functionality judgment. "
        " When the failing script calls a Registry Tool, first locate the traceback/runtime line, read its signature, "
        "return_contract, and example_return, then compare arguments, return-field reads, and actual stdout/stderr. "
        "Tool usage is read-only evidence, not permission to add, remove, switch, or rediscover tools. If evidence "
        "already identifies the root cause, do not propose only changing exception text, debug prints, logging, or traceback output. "
        " For import, name, or signature failures, do not infer a replacement callable from traceback wording or "
        "follow Python 'Did you mean' suggestions as authorization. Do not use semantic or naming similarity, "
        "source-code autocomplete, module discovery, or general model knowledge. A callable identity change is valid "
        "only when the exact import_path/function_name appears in read_only_callable_context. If no matching Registry "
        "fact exists, report callable_repair_evidence_missing instead of proposing another callable. Even when multiple "
        "tools are authorized in the Skill-wide ToolPool, E2E repair must not switch from one tool to another. Callable "
        "repair may only correct the invocation of the tool already represented by the failing source call."
    )
    messages = [{"role": "system", "content": "You diagnose a Creator E2E breakpoint. Output only JSON. Select exactly one primary repair_target from the existing scripts/*.py in this Skill. SKILL.md runtime commands are immutable deterministic projections of the frozen contract and must never be edited by a model. Do not propose edits to commands, placeholders, backend/runtime, contracts, or tools." + callable_boundary}, {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, default=str) + "\nReturn {repair_target, root_cause_hypothesis, evidence, repair_instruction, confidence, callable_contract_evidence}. Include callable_contract_evidence only when read_only_callable_context is non-empty, and cite the exact callable facts that ground the diagnosis. These are real Sandbox experiments with stable failure identities and patch digests. Do not re-propose the same target, breakpoint, and repair region merely by changing hypothesis wording." + callable_boundary}]
    for proposal_attempt in range(3):
        try:
            text = _complete_chat_once_sync_for_e2e(messages, route.model)
            data = _parse_validator_json_object(str(text or "")) or {}
        except Exception as exc:
            logger.warning("[Creator][E2E][diagnosis_unavailable] symptom=%s error=%s", symptom, exc)
            data = {"repair_target": symptom, "root_cause_hypothesis": "Diagnosis model unavailable; verify the runtime symptom with one minimal localized patch.", "evidence": [str(exc)[:300]], "repair_instruction": "Make only the minimum patch supported by the runtime failure.", "confidence": "low"}
        target, hypothesis = str(data.get("repair_target") or "").strip(), str(data.get("root_cause_hypothesis") or "").strip()
        key = f"{target}|{_normalized_debug_hypothesis(hypothesis)}"
        valid = target != "SKILL.md" and _is_skill_repair_target(workspace, target) and bool(hypothesis)
        # Hypothesis wording is explanatory only. Actual experiment deduplication
        # happens after a patch digest exists, before Sandbox is invoked.
        if valid:
            result = {"repair_target": target, "symptom_file": symptom, "root_cause_hypothesis": hypothesis, "evidence": data.get("evidence") or [], "repair_instruction": str(data.get("repair_instruction") or ""), "confidence": data.get("confidence") or "", "hypothesis_key": key}
            if read_only_callable_context:
                result["callable_contract_evidence"] = data.get("callable_contract_evidence") or []
            return result
        messages.append({"role": "user", "content": "Your proposal was invalid. Return a different valid repair target JSON proposal."})
    return {"status": "diagnosis_exhausted", "symptom_file": symptom}

async def _review_failed_e2e_after_local_repairs(
    *,
    skill_name: str,
    target_path: str,
    current_content: str,
    skill_md: str,
    e2e_errors: list[str],
    diagnosis: dict[str, Any],
    minimal_repair_context: dict[str, Any],
    e2e_entry_context: dict[str, Any],
    e2e_session: CreatorE2ESession,
    requested_model: str | None = None,
) -> dict[str, Any]:
    """
    E2E 局部修复多次失败后，交给 Reviewer 做第二轮责任审查。

    Reviewer 只分析：
    - 当前真实 runtime failure；
    - 当前目标文件实现；
    - 已冻结/已知的当前文件合同；
    - 前面局部 repair 为什么没有解决。

    Reviewer 不写代码、不直接修改合同。
    """

    route = route_model(
        VALIDATOR_TASK,
        requested_model=requested_model,
        reason=(
            "Creator E2E local repair exhausted; "
            "second-round reviewer re-analysis"
        ),
    )

    review_payload = {
        "skill_name": skill_name,
        "target_file": target_path,

        "structured_failure": (
            _structured_failure_from_errors(e2e_errors)
        ),

        "current_target_content": (
            current_content[-18000:]
        ),

        # SKILL.md 只作为当前 workflow/runtime 上下文，
        # 不是让 reviewer 重写它。
        "current_skill_md": (
            skill_md[-12000:]
        ),

        # 当前文件已经解析出的接口/职责事实。
        "entry_contract": (
            e2e_entry_context or {}
        ),

        "runtime_repair_context": (
            minimal_repair_context or {}
        ),

        "previous_e2e_diagnosis": (
            diagnosis or {}
        ),

        # 只给最近几次现场修复记录，防止 prompt 无限增长。
        "local_repair_history": (
            list(e2e_session.debug_attempts or [])[-6:]
        ),
    }

    system_prompt = """
你是 Superskills Creator 的第二轮 Reviewer。

第一轮开发人员已经生成了文件，第一轮 Reviewer 也已经审阅过。
现在真实 E2E 试运行失败，并且现场局部 repair 已经尝试但没有稳定解决。

你的职责不是写代码，而是根据：
1. 当前冻结/已知合同；
2. 当前文件实现；
3. 真实 runtime traceback / rendered argv / binding；
4. 已尝试的局部修复记录；

重新判断问题。

优先假设合同和第一轮设计仍然有效。
不要因为代码难修、patch no-op、算法复杂，就宣称合同有问题。

只有合同本身确实没有定义合法行为，
或者合同之间存在真实冲突，
才能返回 contract_review_required。

如果合同已经足够明确，而当前实现没有完整履行合同，
返回 repairable，并给代码模型明确、局部、可执行的修改要求。

不得输出代码。
不得输出 patch。
不得重新生成整个文件。
不得建议整函数重写或整文件重写。
不得修改 Trial Case 来迁就业务代码。

只输出 JSON。
""".strip()

    user_prompt = (
        json.dumps(
            review_payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        )
        + """

返回：

{
  "status": "repairable" | "contract_review_required" | "invalid_e2e_case",
  "issue_type": "wiring" | "implementation" | "contract_gap" | "e2e_case",
  "repair_target": "SKILL.md 或当前 Skill 中已有 scripts/*.py；contract gap 时可为空",
  "root_cause": "明确根因",
  "violated_requirement_ids": [],
  "contract_evidence": [],
  "runtime_evidence": [],
  "repair_instructions": [],
  "must_preserve": []
}

规则：

- wiring：
  command / placeholder / argv / binding / handoff 有问题。

- implementation：
  接口已经正确到达，但脚本实现没有完成已有合同。

- contract_gap：
  只有当前合同本身不足以确定正确行为时才能使用。

- invalid_e2e_case：
  必须有明确冻结输入合同证据证明当前 Trial Case 非法，
  不能仅因为当前脚本跑不通就判定 case 非法。

repairable 时必须提供具体 repair_instructions，
但不能输出源码。
"""
    )

    messages = [
        {
            "role": "system",
            "content": system_prompt,
        },
        {
            "role": "user",
            "content": user_prompt,
        },
    ]

    last_error = ""

    for _ in range(2):
        try:
            text = _complete_chat_once_sync_for_e2e(
                messages,
                route.model,
            )

            data = (
                _parse_validator_json_object(
                    str(text or "")
                )
                or {}
            )

            status = str(
                data.get("status") or ""
            ).strip()

            issue_type = str(
                data.get("issue_type") or ""
            ).strip()

            repair_target = str(
                data.get("repair_target") or ""
            ).strip()

            if status not in {
                "repairable",
                "contract_review_required",
                "invalid_e2e_case",
            }:
                raise ValueError(
                    f"invalid reviewer status: {status!r}"
                )

            if status == "repairable":
                if not repair_target:
                    raise ValueError(
                        "repairable reviewer result "
                        "must provide repair_target"
                    )

                if not _is_skill_repair_target(
                    e2e_session.workspace_dir,
                    repair_target,
                ):
                    raise ValueError(
                        "reviewer returned invalid "
                        f"repair_target={repair_target!r}"
                    )

                instructions = (
                    data.get("repair_instructions")
                    or []
                )

                if not instructions:
                    raise ValueError(
                        "repairable reviewer result "
                        "must provide repair_instructions"
                    )

            result = {
                "status": status,
                "issue_type": issue_type,
                "repair_target": repair_target,
                "root_cause": str(
                    data.get("root_cause") or ""
                ),
                "violated_requirement_ids": list(
                    data.get(
                        "violated_requirement_ids"
                    )
                    or []
                ),
                "contract_evidence": list(
                    data.get(
                        "contract_evidence"
                    )
                    or []
                ),
                "runtime_evidence": list(
                    data.get(
                        "runtime_evidence"
                    )
                    or []
                ),
                "repair_instructions": list(
                    data.get(
                        "repair_instructions"
                    )
                    or []
                ),
                "must_preserve": list(
                    data.get(
                        "must_preserve"
                    )
                    or []
                ),
            }

            logger.info(
                "[Creator][E2E]"
                "[reviewer_reanalysis_result] %s",
                json.dumps(
                    {
                        "target_file": target_path,
                        "status": result["status"],
                        "issue_type": result["issue_type"],
                        "repair_target": result["repair_target"],
                        "violated_requirement_ids": (
                            result[
                                "violated_requirement_ids"
                            ]
                        ),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
            )

            return result

        except Exception as exc:
            last_error = str(exc)

            messages.append({
                "role": "user",
                "content": (
                    "上一轮 Reviewer JSON 不合法。"
                    f"error={last_error}\n"
                    "请严格按照指定 JSON schema "
                    "重新输出，不要输出解释或代码。"
                ),
            })

    logger.warning(
        "[Creator][E2E]"
        "[reviewer_reanalysis_unavailable] "
        "target=%s error=%s",
        target_path,
        last_error,
    )

    return {
        "status": "review_unavailable",
        "issue_type": "",
        "repair_target": "",
        "root_cause": last_error,
        "violated_requirement_ids": [],
        "contract_evidence": [],
        "runtime_evidence": [],
        "repair_instructions": [],
        "must_preserve": [],
    }

async def _repair_existing_file_for_e2e_failure(
    *,
    skill_name: str,
    target_path: str,
    e2e_errors: list[str],
    requested_model: str | None = None,
    external_context: dict[str, Any] | None = None,
    repair_events: list[dict[str, Any]] | None = None,
    e2e_session: CreatorE2ESession | None = None,
    read_only_callable_context: dict[str, Any] | None = None,
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

    structured_failure = _structured_failure_from_errors(e2e_errors)
    if _failure_code_from_structured(structured_failure) == "runtime_io_mapping_failure":
        # Runtime mapping is execution-session state.  It may be replanned, but
        # E2E repair must never turn this failure into permission to edit the
        # frozen Interface Contract (or any Skill source file).
        details = structured_failure.get("details") if isinstance(structured_failure.get("details"), dict) else {}
        return {
            "status": "runtime_io_mapping_replan_required",
            "repaired_target": None,
            "next_target": "RUNTIME_IO_MAPPING",
            "runtime_mapping_path": details.get("runtime_mapping_path"),
            "next_failure": e2e_errors,
            "error_type": "runtime_io_mapping_failure",
            "interface_contract_mutable": False,
            "sandbox_executed": False,
        }
    if _failure_code_from_structured(structured_failure) == "upstream_interface_contract_conflict":
        return {
            "status": "upstream_handoff_required",
            "repaired_target": None,
            "next_target": "INTERFACE",
            "next_failure": e2e_errors,
            "error_type": "upstream_interface_contract_conflict",
            "sandbox_executed": False,
        }

    # target_path is the runtime symptom location supplied by the validator, not a
    # confirmed repair target. Diagnose before any localized-scope decision.
    skill_dir = settings.skills_path / skill_name
    standalone_repair = e2e_session is None
    if e2e_session is None:
        e2e_session = _create_e2e_session(skill_name, source_skill_dir=skill_dir)
    before_identity = _e2e_failure_identity((e2e_errors or [""])[0], target_file=target_path)
    boundary_facts = (
            structured_failure.get("details")
            or {}
    )

    repair_authority = (
        _deterministic_repair_authority(
            structured_failure
        )
    )

    authority_target = str(
        repair_authority.get("target_file")
        or ""
    ).strip()

    if (
            authority_target
            and not _is_skill_repair_target(
        e2e_session.workspace_dir,
        authority_target,
    )
    ):
        # Invalid authority metadata must never become permission
        # to edit an arbitrary file.
        repair_authority = {}
        authority_target = ""

    # A rejected failure/target experiment is terminal for that pair.  Do this
    # before diagnosis so changing hypothesis prose cannot reopen it.
    rejected_target = target_path

    prior_no_progress_count = (
        _count_matching_no_progress_attempts(
            e2e_session,
            repair_target=rejected_target,
            before_failure_identity=before_identity,
        )
    )

    # 只要同一 target + breakpoint 曾经已经做过局部修复，
    # 且确认没有进展，就不要再次重复同样的普通 E2E repair。
    # 下一阶段交给 Reviewer 带着现场证据重新审合同和实现。
    force_reviewer_reanalysis = (
            prior_no_progress_count > 0
    )

    if force_reviewer_reanalysis:
        logger.info(
            "[Creator][E2E]"
            "[local_repair_exhausted] %s",
            json.dumps(
                {
                    "target_file": rejected_target,
                    "failure_identity": before_identity,
                    "no_progress_count": (
                        prior_no_progress_count
                    ),
                    "reason": (
                        "previous_local_repair_no_progress"
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ),
        )
    if target_path.startswith("scripts/") and read_only_callable_context:
        source_path = e2e_session.workspace_dir / target_path
        source = source_path.read_text(encoding="utf-8", errors="replace") if source_path.is_file() else ""
        read_only_callable_context = _called_registry_tool_context(source, read_only_callable_context)
    if _is_imported_callable_identity_failure(e2e_errors) and not read_only_callable_context:
        return {
            "status": "still_failed_same_target",
            "repaired_target": target_path,
            "next_target": None,
            "next_failure": e2e_errors,
            "rejection_reason": "callable_repair_evidence_missing",
            "last_failure": "callable_repair_evidence_missing",
            "error_type": "callable_repair_evidence_missing",
            "sandbox_executed": False,
        }
    if target_path == "SKILL.md" and _is_skill_md_command_format_error(e2e_errors):
        diagnosis = {
            "repair_target": "SKILL.md",
            "symptom_file": "SKILL.md",
            "root_cause_hypothesis": "Canonical command regeneration is required.",
            "evidence": ["command format validation"],
            "repair_instruction": "Run only the deterministic command normalizer.",
            "confidence": "deterministic",
            "hypothesis_key": "deterministic-command-normalizer",
        }
    elif repair_authority:
        diagnosis = {
            "repair_target": authority_target,
            "symptom_file": target_path,
            "root_cause_hypothesis": (
                "A deterministic runtime contract comparison "
                "has already identified the violated boundary."
            ),
            "evidence": [
                repair_authority
            ],
            "repair_instruction": (
                "Treat repair_authority as immutable. "
                "Do not choose another repair target and do not "
                "reinterpret the frozen boundary or expected contract. "
                "Derive the smallest implementation behavior delta "
                "that makes observed satisfy expected. "
                "Preserve every contract and behavior outside "
                "mutable_scope."
            ),
            "confidence": "deterministic",
            "hypothesis_key": (
                    "deterministic-repair-authority:"
                    + _stable_json_hash(
                repair_authority
            )
            ),
        }

    else:
        diagnosis = await (
            _diagnose_e2e_failure_for_repair(
                skill_name=skill_name,
                skill_dir=skill_dir,
                e2e_errors=e2e_errors,
                e2e_session=e2e_session,
                requested_model=requested_model,
                read_only_callable_context=(
                    read_only_callable_context
                ),
            )
        )
    if diagnosis.get("status") == "diagnosis_exhausted":
        return {"status": "diagnosis_exhausted", "repaired_target": None, "next_target": None, "next_failure": e2e_errors}
    target_path = diagnosis["repair_target"]
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
                "sandbox_executed": False,
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

        if repair_events is not None:
            repair_events.append({
                "type": "command_contract_regeneration_required",
                "target_file": "SKILL.md",
                "normalizer_attempted": normalizer_attempted,
                "payload": payload,
            })

        # Commands are deterministic projections of the frozen contract.  If
        # the normalizer cannot regenerate one, an LLM patch would create a
        # second, untrusted interface authority.  Return the failure to the
        # Creator command-contract layer without editing SKILL.md.
        return {
            "status": "command_contract_regeneration_required",
            "repaired_target": None,
            "next_target": "CREATOR_COMMAND_CONTRACT",
            "next_failure": e2e_errors,
            "error_type": "command_normalizer_blocked" if normalization.blocked else "canonical_command_mismatch",
            "command_mutable_by_model": False,
            "sandbox_executed": False,
        }

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
            "不得重新检查脚本职责、RequirementGraph coverage 或 required_capabilities。",
            "不得重新选择、扩展、删除或重排 ToolPool；不得重新判断工具是否应该承担当前职责；"
            "不得请求工具探索或 tool_pool_patch。",
            "E2E 阶段禁止工具库探索：不得请求 tool_pool_patch.add_tool_requests，"
            "不得探索或扩展工具池。",
            "当且仅当真实 traceback 是 import/name/signature 错误时，可以读取 "
            "read_only_callable_context 中已经授权的 Registry callable facts，修正当前报错调用的 "
            "import_path、function_name、signature、参数名或返回字段读取；该 context 不是新的工具选择建议。",
            "Only modify callable identity to an exact Registry callable contained in read_only_callable_context. "
            "Do not invent, infer, autocomplete, substitute, or choose a callable outside that context. "
            "Traceback suggestions are evidence of Python namespace similarity only; they are not Tool authorization.",
            "Do not replace the failing Tool with another Tool from the same Skill ToolPool. "
            "A replacement callable must belong to the same Registry tool identity as the failing call being repaired.",
            "若 E2E 发现缺少第三方依赖，交给 dependency/environment 链路处理，"
            "不得通过重新选工具或改业务职责绕过。",
            "优先输出 edits old_lines/new_lines exact_replace patch，不要输出完整文件。",
        ),
    )

    e2e_entry_context: dict[str, Any] = {}
    generated_script_source = ""

    if target_path.startswith("scripts/"):
        script_path = e2e_session.workspace_dir / target_path
        if script_path.is_file():
            generated_script_source = script_path.read_text(
                encoding="utf-8",
                errors="replace",
            )
        try:
            e2e_entry = (
                _skill_plan_entry_for_file(
                    file_path=target_path,
                    blueprint_text=skill_md,
                )
            )

            script_content = generated_script_source

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

            raw_requirements = (
                    getattr(
                        e2e_entry,
                        "requirements",
                        [],
                    )
                    or []
            )

            normalized_requirements = []

            for item in raw_requirements:
                if hasattr(item, "model_dump"):
                    try:
                        normalized_requirements.append(
                            item.model_dump(mode="json")
                        )
                        continue
                    except Exception:
                        pass

                normalized_requirements.append(
                    str(item)
                )

            e2e_entry_context = {
                "path": target_path,

                "runtime": getattr(
                    e2e_entry,
                    "runtime",
                    "",
                ),

                "purpose": getattr(
                    e2e_entry,
                    "purpose",
                    "",
                ),

                "inputs": list(
                    getattr(
                        e2e_entry,
                        "inputs",
                        [],
                    )
                    or []
                ),

                "outputs": list(
                    getattr(
                        e2e_entry,
                        "outputs",
                        [],
                    )
                    or []
                ),

                "dependencies": list(
                    getattr(
                        e2e_entry,
                        "dependencies",
                        [],
                    )
                    or []
                ),

                "runtime_contract": (
                        getattr(
                            e2e_entry,
                            "runtime_contract",
                            {},
                        )
                        or {}
                ),

                "artifact_contract": (
                        getattr(
                            e2e_entry,
                            "artifact_contract",
                            {},
                        )
                        or {}
                ),

                "coverage_requirements": (
                        getattr(
                            e2e_entry,
                            "coverage_requirements",
                            [],
                        )
                        or []
                ),

                "requirements": (
                    normalized_requirements
                ),

                "script_argv_schema": (
                    argv_schema
                ),

                "run_args_analysis": (
                    run_args_analysis
                ),
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
            "Before repairing generated code, compare the implementation with the declared input/output contracts.\n"
            "Determine whether the failure is caused by a contract mismatch, implementation logic error, "
            "or runtime/environment issue.\n"
            "If the failure is caused by contract mismatch, modify the implementation to consume the declared "
            "contract; do not add temporary conversions or defensive patches that hide the mismatch.\n"
            "Fix the root cause instead of only removing the current traceback.\n"
            "Repair should modify only the implementation related to the contract mismatch or runtime failure.\n"
            "Do not redesign the Skill workflow, introduce new inputs, change interface semantics, or add "
            "task-specific hard-coded rules.\n"
            "你正在验证一个调试假设，只修改指定 repair_target；不得修改其它文件、顺带重构或重新规划职责。\n"
            "只依据本轮结构化失败中的 failed_command、rendered_payload、"
            "stdout、stderr、return_code、失败层和已成功前序 trace 定位问题。\n"
            "修复范围必须直接对应真实失败证据。\n"
            "如果是 argv_schema_error，只核对当前 command JSON argv、"
            "strict_json_argv_guard schema 和 run(args) 实际读取关系。\n"
            "strict_json_argv_guard 是接口不对齐探针，不能通过删除参数降低功能覆盖面。\n"
            "如果是 script_exit，以 raw stderr traceback、异常类型和报错源码行为主；"
            "ModuleNotFoundError / ImportError: No module named 属于 runtime environment，禁止修改或删除 import；"
            "ImportError: cannot import name 仅可在有真实 callable identity 证据时修改直接相关 import，"
            "其它异常只做验证当前调试假设所需的最小局部修改。\n"
            "如果是 stdout_contract/stdout_json_parse，"
            "只修改当前 stdout 组织与返回逻辑。\n"
            "如果是 artifact/final output 失败，"
            "只修改当前产物创建、路径返回或最终 stdout 映射。\n"
            "不得重新判断当前脚本职责是否完整，"
            "不得检查 required_capabilities 或 coverage_requirements。\n"
            "不得重新选择、扩展、删除或重排 ToolPool；不得重新判断工具是否应该承担当前职责；"
            "不得请求工具探索或 tool_pool_patch。\n"
            "如果当前失败脚本调用 Registry Tool，无论异常类型，都必须先读取 read_only_callable_context "
            "中的 signature、input_schema、return_contract、output_schema、example_return 和 common_mistakes，"
            "对照当前参数传递与返回字段读取后再修根因。该上下文只是当前源码已调用 Tool 的只读说明。\n"
            "该 context 不是新的工具选择建议。\n"
            "Only modify callable identity to an exact Registry callable contained in read_only_callable_context.\n"
            "Do not invent, infer, autocomplete, substitute, or choose a callable outside that context.\n"
            "Traceback suggestions are evidence of Python namespace similarity only; they are not Tool authorization.\n"
            "Do not replace the failing Tool with another Tool from the same Skill ToolPool.\n"
            "A replacement callable must belong to the same Registry tool identity as the failing call being repaired.\n"
            "如果当前脚本的核心动作依赖一个已经授权的 callable，不得通过删除 import 但保留未定义调用、"
            "fixed text、返回示例文本、fake path、写入空文件、注释掉核心调用、mock / placeholder / simulated 实现来绕过 ImportError 或调用错误。\n"
            "应优先依据 read_only_callable_context 修正准确 import path、函数名、参数和返回字段。\n"
            "已有 runtime evidence 足够定位时，不得只增强 exception message、Got result、debug print、logger 或 traceback 输出；这些不解决根因。\n"
            "若只读合同中没有可完成该核心动作的 callable，不要伪造实现；保留阻塞状态，让上层重新进入第一轮工具规划或人工修复。\n"
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

    failure_details = structured_failure.get("details") if isinstance(structured_failure, dict) else {}
    artifact_runtime_state = _artifact_runtime_state((failure_details or {}).get("filesystem_trace") or {})
    filesystem_trace_for_context = (failure_details or {}).get("filesystem_trace") or {}
    created_count = int(artifact_runtime_state.get("created_count") or 0)
    missing_reported_count = int(artifact_runtime_state.get("missing_reported_count") or 0)
    failure_code = str((failure_details or {}).get("failure_code") or "")
    failure_layer = str(structured_failure.get("layer") or "")
    is_artifact_failure = failure_code.startswith("artifact_")
    allowed_edit_scope = _allowed_edit_scope_for_failure(
        target_path=target_path,
        failure_code=failure_code,
        failure_layer=failure_layer,
        is_artifact_failure=is_artifact_failure,
        created_count=created_count,
        missing_reported_count=missing_reported_count,
    )
    minimal_repair_context = {
        "target_file": target_path,
        "failure_layer": failure_layer,
        "failure_code": failure_code,
        "is_artifact_failure": is_artifact_failure,
        "runtime_binding_trace": (
                (failure_details or {}).get(
                    "runtime_binding_trace"
                )
                or (
                        failure_details
                        or {}
                ).get(
            "variable_trace",
            {},
        ).get(
            "runtime_binding_trace"
        )
                or {}
        ),
        "rendered_payload": (
                structured_failure.get(
                    "rendered_payload"
                )
                or {}
        ),
        "stdout": (
                structured_failure.get("stdout")
                or ""
        ),
        "stderr": (
                structured_failure.get("stderr")
                or ""
        ),
        "filesystem_trace": (
            filesystem_trace_for_context
        ),
        "artifact_runtime_state": (
            artifact_runtime_state
        ),
        "expected": (
                structured_failure.get("expected")
                or ""
        ),
        "actual": (
                structured_failure.get("actual")
                or ""
        ),
        "allowed_edit_scope": (
            allowed_edit_scope
        ),
        "failed_command": (
                structured_failure.get(
                    "failed_command"
                )
                or ""
        ),

        # 新增
        "repair_authority": repair_authority,

        "debug_diagnosis": diagnosis,

        # Keep the complete evidence set adjacent in the repair payload so the
        # code model can classify contract mismatches before editing source.
        "repair_context": {
            "traceback": (
                structured_failure.get("stderr")
                or ""
            ),
            "failed_command": (
                structured_failure.get("failed_command")
                or ""
            ),
            "generated_script_source": generated_script_source,
            "input_schema": (
                (e2e_entry_context.get("runtime_contract") or {}).get("input_schema")
                or (e2e_entry_context.get("runtime_contract") or {}).get("argv_schema")
                or e2e_entry_context.get("script_argv_schema")
                or e2e_entry_context.get("inputs")
                or {}
            ),
            "output_schema": (
                (e2e_entry_context.get("runtime_contract") or {}).get("output_schema")
                or (e2e_entry_context.get("runtime_contract") or {}).get("stdout_schema")
                or e2e_entry_context.get("outputs")
                or {}
            ),
            "tool_function_output_schema": read_only_callable_context or {},
            "actual_runtime_payload": (
                structured_failure.get("rendered_payload")
                or structured_failure.get("input_payload")
                or {}
            ),
        },
    }

    base_task_context = "\n".join([
        f"Skill 名称：{skill_name}",
        "",
        "最小 E2E 修复上下文（不要重新规划职责/工具）：",
        json.dumps(
            minimal_repair_context,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        ),
        "",
        "当前脚本实际运行接口事实（仅限 argv guard/run 自洽判断）：",
        json.dumps(
            e2e_entry_context,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        ),
        "",
        "第二轮硬性边界：只根据真实 E2E 运行失败申错改错；"
        "不得重新判断脚本职责、required_capabilities、coverage_requirements 或 helper permission；"
        "不得重新选择、扩展、删除或重排 ToolPool，不得请求工具探索或 tool_pool_patch。",
        "",
        "Parameter provenance rules: platform/runtime input uses its exact runtime placeholder; previous-step output uses a graph-backed placeholder; only a user/frozen-contract constant may use a literal; a script-local optional default should preferably be omitted. A literal appearing only in the failing SKILL.md command is not provenance.",
        "Every placeholder introduced in NEW must have current runtime provenance from a platform input, previous successful stdout, frozen graph provenance, or another explicitly supplied runtime value. Do not invent an undeclared nested path or wrapper. A syntactically valid placeholder with no runtime producer is invalid.",
        "The patch must concretely implement the supplied repair instruction. Before returning compare OLD and NEW, confirm they differ, materially change the diagnosed behavior, and correct the failing expression/interface. Do not return no-op, comments-only, logging-only, or diagnostic-only edits.",
        "Repair authority rule: "
        "when repair_authority.mode == 'deterministic', "
        "repair_authority is immutable runtime authority, not a suggestion. "
        "The target, target_region, frozen_boundary, and expected contract "
        "must not be re-selected, renamed, weakened, or rewritten merely "
        "to fit the current implementation. "
        "Treat observed only as runtime evidence. "
        "First derive the smallest behavioral delta from observed to expected, "
        "then modify only mutable_scope to implement that delta. "
        "Preserve all unviolated identities, edges, contracts, inputs, outputs, "
        "tool identities, and unrelated behavior. "
        "If the required delta cannot be justified from the supplied authority "
        "and runtime evidence, return no valid patch rather than inventing "
        "a different contract.",
    ])


    if target_path.startswith("scripts/") and read_only_callable_context:
        base_task_context += (
            "\n\n只读 callable facts（read_only=true，仅用于当前 callable 的 import identity、arguments、return structure 和当前源码如何消费该返回值；不是工具选择或脚本功能评价建议）：\n"
            "The readonly callable facts below are runtime interface authority for callable behavior. "
            "When the failing expression consumes the callable return value, the patch must preserve the declared "
            "return shape. Do not replace object field access with positional indexing, or vice versa, unless runtime "
            "evidence or the exact callable contract requires it. Do not reinterpret an explicitly declared "
            "array<object> as array<array>.\n"
            + json.dumps(
                read_only_callable_context,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                default=str,
            )
        )

    repair_feedback = "\n\n".join(
        repair_state.get(
            "remaining_failed_checks"
        )
        or e2e_errors
    )[-12000:]
    baseline_errors = list(repair_state.get("remaining_failed_checks") or e2e_errors or [])
    last_failure = ""
    # The production API owns the overall (<=10) debug-experiment loop. Keep
    # legacy direct callers usable while they migrate to the session loop.
    # 普通 E2E：
    # attempt 1~2 = 原有 localized repair
    # attempt 3   = Reviewer re-analysis 后，
    #               仍使用 localized coder patch
    #
    # standalone caller 保留原有较大总预算，
    # 但 Reviewer guided repair 仍只执行一次。
    reviewer_guided_attempt = 3

    max_candidate_attempts = (
        10
        if standalone_repair
        else reviewer_guided_attempt
    )

    start_candidate_attempt = (
        reviewer_guided_attempt
        if force_reviewer_reanalysis
        else 1
    )
    sandbox_executed = False

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
            start_candidate_attempt,
            max_candidate_attempts + 1,
    ):
        current_content = working_content
        use_full_rewrite = False
        reviewer_guided = (
            candidate_attempt
            == reviewer_guided_attempt
        )

        reviewer_result: dict[str, Any] = {}
        effective_skill_md = (
            current_content
            if target_path == "SKILL.md"
            else skill_md
        )

        effective_task_context = base_task_context

        if target_path == "SKILL.md":
            effective_task_context = (
                base_task_context
                + "\n\n当前失败 command block：\n"
                + str(minimal_repair_context.get("failed_command") or "")[:4000]
            )

        if reviewer_guided:
            logger.info(
                "[Creator][E2E]"
                "[reviewer_reanalysis_start] %s",
                json.dumps(
                    {
                        "target_file": target_path,
                        "candidate_attempt": (
                            candidate_attempt
                        ),
                        "failure_identity": (
                            before_identity
                        ),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
            )

            reviewer_result = await (
                _review_failed_e2e_after_local_repairs(
                    skill_name=skill_name,
                    target_path=target_path,
                    current_content=current_content,
                    skill_md=effective_skill_md,
                    e2e_errors=(
                        baseline_errors
                        or e2e_errors
                    ),
                    diagnosis=diagnosis,
                    minimal_repair_context=(
                        minimal_repair_context
                    ),
                    e2e_entry_context=(
                        e2e_entry_context
                    ),
                    e2e_session=e2e_session,
                    requested_model=requested_model,
                )
            )

            reviewer_status = str(
                reviewer_result.get(
                    "status"
                )
                or ""
            )

            reviewer_target = str(
                reviewer_result.get(
                    "repair_target"
                )
                or ""
            ).strip()


            # -----------------------------
            # 合同确实不足
            # -----------------------------
            if (
                reviewer_status
                == "contract_review_required"
            ):
                logger.warning(
                    "[Creator][E2E]"
                    "[contract_review_required] %s",
                    json.dumps(
                        reviewer_result,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    )[:5000],
                )

                if repair_events is not None:
                    repair_events.extend(
                        e2e_session.events
                    )

                return {
                    "status": (
                        "contract_review_required"
                    ),
                    "repaired_target": (
                        target_path
                    ),
                    "next_target": None,
                    "next_failure": (
                        baseline_errors
                        or e2e_errors
                    ),
                    "reviewer_result": (
                        reviewer_result
                    ),
                    "attempt": (
                        candidate_attempt
                    ),
                }


            # -----------------------------
            # Trial Case 确实有问题
            # -----------------------------
            if (
                reviewer_status
                == "invalid_e2e_case"
            ):
                return {
                    "status": (
                        "e2e_case_review_required"
                    ),
                    "repaired_target": (
                        target_path
                    ),
                    "next_target": None,
                    "next_failure": (
                        baseline_errors
                        or e2e_errors
                    ),
                    "reviewer_result": (
                        reviewer_result
                    ),
                    "attempt": (
                        candidate_attempt
                    ),
                }


            # -----------------------------
            # Reviewer 自己不可用
            # -----------------------------
            if (
                reviewer_status
                != "repairable"
            ):
                return {
                    "status": (
                        "escalate_to_creator_review"
                    ),
                    "repaired_target": (
                        target_path
                    ),
                    "next_target": None,
                    "next_failure": (
                        baseline_errors
                        or e2e_errors
                    ),
                    "reviewer_result": (
                        reviewer_result
                    ),
                    "attempt": (
                        candidate_attempt
                    ),
                }


            # -----------------------------
            # Reviewer 发现真正目标是另一个文件
            # 例如原来修 script，
            # 重新审后确认其实是 SKILL wiring。
            # -----------------------------
            if (
                reviewer_target
                and reviewer_target
                != target_path
            ):
                return {
                    "status": "target_changed",
                    "repaired_target": (
                        target_path
                    ),
                    "next_target": (
                        reviewer_target
                    ),
                    "next_failure": [
                        (
                            "SECOND_ROUND_REVIEWER="
                            + json.dumps(
                                reviewer_result,
                                ensure_ascii=False,
                                sort_keys=True,
                                default=str,
                            )
                        ),
                        *(
                            baseline_errors
                            or e2e_errors
                        ),
                    ],
                    "reviewer_result": (
                        reviewer_result
                    ),
                    "attempt": (
                        candidate_attempt
                    ),
                }


            # -----------------------------
            # 关键：
            # Reviewer 只分析；
            # 后面仍然交现有 coder patch。
            # -----------------------------
            reviewer_feedback = (
                "\n\n"
                "SECOND_ROUND_REVIEWER_ANALYSIS：\n"
                + json.dumps(
                    reviewer_result,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                    default=str,
                )
                + "\n\n"
                "Reviewer 已完成根因分析。"
                "你现在是代码修改模型，"
                "不要重新判断合同或重新选择修复目标。"
                "严格按照 repair_instructions "
                "对当前目标文件做最小 localized patch。"
                "必须保留 must_preserve。"
                "禁止整函数重写。"
                "禁止整文件重写。"
            )

            repair_feedback = (
                deterministic_error
                + reviewer_feedback
            )

            effective_task_context += (
                reviewer_feedback
            )

        if "proposal_noop" in repair_feedback:
            effective_task_context += (
                "\n\nThe previous patch was rejected because it did not materially change "
                "the failing behavior. Do not repeat the same edit."
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

            unauthorized_callable_identities = []
            if target_path.endswith(".py") and _is_callable_identity_failure(baseline_errors):
                unauthorized_callable_identities = _unauthorized_callable_identity_change(
                    current_content,
                    sanitized,
                    read_only_callable_context,
                )
            if unauthorized_callable_identities:
                compact_rejection = unauthorized_callable_identities[0]
                last_failure = (
                    f"{compact_rejection['reason']}: callable repair must preserve the failing Tool identity. "
                    f"facts={json.dumps(compact_rejection, ensure_ascii=False, sort_keys=True)}"
                )
                repair_feedback = deterministic_error + "\n\n" + last_failure
                continue

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

            before_identity = _e2e_failure_identity((baseline_errors or [""])[0], target_file=target_path)
            patch_digest = _stable_json_hash(_normalize_e2e_failure_text(sanitized))
            diagnosis_family_key = _e2e_diagnosis_family_key(repair_target=target_path, before_failure_identity=before_identity)
            experiment_key = _e2e_experiment_key(repair_target=target_path, before_failure_identity=before_identity, patch_digest=patch_digest)
            if any(attempt.get("experiment_key") == experiment_key for attempt in e2e_session.debug_attempts):
                event = {**e2e_session.to_event_base(), "attempt": candidate_attempt, "target_file": target_path,
                         "status": "duplicate_experiment_rejected", "progress_reason": "duplicate_experiment",
                         "candidate_retained": False, "candidate_rolled_back": False,
                         "before_failure_identity": before_identity, "experiment_key": experiment_key,
                         "diagnosis_family_key": diagnosis_family_key, "rerun_status": "skipped",
                         "writeback_status": "not_written"}
                e2e_session.events.append(event)
                if repair_events is not None:
                    repair_events.extend(e2e_session.events)
                return {"status": "debug_hypothesis_rejected", "rejection_reason": "duplicate_experiment",
                        "progress_reason": "duplicate_experiment", "sandbox_executed": False,
                        "candidate_retained": False, "candidate_rolled_back": False, "repaired_target": target_path,
                        "next_target": None, "next_failure": baseline_errors, "experiment_key": experiment_key, "attempt": candidate_attempt}

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
            previous_session_content = session_target.read_text(encoding="utf-8") if session_target.is_file() else ""

            session_target.write_text(
                sanitized,
                encoding="utf-8",
            )

            candidate_source_digest = _file_sha256(session_target)
            expected_candidate_digest = hashlib.sha256(
                sanitized.encode("utf-8")
            ).hexdigest()[:16]
            if candidate_source_digest != expected_candidate_digest:
                raise RuntimeError(
                    "E2E candidate write verification failed: "
                    f"target={target_path} "
                    f"expected_digest={expected_candidate_digest} "
                    f"actual_digest={candidate_source_digest or '(missing)'}"
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

            sandbox_executed = True
            sandbox_gate = (
                _run_e2e_sandbox_acceptance_gate(
                    skill_name=skill_name,
                    candidate_skill_dir=(
                        e2e_session.workspace_dir
                    ),
                    patched_file=target_path,
                    original_errors=baseline_errors,
                    external_context=external_context,
                    requested_model=requested_model,
                    e2e_session=e2e_session,
                    resume_from_step=(
                        resume_from_step
                    ),
                    expected_source_digests={
                        target_path: expected_candidate_digest,
                    },
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

                gate_errors = sandbox_gate.get("errors") or []
                after_identity = _e2e_failure_identity((gate_errors or [""])[0], target_file=target_path)
                veto_reasons = _e2e_candidate_invariant_veto(
                    previous_session_content, session_target.read_text(encoding="utf-8"),
                    original_errors=baseline_errors, new_errors=gate_errors,
                )
                improved = False if veto_reasons else _e2e_candidate_improved(baseline_errors, gate_errors, target_file=target_path)
                if veto_reasons:
                    logger.info("[Creator][E2E][candidate_invariant_veto] %s", json.dumps({
                        "target": target_path, "reason": veto_reasons,
                        "candidate_retained": False, "candidate_rolled_back": True,
                    }, ensure_ascii=False, sort_keys=True))
                baseline_fingerprint = _e2e_behavior_fingerprint((baseline_errors or [""])[0], target_file=target_path)
                failure_signature = _e2e_behavior_fingerprint((gate_errors or [""])[0], target_file=target_path)
                same_breakpoint_progress = (
                    before_identity["failed_step_index"] == after_identity["failed_step_index"]
                    and before_identity["target_file"] == after_identity["target_file"]
                    and before_identity["layer"] == after_identity["layer"]
                    and _e2e_breakpoint_changed(before_identity, after_identity)
                )
                progress_reason = "same_step_new_breakpoint" if same_breakpoint_progress else (
                    "step_advanced" if _e2e_failure_position((gate_errors or [""])[0]) > _e2e_failure_position((baseline_errors or [""])[0]) else "same_breakpoint_repeated"
                )
                candidate_retained, candidate_rolled_back = bool(improved), not bool(improved)
                decision = {"repair_target": target_path, "candidate_attempt": candidate_attempt,
                            "before_failure_identity": before_identity, "after_failure_identity": after_identity,
                            "improved": improved, "progress_reason": progress_reason,
                            "candidate_retained": candidate_retained, "candidate_rolled_back": candidate_rolled_back,
                            "experiment_key": experiment_key}
                logger.info("[Creator][E2E][candidate_progress_decision] %s", json.dumps(decision, ensure_ascii=False, sort_keys=True, default=str))
                before_digest = _stable_json_hash(previous_session_content)
                candidate_digest = _stable_json_hash(session_target.read_text(encoding="utf-8"))
                active_digest = candidate_digest if candidate_retained else before_digest
                logger.info(
                    "[Creator][e2e_candidate] before_revision_digest=%s candidate_revision_digest=%s decision=%s active_revision_digest=%s rollback_target_digest=%s reason=%s",
                    before_digest, candidate_digest, "retain" if candidate_retained else "rollback",
                    active_digest, before_digest if candidate_rolled_back else "", progress_reason,
                )
                if e2e_session.events:
                    e2e_session.events[-1].update(decision)
                attempt_record = {"symptom_file": diagnosis["symptom_file"], "repair_target": target_path,
                    "root_cause_hypothesis": diagnosis["root_cause_hypothesis"], "hypothesis_key": diagnosis["hypothesis_key"],
                    "patch_digest": patch_digest, "diagnosis_family_key": diagnosis_family_key, "experiment_key": experiment_key,
                    "before_failure_identity": before_identity, "after_failure_identity": after_identity,
                    "before_failure_signature": baseline_fingerprint, "after_failure_signature": failure_signature,
                    "improved": improved, "result": "progressed" if improved else "no_progress"}
                if not improved:
                    session_target.write_text(previous_session_content, encoding="utf-8")
                    e2e_session.current_revision += 1
                    if earliest_step:
                        _invalidate_checkpoints_from(e2e_session, earliest_step)
                    e2e_session.debug_attempts.append(attempt_record)
                    no_progress_count = _count_matching_no_progress_attempts(e2e_session, repair_target=target_path, before_failure_identity=before_identity)
                    if e2e_session.events:
                        e2e_session.events[-1].update({"status": "debug_hypothesis_rejected", "no_progress_count": no_progress_count, "writeback_status": "rolled_back"})
                    if repair_events is not None:
                        repair_events.extend(e2e_session.events)
                    if reviewer_guided:
                        logger.warning(
                            "[Creator][E2E]"
                            "[reviewer_guided_repair_failed] %s",
                            json.dumps(
                                {
                                    "target_file": (
                                        target_path
                                    ),
                                    "candidate_attempt": (
                                        candidate_attempt
                                    ),
                                    "reason": (
                                        "same_breakpoint_repeated"
                                    ),
                                    "reviewer_result": (
                                        reviewer_result
                                    ),
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                                default=str,
                            )[:5000],
                        )

                        return {
                            "status": (
                                "escalate_to_creator_review"
                            ),
                            "repaired_target": (
                                target_path
                            ),
                            "next_target": None,
                            "next_failure": (
                                gate_errors
                            ),
                            "reviewer_result": (
                                reviewer_result
                            ),
                            "rejection_reason": (
                                "reviewer_guided_repair_"
                                "made_no_progress"
                            ),
                            "progress_reason": (
                                "same_breakpoint_repeated"
                            ),
                            "candidate_retained": False,
                            "candidate_rolled_back": True,
                            "attempt": (
                                candidate_attempt
                            ),
                        }

                    return {"status": "debug_hypothesis_rejected", "rejection_reason": "same_breakpoint_repeated",
                            "progress_reason": "same_breakpoint_repeated", "sandbox_executed": True,
                            "candidate_retained": False, "candidate_rolled_back": True, "repaired_target": target_path,
                            "next_target": None, "next_failure": gate_errors, "hypothesis_key": diagnosis["hypothesis_key"],
                            "experiment_key": experiment_key, "no_progress_count": no_progress_count, "attempt": candidate_attempt}
                e2e_session.debug_attempts.append(attempt_record)

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
                        "behavioral_status": "behavioral_oscillation",
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
                        "sandbox_executed": True,
                        "repaired_target": target_path,
                        "next_target": next_target,
                        "next_failure": gate_errors,
                        "attempt": candidate_attempt,
                    }

                if improved:
                    target_file.write_text(sanitized, encoding="utf-8")
                    if e2e_session.events:
                        e2e_session.events[-1].update({"status": "debug_progress", "writeback_status": "written"})
                    if repair_events is not None: repair_events.extend(e2e_session.events)
                    return {"status": "debug_progress", "progress_reason": progress_reason, "sandbox_executed": True,
                            "candidate_retained": True, "candidate_rolled_back": False, "repaired_target": target_path,
                            "next_target": None, "next_failure": gate_errors, "experiment_key": experiment_key, "attempt": candidate_attempt}

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

            e2e_session.debug_attempts.append({
                "symptom_file": diagnosis["symptom_file"], "repair_target": target_path,
                "root_cause_hypothesis": diagnosis["root_cause_hypothesis"],
                "hypothesis_key": diagnosis["hypothesis_key"], "patch_digest": _stable_json_hash(sanitized),
                "before_failure_signature": _e2e_behavior_fingerprint((baseline_errors or [""])[0], target_file=target_path),
                "after_failure_signature": "", "improved": True, "result": "passed",
            })
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
                "sandbox_executed": True,
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

            if reviewer_guided:
                logger.warning(
                    "[Creator][E2E]"
                    "[reviewer_guided_repair_failed] "
                    "target=%s error=%s",
                    target_path,
                    candidate_exc,
                )

                if repair_events is not None:
                    repair_events.extend(
                        e2e_session.events
                    )

                return {
                    "status": (
                        "escalate_to_creator_review"
                    ),
                    "repaired_target": (
                        target_path
                    ),
                    "next_target": None,
                    "next_failure": (
                        repair_feedback.split(
                            "\n\n"
                        )[:8]
                    ),
                    "last_failure": (
                        str(candidate_exc)[:12000]
                    ),
                    "reviewer_result": (
                        reviewer_result
                    ),
                    "attempt": (
                        candidate_attempt
                    ),
                    "error_type": (
                        "reviewer_guided_repair_failed"
                    ),
                }

            continue

    if repair_events is not None:
        repair_events.extend(
            e2e_session.events
        )

    if not standalone_repair and not sandbox_executed:
        e2e_session.debug_attempts.append({
            "symptom_file": diagnosis["symptom_file"], "repair_target": target_path,
            "root_cause_hypothesis": diagnosis["root_cause_hypothesis"], "hypothesis_key": diagnosis["hypothesis_key"],
            "patch_digest": "", "before_failure_signature": _e2e_behavior_fingerprint((baseline_errors or [""])[0], target_file=target_path),
            "after_failure_signature": "", "improved": None, "result": "patch_failed",
        })
        return {"status": "patch_proposal_exhausted", "repaired_target": target_path, "next_target": None,
                "next_failure": repair_feedback.split("\n\n")[:8], "hypothesis_key": diagnosis["hypothesis_key"], "attempt": max_candidate_attempts}
    return {
        "status": "still_failed_same_target",
        "repaired_target": target_path,
        "next_target": None,
        "next_failure": repair_feedback.split("\n\n")[:8],
        "last_failure": last_failure[:12000], "attempt": max_candidate_attempts,
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
    expected_source_digests: dict[str, str] | None = None,
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
        expected_source_digests=expected_source_digests,
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
    hard requirements here are: stdout is a JSON object, it does not report an
    error, and any file-looking values it declares point at real files.  Empty
    display values are legitimate results; required outputs require presence,
    not truthiness.
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
    if canonical_contract is not None:
        stdout_schema = getattr(canonical_contract, "stdout_schema", {}) or {}
        required = stdout_schema.get("required") if isinstance(stdout_schema, dict) else []
        missing = [str(key) for key in required or [] if str(key) not in payload]
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
        missing = [str(key) for key in required or [] if str(key) not in payload]
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
    dependency_import_names = {
        "python-docx": "docx",
        "python_docx": "docx",

        "python-pptx": "pptx",
        "python_pptx": "pptx",
    }
    dependency_package_names = {
        "python-docx": "python-docx",
        "python_docx": "python-docx",

        "python-pptx": "python-pptx",
        "python_pptx": "python-pptx",
    }
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
            package_name = (
                dependency_package_names.get(
                    dependency,
                    dependency,
                )
            )

            if package_name not in missing:
                missing.append(package_name)

    if not missing:
        return

    logger.info("skill-env: pip installing %s deps into venv: %s", source_label, missing)
    result = subprocess.run(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--quiet",
            "--disable-pip-version-check",
            "--no-input",
            "--prefer-binary",
            "--index-url",
            _E2E_PIP_INDEX_URL,
            "--timeout",
            str(_E2E_PIP_NETWORK_TIMEOUT_SECONDS),
            "--retries",
            str(_E2E_PIP_RETRIES),
            *missing,
        ],
        timeout=_E2E_PIP_INSTALL_TIMEOUT_SECONDS,
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
