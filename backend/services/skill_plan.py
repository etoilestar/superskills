"""SkillPlan role/contracts for Creator file generation.

This module provides a small, deterministic planning layer between a parsed
blueprint and file generation.  It intentionally keeps keyword signals as
classification hints only; downstream generation/validation is driven by the
resolved file role rather than by scanning the whole blueprint for domain words.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
import json
import re
from typing import Literal
from .creator_tool_registry import get_role_pattern, get_script_roles, get_tool_capability, is_resource_role, is_script_role
from .skill_dataflow import parse_schema_input_item
from .platform_io_contract import build_platform_io_contract


FileKind = Literal["script", "skill_doc", "reference", "asset", "config"]
FileType = Literal["skill", "script", "reference", "asset", "skill_md"]
Language = Literal["python", "javascript", "bash", "sql", "yaml", "json", "markdown", "html", "css", "text"]
Runtime = Literal["python", "node", "bash", "shell", "generic", "none"]
ScriptRole = str
ResourceRole = Literal["skill_overview", "reference", "asset"]
FileRole = str

SCRIPT_ROLES: frozenset[str] = frozenset(get_script_roles())


class MissingCommandArgBindingError(ValueError):
    """Raised when required argv keys have no graph/command binding."""

    code = "missing_command_arg_binding"

    def __init__(self, script_path: str, missing_keys: list[str]) -> None:
        self.script_path = script_path
        self.missing_keys = missing_keys
        super().__init__(
            "missing_command_arg_binding: "
            f"{script_path} missing graph/command binding for required argv keys: {', '.join(missing_keys)}"
        )

PLATFORM_LAYER_NAMES: frozenset[str] = frozenset({
    "creator_internal",
    "business_skill",
    "runtime_artifact",
    "static_resource",
    "platform_protocol",
})

# Capabilities in SkillPlan are a business contract. Host execution/sandbox
# protocol and Creator safety controls are tracked separately so generated
# business Skills cannot smuggle platform policy through required/forbidden
# capability lists.
_PLATFORM_PROTOCOL_CAPABILITIES: frozenset[str] = frozenset({
    "deterministic_execution",
    "runtime_execution",
    "sandbox_execution",
    "host_scheduling",
    "script_runner",
})
_PLATFORM_SAFETY_CAPABILITIES: frozenset[str] = frozenset({
    "network_disabled",
    "filesystem_sandbox",
    "secret_redaction",
    "user_confirmation",
    "approval_required",
})

def capability_layer(capability: str) -> str:
    """Classify a declared capability by ownership layer."""
    name = re.sub(r"[^A-Za-z0-9_-]", "", str(capability or "").strip())
    if not name:
        return "business_skill"
    if name in _PLATFORM_PROTOCOL_CAPABILITIES:
        return "platform_protocol"
    if name in _PLATFORM_SAFETY_CAPABILITIES:
        return "creator_internal"
    cap = get_tool_capability(name)
    if cap and cap.category in {"authoring"}:
        return "creator_internal"
    if cap and cap.category == "resource":
        return "static_resource"
    return "business_skill"


def is_business_capability(capability: str) -> bool:
    return capability_layer(capability) == "business_skill"


def is_platform_safety_constraint(capability: str) -> bool:
    return capability_layer(capability) == "creator_internal"


def _dedupe_capabilities(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values or []:
        name = re.sub(r"[^A-Za-z0-9_-]", "", str(value or "").strip())
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(name)
    return result


def normalize_required_capabilities(
    *,
    role: str,
    path: str,
    required_capabilities: list[str],
    user_blueprint_text: str = "",
) -> list[str]:
    """Normalize explicitly planned business capabilities.

    Capability decomposition is owned by the planning model and SkillPlan.

    This function must not:
    - infer capabilities from role;
    - infer capabilities from file path;
    - mine blueprint prose for extra capabilities;
    - resolve Registry tools;
    - authorize tools.

    It only preserves explicitly declared business capabilities and removes
    platform/Creator/resource-layer capability names.

    The normalized result is the shared semantic capability source for:
    - FileSpecOut.required_capabilities;
    - RequirementItem.required_tools;
    - Creator Tool embedding recall.
    """

    _ = role
    _ = path
    _ = user_blueprint_text

    normalized = _dedupe_capabilities(
        list(
            required_capabilities
            or []
        )
    )

    return [
        capability
        for capability in normalized
        if is_business_capability(
            capability
        )
    ]

RESOURCE_ROLES: frozenset[str] = frozenset({"skill_overview", "reference", "asset"})
_CREATOR_INTERNAL_REFERENCE_PATHS: tuple[str, ...] = (
    "kernel/references/best-practices.md",
    "kernel/references/workflows.md",
    "kernel/references/output-patterns.md",
)


def _is_skill_local_reference(path: str) -> bool:
    return path.startswith(("references/", "assets/", "scripts/"))


def _is_creator_internal_reference(path: str) -> bool:
    return path.startswith("kernel/references/")


def _dedupe_paths(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in paths:
        path = str(raw).strip().replace("\\", "/")
        if not path or path in seen:
            continue
        seen.add(path)
        result.append(path)
    return result


@dataclass(frozen=True)
class RoleClassification:
    """Classifier output for a single file role decision."""

    role: FileRole
    confidence: float
    reason: str
    heuristic_signals: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ToolSlot:
    """Structured interface need resolved after blueprint normalization."""

    slot_id: str
    functional_requirement: str = ""
    tool_id: str = ""
    call_template: dict[str, object] = field(default_factory=dict)
    input_construction: dict[str, object] = field(default_factory=dict)
    output_consumption: dict[str, object] = field(default_factory=dict)
    input_contract: dict[str, object] = field(default_factory=dict)
    output_contract: dict[str, object] = field(default_factory=dict)
    input_modality: str = "json"
    output_modality: str = "json"
    side_effects: list[str] = field(default_factory=list)
    runtime_requirements: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ImplementationStrategy:
    """How a normalized tool slot can be implemented."""

    slot_id: str
    strategy: str = "generate_code"
    tool_id: str = ""
    reason: str = ""




@dataclass(frozen=True)
class ScriptRuntimeSpec:
    """Internal post-trial runtime spec used to render final SKILL.md commands.

    This is not written into SKILL.md; it records the argv/stdout/artifact
    shape that actually ran successfully during script validation.
    """

    script_path: str
    runtime: str
    role: str
    responsibility: str
    input_policy: str
    accepted_sample_argv: dict[str, object] = field(default_factory=dict)
    required_outputs: list[str] = field(default_factory=list)
    actual_stdout_fields: list[str] = field(default_factory=list)
    artifact_fields: list[str] = field(default_factory=list)
    file_outputs: list[str] = field(default_factory=list)
    stdout_json: dict[str, object] = field(default_factory=dict)
    artifact_paths: list[str] = field(default_factory=list)
    command_template: str = ""


@dataclass(frozen=True)
class SkillPlanEntry:
    """Normalized contract for one file that Creator will generate.

    ``role`` is retained as a component hint for prompts/UI/backwards
    compatibility. Hard execution semantics are carried by file_kind, I/O, tool
    slots, runtime_contract, and artifact_contract.
    """

    path: str
    file_type: FileType
    role: FileRole
    purpose: str
    file_kind: FileKind = "config"
    component_hint: str = ""
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    default_values: dict[str, object] = field(default_factory=dict)
    dependencies: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    raw_capability_hints: list[str] = field(default_factory=list)
    optional_capabilities: list[str] = field(default_factory=list)
    allowed_capabilities: list[str] = field(default_factory=list)
    forbidden_capabilities: list[str] = field(default_factory=list)
    business_capabilities: list[str] = field(default_factory=list)
    platform_capabilities: list[str] = field(default_factory=list)
    business_forbidden_capabilities: list[str] = field(default_factory=list)
    platform_safety_constraints: list[str] = field(default_factory=list)
    execution_contract: dict[str, str] = field(default_factory=dict)
    layer: str = "business_skill"
    reference_files: list[str] = field(default_factory=list)
    skill_local_references: list[str] = field(default_factory=list)
    creator_internal_references: list[str] = field(default_factory=list)
    language: Language = "text"
    runtime: Runtime = "none"
    entrypoint: str = ""
    command_template: str = ""
    input_binding: list[dict[str, object]] = field(default_factory=list)
    command_arg_bindings: list[dict[str, object]] = field(default_factory=list)
    workflow_order: int = 0
    logical_edges: list[dict[str, object]] = field(default_factory=list)
    required_tool_slots: list[ToolSlot] = field(default_factory=list)
    implementation_strategy: list[ImplementationStrategy] = field(default_factory=list)
    side_effects: list[str] = field(default_factory=list)
    runtime_contract: dict[str, object] = field(default_factory=dict)
    artifact_contract: dict[str, object] = field(default_factory=dict)
    constraints: list[dict[str, object]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    required: bool = True
    can_skip: bool = False
    confidence: float = 0.0
    reason: str = ""
    heuristic_signals: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SkillPlan:
    """Full Creator plan for a skill package."""

    skill_name: str
    files: list[SkillPlanEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    function_items: list[dict[str, object]] = field(default_factory=list)
    responsibility_edges: list[dict[str, object]] = field(default_factory=list)


_ALLOWED_FUNCTION_ITEM_FIELDS = {
    "target_file",
    "role",
    "purpose",
    "inputs",
    "outputs",
    "required_capabilities",
    "constraints",
}




def is_python_function_item_target(path: str) -> bool:
    normalized = str(path or "").strip().replace("\\", "/")
    return normalized.startswith("scripts/") and normalized.endswith(".py")

def _normalize_string_array(value: object, *, source: str, index: int, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{source}.function_items[{index}].{field} must be a string array")
    return [item for item in value]


def normalize_structured_function_items(raw_items: object, *, source: str = "planner") -> list[dict[str, object]]:
    """Normalize structured planner FunctionItems without semantic repair."""
    if not isinstance(raw_items, list):
        raise ValueError(f"{source}.function_items must be a list")
    normalized: list[dict[str, object]] = []
    seen_targets: set[str] = set()
    invalid: list[dict[str, object]] = []
    required = set(_ALLOWED_FUNCTION_ITEM_FIELDS)
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            invalid.append({"index": index, "type": type(item).__name__})
            continue
        unknown = sorted(str(key) for key in item.keys() if key not in _ALLOWED_FUNCTION_ITEM_FIELDS)
        missing = sorted(required - set(item.keys()))
        if unknown or missing:
            issue: dict[str, object] = {"index": index}
            if unknown:
                issue["unknown_fields"] = unknown
            if missing:
                issue["missing_fields"] = missing
            invalid.append(issue)
            continue
        target_file = item.get("target_file")
        role = item.get("role")
        purpose = item.get("purpose")
        if not isinstance(target_file, str) or not target_file.strip():
            invalid.append({"index": index, "field": "target_file"})
            continue
        target = target_file.strip()
        if not is_python_function_item_target(target):
            invalid.append({"index": index, "field": "target_file", "target_file": target, "reason": "must_be_scripts_python"})
            continue
        if target in seen_targets:
            invalid.append({"index": index, "field": "target_file", "target_file": target, "reason": "duplicate"})
            continue
        if not isinstance(role, str):
            invalid.append({"index": index, "field": "role"})
            continue
        if not isinstance(purpose, str) or not purpose.strip():
            invalid.append({"index": index, "field": "purpose"})
            continue
        try:
            inputs = _normalize_string_array(item.get("inputs"), source=source, index=index, field="inputs")
            outputs = _normalize_string_array(item.get("outputs"), source=source, index=index, field="outputs")
            required_capabilities = _normalize_string_array(item.get("required_capabilities"), source=source, index=index, field="required_capabilities")
        except ValueError as exc:
            invalid.append({"index": index, "error": str(exc)})
            continue
        constraints = item.get("constraints")
        if not isinstance(constraints, list) or not all(isinstance(constraint, dict) for constraint in constraints):
            invalid.append({"index": index, "field": "constraints"})
            continue
        seen_targets.add(target)
        normalized.append({
            "target_file": target,
            "role": role,
            "purpose": purpose.strip(),
            "inputs": inputs,
            "outputs": outputs,
            "required_capabilities": required_capabilities,
            "constraints": [dict(constraint) for constraint in constraints],
        })
    if invalid:
        raise ValueError(f"{source}.function_items contains invalid function items: {invalid}")
    return normalized



_ALLOWED_RESPONSIBILITY_EDGE_FIELDS = {"from_node", "from_output", "to_node", "to_input", "purpose", "constraints"}


def normalize_structured_responsibility_edges(raw_edges: object, *, source: str = "planner") -> list[dict[str, object]]:
    """Normalize structured planner ResponsibilityEdges without semantic repair.

    This path is used for Creator-owned transport of the planner JSON field.
    Non-empty invalid payloads are protocol errors instead of silently becoming
    an empty graph. Legacy markdown parsing remains best-effort separately.
    """
    if raw_edges is None:
        return []
    if not isinstance(raw_edges, list):
        raise ValueError(f"{source}.responsibility_edges must be a list")
    normalized: list[dict[str, object]] = []
    invalid: list[dict[str, object]] = []
    for index, item in enumerate(raw_edges):
        if not isinstance(item, dict):
            invalid.append({"index": index, "type": type(item).__name__})
            continue
        unknown = sorted(str(key) for key in item.keys() if key not in _ALLOWED_RESPONSIBILITY_EDGE_FIELDS)
        if unknown:
            invalid.append({"index": index, "unknown_fields": unknown})
            continue
        edge = {key: item.get(key) for key in _ALLOWED_RESPONSIBILITY_EDGE_FIELDS if key in item}
        missing_required = [key for key in ("from_node", "from_output", "to_node", "to_input", "purpose", "constraints") if key not in edge]
        if missing_required:
            invalid.append({"index": index, "missing_fields": missing_required})
            continue
        empty_endpoint_fields = [
            key
            for key in ("from_node", "from_output", "to_node", "to_input")
            if not str(edge.get(key) or "").strip()
        ]
        if empty_endpoint_fields:
            invalid.append({"index": index, "empty_endpoint_fields": empty_endpoint_fields})
            continue
        constraints = edge.get("constraints", [])
        if constraints is None:
            constraints = []
        if not isinstance(constraints, list) or not all(isinstance(c, dict) for c in constraints):
            invalid.append({"index": index, "field": "constraints"})
            continue
        edge["constraints"] = [dict(c) for c in constraints]
        normalized.append(edge)
    if invalid:
        raise ValueError(f"{source}.responsibility_edges contains invalid edge items: {invalid}")
    return normalized


def validate_structured_responsibility_edge_transport(
    raw_edges: object,
    *,
    function_items: object | None = None,
    source: str = "planner",
) -> list[dict[str, object]]:
    """Validate canonical ResponsibilityEdge transport topology.

    This helper intentionally validates only Creator-owned transport protocol:
    exact structured edge shape, immutable platform boundary slots/direction,
    and the shared endpoint domain invariant used by ResponsibilityGraph.
    Non-platform endpoints must be current FunctionItem targets. It does not
    validate script-local semantic IO or infer mappings.
    """
    if raw_edges is None:
        raise ValueError(
            f"{source}.responsibility_edges must not be null"
        )

    normalized_edges = normalize_structured_responsibility_edges(
        raw_edges,
        source=source,
    )

    contract = build_platform_io_contract()
    boundary = (
        contract.get("platform_skill_boundary")
        if isinstance(contract, dict)
        else {}
    )
    if not isinstance(boundary, dict):
        boundary = {}

    input_fields = {
        str(value)
        for value in (
            boundary.get("input_envelope_fields")
            or []
        )
        if str(value or "").strip()
    }
    output_fields = {
        str(value)
        for value in (
            boundary.get("final_output_fields")
            or []
        )
        if str(value or "").strip()
    }

    function_item_targets: set[str] | None = None
    if function_items is not None:
        normalized_function_items = normalize_structured_function_items(
            function_items,
            source=source,
        )
        function_item_targets = {
            str(item.get("target_file") or "").strip()
            for item in normalized_function_items
            if str(item.get("target_file") or "").strip()
        }
        function_item_io = {
            str(item.get("target_file") or "").strip(): {
                "inputs": {
                    str(value)
                    for value in (item.get("inputs") or [])
                    if str(value or "").strip()
                },
                "outputs": {
                    str(value)
                    for value in (item.get("outputs") or [])
                    if str(value or "").strip()
                },
            }
            for item in normalized_function_items
            if str(item.get("target_file") or "").strip()
        }
    else:
        function_item_io = {}

    for index, edge in enumerate(normalized_edges):
        from_node = str(edge.get("from_node") or "")
        from_output = str(edge.get("from_output") or "")
        to_node = str(edge.get("to_node") or "")
        to_input = str(edge.get("to_input") or "")

        if from_node == "platform_output_node":
            raise ValueError(
                f"{source}.responsibility_edges uses platform_output_node "
                "as an edge source; "
                f"index={index}"
            )

        if to_node == "platform_input_node":
            raise ValueError(
                f"{source}.responsibility_edges uses platform_input_node "
                "as an edge target; "
                f"index={index}"
            )

        if from_node == "platform_input_node" and to_node == "platform_output_node":
            raise ValueError(
                f"{source}.responsibility_edges uses direct platform_input_node "
                "to platform_output_node edge; "
                f"index={index}"
            )

        if from_node == "platform_input_node" and from_output not in input_fields:
            raise ValueError(
                f"{source}.responsibility_edges references undefined "
                "platform input field; "
                f"index={index}; "
                f"from_output={from_output}"
            )

        if to_node == "platform_output_node" and to_input not in output_fields:
            raise ValueError(
                f"{source}.responsibility_edges references undefined "
                "platform output field; "
                f"index={index}; "
                f"to_input={to_input}"
            )

        if (
            function_item_targets is not None
            and from_node != "platform_input_node"
            and from_node not in function_item_targets
        ):
            raise ValueError(
                f"{source}.responsibility_edges references non-FunctionItem "
                "source endpoint; "
                f"index={index}; "
                f"from_node={from_node}"
            )

        if (
            function_item_targets is not None
            and to_node != "platform_output_node"
            and to_node not in function_item_targets
        ):
            raise ValueError(
                f"{source}.responsibility_edges references non-FunctionItem "
                "target endpoint; "
                f"index={index}; "
                f"to_node={to_node}"
            )

        if (
            function_item_targets is not None
            and from_node != "platform_input_node"
            and from_output not in function_item_io[from_node]["outputs"]
        ):
            raise ValueError(
                f"{source}.responsibility_edges references undefined FunctionItem "
                "source output; "
                f"index={index}; from_node={from_node}; from_output={from_output}"
            )

        if (
            function_item_targets is not None
            and to_node != "platform_output_node"
            and to_input not in function_item_io[to_node]["inputs"]
        ):
            raise ValueError(
                f"{source}.responsibility_edges references undefined FunctionItem "
                "target input; "
                f"index={index}; to_node={to_node}; to_input={to_input}"
            )

    return normalized_edges


def parse_responsibility_edges(blueprint_text: str) -> list[dict[str, object]]:
    """Parse top-level ResponsibilityEdges as model-owned graph data.

    This parser only transports an explicit JSON array. It does not infer,
    repair, or synthesize edges from FunctionItem IO, filenames, roles, or
    workflow prose. Constraint semantics are preserved as opaque dict objects.
    """
    text = str(blueprint_text or "")
    match = re.search(r"(?im)^\s*ResponsibilityEdges\s*[：:=]\s*", text)
    if not match:
        return []
    raw = text[match.end():].lstrip()
    try:
        value, _end = json.JSONDecoder().raw_decode(raw)
    except Exception:
        return []
    if not isinstance(value, list):
        return []
    edges: list[dict[str, object]] = []
    allowed = _ALLOWED_RESPONSIBILITY_EDGE_FIELDS
    for item in value:
        if not isinstance(item, dict):
            continue
        edge = {key: item.get(key) for key in allowed if key in item}
        constraints = edge.get("constraints", [])
        if not isinstance(constraints, list):
            constraints = []
        edge["constraints"] = [dict(c) for c in constraints if isinstance(c, dict)]
        edges.append(edge)
    return edges

def file_type_for_path(path: str) -> FileType:
    if path == "SKILL.md":
        return "skill_md"
    if path.startswith("scripts/"):
        return "script"
    if path.startswith("references/"):
        return "reference"
    if path.startswith("assets/"):
        return "asset"
    return "asset"


def file_kind_for_path(path: str) -> FileKind:
    normalized = (path or "").replace("\\", "/")
    if normalized == "SKILL.md":
        return "skill_doc"
    if normalized.startswith("scripts/"):
        return "script"
    if normalized.startswith("references/"):
        return "reference"
    if normalized.startswith("assets/"):
        return "asset"
    return "config"


def heuristic_signals_for_file(file_path: str, purpose: str = "", blueprint_summary: str = "") -> list[str]:
    """Return platform-structure debug signals only; never business semantics."""
    signals: list[str] = []
    file_type = file_type_for_path(file_path)
    if file_type == "script":
        signals.append("path_is_script")
    elif file_type == "reference":
        signals.append("path_is_reference")
    elif file_type == "asset":
        signals.append("path_is_asset")
    elif file_type == "skill_md":
        signals.append("path_is_skill_md")
    explicit_role = _explicit_role_from_plan_text(
        file_path=file_path,
        purpose=purpose,
        blueprint_summary=blueprint_summary,
    )
    if explicit_role:
        signals.append("explicit_role_declared" if explicit_role in SCRIPT_ROLES or explicit_role in RESOURCE_ROLES else "invalid_role_for_path")
    elif file_type == "script":
        signals.append("missing_explicit_role")
    return signals


def language_for_path(path: str) -> Language:
    ext = Path(path).suffix.lower()
    if ext == ".py":
        return "python"
    if ext in {".js", ".mjs", ".cjs", ".ts"}:
        return "javascript"
    if ext in {".sh", ".bash"}:
        return "bash"
    if ext == ".sql":
        return "sql"
    if ext in {".yaml", ".yml"}:
        return "yaml"
    if ext == ".json":
        return "json"
    if ext == ".md":
        return "markdown"
    if ext == ".html":
        return "html"
    if ext == ".css":
        return "css"
    return "text"


def runtime_for_language(language: str, file_type: FileType) -> Runtime:
    if file_type != "script":
        return "none"
    if language == "python":
        return "python"
    if language == "javascript":
        return "node"
    if language == "bash":
        return "bash"
    return "generic"


def _runner_for_runtime(runtime: Runtime) -> str:
    if runtime == "python":
        return "python"
    if runtime == "node":
        return "node"
    if runtime == "bash":
        return "bash"
    if runtime == "shell":
        return "sh"
    return ""


def _stable_external_envelope(values: dict[str, str] | None = None) -> dict[str, object]:
    values = values or {}
    return {
        "payload": values.get("payload", "{{user_request}}"),
        "fields": {},
        "options": {},
        "input_files": [],
    }


def _command_args_from_runtime_contract(runtime_contract: dict[str, object] | None) -> dict[str, object]:
    contract = runtime_contract or {}
    command_args = contract.get("command_args")
    if isinstance(command_args, dict):
        return dict(command_args)
    return {}


def _render_command(path: str, runtime: Runtime | str, payload: dict[str, object]) -> str:
    rendered_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    runner = _runner_for_runtime(runtime)  # type: ignore[arg-type]
    if runner:
        return f"{runner} {path} '{rendered_payload}'"
    return f"{path} '{rendered_payload}'"


def command_template_for_entry(path: str, runtime: Runtime, inputs: list[str], runtime_contract: dict[str, object] | None = None) -> str:
    """Render a generic command without treating SkillPlan.inputs as argv keys."""
    payload = _command_args_from_runtime_contract(runtime_contract) or _stable_external_envelope()
    return _render_command(path, runtime, payload)


def render_script_command_from_runtime_schema(
    entry: SkillPlanEntry,
    script_argv_schema: dict[str, object],
    *,
    previous_stdout_fields: set[str] | None = None,
    is_first_step: bool = False,
) -> str:
    """Render a deterministic SKILL.md argv template from script schema/bindings.

    The generated command contains placeholders derived from graph/bindings only.
    """
    previous_stdout_fields = previous_stdout_fields or set()
    expected_types = script_argv_schema.get("expected_types") if isinstance(script_argv_schema, dict) else {}
    if not isinstance(expected_types, dict):
        expected_types = {}
    required = script_argv_schema.get("required_keys") if isinstance(script_argv_schema, dict) else []
    if not isinstance(required, list):
        required = []

    bindings = list(getattr(entry, "command_arg_bindings", []) or []) or list(getattr(entry, "input_binding", []) or [])
    binding_by_key = {
        str(binding.get("argv_key")): binding
        for binding in bindings
        if isinstance(binding, dict) and str(binding.get("argv_key") or "").strip()
    }

    payload: dict[str, object] = {}
    missing_bindings: list[str] = []
    for key in sorted(str(item) for item in required if str(item or "").strip()):
        binding = binding_by_key.get(key)
        template = binding.get("value_template") if isinstance(binding, dict) else None
        if isinstance(template, str) and re.fullmatch(r"\{\{\s*[^{}]+?\s*\}\}", template.strip()):
            payload[key] = template.strip()
            continue
        # Without a graph edge/binding, do not invent an internal field name or
        # value template. E2E/dataflow validation should surface a repairable
        # binding failure.
        missing_bindings.append(key)

    if missing_bindings:
        raise MissingCommandArgBindingError(entry.path, missing_bindings)

    return _render_command(entry.path, entry.runtime, payload)


def render_script_command_from_skill_plan(
    entry: SkillPlanEntry,
    values: dict[str, str] | None = None,
    runtime_spec: ScriptRuntimeSpec | dict[str, object] | None = None,
) -> str:
    """Render a SKILL.md script command from verified runtime args when available.

    Priority: schema/bindings supplied by E2E repair, runtime_contract.command_args,
    entry.command_template, then the platform-stable external envelope.
    SkillPlan.inputs remain generation hints and are not used as argv keys.
    """
    if runtime_spec is not None:
        if isinstance(runtime_spec, ScriptRuntimeSpec):
            if runtime_spec.command_template:
                return runtime_spec.command_template
        elif isinstance(runtime_spec, dict):
            schema = runtime_spec.get("script_argv_schema")
            if isinstance(schema, dict):
                return render_script_command_from_runtime_schema(entry, schema)
            template = str(runtime_spec.get("command_template") or "")
            if template:
                return template

    payload = _command_args_from_runtime_contract(entry.runtime_contract)
    if payload:
        return _render_command(entry.path, entry.runtime, payload)
    if entry.command_template:
        return entry.command_template
    return _render_command(entry.path, entry.runtime, _stable_external_envelope(values))



_EXPLICIT_ROLE_RE = re.compile(
    rf"(?:role|角色|职责)\s*[：:=]\s*({get_role_pattern()})",
    re.I,
)


def _normalize_role(value: str) -> FileRole | None:
    lowered = (value or "").strip().lower()
    return lowered if is_script_role(lowered) or is_resource_role(lowered) else None  # type: ignore[return-value]




def _segment_for_file(file_path: str, *texts: str) -> str:
    """Return nearby plan text for a file path, stopping before the next file block.

    Capability fields must be scoped to the concrete file block.  Earlier
    versions fell back to the whole blueprint when a field was not found near
    ``file_path``; that allowed global model notes in SKILL.md to leak into
    deterministic builders such as ``scripts/build_pdf.py``.  The fallback is
    now limited to the explicit per-file ``purpose`` text passed by the caller
    (the first argument), never to the full blueprint/SKILL.md summary.
    """
    next_path_re = r"(?m)^\s*(?:(?:[-*]\s*)?(?:(?:scripts|references|assets)/[A-Za-z0-9_./-]+|SKILL\.md)|`{3,}|~{3,}|#{1,6}\s+)"
    best = ""
    saw_path = False
    for text in texts:
        text = text or ""
        for occurrence in re.finditer(re.escape(file_path), text):
            saw_path = True
            after = text[occurrence.end():]
            next_match = re.search(next_path_re, after)
            segment = after[: next_match.start()] if next_match else after
            # Prefer block-style segments that actually contain contract fields;
            # inline path mentions in section summaries often have no local data.
            if re.search(r"\b(?:role|inputs|outputs|dependencies|required_capabilities|optional_capabilities|allowed_capabilities|business_forbidden_capabilities|forbidden_capabilities)\b\s*[：:=]", segment, re.I):
                return segment
            if len(segment) > len(best):
                best = segment
    if saw_path:
        return best
    # The first text is the caller-provided purpose, which is already a local
    # per-file description.  Do not mine the full blueprint/SKILL.md for fields
    # when it does not contain this file path.
    return texts[0] if texts else ""


_FIELD_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_FIELD_ANNOTATION_RE = re.compile(r"\s*(?::|=|（|\()\s*", re.I)
_FIELD_AMBIGUOUS_RE = re.compile(
    r"(?:[|/+&]|\b(?:or|alias|aka|alternative|alternatives)\b|或|或者|别名|候选|可选)",
    re.I,
)
_FIELD_LIST_NAMES_RE = r"role|inputs|outputs|dependencies|constraints|required_capabilities|optional_capabilities|allowed_capabilities|business_forbidden_capabilities|forbidden_capabilities|side_effects|required_tool_slots|language|runtime"


def _clean_concrete_field_name(raw_item: str) -> tuple[str | None, bool]:
    """Return a concrete SkillPlan field name, or mark ambiguous declarations.

    ``inputs``/``outputs`` are a declaration contract, not a place for
    alternatives.  Keep type/default annotations such as ``field: string`` or
    ``field (default: value)``, but never silently remove candidate separators
    and concatenate multiple possible field names into a synthetic one.
    """
    item = str(raw_item or "").strip().strip("'\"`")
    if not item:
        return None, False
    if _FIELD_AMBIGUOUS_RE.search(item):
        return None, True

    head = _FIELD_ANNOTATION_RE.split(item, maxsplit=1)[0].strip().strip("'\"`")
    if not head:
        return None, False
    if not _FIELD_NAME_RE.fullmatch(head):
        # Invalid punctuation/whitespace would previously be stripped and could
        # merge candidate tokens. Surface it as an ambiguous field declaration
        # instead of guessing which token should survive.
        return None, True
    return head, False


def _parse_explicit_list_field(field_name: str, *, file_path: str, purpose: str = "", blueprint_summary: str = "") -> tuple[list[str] | None, list[str]]:
    """Extract SkillPlan list fields and declaration-format warnings."""
    segment = _segment_for_file(file_path, purpose, blueprint_summary)
    pattern = re.compile(rf"(?:{re.escape(field_name)}|{re.escape(field_name.replace('_', ' '))})\s*[：:=]\s*\[?([^\]\n;]+)\]?", re.I)
    match = pattern.search(segment)
    if not match:
        return None, []
    raw = match.group(1)
    raw = re.split(rf"\s+(?:{_FIELD_LIST_NAMES_RE})\s*[：:=]", raw, maxsplit=1, flags=re.I)[0]
    values = [item.strip().strip("'\"") for item in re.split(r"[,，、]\s*", raw) if item.strip()]
    cleaned: list[str] = []
    warnings: list[str] = []
    ambiguous_seen = False
    for item in values:
        if field_name in {"inputs", "outputs"}:
            cleaned_item, ambiguous = _clean_concrete_field_name(item)
            ambiguous_seen = ambiguous_seen or ambiguous
        else:
            cleaned_item = re.sub(r"[^A-Za-z0-9_./-]", "", item)
            ambiguous = False

        if cleaned_item:
            cleaned.append(cleaned_item)

    if ambiguous_seen and field_name in {"inputs", "outputs"}:
        warnings.append(
            f"skill_plan.field_ambiguous: {file_path} {field_name} contains ambiguous alternatives. "
            "SkillPlan inputs/outputs must use concrete field names; ambiguous alternatives are not allowed."
        )

    return cleaned, warnings


def _explicit_list_field(field_name: str, *, file_path: str, purpose: str = "", blueprint_summary: str = "") -> list[str] | None:
    """Extract SkillPlan list fields such as inputs/outputs/dependencies.

    Accepted syntaxes include `inputs: field, other_field`,
    `inputs=[field,other_field]`, and Chinese full-width separators.  For
    inputs/outputs, ambiguous alternatives are omitted instead of being
    sanitized into a synthetic concatenated field name.
    """
    values, _warnings = _parse_explicit_list_field(
        field_name,
        file_path=file_path,
        purpose=purpose,
        blueprint_summary=blueprint_summary,
    )
    return values



def _explicit_constraints_field(*, file_path: str, purpose: str = "", blueprint_summary: str = "") -> list[dict[str, object]]:
    """Read an inline JSON array `constraints: [...]` from the current file block.

    Creator transport preserves dict items only and does not interpret any
    constraint names, kinds, values, comparators, or units.
    """
    segment = _segment_for_file(file_path, purpose, blueprint_summary)
    match = re.search(r"(?:^|\s)constraints\s*[：:=]\s*", segment, re.I)
    if not match:
        return []
    raw = segment[match.end():].lstrip()
    try:
        value, _end = json.JSONDecoder().raw_decode(raw)
    except Exception:
        return []
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]

def skill_plan_field_declaration_warnings(*, file_path: str, purpose: str = "", blueprint_summary: str = "") -> list[str]:
    """Return user-visible warnings for invalid SkillPlan field declarations."""
    warnings: list[str] = []
    for field_name in ("inputs", "outputs"):
        _values, field_warnings = _parse_explicit_list_field(
            field_name,
            file_path=file_path,
            purpose=purpose,
            blueprint_summary=blueprint_summary,
        )
        warnings.extend(field_warnings)
    return warnings



def _explicit_default_values(*, file_path: str, purpose: str = "", blueprint_summary: str = "") -> dict[str, object]:
    """Extract structured input defaults from local plan text without role heuristics."""
    segment = _segment_for_file(file_path, purpose, blueprint_summary)
    defaults: dict[str, object] = {}
    inputs_match = re.search(r"(?:inputs|inputs)\s*[：:=]\s*\[?([^\]\n;]+)\]?", segment, re.I)
    if inputs_match:
        for raw_item in re.split(r"[,，、]\s*", inputs_match.group(1)):
            key, default = parse_schema_input_item(raw_item)
            if key and default is not None:
                defaults[key] = default
    for field_name in ("defaults", "default_values", "默认值", "默认参数"):
        pattern = re.compile(rf"(?:{re.escape(field_name)}|{re.escape(field_name.replace('_', ' '))})\s*[：:=]\s*\[?([^\]\n;]+)\]?", re.I)
        for match in pattern.finditer(segment):
            for raw_item in re.split(r"[,，、]\s*", match.group(1)):
                key, default = parse_schema_input_item(raw_item)
                if key and default is not None:
                    defaults[key] = default
    return defaults



def _explicit_scalar_field(field_name: str, *, file_path: str, purpose: str = "", blueprint_summary: str = "") -> str | None:
    """Extract scalar SkillPlan fields such as language/runtime from local plan text."""
    segment = _segment_for_file(file_path, purpose, blueprint_summary)
    pattern = re.compile(rf"(?:{re.escape(field_name)}|{re.escape(field_name.replace('_', ' '))})\s*[：:=]\s*([^\s\n;,]+)", re.I)
    match = pattern.search(segment)
    if not match:
        return None
    value = match.group(1).strip().strip("'\"").lower()
    value = re.sub(r"[^a-z0-9_-]", "", value)
    return value or None

def _explicit_role_from_plan_text(*, file_path: str, purpose: str = "", blueprint_summary: str = "") -> FileRole | None:
    """Extract an explicit role declared by the plan/model, not by domain keywords.

    Supports both compact one-line declarations such as
    ``scripts/a.py role: text_generator`` and SkillPlan blocks where the path
    appears on one line and ``role: ...`` appears in the following indented
    contract lines.
    """
    next_path_re = r"(?m)^\s*(?:(?:[-*]\s*)?(?:(?:scripts|references|assets)/[A-Za-z0-9_./-]+|SKILL\.md)|`{3,}|~{3,}|#{1,6}\s+)"

    def segment_after_path(text: str) -> str:
        if file_path not in text:
            return text
        after = text.split(file_path, 1)[1]
        next_match = re.search(next_path_re, after)
        if next_match:
            between = after[: next_match.start()]
            # Inline prose may mention a reference/dependency path before the
            # role; block-style SkillPlan keeps role on following indented lines.
            if _EXPLICIT_ROLE_RE.search(between):
                return between
            role_before_next_file = _EXPLICIT_ROLE_RE.search(after)
            if role_before_next_file and role_before_next_file.start() < 500:
                return after[: role_before_next_file.end()]
            return between
        return after

    for text in (purpose, blueprint_summary):
        text = text or ""
        if file_path in text:
            segment = segment_after_path(text)
            match = _EXPLICIT_ROLE_RE.search(segment)
            if match:
                return _normalize_role(match.group(1))
        for line in text.splitlines():
            if file_path not in line and line.strip() != purpose.strip():
                continue
            segment = segment_after_path(line)
            match = _EXPLICIT_ROLE_RE.search(segment)
            if match:
                return _normalize_role(match.group(1))

    match = _EXPLICIT_ROLE_RE.search(purpose or "")
    if match:
        return _normalize_role(match.group(1))
    return None



def _augment_inputs_for_role(role: FileRole, inputs: list[str], *, purpose: str = "", blueprint_summary: str = "") -> list[str]:
    """Return declared inputs without platform-invented business fields."""
    return list(inputs)

def file_role_classifier(
    *,
    file_path: str,
    purpose: str = "",
    blueprint_summary: str = "",
    heuristic_signals: list[str] | None = None,
) -> RoleClassification:
    """Classify files by platform path and explicit SkillPlan role only."""
    file_type = file_type_for_path(file_path)
    signals = list(heuristic_signals or heuristic_signals_for_file(file_path, purpose, blueprint_summary))

    if file_type == "skill_md":
        return RoleClassification("skill_overview", 1.0, "SKILL.md is the process overview file", signals)
    if file_type == "reference":
        return RoleClassification("reference", 1.0, "references/ files contain auxiliary reference material", signals)
    if file_type == "asset":
        return RoleClassification("asset", 1.0, "assets/ files are static resources or templates", signals)

    explicit_role = _explicit_role_from_plan_text(
        file_path=file_path,
        purpose=purpose,
        blueprint_summary=blueprint_summary,
    )
    if explicit_role in SCRIPT_ROLES:
        return RoleClassification(explicit_role, 0.95, "explicit role declared by SkillPlan", signals)

    return RoleClassification(
        "generic_script",
        0.45,
        "no explicit script role in SkillPlan; using conservative generic_script fallback",
        signals,
    )


def default_io_for_role(role: FileRole) -> tuple[list[str], list[str]]:
    """Backward-compatible neutral default; role never decides IO."""
    return [], []


def default_io_for_file_kind(file_kind: FileKind) -> tuple[list[str], list[str]]:
    """Conservative IO defaults derived only from file kind."""
    if file_kind == "script":
        return ["payload"], []
    if file_kind == "skill_doc":
        return ["user_request"], ["workflow"]
    return [], []


def capabilities_for_role(role: FileRole) -> tuple[list[str], list[str]]:
    """Do not infer runtime capabilities from role/component_hint."""
    return [], []


def build_skill_plan_entry(
    *,
    file_path: str,
    purpose: str = "",
    required: bool = True,
    can_skip: bool = False,
    blueprint_summary: str = "",
    reference_files: list[str] | None = None,
) -> SkillPlanEntry:
    """Build one normalized SkillPlanEntry from an explicit SkillPlan block.

    Business capability decomposition comes only from the explicit
    required_capabilities declaration in the concrete file block.

    This function does not infer business capabilities from:
    - role;
    - file name;
    - global blueprint words;
    - Tool Registry.

    The normalized required_capabilities field is the shared semantic source
    used by responsibility graph construction and Creator Tool recall.
    """

    classification = file_role_classifier(
        file_path=file_path,
        purpose=purpose,
        blueprint_summary=blueprint_summary,
    )

    file_type = file_type_for_path(
        file_path
    )

    explicit_required_capabilities = (
        _explicit_list_field(
            "required_capabilities",
            file_path=file_path,
            purpose=purpose,
            blueprint_summary=blueprint_summary,
        )
    )

    explicit_optional_capabilities = (
        _explicit_list_field(
            "optional_capabilities",
            file_path=file_path,
            purpose=purpose,
            blueprint_summary=blueprint_summary,
        )
    )

    explicit_allowed_capabilities = (
        _explicit_list_field(
            "allowed_capabilities",
            file_path=file_path,
            purpose=purpose,
            blueprint_summary=blueprint_summary,
        )
    )

    explicit_side_effects = (
        _explicit_list_field(
            "side_effects",
            file_path=file_path,
            purpose=purpose,
            blueprint_summary=blueprint_summary,
        )
        or []
    )

    explicit_tool_slots = (
        _explicit_list_field(
            "required_tool_slots",
            file_path=file_path,
            purpose=purpose,
            blueprint_summary=blueprint_summary,
        )
        or []
    )

    explicit_constraints = _explicit_constraints_field(
        file_path=file_path,
        purpose=purpose,
        blueprint_summary=blueprint_summary,
    )

    role = classification.role

    role_reason = (
        classification.reason
    )

    explicit_inputs = (
        _explicit_list_field(
            "inputs",
            file_path=file_path,
            purpose=purpose,
            blueprint_summary=blueprint_summary,
        )
    )

    explicit_outputs = (
        _explicit_list_field(
            "outputs",
            file_path=file_path,
            purpose=purpose,
            blueprint_summary=blueprint_summary,
        )
    )

    explicit_default_values = (
        _explicit_default_values(
            file_path=file_path,
            purpose=purpose,
            blueprint_summary=blueprint_summary,
        )
    )

    file_kind = file_kind_for_path(
        file_path
    )

    (
        default_inputs,
        default_outputs,
    ) = default_io_for_file_kind(
        file_kind
    )

    inputs = (
        explicit_inputs
        if explicit_inputs is not None
        else default_inputs
    )

    inputs = _augment_inputs_for_role(
        role,
        inputs,
        purpose=purpose,
        blueprint_summary=blueprint_summary,
    )

    outputs = (
        explicit_outputs
        if explicit_outputs is not None
        else default_outputs
    )

    all_reference_files = _dedupe_paths(
        list(
            reference_files
            or []
        )
    )

    skill_local_references = [
        reference
        for reference
        in all_reference_files
        if _is_skill_local_reference(
            reference
        )
    ]

    creator_internal_references = [
        reference
        for reference
        in all_reference_files
        if _is_creator_internal_reference(
            reference
        )
    ]

    for reference in (
        _CREATOR_INTERNAL_REFERENCE_PATHS
    ):
        if (
            reference
            not in creator_internal_references
        ):
            creator_internal_references.append(
                reference
            )

    explicit_dependencies = (
        _explicit_list_field(
            "dependencies",
            file_path=file_path,
            purpose=purpose,
            blueprint_summary=blueprint_summary,
        )
    )

    dependencies = _dedupe_paths([
        reference
        for reference in (
            explicit_dependencies
            or skill_local_references
        )
        if _is_skill_local_reference(
            reference
        )
    ])

    raw_required_capabilities = (
        explicit_required_capabilities
        or []
    )

    raw_capability_hints = (
        _dedupe_capabilities(
            raw_required_capabilities
        )
    )

    platform_capabilities = [
        capability
        for capability
        in raw_capability_hints
        if capability_layer(
            capability
        )
        == "platform_protocol"
    ]

    required_capabilities = (
        normalize_required_capabilities(
            role=str(role or ""),
            path=file_path,
            required_capabilities=(
                raw_required_capabilities
            ),
            user_blueprint_text="",
        )
    )

    optional_capabilities = [
        capability
        for capability
        in _dedupe_capabilities(
            explicit_optional_capabilities
            or []
        )
        if (
            is_business_capability(
                capability
            )
            and capability
            not in required_capabilities
        )
    ]

    allowed_capabilities = [
        capability
        for capability
        in _dedupe_capabilities(
            explicit_allowed_capabilities
            or []
        )
        if is_business_capability(
            capability
        )
    ]

    explicit_forbidden = (
        _explicit_list_field(
            "business_forbidden_capabilities",
            file_path=file_path,
            purpose=purpose,
            blueprint_summary=blueprint_summary,
        )
        or _explicit_list_field(
            "forbidden_capabilities",
            file_path=file_path,
            purpose=purpose,
            blueprint_summary=blueprint_summary,
        )
    )

    raw_forbidden_capabilities = (
        explicit_forbidden
        or []
    )

    platform_safety_constraints = [
        capability
        for capability
        in _dedupe_capabilities(
            raw_forbidden_capabilities
        )
        if is_platform_safety_constraint(
            capability
        )
    ]

    forbidden_capabilities = [
        capability
        for capability
        in _dedupe_capabilities(
            raw_forbidden_capabilities
        )
        if (
            is_business_capability(
                capability
            )
            and capability
            not in required_capabilities
        )
    ]

    detected_language = language_for_path(
        file_path
    )

    explicit_language = (
        _explicit_scalar_field(
            "language",
            file_path=file_path,
            purpose=purpose,
            blueprint_summary=blueprint_summary,
        )
    )

    language = (
        explicit_language
        if explicit_language in {
            "python",
            "javascript",
            "bash",
            "sql",
            "yaml",
            "json",
            "markdown",
            "html",
            "css",
            "text",
        }
        else detected_language
    )

    detected_runtime = runtime_for_language(
        language,
        file_type,
    )

    explicit_runtime = (
        _explicit_scalar_field(
            "runtime",
            file_path=file_path,
            purpose=purpose,
            blueprint_summary=blueprint_summary,
        )
    )

    runtime = (
        explicit_runtime
        if explicit_runtime in {
            "python",
            "node",
            "bash",
            "shell",
            "generic",
            "none",
        }
        else detected_runtime
    )

    return SkillPlanEntry(
        path=file_path,
        file_type=file_type,
        role=role,
        purpose=purpose,
        file_kind=file_kind,
        component_hint=role,
        inputs=inputs,
        outputs=outputs,
        default_values=(
            explicit_default_values
        ),
        dependencies=dependencies,
        side_effects=(
            explicit_side_effects
        ),

        required_capabilities=(
            required_capabilities
        ),

        raw_capability_hints=(
            raw_capability_hints
        ),

        optional_capabilities=(
            optional_capabilities
        ),

        allowed_capabilities=(
            allowed_capabilities
        ),

        forbidden_capabilities=(
            forbidden_capabilities
        ),

        business_capabilities=list(
            required_capabilities
        ),

        platform_capabilities=(
            platform_capabilities
        ),

        business_forbidden_capabilities=(
            forbidden_capabilities
        ),

        platform_safety_constraints=(
            platform_safety_constraints
        ),

        execution_contract=(
            {
                "runtime": runtime,
                "entrypoint": file_path,
            }
            if file_type == "script"
            else {}
        ),

        runtime_contract=(
            {
                "runtime": runtime,
                "entrypoint": file_path,
                "argv": "json_object",
            }
            if file_type == "script"
            else {
                "runtime": "none",
            }
        ),

        artifact_contract=(
            {
                "stdout_fields": list(
                    outputs or []
                ),
                "final": bool(
                    file_type == "script"
                ),
            }
            if file_type == "script"
            else {}
        ),
        constraints=explicit_constraints,

        layer=(
            "business_skill"
            if file_type in {
                "skill",
                "script",
                "skill_md",
            }
            else "static_resource"
        ),

        reference_files=(
            skill_local_references
        ),

        skill_local_references=(
            skill_local_references
        ),

        creator_internal_references=(
            creator_internal_references
        ),

        language=language,
        runtime=runtime,

        entrypoint=(
            file_path
            if file_type == "script"
            else ""
        ),

        command_template=(
            command_template_for_entry(
                file_path,
                runtime,
                inputs,
            )
            if file_type == "script"
            else ""
        ),

        required_tool_slots=(
            explicit_tool_slots
        ),

        required=required,
        can_skip=can_skip,
        confidence=(
            classification.confidence
        ),
        reason=role_reason,
        heuristic_signals=(
            classification.heuristic_signals
        ),
    )


_RUNTIME_OUTPUT_DIR_RE = re.compile(r"(?:^|/)(?:outputs?|output|generated|build|dist|tmp|temp|artifacts?)(?:/|$)|输出目录|产物目录|结果目录|中间目录|OUTPUT_DIR", re.I)
_RUNTIME_OUTPUT_EXT_RE = re.compile(r"\.(?:pdf|docx|pptx|xlsx|csv|json|png|jpe?g|webp|gif|svg|zip|html)$", re.I)
_DYNAMIC_PATH_RE = re.compile(r"\{\{|\}\}|\$\{|<[^>/]+>|\[[^\]]+\]|\*|\?|按.*(?:生成|输出)|动态|占位符|变量|runtime|运行时", re.I)
_GENERATED_SEMANTIC_RE = re.compile(r"生成|产物|输出|导出|写入|构建|保存|最终结果|最终文件|中间文件|runtime|运行时|generated|output|artifact|export|build|result", re.I)
_UPLOAD_ONLY_RE = re.compile(r"上传|已有|现成|预置|静态|模板|素材|upload|provided|static|bundled", re.I)
def is_dynamic_file_path(path: str) -> bool:
    return bool(_DYNAMIC_PATH_RE.search(path or ""))


def is_runtime_output_path(path: str) -> bool:
    normalized = (path or "").replace("\\", "/")
    return bool(_RUNTIME_OUTPUT_DIR_RE.search(normalized))


def is_runtime_artifact_semantic(path: str, text: str = "") -> bool:
    """Return True when a file-plan item describes a runtime/generated artifact.

    Static uploaded assets can have image/document extensions, so an extension is
    not enough by itself.  We require a runtime/output directory, a dynamic path,
    or generated/final-output semantics near the file contract.
    """
    normalized = (path or "").replace("\\", "/")
    semantic_text = f"{normalized}\n{text or ''}"
    if is_dynamic_file_path(normalized):
        return True
    if is_runtime_output_path(normalized):
        return True
    if _GENERATED_SEMANTIC_RE.search(semantic_text) and (_RUNTIME_OUTPUT_EXT_RE.search(normalized) or normalized.startswith("assets/")):
        if normalized.startswith("assets/") and _UPLOAD_ONLY_RE.search(text or "") and not re.search(r"最终|产物|输出|导出|生成|generated|output|artifact|export", text or "", re.I):
            return False
        return True
    return False


def _is_asset_upload_only(entry: SkillPlanEntry) -> bool:
    text = f"{entry.purpose}\n{' '.join(entry.inputs)}\n{' '.join(entry.outputs)}\n{' '.join(entry.dependencies)}"
    if entry.inputs or entry.outputs or entry.dependencies or entry.required_capabilities:
        return False
    if is_dynamic_file_path(entry.path) or is_runtime_output_path(entry.path):
        return False
    explicit_static_source = re.search(r"(?m)^\s*(?:source|asset_source)\s*:\s*(?:user_upload|upload|uploaded|bundled|static)\s*$", entry.purpose or "", re.I)
    explicit_runtime_artifact = re.search(r"运行时产物|运行时生成|脚本生成|最终产物|最终生成|runtime\s+artifact|generated\s+artifact", entry.purpose or "", re.I)
    if explicit_static_source and not explicit_runtime_artifact:
        return True
    if is_runtime_artifact_semantic(entry.path, text):
        return False
    return True


def dependency_is_output_semantic(dep: str, prior_outputs: set[str] | None = None) -> bool:
    dep = str(dep or "").strip().replace("\\", "/")
    if not dep:
        return False
    if is_runtime_output_path(dep) or is_dynamic_file_path(dep):
        return True
    if prior_outputs and dep in prior_outputs:
        return True
    return False


def _command_template_for_entry_with_values(entry: SkillPlanEntry) -> str:
    return render_script_command_from_skill_plan(entry)


def _tool_slots_from_structured_contract(entry: SkillPlanEntry) -> list[ToolSlot]:
    """Infer real external/interface slots from structured fields only.

    Runtime argv/stdout and final artifact metadata are represented by
    runtime_contract/artifact_contract and are intentionally not tool slots.
    """
    slots: list[ToolSlot] = []
    for name in getattr(entry, "required_tool_slots", []) or []:
        if isinstance(name, ToolSlot):
            slots.append(name)
        elif str(name).strip():
            slots.append(ToolSlot(slot_id=str(name).strip()))
    for effect in getattr(entry, "side_effects", []) or []:
        effect_name = str(effect).strip()
        if effect_name:
            slots.append(ToolSlot(slot_id=f"{entry.path}:{effect_name}", side_effects=[effect_name], input_contract={key: "any" for key in entry.inputs}, output_contract={key: "any" for key in entry.outputs}, runtime_requirements={"runtime": entry.runtime}))
    seen: set[str] = set()
    deduped: list[ToolSlot] = []
    for slot in slots:
        if slot.slot_id in seen:
            continue
        seen.add(slot.slot_id)
        deduped.append(slot)
    return deduped


def _implementation_strategy_for_slot(slot: ToolSlot) -> ImplementationStrategy:
    effects = {str(item).strip().lower() for item in (slot.side_effects or []) if str(item).strip()}
    requirements = slot.runtime_requirements or {}
    if effects & {"user_asset", "user_upload", "uploaded_asset"}:
        return ImplementationStrategy(slot_id=slot.slot_id, strategy="require_user_asset", reason="slot declares a user-provided asset side effect")
    if effects & {"external_api", "network", "http", "webhook", "database", "secret"}:
        return ImplementationStrategy(slot_id=slot.slot_id, strategy="require_external_config", reason="slot requires external service/configuration")
    if requirements.get("tool_id") or requirements.get("registered_tool"):
        return ImplementationStrategy(slot_id=slot.slot_id, strategy="use_registered_tool", tool_id=str(requirements.get("tool_id") or requirements.get("registered_tool") or ""), reason="slot explicitly references a registered tool")
    if effects - {"file_write", "local_file", "stdout_json"}:
        return ImplementationStrategy(slot_id=slot.slot_id, strategy="unsupported", reason="slot side effects are not locally implementable without a selected tool/config")
    return ImplementationStrategy(slot_id=slot.slot_id, strategy="generate_code", reason="slot is implementable with ordinary local code")


def normalize_skill_plan(
    plan: SkillPlan,
) -> SkillPlan:
    """Normalize a parsed SkillPlan without changing business semantics.

    Capability ownership:

        explicit SkillPlan required_capabilities
        -> build_skill_plan_entry
        -> SkillPlanEntry.required_capabilities
        -> normalize_skill_plan
        -> FileSpecOut.required_capabilities
        -> RequirementItem.required_tools
        -> Creator Tool recall

    This normalizer may:
    - normalize paths;
    - remove invalid runtime dependencies;
    - remove runtime capabilities from resource/meta files;
    - normalize script runtime/artifact contracts;
    - normalize explicit business capability lists.

    This normalizer must not:
    - erase script required_capabilities;
    - infer capabilities from role or path;
    - infer capabilities from blueprint prose;
    - resolve Registry tools;
    - authorize tools.
    """

    entries: list[
        SkillPlanEntry
    ] = []

    warnings = list(
        plan.warnings
        or []
    )

    seen_skill_md = False

    prior_outputs: set[str] = set()

    for entry in (
        plan.files
        or []
    ):
        path = (
            str(
                entry.path
                or ""
            )
            .replace(
                "\\",
                "/",
            )
            .strip()
        )

        if path == "SKILL.md":
            if seen_skill_md:
                warnings.append(
                    (
                        "已移除重复的 SKILL.md "
                        "文件计划项；Skill 包只能"
                        "有一个 SKILL.md。"
                    )
                )

                continue

            seen_skill_md = True

        raw_capability_hints = (
            _dedupe_capabilities(
                list(
                    entry.raw_capability_hints
                    or entry.required_capabilities
                    or []
                )
            )
        )

        # required_capabilities is already the normalized
        # business capability contract produced by
        # build_skill_plan_entry.
        #
        # Normalize it again only as a defensive layer.
        # Never replace it with [] merely because this is a
        # second normalization pass.
        normalized_required = (
            normalize_required_capabilities(
                role=str(
                    entry.role
                    or ""
                ),
                path=path,
                required_capabilities=list(
                    entry.required_capabilities
                    or []
                ),
                user_blueprint_text="",
            )
        )

        normalized_optional = [
            capability
            for capability
            in _dedupe_capabilities(
                list(
                    entry.optional_capabilities
                    or []
                )
            )
            if (
                is_business_capability(
                    capability
                )
                and capability
                not in normalized_required
            )
        ]

        normalized_allowed = [
            capability
            for capability
            in _dedupe_capabilities(
                list(
                    entry.allowed_capabilities
                    or []
                )
            )
            if is_business_capability(
                capability
            )
        ]

        normalized_forbidden = [
            capability
            for capability
            in _dedupe_capabilities(
                list(
                    entry.business_forbidden_capabilities
                    or entry.forbidden_capabilities
                    or []
                )
            )
            if (
                is_business_capability(
                    capability
                )
                and capability
                not in normalized_required
            )
        ]

        normalized_platform_capabilities = [
            capability
            for capability
            in _dedupe_capabilities(
                list(
                    entry.platform_capabilities
                    or []
                )
            )
            if (
                capability_layer(
                    capability
                )
                == "platform_protocol"
            )
        ]

        normalized_platform_safety_constraints = [
            capability
            for capability
            in _dedupe_capabilities(
                list(
                    entry.platform_safety_constraints
                    or []
                )
            )
            if is_platform_safety_constraint(
                capability
            )
        ]

        dependencies = [
            dependency
            for dependency
            in _dedupe_paths(
                list(
                    entry.dependencies
                    or []
                )
            )
            if not dependency_is_output_semantic(
                dependency,
                prior_outputs,
            )
        ]

        removed_dependencies = (
            set(
                entry.dependencies
                or []
            )
            - set(
                dependencies
            )
        )

        for dependency in sorted(
            removed_dependencies
        ):
            warnings.append(
                (
                    f"已从 {path} dependencies "
                    "移除输出/动态路径 "
                    f"{dependency}；"
                    "dependencies 只能表示"
                    "运行前输入依赖。"
                )
            )

        cleaned = replace(
            entry,

            path=path,

            file_kind=(
                file_kind_for_path(
                    path
                )
            ),

            component_hint=(
                entry.component_hint
                or entry.role
            ),

            required_capabilities=(
                normalized_required
            ),

            raw_capability_hints=(
                raw_capability_hints
            ),

            optional_capabilities=(
                normalized_optional
            ),

            allowed_capabilities=(
                normalized_allowed
            ),

            forbidden_capabilities=(
                normalized_forbidden
            ),

            business_capabilities=list(
                normalized_required
            ),

            platform_capabilities=(
                normalized_platform_capabilities
            ),

            business_forbidden_capabilities=(
                normalized_forbidden
            ),

            platform_safety_constraints=(
                normalized_platform_safety_constraints
            ),

            dependencies=dependencies,
        )

        # Resource/meta files cannot own runtime business
        # capabilities.
        #
        # This is a platform boundary cleanup, not business
        # capability inference.
        if (
            cleaned.role
            in RESOURCE_ROLES
            or cleaned.file_type
            in {
                "skill_md",
                "reference",
                "asset",
            }
        ):
            cleaned = replace(
                cleaned,

                required_capabilities=[],

                optional_capabilities=[],

                allowed_capabilities=[],

                business_capabilities=[],
            )

        if (
            cleaned.role == "asset"
            or cleaned.file_type == "asset"
            or path.startswith(
                "assets/"
            )
        ):
            if not _is_asset_upload_only(
                cleaned
            ):
                warnings.append(
                    (
                        "已移除非法 asset 文件计划项 "
                        f"{path}；"
                        "assets/ 只能表示用户上传"
                        "或系统预置的静态素材，"
                        "不能是运行时产物。"
                    )
                )

                continue

            cleaned = replace(
                cleaned,

                inputs=[],

                outputs=[],

                dependencies=[],

                required_capabilities=[],

                raw_capability_hints=[],

                optional_capabilities=[],

                allowed_capabilities=[],

                business_capabilities=[],

                platform_capabilities=[],

                runtime="none",

                entrypoint="",

                command_template="",

                execution_contract={},

                runtime_contract={
                    "runtime": "none",
                },

                artifact_contract={},

                layer="static_resource",
            )

        if (
            cleaned.file_type
            == "reference"
            and cleaned.path.startswith(
                "references/"
            )
            and cleaned.runtime
            != "none"
        ):
            cleaned = replace(
                cleaned,

                runtime="none",

                entrypoint="",

                command_template="",

                execution_contract={},

                runtime_contract={
                    "runtime": "none",
                },

                artifact_contract={},
            )

        if (
            cleaned.file_type
            == "script"
        ):
            slots = (
                _tool_slots_from_structured_contract(
                    cleaned
                )
            )

            strategies = [
                _implementation_strategy_for_slot(
                    slot
                )
                for slot in slots
            ]

            cleaned = replace(
                cleaned,

                command_template=(
                    _command_template_for_entry_with_values(
                        cleaned
                    )
                ),

                required_tool_slots=(
                    slots
                ),

                implementation_strategy=(
                    strategies
                ),

                runtime_contract={
                    "runtime": (
                        cleaned.runtime
                    ),

                    "entrypoint": (
                        cleaned.path
                    ),

                    "argv": (
                        "json_object"
                    ),
                },

                artifact_contract={
                    "stdout_fields": list(
                        cleaned.outputs
                        or []
                    ),

                    "final": True,
                },
            )

            prior_outputs.update(
                cleaned.outputs
                or []
            )

        entries.append(
            cleaned
        )

    return SkillPlan(
        skill_name=plan.skill_name,

        files=entries,

        warnings=warnings,
        function_items=list(plan.function_items or []),
        responsibility_edges=list(plan.responsibility_edges or []),
    )


def validate_file_plan_semantics(plan: SkillPlan) -> list[str]:
    """Return generic semantic file-plan violations after normalization."""
    issues: list[str] = []
    skill_md_count = sum(1 for entry in plan.files if entry.path == "SKILL.md")
    if skill_md_count != 1:
        issues.append(f"SKILL.md must be unique; found {skill_md_count}.")

    prior_outputs: set[str] = set()
    for entry in plan.files:
        if entry.path == "SKILL.md" and entry.role != "skill_overview":
            issues.append("SKILL.md role must be skill_overview.")
        if entry.file_type == "script" and not entry.path.startswith("scripts/"):
            issues.append(f"Script file must be under scripts/: {entry.path}")
        if entry.file_type == "reference" and not entry.path.startswith("references/"):
            issues.append(f"Reference file must be under references/: {entry.path}")
        if entry.file_type == "asset" or entry.role == "asset" or entry.path.startswith("assets/"):
            if not _is_asset_upload_only(entry):
                issues.append(f"Asset must be upload-only static resource: {entry.path}")
        if entry.file_type != "script" and entry.required_capabilities:
            issues.append(f"Resource/meta file must not declare runtime capabilities: {entry.path}")
        for dep in entry.dependencies:
            if dependency_is_output_semantic(dep, prior_outputs):
                issues.append(f"Dependency cannot be output/dynamic path: {entry.path} -> {dep}")
        if entry.file_type != "script" and is_runtime_artifact_semantic(entry.path, entry.purpose):
            issues.append(f"Runtime artifact cannot be a Creator file-plan item: {entry.path}")
        prior_outputs.update(entry.outputs or [])
    return issues


def command_payload_placeholders(command: str, script_path: str) -> dict[str, str] | None:
    import json, shlex
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
            placeholders: dict[str, str] = {}
            for key, value in payload.items():
                if isinstance(value, str):
                    match = re.fullmatch(r"\{\{\s*([A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)*)\s*\}\}", value.strip())
                    placeholders[str(key)] = match.group(1) if match else value.strip()
                else:
                    placeholders[str(key)] = ""
            return placeholders
    return None


def validate_skill_plan_dataflow(plan: SkillPlan) -> list[str]:
    """Validate linear script I/O edges without assuming business field names.

    Any input may originate from the user's request, extracted runtime context,
    or static references/assets.  The platform only enforces a stricter edge
    when a downstream input corresponds to a field already produced by an
    upstream script: in that case the command must pass that prior stdout JSON
    field instead of silently falling back to unrelated raw text.
    """
    issues: list[str] = []
    produced: set[str] = set()
    scripts = [entry for entry in plan.files if entry.file_type == "script"]
    consumed_outputs: set[str] = set()
    for entry in scripts:
        placeholders = command_payload_placeholders(entry.command_template, entry.path)
        if placeholders is None:
            issues.append(f"{entry.path} command_template must pass a JSON object argv.")
            placeholders = {}
        for key in entry.inputs:
            placeholder = placeholders.get(key)
            if placeholder is None:
                issues.append(f"{entry.path} command_template missing input key '{key}'.")
            elif key in produced and placeholder not in produced and placeholder != key:
                issues.append(f"{entry.path} input '{key}' must reference prior stdout JSON field, not '{placeholder}'.")
            elif placeholder in produced:
                consumed_outputs.add(placeholder)
        produced.update(entry.outputs or [])
    final_outputs = set(scripts[-1].outputs or []) if scripts else set()
    dangling = produced - consumed_outputs - final_outputs
    for output in sorted(dangling):
        issues.append(f"Script output '{output}' is neither consumed by a later script nor final output metadata.")
    return issues


def validate_role(role: str, file_type: FileType) -> bool:
    if file_type == "script":
        return is_script_role(role)
    return is_resource_role(role)


def logical_plan_check(plan: SkillPlan) -> list[str]:
    """Validate normalized-plan logical closure before concrete tool choice.

    This check is intentionally independent of role/capability labels and tool
    registry availability. It verifies only file presence, acyclic dependencies,
    sortable workflow order, input/output flow, and final-artifact clarity.
    """
    issues = validate_file_plan_semantics(plan)
    issues.extend(validate_skill_plan_dataflow(plan))
    paths = {entry.path for entry in plan.files}
    for entry in plan.files:
        for dep in entry.dependencies:
            if dep.startswith(("scripts/", "references/", "assets/")) and dep not in paths:
                issues.append(f"{entry.path} dependency has no source file: {dep}")
    scripts = [entry for entry in plan.files if entry.file_kind == "script"]
    if scripts and not any(entry.outputs for entry in scripts):
        issues.append("Final artifact/output is unresolved; at least one script must declare outputs.")
    return issues


def implementation_resolution(plan: SkillPlan) -> list[str]:
    """Validate that each normalized tool slot has an implementation strategy."""
    issues: list[str] = []
    for entry in plan.files:
        strategies = {strategy.slot_id: strategy.strategy for strategy in (entry.implementation_strategy or [])}
        for slot in entry.required_tool_slots or []:
            strategy = strategies.get(slot.slot_id)
            if not strategy:
                issues.append(f"{entry.path} tool slot {slot.slot_id} has no implementation strategy.")
            elif strategy == "unsupported":
                issues.append(f"{entry.path} tool slot {slot.slot_id} is unsupported.")
    return issues
