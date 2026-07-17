"""Deterministic SKILL.md runtime command format normalization.

This module is intentionally narrow: it only parses and normalizes Markdown
bash/sh command blocks that invoke scripts/*. It never infers business
semantics, argument names, or dataflow bindings from skill/script names.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import shlex
from pathlib import Path
from typing import Any
from ..skill_dataflow import (
    normalize_bare_json_placeholders,
    placeholder_pattern,
)
_PLATFORM_PLACEHOLDER_RE = placeholder_pattern()

@dataclass
class SkillMdCommandBlock:
    start: int
    end: int
    lang: str
    content: str
    script_path: str | None = None
    body_start: int = 0
    body_end: int = 0
    full_block_text: str = ""
    command_body_text: str = ""

    @property
    def block_start(self) -> int:
        return self.start

    @property
    def block_end(self) -> int:
        return self.end


@dataclass
class CommandFormatIssue:
    code: str
    message: str
    script_path: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class CommandNormalizationResult:
    changed: bool
    content: str
    issues: list[CommandFormatIssue] = field(default_factory=list)
    blocked: bool = False


_SHELL_LANGS = {"bash", "sh", "shell"}
_ANY_PLACEHOLDER_RE = re.compile(r"\{\{\s*(.*?)\s*\}\}", re.S)


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _effective_command_lines(command: str) -> list[str]:
    return [line.strip() for line in str(command or "").splitlines() if line.strip() and not line.strip().startswith("#")]


def _script_path_from_command(command: str) -> str | None:
    try:
        parts = shlex.split(str(command or ""), posix=True)
    except ValueError:
        return None
    for part in parts[1:]:
        normalized = part.replace("\\", "/")
        if normalized.startswith("scripts/") and Path(normalized).suffix:
            return normalized
    return None


def parse_skill_md_bash_command_blocks(skill_md: str) -> list[SkillMdCommandBlock]:
    """Parse Markdown fenced bash/sh/shell blocks and locate scripts/* calls."""
    text = str(skill_md or "")
    lines = text.splitlines(keepends=True)
    blocks: list[SkillMdCommandBlock] = []
    offset = 0
    in_block = False
    fence_char = ""
    fence_len = 0
    lang = ""
    block_start = 0
    body_start = 0
    body_lines: list[str] = []

    open_re = re.compile(r"^\s*(`{3,}|~{3,})([^`\n]*)\s*$")
    for line in lines:
        if not in_block:
            match = open_re.match(line.rstrip("\n\r"))
            if match:
                fence = match.group(1)
                fence_char = fence[0]
                fence_len = len(fence)
                lang = (match.group(2) or "").strip().lower().split(maxsplit=1)[0]
                block_start = offset
                body_start = offset + len(line)
                body_lines = []
                in_block = True
            offset += len(line)
            continue

        close_re = re.compile(rf"^\s*{re.escape(fence_char)}{{{fence_len},}}\s*$")
        if close_re.match(line.rstrip("\n\r")):
            content = "".join(body_lines).strip()
            if lang in _SHELL_LANGS and "scripts/" in content.replace("\\", "/"):
                block_end = offset + len(line)
                raw_body = "".join(body_lines)
                blocks.append(SkillMdCommandBlock(
                    start=block_start,
                    end=block_end,
                    lang=lang,
                    content=content,
                    script_path=_script_path_from_command(_effective_command_lines(content)[0] if _effective_command_lines(content) else content),
                    body_start=body_start,
                    body_end=offset,
                    full_block_text=text[block_start:block_end],
                    command_body_text=raw_body,
                ))
            in_block = False
            offset += len(line)
            continue

        body_lines.append(line)
        offset += len(line)
    return blocks



def _sanitize_template_value(value: str) -> tuple[str, dict[str, Any] | None, bool]:
    text = str(value)
    match = _ANY_PLACEHOLDER_RE.fullmatch(text.strip())
    if not match:
        return value, None, False
    expr = match.group(1).strip()
    input_match = re.fullmatch(r"(?:input_files|uploaded_files)\[(\d+)\]", expr)
    if input_match:
        index = int(input_match.group(1))
        placeholder = "__RUNTIME_INPUT_FILE__" if index == 0 else f"__RUNTIME_INPUT_FILE_{index}__"
        return placeholder, {"source": "runtime_input_file", "index": index, "placeholder": placeholder}, True
    path_match = re.fullmatch(r"(references|assets)/([A-Za-z0-9._/-]+)", expr)
    if path_match and ".." not in path_match.group(2).split("/"):
        path = f"{path_match.group(1)}/{path_match.group(2)}"
        return path, {"source": "reference_file" if path.startswith("references/") else "asset_file", "path": path}, True
    if expr in {"TEXT_MODEL", "model"}:
        return "TEXT_MODEL", {"source": "runtime_model", "placeholder": "TEXT_MODEL"}, True
    return value, None, False


def _sanitize_json_obj_templates(obj: Any, path: str = "") -> tuple[Any, dict[str, Any], bool]:
    bindings: dict[str, Any] = {}
    changed = False
    if isinstance(obj, dict):
        out = {}
        for key, item in obj.items():
            child_path = f"{path}.{key}" if path else str(key)
            new_item, child_bindings, child_changed = _sanitize_json_obj_templates(item, child_path)
            out[key] = new_item
            bindings.update(child_bindings)
            changed = changed or child_changed
        return out, bindings, changed
    if isinstance(obj, list):
        out_list = []
        for index, item in enumerate(obj):
            child_path = f"{path}[{index}]"
            new_item, child_bindings, child_changed = _sanitize_json_obj_templates(item, child_path)
            out_list.append(new_item)
            bindings.update(child_bindings)
            changed = changed or child_changed
        return out_list, bindings, changed
    if isinstance(obj, str):
        new_value, binding, value_changed = _sanitize_template_value(obj)
        if binding:
            bindings[path or "$"] = binding
        return new_value, bindings, value_changed
    return obj, bindings, False


def _sanitize_json_argv_template_values(
    raw_json_arg: str,
) -> tuple[str, dict[str, Any]]:
    """Normalize platform placeholders and static argv template values.

    ``{{path}}`` is a platform dataflow token.  When a whole placeholder is
    written as a bare JSON value, normalize it to a JSON string template before
    parsing.  Runtime placeholder resolution later restores the original JSON
    value and type.

    This function only normalizes platform template syntax and known static
    runtime bindings.  It does not infer business fields or dataflow semantics.
    """
    raw = str(raw_json_arg or "")

    placeholder_normalized = (
        normalize_bare_json_placeholders(raw)
    )

    parsed = json.loads(placeholder_normalized)

    sanitized, bindings, binding_changed = (
        _sanitize_json_obj_templates(parsed)
    )

    placeholder_changed = (
        placeholder_normalized != raw
    )

    if not placeholder_changed and not binding_changed:
        return raw, bindings

    return (
        json.dumps(
            sanitized,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        bindings,
    )

def _complex_template_paths(
    value: Any,
    path: str = "",
) -> list[str]:
    """Return JSON paths containing non-platform placeholder expressions."""
    found: list[str] = []

    if isinstance(value, dict):
        for key, item in value.items():
            child_path = (
                f"{path}.{key}"
                if path
                else str(key)
            )
            found.extend(
                _complex_template_paths(
                    item,
                    child_path,
                )
            )

    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(
                _complex_template_paths(
                    item,
                    f"{path}[{index}]",
                )
            )

    elif isinstance(value, str):
        for match in _ANY_PLACEHOLDER_RE.finditer(value):
            token = (
                "{{"
                + match.group(1).strip()
                + "}}"
            )

            if not _PLATFORM_PLACEHOLDER_RE.fullmatch(
                token
            ):
                found.append(path or "$")

    return found


def validate_runtime_command_format(command: str) -> list[CommandFormatIssue]:
    """Validate only shell/JSON argv shape; do not validate business semantics."""
    issues: list[CommandFormatIssue] = []
    lines = _effective_command_lines(command)
    script_path = _script_path_from_command(lines[0]) if len(lines) == 1 else None
    if len(lines) != 1:
        return [CommandFormatIssue("runtime_command_invalid", "command block must contain exactly one command", script_path)]

    try:
        parts = shlex.split(lines[0], posix=True)
    except ValueError as exc:
        return [CommandFormatIssue("command_parse_failed", f"shell command is not parseable: {exc}", script_path)]

    if not parts:
        return [CommandFormatIssue("runtime_command_invalid", "empty command", script_path)]
    runner = Path(parts[0]).name
    if runner not in {"python", "python3"}:
        return [CommandFormatIssue("runtime_command_invalid", "command does not use a supported script runner", script_path)]
    if len(parts) < 2 or not parts[1].replace("\\", "/").startswith("scripts/"):
        return [CommandFormatIssue("runtime_command_invalid", "command does not directly call scripts/*", script_path)]
    script_path = parts[1].replace("\\", "/")
    if len(parts) != 3:
        return [CommandFormatIssue("runtime_command_invalid", "command must pass exactly one JSON argv object", script_path, {"argc": len(parts)})]

    try:
        sanitized_arg, _bindings = _sanitize_json_argv_template_values(parts[2])
        argv = json.loads(sanitized_arg)
    except json.JSONDecodeError as exc:
        return [CommandFormatIssue("invalid_json_arg", f"JSON argv is not parseable: {exc.msg}", script_path)]
    if not isinstance(argv, dict):
        return [CommandFormatIssue("json_argv_not_object", "JSON argv must be an object", script_path)]
    complex_paths = _complex_template_paths(argv)
    if complex_paths:
        issues.append(CommandFormatIssue("placeholder_json_invalid", "complex template expressions are not allowed inside JSON argv", script_path, {"paths": complex_paths}))
    return issues


def _format_command(script_path: str, argv: dict[str, Any]) -> str:
    return f"python {script_path} {shlex.quote(json.dumps(argv, ensure_ascii=False, separators=(',', ':')))}"


def _sanitized_command_from_block(command: str) -> tuple[str | None, dict[str, Any]]:
    lines = _effective_command_lines(command)
    if len(lines) != 1:
        return None, {}
    try:
        parts = shlex.split(lines[0], posix=True)
    except ValueError:
        return None, {}
    if len(parts) != 3 or not parts[1].replace('\\', '/').startswith('scripts/'):
        return None, {}
    try:
        sanitized_arg, bindings = _sanitize_json_argv_template_values(parts[2])
        argv = json.loads(sanitized_arg)
    except json.JSONDecodeError:
        return None, {}
    if not isinstance(argv, dict):
        return None, {}
    return _format_command(parts[1].replace('\\', '/'), argv), bindings


def _argv_contract_markdown(bindings: dict[str, Any]) -> str:
    if not bindings:
        return ""
    lines = ["", "**argv JSON contract**", ""]
    for path, binding in bindings.items():
        key = str(path).split('.')[-1]
        if '[' in key:
            key = key.split('[')[0]
        if binding.get('source') == 'runtime_input_file':
            lines.append(f"* `{key}`: 运行时用户上传文件路径，由宿主将第 {int(binding.get('index', 0)) + 1} 个上传文件绑定到 `{binding.get('placeholder')}`。")
        elif binding.get('source') in {'reference_file', 'asset_file'}:
            lines.append(f"* `{key}`: 固定资源文件路径 `{binding.get('path')}`。")
        elif binding.get('source') == 'runtime_model':
            lines.append(f"* `{key}`: 宿主注入的文本模型名称，占位值为 `{binding.get('placeholder')}`。")
    return "\n".join(lines) + "\n"

def _explicit_bindings_from_requirement_graph(requirement_graph: Any) -> dict[str, str]:
    bindings: dict[str, str] = {}
    edges = _get(requirement_graph, "dataflow_edges", None) or _get(requirement_graph, "edges", None) or []
    for edge in edges if isinstance(edges, list) else []:
        to_field = _get(edge, "to_field")
        from_field = _get(edge, "from_field")
        from_node = _get(edge, "from_node")
        if isinstance(to_field, str) and to_field.strip() and isinstance(from_field, str) and from_field.strip():
            source = from_field.strip()
            if isinstance(from_node, str) and from_node.strip():
                source = f"{from_node.strip()}.{source}"
            bindings[to_field.strip()] = source
            continue

        target = _get(edge, "target") or _get(edge, "to") or _get(edge, "target_key")
        source = _get(edge, "source") or _get(edge, "from") or _get(edge, "source_key")
        if isinstance(target, str) and isinstance(source, str) and target:
            bindings[target] = source
    return bindings


def render_canonical_command_from_verified_contract(
    *,
    script_path: str,
    skill_plan_entry: Any | None = None,
    runtime_spec: Any | None = None,
    requirement_graph: Any | None = None,
) -> tuple[str | None, list[CommandFormatIssue]]:
    """Render a canonical command only from explicit verified sources."""
    sources: list[str] = []
    for source_name, source in (("runtime_spec.command_template", runtime_spec), ("SkillPlanEntry.command_template", skill_plan_entry)):
        template = _get(source, "command_template", "") if source is not None else ""
        if isinstance(template, str) and template.strip():
            issues = validate_runtime_command_format(template)
            matching = _script_path_from_command(template) == script_path
            if not issues and matching:
                return template.strip(), []
            sources.append(source_name)

    command_args = _get(_get(skill_plan_entry, "runtime_contract", {}) or {}, "command_args", None)
    if command_args is not None:
        if isinstance(command_args, dict):
            return _format_command(script_path, command_args), []
        sources.append("SkillPlanEntry.runtime_contract.command_args")

    schema = _get(runtime_spec, "script_argv_schema", None)
    if isinstance(schema, dict):
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        raw_required = schema.get("required", schema.get("required_keys", []))
        required = [str(key) for key in raw_required if isinstance(key, str)]
        bindings = _explicit_bindings_from_requirement_graph(requirement_graph)
        missing = [key for key in required if key not in bindings]
        if missing:
            return None, [CommandFormatIssue("missing_command_arg_binding", "required argv keys have no explicit binding", script_path, {"missing_keys": missing, "available_contract_sources": sources + ["runtime_spec.script_argv_schema", "requirement_graph"]})]
        argv = {key: f"{{{{{bindings[key]}}}}}" for key in props if key in bindings}
        return _format_command(script_path, argv), []

    return None, [CommandFormatIssue("missing_command_arg_binding", "no verified command contract can determine argv", script_path, {"available_contract_sources": sources})]


def replace_skill_md_command_block(skill_md: str, block: SkillMdCommandBlock, canonical_command: str) -> str:
    text = str(canonical_command or "").strip()
    contract = ""
    if "\n**argv JSON contract**" in text:
        text, contract = text.split("\n**argv JSON contract**", 1)
        contract = "\n**argv JSON contract**" + contract.rstrip() + "\n"
    return str(skill_md or "")[:block.start] + f"```bash\n{text.strip()}\n```\n{contract}" + str(skill_md or "")[block.end:]


def canonicalize_skill_md_runtime_commands(
    *,
    skill_name: str,
    skill_md: str,
    files: list[Any] | None = None,
    requirement_graph: Any | None = None,
    runtime_specs: dict[str, Any] | None = None,
) -> CommandNormalizationResult:
    content = str(skill_md or "")
    blocks = parse_skill_md_bash_command_blocks(content)
    issues: list[CommandFormatIssue] = []
    replacements: list[tuple[SkillMdCommandBlock, str]] = []
    entries = {str(_get(item, "path", "")): item for item in (files or []) if _get(item, "path", "")}

    for block in blocks:
        sanitized_command, sanitized_bindings = _sanitized_command_from_block(block.content)
        block_issues = validate_runtime_command_format(sanitized_command or block.content)
        if not block_issues:
            if sanitized_command and sanitized_command.strip() != block.content.strip():
                contract = _argv_contract_markdown(sanitized_bindings)
                replacements.append((block, sanitized_command + contract))
            continue
        issues.extend(block_issues)
        script_path = block.script_path or next((issue.script_path for issue in block_issues if issue.script_path), None)
        if not script_path:
            return CommandNormalizationResult(False, content, issues, True)
        command, render_issues = render_canonical_command_from_verified_contract(
            script_path=script_path,
            skill_plan_entry=entries.get(script_path),
            runtime_spec=(runtime_specs or {}).get(script_path),
            requirement_graph=requirement_graph,
        )
        if command is None:
            issues.extend(render_issues)
            return CommandNormalizationResult(False, content, issues, True)
        replacements.append((block, command))

    for block, command in sorted(replacements, key=lambda item: item[0].start, reverse=True):
        content = replace_skill_md_command_block(content, block, command)
    return CommandNormalizationResult(bool(replacements), content, issues, False)
