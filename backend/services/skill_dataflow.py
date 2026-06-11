"""Generic SKILL.md/script stdout dataflow helpers.

The sandbox workflow runtime must derive variable flow from Action schema and
``{{placeholder}}`` usage rather than from business-specific field names.  This
module owns default parsing, context merging, placeholder resolution, generic
foreach expansion, and loop-output collection.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import shlex
from pathlib import Path
from typing import Any, Iterable

_COMMAND_BLOCK_RE = re.compile(r"```(?:bash|sh|shell)?\s*\n([\s\S]*?)\n```", re.IGNORECASE)
_PLACEHOLDER_RE = re.compile(r"{{\s*([A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*|\.[0-9]+)*)\s*}}")
_KEY_VALUE_RE = re.compile(r"(?P<key>[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)*)\s*[：:=]\s*(?P<value>[^，。\n,;；]+)")


@dataclass(frozen=True)
class SkillCommandDataflow:
    """A command found in SKILL.md plus variables it consumes."""

    script_path: str
    command: str
    required_variables: tuple[str, ...]


class DataflowError(ValueError):
    """Base class for generic workflow dataflow errors."""


class MissingVariablesError(DataflowError):
    """Raised when placeholders cannot be resolved from context."""

    def __init__(self, missing: Iterable[str], *, message: str | None = None):
        self.missing = tuple(dict.fromkeys(str(item) for item in missing if str(item)))
        needed = ", ".join(f"{{{{{key}}}}}" for key in self.missing)
        super().__init__(message or f"dataflow_mismatch: 缺少变量 {needed}")


class LoopExpansionError(DataflowError):
    """Raised when a step's missing variables cannot be expanded from a list."""

    def __init__(self, missing: Iterable[str], *, message: str | None = None):
        self.missing = tuple(dict.fromkeys(str(item) for item in missing if str(item)))
        needed = ", ".join(f"{{{{{key}}}}}" for key in self.missing)
        super().__init__(message or f"dataflow_mismatch: 循环变量无法展开：{needed}")


def placeholder_pattern() -> re.Pattern[str]:
    return _PLACEHOLDER_RE


def extract_placeholders(value: Any) -> set[str]:
    """Return all placeholder paths referenced by a nested value."""
    keys: set[str] = set()
    if isinstance(value, str):
        keys.update(_PLACEHOLDER_RE.findall(value))
    elif isinstance(value, list):
        for item in value:
            keys.update(extract_placeholders(item))
    elif isinstance(value, dict):
        for item in value.values():
            keys.update(extract_placeholders(item))
    return keys


def context_has(context: dict[str, Any], path: str) -> bool:
    try:
        value = resolve_context_value(context, path)
    except KeyError:
        return False
    return _value_present(value)


def missing_placeholders(keys: Iterable[str], context: dict[str, Any]) -> list[str]:
    """Return placeholder paths that cannot be resolved from context."""
    return sorted(key for key in dict.fromkeys(keys) if not context_has(context, key))


def resolve_context_value(context: dict[str, Any], path: str) -> Any:
    """Resolve a plain or dotted placeholder path from a context object."""
    parts = [part for part in str(path or "").split(".") if part]
    if not parts:
        raise KeyError(path)
    current: Any = context
    for part in parts:
        if isinstance(current, dict):
            if part not in current:
                raise KeyError(path)
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit():
            index = int(part)
            try:
                current = current[index]
            except IndexError as exc:
                raise KeyError(path) from exc
            continue
        raise KeyError(path)
    return current


def replace_placeholders_in_value(value: Any, context: dict[str, Any]) -> Any:
    """Replace placeholders in nested values using only the provided context."""
    if isinstance(value, str):
        exact = _PLACEHOLDER_RE.fullmatch(value.strip())
        if exact:
            key = exact.group(1)
            try:
                return resolve_context_value(context, key)
            except KeyError as exc:
                raise MissingVariablesError([key]) from exc

        missing = missing_placeholders(_PLACEHOLDER_RE.findall(value), context)
        if missing:
            raise MissingVariablesError(missing)

        def repl(match: re.Match[str]) -> str:
            replacement = resolve_context_value(context, match.group(1))
            if isinstance(replacement, (dict, list)):
                return json.dumps(replacement, ensure_ascii=False)
            return str(replacement)

        return _PLACEHOLDER_RE.sub(repl, value)
    if isinstance(value, list):
        return [replace_placeholders_in_value(item, context) for item in value]
    if isinstance(value, dict):
        return {key: replace_placeholders_in_value(item, context) for key, item in value.items()}
    return value


def _extract_script_path(command: str) -> str | None:
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    for part in parts:
        normalized = part.replace("\\", "/").lstrip("./")
        if normalized.startswith("scripts/"):
            return normalized
        idx = normalized.find("/scripts/")
        if idx >= 0:
            return normalized[idx + 1 :]
    return None


def extract_skill_commands(skill_md: str) -> list[SkillCommandDataflow]:
    """Parse bash-like SKILL.md command blocks and their ``{{variables}}``."""
    commands: list[SkillCommandDataflow] = []
    for match in _COMMAND_BLOCK_RE.finditer(skill_md or ""):
        command = match.group(1).strip()
        script_path = _extract_script_path(command)
        if not script_path:
            continue
        variables = tuple(dict.fromkeys(_PLACEHOLDER_RE.findall(command)))
        commands.append(SkillCommandDataflow(script_path=script_path, command=command, required_variables=variables))
    return commands


def parse_schema_input_item(item: str) -> tuple[str, Any | None]:
    """Parse one input/default declaration into ``(key, default)``."""
    raw = (item or "").strip().strip("'\"")
    if not raw:
        return "", None

    default: Any | None = None
    default_match = re.search(
        r"(?:\(|（)\s*(?:default|默认值?|缺省值?)\s*(?:[:：=])\s*([^()（）]+?)\s*(?:\)|）)",
        raw,
        re.I,
    )
    if default_match:
        default = coerce_default_value(default_match.group(1).strip())
        raw = (raw[: default_match.start()] + raw[default_match.end() :]).strip()

    inline_match = re.match(r"^\s*([A-Za-z_][\w./-]*)\??\s*(?:[:：=])\s*(.+?)\s*$", raw)
    if inline_match:
        key = _clean_key(inline_match.group(1).rstrip("?"))
        if default is None:
            default = coerce_default_value(inline_match.group(2).strip())
        return key, default

    key = re.split(r"\s*(?:：|:|=|（|\(|\s)\s*", raw, maxsplit=1)[0]
    return _clean_key(key.rstrip("?")), default


def coerce_default_value(value: str) -> Any:
    """Coerce a textual default without using field-name heuristics."""
    cleaned = (value or "").strip().strip("'\"")
    if cleaned == "":
        return ""
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    if re.fullmatch(r"-?\d+", cleaned):
        return int(cleaned)
    if re.fullmatch(r"-?\d+\.\d+", cleaned):
        return float(cleaned)
    lowered = cleaned.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none"}:
        return None
    return cleaned


def parse_schema_default_values(text: str, *, field_pattern_factory) -> dict[str, Any]:
    """Extract default values from an Action schema text block."""
    defaults: dict[str, Any] = {}
    inputs_match = re.search(field_pattern_factory("inputs"), text or "", re.I)
    if inputs_match:
        for raw_item in re.split(r"[,，、]\s*", inputs_match.group(1)):
            key, default = parse_schema_input_item(raw_item)
            if key and default is not None:
                defaults[key] = default

    for field in ("defaults", "default_values", "默认值", "默认参数"):
        for match in re.finditer(field_pattern_factory(field), text or "", re.I):
            for raw_item in re.split(r"[,，、]\s*", match.group(1)):
                key, default = parse_schema_input_item(raw_item)
                if key and default is not None:
                    defaults[key] = default
    return defaults


def initial_context_from_entries(entries: Iterable[dict[str, Any]], *, user_text: str = "", user_context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build initial workflow context from schema defaults, user text, and planner context.

    User-provided values intentionally override SKILL.md defaults.  Free-form
    natural language is only stored under generic request keys; arbitrary field
    extraction must come from JSON or explicit ``key: value`` pairs.
    """
    context: dict[str, Any] = {}
    for entry in entries:
        defaults = entry.get("default_values") or {}
        if isinstance(defaults, dict):
            context.update(defaults)

    text = (user_text or "").strip()
    if text:
        context.update({"user_request": text, "input": text, "text": text})
        structured_fields = extract_inline_context_values(text)
        if structured_fields:
            context.setdefault("fields", {}).update(structured_fields)
            context.update(structured_fields)

    context.update(user_context or {})
    return context


def extract_inline_context_values(user_text: str) -> dict[str, Any]:
    """Extract generic JSON or explicit ``key: value`` user-provided values."""
    values: dict[str, Any] = {}
    try:
        maybe_json = json.loads(user_text)
    except json.JSONDecodeError:
        maybe_json = None
    if isinstance(maybe_json, dict):
        values.update(maybe_json)

    for match in _KEY_VALUE_RE.finditer(user_text or ""):
        key = match.group("key")
        raw_value = match.group("value").strip()
        values[key] = coerce_default_value(raw_value)
    return values


def parse_stdout_context(stdout: str) -> dict[str, Any]:
    """Return a script stdout JSON object, or raise ValueError with context."""
    try:
        payload = json.loads((stdout or "").strip())
    except json.JSONDecodeError as exc:
        raise ValueError("stdout 不是合法 JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError("stdout 必须是 JSON object")
    return payload


def merge_context(*contexts: dict[str, Any]) -> dict[str, Any]:
    """Merge context dictionaries from left to right."""
    merged: dict[str, Any] = {}
    for context in contexts:
        if isinstance(context, dict):
            merged.update(context)
    return merged


def merge_step_output(context: dict[str, Any], script_path: str, stdout_json: dict[str, Any]) -> dict[str, Any]:
    """Merge one script stdout JSON into flat and script-name namespaced context."""
    if not isinstance(stdout_json, dict):
        return context
    context.update(stdout_json)
    namespace = Path(script_path).stem
    if namespace:
        context[namespace] = stdout_json
    return context


def expand_step_contexts(entry: dict[str, Any], context: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one or more execution contexts for a workflow step.

    If all placeholders already resolve, the step runs once.  If some are
    missing, the runtime searches existing list values whose elements can supply
    the missing placeholders and expands the step once per list element.
    """
    placeholders = set(entry.get("placeholder_keys") or [])
    missing = missing_placeholders(placeholders, context)
    if not missing:
        return [context]

    for list_key, items in _iter_context_lists(context):
        expanded: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            child = dict(context)
            child["loop_item"] = item
            child["loop_index"] = index
            child[f"{list_key}_item"] = item
            if isinstance(item, dict):
                child.update(item)
            elif len(missing) == 1:
                child[missing[0]] = item
            if missing_placeholders(missing, child):
                expanded = []
                break
            expanded.append(child)
        if expanded:
            return expanded

    if any(isinstance(value, list) for value in context.values()):
        raise LoopExpansionError(missing)
    raise MissingVariablesError(missing)


def collect_loop_outputs(step_payloads: list[dict[str, Any]], entry: dict[str, Any]) -> dict[str, Any]:
    """Collect repeated step stdout values into generic aggregate variables."""
    if len(step_payloads) <= 1:
        return {}

    keys: list[str] = []
    for key in entry.get("outputs") or []:
        key_text = str(key or "").strip()
        if key_text:
            keys.append(key_text)
    for payload in step_payloads:
        if isinstance(payload, dict):
            for key in payload.keys():
                if key not in keys:
                    keys.append(key)

    collected: dict[str, Any] = {}
    for key in keys:
        values: list[Any] = []
        for payload in step_payloads:
            if not isinstance(payload, dict) or key not in payload:
                continue
            value = payload[key]
            if isinstance(value, list):
                values.extend(value)
            else:
                values.append(value)
        if not values:
            continue
        collected[key] = values
        collected[_pluralize_key(key)] = values
        collected[f"{key}_list"] = values
    return collected


def missing_variables_for_command(command: SkillCommandDataflow, context: dict[str, Any]) -> list[str]:
    """Return placeholders required by ``command`` that context cannot satisfy."""
    return [name for name in command.required_variables if not context_has(context, name)]


def validate_dataflow_closed(
    commands: Iterable[SkillCommandDataflow],
    initial_context: dict[str, Any] | None = None,
    stdout_by_script: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Validate that each command's placeholders are provided before use."""
    context = dict(initial_context or {})
    stdout_map = stdout_by_script or {}
    for command in commands:
        missing = missing_variables_for_command(command, context)
        if missing:
            raise ValueError(f"{command.script_path} 缺少上游变量: {', '.join(missing)}")
        if command.script_path in stdout_map:
            context = merge_context(context, parse_stdout_context(stdout_map[command.script_path]))
    return context


def validate_and_align_step_stdout(entry: dict[str, Any], step_plan: dict[str, Any], stdout_json: dict[str, Any]) -> dict[str, Any]:
    """Validate one step stdout against generic planned/schema outputs.

    Exact stdout keys win. For singular-vs-collection naming mismatches, add
    generic aliases such as ``name``, pluralized ``name`` and ``name_list``
    without relying on business-specific fields.
    """
    if not isinstance(stdout_json, dict):
        raise DataflowError("stdout 必须是 JSON object")
    expected = _expected_output_specs(entry, step_plan)
    if not expected:
        return stdout_json

    aligned = dict(stdout_json)
    missing: list[str] = []
    type_errors: list[str] = []
    for spec in expected:
        key = str(spec.get("name") or "").strip()
        if not key:
            continue
        if key not in aligned:
            alias_value = _generic_output_alias_value(key, aligned)
            if alias_value is not _MISSING:
                aligned[key] = alias_value
        if key not in aligned:
            missing.append(key)
            continue
        expected_type = str(spec.get("type") or spec.get("value_type") or "").strip().lower()
        if expected_type and not _stdout_value_matches_type(aligned[key], expected_type):
            type_errors.append(f"{key}: expected {expected_type}, actual {type(aligned[key]).__name__}")

    if missing or type_errors:
        details = {"missing_outputs": missing, "type_errors": type_errors, "stdout_keys": sorted(str(key) for key in stdout_json.keys())}
        raise DataflowError("workflow stdout 与 plan outputs 不一致: " + json.dumps(details, ensure_ascii=False))
    return aligned


def _expected_output_specs(entry: dict[str, Any], step_plan: dict[str, Any]) -> list[dict[str, Any]]:
    raw_outputs = step_plan.get("outputs") or step_plan.get("expected_outputs") or entry.get("outputs") or []
    if isinstance(raw_outputs, dict):
        raw_outputs = raw_outputs.items()
    specs: list[dict[str, Any]] = []
    for item in raw_outputs:
        if isinstance(item, str):
            name = item.strip()
            if name:
                specs.append({"name": name})
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("key") or item.get("path") or "").strip()
            if name:
                spec = dict(item)
                spec["name"] = name
                specs.append(spec)
        elif isinstance(item, tuple) and len(item) == 2:
            name = str(item[0]).strip()
            if name:
                specs.append({"name": name, "type": item[1]})
    return specs


_MISSING = object()


def _generic_output_alias_value(expected_key: str, payload: dict[str, Any]) -> Any:
    aliases = [_pluralize_key(expected_key), f"{expected_key}_list"]
    if expected_key.endswith("_list"):
        aliases.append(expected_key[: -len("_list")])
    singular = _singularize_key(expected_key)
    if singular != expected_key:
        aliases.append(singular)
    for alias in dict.fromkeys(alias for alias in aliases if alias):
        if alias not in payload:
            continue
        value = payload[alias]
        if alias == singular and expected_key != singular and not isinstance(value, list):
            return [value]
        return value
    return _MISSING


def _singularize_key(key: str) -> str:
    if key.endswith("ies") and len(key) > 3:
        return key[:-3] + "y"
    if key.endswith("s") and not key.endswith("ss") and len(key) > 1:
        return key[:-1]
    return key


def _stdout_value_matches_type(value: Any, expected_type: str) -> bool:
    if expected_type in {"list", "array"}:
        return isinstance(value, list)
    if expected_type in {"dict", "object", "json"}:
        return isinstance(value, dict)
    if expected_type in {"str", "string", "text"}:
        return isinstance(value, str)
    if expected_type in {"int", "integer"}:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type in {"float", "number"}:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type in {"bool", "boolean"}:
        return isinstance(value, bool)
    return True


def _clean_key(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_./-]", "", key or "")


def _value_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set)):
        return bool(value)
    if isinstance(value, dict):
        return bool(value)
    return True


def _iter_context_lists(context: dict[str, Any]):
    for key, value in context.items():
        if isinstance(value, list) and value:
            yield str(key), value


def _pluralize_key(key: str) -> str:
    if key.endswith("s"):
        return key
    if key.endswith("y") and len(key) > 1 and key[-2].lower() not in "aeiou":
        return key[:-1] + "ies"
    return key + "s"


# ---- Model-planned workflow dataflow -------------------------------------

def entry_default_context(entries: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Return Action schema defaults without reading business-specific names."""
    context: dict[str, Any] = {}
    for entry in entries:
        defaults = entry.get("default_values") or {}
        if isinstance(defaults, dict):
            context.update(defaults)
    return context


def deterministic_dataflow_plan_from_schema(
    entries: Iterable[dict[str, Any]],
    *,
    user_text: str = "",
    user_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a conservative schema-derived plan used for validation fallback/tests.

    The runtime planner should normally be model-produced.  This helper is still
    generic and field-name agnostic: it only copies defaults, explicit user
    key/value data, command placeholders, and schema order.
    """
    entry_list = [entry for entry in entries if isinstance(entry, dict)]
    initial_context = initial_context_from_entries(entry_list, user_text=user_text, user_context=user_context or {})
    planned_steps: list[dict[str, Any]] = []
    known_outputs: set[str] = set(initial_context.keys())
    for entry in entry_list:
        placeholders = list(entry.get("placeholder_keys") or [])
        missing_now = [key for key in placeholders if key not in known_outputs]
        loop = None
        # If a placeholder is not known before the step, allow runtime loop item
        # expansion to satisfy it from a previous list value.  The validator will
        # still reject it when no such list exists at execution time.
        if missing_now:
            loop = {"collection": "auto", "item_name": "loop_item"}
        planned_steps.append({
            "script_path": entry.get("script_path"),
            "command": entry.get("command"),
            "input_mapping": {key: f"{{{{{key}}}}}" for key in placeholders},
            "loop": loop,
            "outputs": list(entry.get("outputs") or []),
            "output_policy": "merge_stdout",
        })
        for key in entry.get("outputs") or []:
            if str(key).strip():
                known_outputs.add(str(key).strip())
                known_outputs.add(_pluralize_key(str(key).strip()))
                known_outputs.add(f"{str(key).strip()}_list")
    return {"initial_context": initial_context, "steps": planned_steps, "collections": [], "missing": [], "errors": []}


def validate_workflow_dataflow_plan(plan: dict[str, Any], entries: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Validate and normalize a model-produced workflow dataflow plan.

    Validation is intentionally structural and schema based.  It never trusts the
    model to execute anything, and it rejects unknown scripts, missing steps, and
    input mappings that do not cover command placeholders.
    """
    if not isinstance(plan, dict):
        raise DataflowError("workflow dataflow plan 必须是 JSON object")
    missing = plan.get("missing") or []
    errors = plan.get("errors") or []
    if missing or errors:
        raise MissingVariablesError([str(item) for item in missing] or [str(item) for item in errors], message="workflow dataflow planner 报告缺失或错误: " + json.dumps({"missing": missing, "errors": errors}, ensure_ascii=False))

    entry_list = [entry for entry in entries if isinstance(entry, dict)]
    expected_scripts = [_normalize_script_path(str(entry.get("script_path") or "")) for entry in entry_list]
    steps = plan.get("steps")
    if not isinstance(steps, list):
        raise DataflowError("workflow dataflow plan 缺少 steps 数组")
    if len(steps) != len(expected_scripts):
        raise DataflowError(f"workflow dataflow plan steps 数量不匹配: expected={len(expected_scripts)} actual={len(steps)}")

    normalized_steps: list[dict[str, Any]] = []
    for index, (step, entry, expected_script) in enumerate(zip(steps, entry_list, expected_scripts, strict=False)):
        if not isinstance(step, dict):
            raise DataflowError(f"workflow dataflow plan step[{index}] 必须是 object")
        script_path = _normalize_script_path(str(step.get("script_path") or ""))
        if script_path != expected_script:
            raise DataflowError(f"workflow dataflow plan step[{index}] script_path 不匹配: expected={expected_script} actual={script_path}")
        mapping = step.get("input_mapping") or {}
        if not isinstance(mapping, dict):
            raise DataflowError(f"workflow dataflow plan step[{index}] input_mapping 必须是 object")
        placeholders = set(entry.get("placeholder_keys") or [])
        missing_mapping = sorted(key for key in placeholders if key not in mapping)
        if missing_mapping:
            raise MissingVariablesError(missing_mapping, message=f"workflow dataflow plan step[{index}] 缺少 input_mapping: {', '.join(missing_mapping)}")
        loop = step.get("loop")
        if loop is not None and not isinstance(loop, dict):
            raise DataflowError(f"workflow dataflow plan step[{index}] loop 必须是 object 或 null")
        normalized = dict(step)
        normalized["script_path"] = script_path
        normalized["input_mapping"] = dict(mapping)
        normalized["outputs"] = step.get("outputs") if isinstance(step.get("outputs"), (list, dict)) else list(entry.get("outputs") or [])
        normalized["output_policy"] = str(step.get("output_policy") or "merge_stdout")
        normalized_steps.append(normalized)

    initial_context = plan.get("initial_context") or {}
    if not isinstance(initial_context, dict):
        raise DataflowError("workflow dataflow plan initial_context 必须是 object")
    return {
        "initial_context": initial_context,
        "steps": normalized_steps,
        "collections": plan.get("collections") if isinstance(plan.get("collections"), list) else [],
        "missing": [],
        "errors": [],
    }


def context_from_dataflow_plan(
    plan: dict[str, Any],
    entries: Iterable[dict[str, Any]],
    *,
    user_text: str = "",
    user_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge defaults, user-provided context, and planner initial_context."""
    base = initial_context_from_entries(entries, user_text=user_text, user_context=user_context or {})
    planner_context = plan.get("initial_context") if isinstance(plan, dict) else {}
    return merge_context(base, planner_context if isinstance(planner_context, dict) else {})

def _preview_value_for_error(value: Any, *, max_len: int = 300) -> str:
    try:
        if isinstance(value, list):
            first = value[0] if value else None
            return (
                f"type=list len={len(value)} "
                f"first_type={type(first).__name__ if value else None} "
                f"first_preview={str(first)[:max_len] if first is not None else ''}"
            )
        if isinstance(value, dict):
            return (
                f"type=dict keys={sorted(str(k) for k in value.keys())[:20]} "
                f"preview={str(value)[:max_len]}"
            )
        if isinstance(value, str):
            return f"type=str len={len(value)} preview={value[:max_len]}"
        return f"type={type(value).__name__} preview={str(value)[:max_len]}"
    except Exception as exc:
        return f"type={type(value).__name__} preview_error={exc}"

def materialize_step_contexts_from_plan(
    step_plan: dict[str, Any],
    entry: dict[str, Any],
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    """Resolve a step's input_mapping into one or more executable contexts with diagnostics."""
    loop = step_plan.get("loop")
    collection_path = workflow_loop_collection_path(loop)

    if isinstance(loop, dict) and loop:
        if collection_path == "auto":
            mapping = step_plan.get("input_mapping") or {}
            needed = {_source_path_for_error(source) or str(key) for key, source in mapping.items()}
            collection = None
            collection_name = "auto"
            for key, value in context.items():
                if not isinstance(value, list) or not value:
                    continue
                if all(isinstance(item, dict) and needed.intersection(item.keys()) for item in value):
                    collection = value
                    collection_name = str(key)
                    break
            if collection is None:
                raise LoopExpansionError(sorted(needed), message="自动循环集合不存在或无法满足输入映射")
        elif collection_path:
            try:
                collection = resolve_context_value(context, collection_path)
                collection_name = collection_path
            except KeyError as exc:
                raise LoopExpansionError([collection_path], message=f"循环集合不存在: {collection_path}") from exc
        else:
            collection = None
            collection_name = collection_path

        if collection is not None:
            if not isinstance(collection, list) or not collection:
                raise LoopExpansionError(
                    [collection_name],
                    message=f"循环集合不是非空列表: {collection_name}; {_preview_value_for_error(collection)}",
                )

            item_name = str(loop.get("item_name") or "loop_item")
            contexts: list[dict] = []

            for index, item in enumerate(collection):
                child = dict(context)
                child[item_name] = item
                child["loop_item"] = item
                child["loop_index"] = index
                if isinstance(item, dict):
                    child.update(item)
                child.update(resolve_input_mapping(step_plan.get("input_mapping") or {}, child))
                contexts.append(child)

            return contexts

    # 非循环步骤
    mapped = resolve_input_mapping(step_plan.get("input_mapping") or {}, context)
    return [merge_context(context, mapped)]

def resolve_input_mapping(mapping: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Resolve model input_mapping values against the current context."""
    resolved: dict[str, Any] = {}
    missing: list[str] = []
    for key, source in mapping.items():
        try:
            resolved[str(key)] = resolve_dataflow_source(source, context)
        except KeyError:
            missing.append(_source_path_for_error(source) or str(key))
        except MissingVariablesError as exc:
            missing.extend(exc.missing)
    if missing:
        raise MissingVariablesError(missing)
    return resolved


def resolve_dataflow_source(source: Any, context: dict[str, Any]) -> Any:
    """Resolve one planner source value. Supports placeholders and source objects."""
    if isinstance(source, str):
        stripped = source.strip()
        placeholder = _PLACEHOLDER_RE.fullmatch(stripped)
        if placeholder:
            return resolve_context_value(context, placeholder.group(1))
        if re.fullmatch(r"[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*|\.[0-9]+)*", stripped):
            return resolve_context_value(context, stripped)
        return replace_placeholders_in_value(source, context)
    if isinstance(source, dict):
        if "value" in source:
            return source["value"]
        path = source.get("path") or source.get("key") or source.get("name") or source.get("from") or source.get("source")
        if path:
            return resolve_context_value(context, _strip_braces(str(path)))
        return replace_placeholders_in_value(source, context)
    return replace_placeholders_in_value(source, context)


def apply_dataflow_collections(
    collections: list[Any],
    context: dict[str, Any],
    step_payloads: list[dict[str, Any]],
    *,
    script_path: str = "",
    step_index: int | None = None,
) -> dict[str, Any]:
    """Apply optional model-declared loop aggregations to context.

    Collection specs are generic: ``target`` names the context key to write and
    ``source`` may be a stdout key or dotted stdout path.  Specs can optionally
    be scoped with ``script_path``/``step_index``/``after_step``.
    """
    if not isinstance(collections, list):
        return {}
    updates: dict[str, Any] = {}
    last_payloads = [payload for payload in step_payloads if isinstance(payload, dict)]
    for collection in collections:
        if not isinstance(collection, dict) or not _collection_applies_to_step(collection, script_path=script_path, step_index=step_index):
            continue
        target = str(collection.get("target") or collection.get("name") or "").strip()
        source = str(collection.get("source") or collection.get("field") or "").strip()
        if not target or not source:
            continue
        values: list[Any] = []
        for payload in last_payloads:
            try:
                value = resolve_context_value(payload, _strip_braces(source))
            except KeyError:
                continue
            if isinstance(value, list):
                values.extend(value)
            else:
                values.append(value)
        if values:
            updates[target] = values
    context.update(updates)
    return updates


def workflow_loop_collection_path(loop: Any) -> str:
    """Return a normalized loop collection path for diagnostics/execution."""
    if not isinstance(loop, dict) or not loop:
        return ""
    raw = loop.get("collection") or loop.get("source") or loop.get("list") or ""
    if isinstance(raw, dict):
        return _source_path_for_error(raw)
    return _strip_braces(str(raw))


def _collection_applies_to_step(collection: dict[str, Any], *, script_path: str, step_index: int | None) -> bool:
    expected_script = str(collection.get("script_path") or collection.get("step_script") or "").strip()
    if expected_script and _normalize_script_path(expected_script) != _normalize_script_path(script_path):
        return False
    raw_index = collection.get("step_index", collection.get("after_step"))
    if raw_index is None or raw_index == "":
        return True
    try:
        return step_index == int(raw_index)
    except (TypeError, ValueError):
        return True


def _normalize_script_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _strip_braces(path: str) -> str:
    match = _PLACEHOLDER_RE.fullmatch((path or "").strip())
    return match.group(1) if match else (path or "").strip()


def _source_path_for_error(source: Any) -> str:
    if isinstance(source, str):
        return _strip_braces(source)
    if isinstance(source, dict):
        for key in ("path", "key", "name", "from", "source"):
            if source.get(key):
                return _strip_braces(str(source[key]))
    return ""
