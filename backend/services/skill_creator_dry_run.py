"""Creator-side workflow dry-run.

Run the generated scripts according to WorkflowContract before publishing a Skill.
This catches cross-step mismatch that single-file trial runs cannot catch.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .skill_contract import WorkflowContract, StepContract
from .artifact_validator import FileOutputValidationError, validate_stdout_file_outputs
from .skill_dataflow import extract_inline_context_values, replace_placeholders_in_value
from .skill_contract_validator import (
    ContractIssue,
    build_downstream_requirements,
    parse_stdout_json_object,
    validate_stdout_against_output_schema,
)


@dataclass
class DryRunStepTrace:
    step_id: str
    script_path: str
    command: str
    stdout: str
    stderr: str
    returncode: int
    payload: dict[str, Any] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DryRunResult:
    ok: bool
    context: dict[str, Any]
    traces: list[DryRunStepTrace]
    issues: list[dict[str, Any]]
    output_files: list[dict[str, str]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "context": self.context,
            "traces": [
                {
                    "step_id": t.step_id,
                    "script_path": t.script_path,
                    "command": t.command,
                    "stdout": t.stdout,
                    "stderr": t.stderr,
                    "returncode": t.returncode,
                    "payload": t.payload,
                    "issues": t.issues,
                }
                for t in self.traces
            ],
            "issues": self.issues,
            "output_files": self.output_files,
        }


def build_creator_external_input_context(
    *,
    messages: list[Any] | None = None,
    input_files: list[dict[str, Any]] | None = None,
    options: dict[str, Any] | None = None,
    fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the deterministic platform-to-Creator Skill input envelope.

    This mirrors the sandbox field protocol: free-form user text is only
    exposed as ``user_request``/``input``/``text``; business fields are copied
    only from JSON or explicit key/value text (plus caller-provided fields).
    """
    user_text = _last_user_message_text(messages or [])
    input_files = list(input_files or [])
    inline_fields = extract_inline_context_values(user_text) if user_text else {}
    merged_fields = _non_secret_mapping({**inline_fields, **(fields or {})})
    safe_options = _non_secret_mapping(options or {})
    files = _simplify_input_files(input_files)
    context: dict[str, Any] = {
        "user_request": user_text,
        "input": user_text,
        "text": user_text,
        "input_files": input_files,
        "files": files,
        "fields": merged_fields,
        "options": safe_options,
    }
    context.update(merged_fields)
    return context


def run_creator_workflow_dry_run(
    *,
    skill_dir: Path,
    contract: WorkflowContract,
    sample_input: dict[str, Any] | None = None,
    chat_request: Any | None = None,
    python_executable: str | None = None,
    timeout_seconds: int = 120,
) -> DryRunResult:
    skill_dir = skill_dir.resolve()
    python_executable = python_executable or sys.executable
    context: dict[str, Any] = {}
    if chat_request is not None:
        context.update(_context_from_chat_request(chat_request))
    context.update(sample_input or {})
    traces: list[DryRunStepTrace] = []
    all_issues: list[ContractIssue] = []
    output_files: list[dict[str, str]] = []
    downstream = build_downstream_requirements(contract)

    # Seed defaults.
    for step in contract.steps:
        for key, value in step.default_values.items():
            context.setdefault(key, value)
        for key, spec in step.inputs.items():
            if spec.default is not None:
                context.setdefault(key, spec.default)

    abort = False
    for index, step in enumerate(contract.steps):
        if abort:
            break
        try:
            step_contexts = _materialize_step_contexts(step, context)
        except Exception as exc:
            issue = _missing_input_issue(exc, step=step, first_step=index == 0)
            all_issues.append(issue)
            abort = True
            break

        for step_context in step_contexts:
            try:
                command = _render_command(step, step_context)
            except Exception as exc:
                issue = _missing_input_issue(exc, step=step, first_step=index == 0)
                all_issues.append(issue)
                abort = True
                break
            completed = subprocess.run(
                command,
                cwd=str(skill_dir),
                shell=True,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                env=_script_env(skill_dir),
            )

            trace = DryRunStepTrace(
                step_id=step.id,
                script_path=step.script_path,
                command=command,
                stdout=completed.stdout,
                stderr=completed.stderr,
                returncode=completed.returncode,
            )
            traces.append(trace)

            if completed.returncode != 0:
                issue = ContractIssue(
                    "dry_run_command_failed",
                    f"脚本返回非 0: {completed.returncode}",
                    step_id=step.id,
                    script_path=step.script_path,
                    details={"stderr": completed.stderr[-2000:]},
                )
                all_issues.append(issue)
                trace.issues.append(issue.to_dict())
                continue

            payload, parse_issue = parse_stdout_json_object(completed.stdout)
            if parse_issue:
                parse_issue.step_id = step.id
                parse_issue.script_path = step.script_path
                all_issues.append(parse_issue)
                trace.issues.append(parse_issue.to_dict())
                continue

            assert payload is not None
            trace.payload = payload

            issues = validate_stdout_against_output_schema(
                payload,
                step,
                execution_root=skill_dir,
                downstream_requirements=downstream.get(step.id) or {},
            )
            try:
                declared_outputs = validate_stdout_file_outputs(completed.stdout, skill_dir=skill_dir, cwd=skill_dir / "scripts")
            except FileOutputValidationError as exc:
                issues.append(ContractIssue(
                    exc.code or "file_output_missing",
                    str(exc),
                    step_id=step.id,
                    script_path=step.script_path,
                ))
                declared_outputs = []
            for item in declared_outputs:
                _add_output_file(output_files, item["path"], contract.skill_name or skill_dir.name)
            all_issues.extend(issues)
            trace.issues.extend([x.to_dict() for x in issues])

            _merge_payload(context, step, payload)
            _collect_outputs(context, step, payload)

            for value in payload.values():
                _collect_file_paths(value, skill_dir, output_files, contract.skill_name or skill_dir.name)

    return DryRunResult(
        ok=not any(issue.severity == "error" for issue in all_issues),
        context=context,
        traces=traces,
        issues=[x.to_dict() for x in all_issues],
        output_files=output_files,
    )


def _materialize_step_contexts(step: StepContract, context: dict[str, Any]) -> list[dict[str, Any]]:
    if not step.foreach:
        return [_resolve_inputs(step, context)]

    collection = _resolve_context_path(context, step.foreach.collection)
    if not isinstance(collection, list) or not collection:
        raise ValueError(f"foreach collection 不可展开: {step.foreach.collection}")

    out: list[dict[str, Any]] = []
    for idx, item in enumerate(collection):
        child = dict(context)
        child[step.foreach.item_name] = item
        child["loop_item"] = item
        child["loop_index"] = idx
        if isinstance(item, dict):
            child.update(item)
        out.append(_resolve_inputs(step, child))
    return out


def _resolve_inputs(step: StepContract, context: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, spec in step.inputs.items():
        if spec.source:
            payload[key] = _resolve_context_path(context, spec.source)
        elif key in context:
            payload[key] = context[key]
        elif key in step.default_values:
            payload[key] = step.default_values[key]
        elif spec.default is not None:
            payload[key] = spec.default
        elif spec.required:
            raise ValueError(f"缺少输入 {key} for {step.script_path}")
    merged = dict(context)
    merged.update(payload)
    return merged


def _render_command(step: StepContract, context: dict[str, Any]) -> str:
    if step.command_template:
        return _replace_placeholders(step.command_template, context)

    payload = {key: context.get(key) for key in step.inputs.keys()}
    return f"{shlex.quote(sys.executable)} {shlex.quote(step.script_path)} {shlex.quote(json.dumps(payload, ensure_ascii=False))}"


def _replace_placeholders(template: str, context: dict[str, Any]) -> str:
    """Render command templates with sandbox-compatible JSON argv handling."""
    import re

    try:
        parts = shlex.split(template)
    except ValueError:
        parts = []

    script_idx = None
    for idx, part in enumerate(parts):
        normalized = part.replace("\\", "/").lstrip("./")
        if normalized.startswith("scripts/") or "/scripts/" in normalized:
            script_idx = idx
            break

    if script_idx is not None and script_idx + 1 < len(parts):
        candidate = parts[script_idx + 1].strip()
        if candidate.startswith("{"):
            payload = json.loads(candidate)
            if not isinstance(payload, dict):
                raise ValueError("script argv JSON 必须是 object")
            parts[script_idx + 1] = json.dumps(
                replace_placeholders_in_value(payload, context),
                ensure_ascii=False,
            )
            return " ".join(shlex.quote(part) for part in parts)

    def repl(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        value = _resolve_context_path(context, key)
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    return re.sub(r"\{\{\s*([^}]+)\s*\}\}", repl, template)


def _resolve_context_path(context: dict[str, Any], path: str) -> Any:
    path = path.strip()
    for prefix in ("context.", "user."):
        if path.startswith(prefix):
            path = path[len(prefix):]
    parts = [p for p in path.split(".") if p]
    value: Any = context
    for part in parts:
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit():
            value = value[int(part)]
        else:
            raise KeyError(path)
    return value


def _merge_payload(context: dict[str, Any], step: StepContract, payload: dict[str, Any]) -> None:
    context.update(payload)
    context[step.id] = payload


def _collect_outputs(context: dict[str, Any], step: StepContract, payload: dict[str, Any]) -> None:
    for collect in step.collect:
        source = collect.source
        if source.startswith("each."):
            source = source[len("each."):]
        try:
            value = _resolve_context_path(payload, source)
        except Exception:
            continue

        if collect.type in {"array", "file_paths"}:
            bucket = context.setdefault(collect.target, [])
            if not isinstance(bucket, list):
                bucket = []
                context[collect.target] = bucket
            if isinstance(value, list):
                bucket.extend(value)
            else:
                bucket.append(value)
        else:
            context[collect.target] = value


def _collect_file_paths(value: Any, skill_dir: Path, output_files: list[dict[str, str]], skill_name: str) -> None:
    if isinstance(value, str):
        p = Path(value)
        candidates = [p, skill_dir / p] if not p.is_absolute() else [p]
        for c in candidates:
            if c.is_file():
                rel = c.relative_to(skill_dir).as_posix() if c.is_relative_to(skill_dir) else str(c)
                _add_output_file(output_files, rel, skill_name)
                return
    elif isinstance(value, list):
        for item in value:
            _collect_file_paths(item, skill_dir, output_files, skill_name)
    elif isinstance(value, dict):
        for item in value.values():
            _collect_file_paths(item, skill_dir, output_files, skill_name)


def _add_output_file(output_files: list[dict[str, str]], rel_path: str, skill_name: str) -> None:
    rel_path = str(rel_path).replace("\\", "/").lstrip("./")
    if not rel_path:
        return
    if any(item.get("path") == rel_path for item in output_files):
        return
    output_files.append({
        "path": rel_path,
        "url": f"/api/skills/{skill_name}/files/{rel_path}",
    })


def _script_env(skill_dir: Path) -> dict[str, str]:
    return {
        **os.environ,
        "OUTPUT_DIR": str(skill_dir / "outputs"),
        "INPUT_DIR": str(skill_dir / "inputs"),
        "PYTHONPATH": os.pathsep.join(part for part in [str(skill_dir), os.environ.get("PYTHONPATH", "")] if part),
    }


def _context_from_chat_request(chat_request: Any) -> dict[str, Any]:
    if isinstance(chat_request, dict):
        messages = chat_request.get("messages") or []
        input_files = chat_request.get("input_files") or []
        fields = chat_request.get("fields") or {}
        options = chat_request.get("options") or {}
    else:
        messages = getattr(chat_request, "messages", []) or []
        input_files = getattr(chat_request, "input_files", []) or []
        fields = getattr(chat_request, "fields", {}) or {}
        options = getattr(chat_request, "options", {}) or {}
    return build_creator_external_input_context(messages=messages, input_files=input_files, fields=fields, options=options)


def _last_user_message_text(messages: list[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, dict):
            role = message.get("role")
            content = message.get("content")
        else:
            role = getattr(message, "role", None)
            content = getattr(message, "content", None)
        if role == "user":
            return str(content or "").strip()
    return ""


def _simplify_input_files(input_files: list[dict[str, Any]]) -> dict[str, str]:
    files: dict[str, str] = {}
    for item in input_files:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        filename = str(item.get("filename") or Path(path).name or "")
        if filename and path:
            files[filename] = path
    return files


def _missing_input_issue(exc: Exception, *, step: StepContract, first_step: bool) -> ContractIssue:
    code = "external_input_missing" if first_step else "e2e_dataflow_missing"
    message = str(exc)
    if isinstance(exc, KeyError) and exc.args:
        message = f"缺少确定来源变量: {exc.args[0]}"
    elif not message:
        message = "缺少确定来源变量"
    if first_step:
        message = f"平台外部输入缺失，不能由模型猜字段：{message}"
    return ContractIssue(
        code,
        message,
        step_id=step.id,
        script_path=step.script_path,
    )


def _non_secret_mapping(values: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in dict(values or {}).items():
        lowered = str(key).lower()
        if any(token in lowered for token in ("secret", "password", "token", "api_key", "apikey")):
            continue
        safe[str(key)] = value
    return safe
