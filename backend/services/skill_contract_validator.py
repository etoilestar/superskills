"""Validation helpers for WorkflowContract and script stdout."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .skill_contract import ContractIssue, WorkflowContract, StepContract, OutputSpec


HIGH_IMPACT_ROLE_CAPABILITY = {
    "image_generator": {"image_generation"},
    "pdf_builder": {"pdf_generation"},
    "docx_builder": {"docx_generation"},
    "pptx_builder": {"pptx_generation"},
    "html_asset_builder": {"html_generation", "html_asset_generation"},
    "composite_generator": set(),
    "text_generator": set(),
    "generic_script": set(),
}


def validate_workflow_contract(contract: WorkflowContract) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    seen_ids: set[str] = set()
    seen_scripts: set[str] = set()
    produced: dict[str, tuple[str, OutputSpec]] = {}
    step_index: dict[str, int] = {}

    for idx, step in enumerate(contract.steps):
        step_index[step.id] = idx
        if not step.id:
            issues.append(ContractIssue("missing_step_id", "step.id 不能为空", script_path=step.script_path))
        if step.id in seen_ids:
            issues.append(ContractIssue("duplicate_step_id", f"重复 step.id: {step.id}", step_id=step.id, script_path=step.script_path))
        seen_ids.add(step.id)

        if not step.script_path or not step.script_path.startswith("scripts/"):
            issues.append(ContractIssue("invalid_script_path", "script_path 必须是 scripts/... 相对路径", step_id=step.id, script_path=step.script_path))
        if step.script_path in seen_scripts:
            issues.append(ContractIssue("duplicate_script_path", f"重复 script_path: {step.script_path}", step_id=step.id, script_path=step.script_path, severity="warning"))
        seen_scripts.add(step.script_path)

        issues.extend(_validate_role_capabilities(step))
        issues.extend(_validate_command_keys(step))

        for output_name, spec in step.outputs.items():
            produced[f"{step.id}.{output_name}"] = (step.id, spec)
            produced[output_name] = (step.id, spec)

    # First-round contract validation intentionally stops at static contract
    # shape and command JSON parseability. Cross-step input/source/foreach/
    # collect alignment is validated by the second-round E2E dry-run with real
    # stdout payloads and placeholder rendering.

    return issues


def validate_stdout_against_output_schema(
    stdout_payload: dict[str, Any],
    step: StepContract,
    *,
    execution_root: Path | None = None,
    downstream_requirements: dict[str, Any] | None = None,
) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    downstream_requirements = downstream_requirements or {}

    for name, spec in step.outputs.items():
        if spec.required and name not in stdout_payload:
            issues.append(ContractIssue(
                "stdout_required_output_missing",
                f"stdout 缺少 required output: {name}",
                step_id=step.id,
                script_path=step.script_path,
                field=name,
            ))
            continue
        if name not in stdout_payload:
            continue

        value = stdout_payload.get(name)
        issues.extend(_validate_value(name, value, spec, step, execution_root=execution_root))

    for name, req in downstream_requirements.items():
        if name not in stdout_payload:
            continue
        value = stdout_payload[name]
        if req.get("used_as_foreach") and (not isinstance(value, list) or not value):
            issues.append(ContractIssue(
                "stdout_downstream_foreach_invalid",
                f"输出 {name} 被下游 foreach 使用，必须是非空 array",
                step_id=step.id,
                script_path=step.script_path,
                field=name,
                details={"actual_type": type(value).__name__, "len": len(value) if isinstance(value, list) else None},
            ))
        item_required = req.get("item_required") or []
        if item_required and isinstance(value, list):
            for idx, item in enumerate(value):
                if not isinstance(item, dict):
                    issues.append(ContractIssue(
                        "stdout_downstream_item_not_object",
                        f"输出 {name}[{idx}] 被下游当作 object 使用，但实际不是 object",
                        step_id=step.id,
                        script_path=step.script_path,
                        field=name,
                    ))
                    continue
                missing = [field for field in item_required if field not in item]
                if missing:
                    issues.append(ContractIssue(
                        "stdout_downstream_item_missing_fields",
                        f"输出 {name}[{idx}] 缺少下游需要字段: {missing}",
                        step_id=step.id,
                        script_path=step.script_path,
                        field=name,
                        details={"missing": missing, "index": idx},
                    ))

    return issues


def parse_stdout_json_object(stdout: str) -> tuple[dict[str, Any] | None, ContractIssue | None]:
    text = (stdout or "").strip()
    if not text:
        return None, ContractIssue("stdout_empty", "stdout 为空")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, ContractIssue("stdout_json_invalid", f"stdout 不是有效 JSON: {exc}")
    if not isinstance(data, dict):
        return None, ContractIssue("stdout_not_object", "stdout JSON 必须是 object")
    return data, None


def build_downstream_requirements(contract: WorkflowContract) -> dict[str, dict[str, Any]]:
    """Return requirements for each step output based on downstream usage.

    Result shape:
    {
      "step_id": {
        "output_name": {"used_as_foreach": bool, "item_required": [...]}
      }
    }
    """
    result: dict[str, dict[str, Any]] = {step.id: {} for step in contract.steps}

    for step in contract.steps:
        if step.foreach:
            resolved = _split_source(step.foreach.collection)
            if resolved:
                producer, output = resolved
                req = result.setdefault(producer, {}).setdefault(output, {})
                req["used_as_foreach"] = True

                item_required = set(req.get("item_required") or [])
                item_name = step.foreach.item_name
                prefix = item_name + "."
                for spec in step.inputs.values():
                    source = spec.source or ""
                    if source.startswith(prefix):
                        item_required.add(source[len(prefix):].split(".", 1)[0])
                req["item_required"] = sorted(item_required)

    return result


def _validate_role_capabilities(step: StepContract) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    expected = HIGH_IMPACT_ROLE_CAPABILITY.get(step.role, set())
    caps = set(step.required_capabilities)

    if step.role == "generic_script" and caps.intersection({"image_generation", "pdf_generation", "docx_generation", "pptx_generation", "html_generation", "html_asset_generation"}):
        issues.append(ContractIssue(
            "generic_script_high_impact_capability",
            "generic_script 不允许声明高风险生成能力，请使用明确 role",
            step_id=step.id,
            script_path=step.script_path,
            details={"required_capabilities": sorted(caps)},
        ))

    missing = expected - caps
    if missing:
        issues.append(ContractIssue(
            "role_missing_required_capability",
            f"role {step.role} 缺少 required_capabilities: {sorted(missing)}",
            step_id=step.id,
            script_path=step.script_path,
            severity="warning",
        ))
    return issues


def _validate_command_keys(step: StepContract) -> list[ContractIssue]:
    import shlex
    issues: list[ContractIssue] = []
    if not step.command_template:
        return issues
    try:
        parts = shlex.split(step.command_template)
    except ValueError as exc:
        return [ContractIssue("command_parse_failed", f"command_template 解析失败: {exc}", step_id=step.id, script_path=step.script_path)]

    script_idx = None
    for idx, part in enumerate(parts):
        norm = part.replace("\\", "/").lstrip("./")
        if norm == step.script_path or norm.endswith("/" + step.script_path):
            script_idx = idx
            break
    if script_idx is None:
        return [ContractIssue("command_missing_script_path", "command_template 没有调用对应 script_path", step_id=step.id, script_path=step.script_path)]

    if script_idx + 1 >= len(parts):
        command_keys = set()
    else:
        try:
            payload = json.loads(parts[script_idx + 1])
        except json.JSONDecodeError:
            return [ContractIssue("command_json_invalid", "script 后第一个 argv 必须是 JSON object", step_id=step.id, script_path=step.script_path)]
        if not isinstance(payload, dict):
            return [ContractIssue("command_json_not_object", "script argv JSON 必须是 object", step_id=step.id, script_path=step.script_path)]
        command_keys = {str(k) for k in payload.keys()}

    # First round only proves that the fenced command calls the declared script
    # and that the script argv is a JSON object. The exact key set and
    # placeholder/dataflow alignment are E2E responsibilities.
    return issues


def _validate_value(name: str, value: Any, spec: OutputSpec, step: StepContract, *, execution_root: Path | None) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    typ = spec.type

    if typ == "string" and not isinstance(value, str):
        issues.append(_type_issue(name, "string", value, step))
    elif typ == "integer" and not isinstance(value, int):
        issues.append(_type_issue(name, "integer", value, step))
    elif typ == "number" and not isinstance(value, (int, float)):
        issues.append(_type_issue(name, "number", value, step))
    elif typ == "boolean" and not isinstance(value, bool):
        issues.append(_type_issue(name, "boolean", value, step))
    elif typ == "object" and not isinstance(value, dict):
        issues.append(_type_issue(name, "object", value, step))
    elif typ in {"array", "file_paths"} and not isinstance(value, list):
        issues.append(_type_issue(name, "array", value, step))
    elif typ == "file_path" and not isinstance(value, str):
        issues.append(_type_issue(name, "file_path", value, step))

    if isinstance(value, str) and spec.min_length is not None and len(value.strip()) < spec.min_length:
        issues.append(ContractIssue("stdout_string_too_short", f"{name} 字符串长度不足", step_id=step.id, script_path=step.script_path, field=name))

    if isinstance(value, list):
        if spec.min_items is not None and len(value) < spec.min_items:
            issues.append(ContractIssue("stdout_array_too_short", f"{name} 数组元素不足", step_id=step.id, script_path=step.script_path, field=name, details={"len": len(value), "min_items": spec.min_items}))
        if spec.item_required:
            for idx, item in enumerate(value):
                if not isinstance(item, dict):
                    issues.append(ContractIssue("stdout_array_item_not_object", f"{name}[{idx}] 不是 object", step_id=step.id, script_path=step.script_path, field=name))
                    continue
                missing = [k for k in spec.item_required if k not in item]
                if missing:
                    issues.append(ContractIssue("stdout_array_item_missing_fields", f"{name}[{idx}] 缺少字段: {missing}", step_id=step.id, script_path=step.script_path, field=name, details={"index": idx, "missing": missing}))

    if spec.path_must_exist:
        paths: list[str] = []
        if typ == "file_path" and isinstance(value, str):
            paths = [value]
        elif typ in {"file_paths", "array"} and isinstance(value, list):
            paths = [str(x) for x in value if isinstance(x, str)]
        for raw in paths:
            if not _path_exists(raw, execution_root):
                issues.append(ContractIssue("stdout_file_missing", f"文件输出不存在: {raw}", step_id=step.id, script_path=step.script_path, field=name, details={"path": raw}))

    return issues


def _type_issue(name: str, expected: str, value: Any, step: StepContract) -> ContractIssue:
    return ContractIssue(
        "stdout_type_mismatch",
        f"{name} 类型不匹配，期望 {expected}，实际 {type(value).__name__}",
        step_id=step.id,
        script_path=step.script_path,
        field=name,
        details={"expected": expected, "actual": type(value).__name__},
    )


def _path_exists(raw: str, execution_root: Path | None) -> bool:
    p = Path(raw)
    candidates = [p]
    if execution_root and not p.is_absolute():
        candidates.append(execution_root / p)
    return any(c.is_file() for c in candidates)


def _source_available(source: str, produced: dict[str, tuple[str, OutputSpec]], step_index: dict[str, int], current_idx: int, step: StepContract) -> bool:
    if source.startswith("context.") or source.startswith("user."):
        return True
    if step.foreach and (source.startswith(step.foreach.item_name + ".") or source.startswith("loop_item.")):
        return True
    return _resolve_output_spec(source, produced) is not None


def _resolve_output_spec(source: str, produced: dict[str, tuple[str, OutputSpec]]) -> OutputSpec | None:
    source = source.strip()
    if source in produced:
        return produced[source][1]
    parts = source.split(".")
    if len(parts) >= 2:
        key = ".".join(parts[:2])
        if key in produced:
            return produced[key][1]
        if parts[-1] in produced:
            return produced[parts[-1]][1]
    return None


def _split_source(source: str) -> tuple[str, str] | None:
    parts = source.split(".")
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None
