"""Prompt construction, model calls, content normalization, and file-generation helpers."""

from .common import *  # noqa: F403
from .contracts import *  # noqa: F403
from .e2e import *  # noqa: F403
from .repair import *  # noqa: F403
from ..platform_io_contract import (
    build_platform_io_contract,
    get_platform_output_sink,
    render_runtime_output_mapping,
)

_RUNTIME_BINDING_AUTHORITY_PROMPT = """## Runtime Binding Authority

Interface Plan is the single source of truth for executable interface binding.
The ResponsibilityGraph values supplied here are a deterministic materialized
projection of that plan, not a second interface authority. Natural-language
requirements, graph node names, blueprint descriptions, function descriptions,
SKILL.md, examples, and source code must not be used to infer, rename, wrap, or
redesign input/output fields.

The runtime input contract has already been resolved by the platform.

Inputs defined in SkillPlan/runtime_contract/input_binding are actual runtime ports.
They are not suggestions, descriptions, or placeholders for redesign.

The platform is responsible for:
- constructing runtime inputs
- resolving input bindings
- passing values into the generated skill

The generated script is only responsible for consuming these inputs and implementing business logic.

Do not redesign the runtime interface.

Do not:
- wrap existing inputs into another object
- create wrapper objects not declared by Interface Plan (names such as
  request/options/config/params are valid when, and only when, they are declared)
- introduce a second input schema
- rename existing runtime inputs
- add an adapter layer for invocation

The generated implementation must directly consume the declared runtime inputs according to their declared types.

The runtime contract is the source of truth.

The generated script is a component executed inside an existing runtime framework.

You are implementing the component logic, not designing the caller protocol.

Do not recreate framework-level transport, invocation, or input handling logic inside the generated skill.
"""

_SKILL_MD_RUNTIME_BINDING_PROMPT = """The execution environment already provides runtime inputs.

Interface Plan is the single source of truth for executable interface binding.
Do NOT infer bindings from graph node names, blueprint descriptions, or function
descriptions.  Use the supplied canonical target.field as the command variable
and its canonical source.field as the runtime placeholder path, verbatim.

SKILL.md should describe how the skill uses existing inputs.

It should not describe a new invocation protocol.

The command block is a runtime binding declaration.

It maps existing platform/runtime values into the script entry contract.

It must preserve the value boundary and semantic meaning defined by the upstream runtime contract.

Do not reinterpret, reconstruct, or redesign runtime inputs inside the command block.

The generated command must consume the resolved runtime interface, not create a new caller interface.

The documented execution flow must match the existing runtime contract.
"""

def _is_valid_normalized_script_source(file_path: str, content: str) -> bool:
    """Return whether content is safe to accept as the requested raw script.

    This helper is intentionally narrower than the full script validator: it only
    checks that deterministic Markdown/bundle cleanup produced one raw source
    file.  Full fake-implementation, contract, dependency and trial-run checks
    still happen in the normal validation pipeline.
    """
    stripped = content.strip()
    if not stripped or "```" in stripped or "~~~" in stripped or _MULTI_FILE_MARKER_RE.search(stripped):
        return False

    return True


def _extract_single_wrapping_fence(content: str) -> str | None:
    """Extract a code block only when it wraps the entire model response.

    Models often wrap repaired scripts in ```text fences, sometimes with CRLF
    line endings or a closing fence that is longer than the opener.  This parser
    intentionally accepts only a whole-response fence: any prose before/after the
    block, or any non-fence trailing line, returns None and lets validation reject
    the ambiguous output.
    """
    stripped = content.strip().lstrip("\ufeff")
    lines = stripped.splitlines()
    if len(lines) < 2:
        return None

    opening = lines[0].strip()
    opening_match = re.match(r"^(`{3,}|~{3,})[^`~]*$", opening)
    if not opening_match:
        return None

    fence = opening_match.group(1)
    fence_char = fence[0]
    min_fence_len = len(fence)
    closing = lines[-1].strip()
    if not re.fullmatch(rf"{re.escape(fence_char)}{{{min_fence_len},}}", closing):
        return None

    return "\n".join(lines[1:-1]).strip()


def _extract_only_fenced_block(content: str) -> str | None:
    """Extract the body only when exactly one fenced block appears in content."""
    lines = content.strip().lstrip("\ufeff").splitlines()
    blocks: list[str] = []
    idx = 0
    while idx < len(lines):
        opening = lines[idx].strip()
        opening_match = re.match(r"^(`{3,}|~{3,})[^`~]*$", opening)
        if not opening_match:
            idx += 1
            continue

        fence = opening_match.group(1)
        fence_char = fence[0]
        min_fence_len = len(fence)
        body: list[str] = []
        idx += 1
        while idx < len(lines):
            closing = lines[idx].strip()
            if re.fullmatch(rf"{re.escape(fence_char)}{{{min_fence_len},}}", closing):
                blocks.append("\n".join(body).strip())
                break
            body.append(lines[idx])
            idx += 1
        else:
            return None

        if len(blocks) > 1:
            return None
        idx += 1

    if len(blocks) != 1:
        return None
    return blocks[0]


def _extract_first_fenced_block(content: str) -> str | None:
    """Extract the first fenced block body, or None if no complete block exists."""
    lines = content.strip().lstrip("\ufeff").splitlines()
    idx = 0
    while idx < len(lines):
        opening = lines[idx].strip()
        opening_match = re.match(r"^(`{3,}|~{3,})[^`~]*$", opening)
        if not opening_match:
            idx += 1
            continue
        fence = opening_match.group(1)
        fence_char = fence[0]
        min_fence_len = len(fence)
        body: list[str] = []
        idx += 1
        while idx < len(lines):
            closing = lines[idx].strip()
            if re.fullmatch(rf"{re.escape(fence_char)}{{{min_fence_len},}}", closing):
                return "\n".join(body).strip()
            body.append(lines[idx])
            idx += 1
        return None
    return None

def _count_fenced_blocks(content: str) -> int:
    """Count complete Markdown fenced code blocks using only fence structure."""
    lines = str(content or "").strip().lstrip("\ufeff").splitlines()
    count = 0
    idx = 0
    while idx < len(lines):
        opening_match = re.match(r"^\s*(`{3,}|~{3,})[^`~]*\s*$", lines[idx])
        if not opening_match:
            idx += 1
            continue
        fence = opening_match.group(1)
        fence_char = fence[0]
        min_fence_len = len(fence)
        idx += 1
        while idx < len(lines):
            if re.fullmatch(rf"\s*{re.escape(fence_char)}{{{min_fence_len},}}\s*", lines[idx]):
                count += 1
                break
            idx += 1
        idx += 1
    return count


def script_raw_source_candidate_error_id(content: str) -> str | None:
    """Return a structural scripts/* raw-source format error id, if any.

    This is intentionally language-agnostic: it only looks at Markdown fence
    structure and multi-file bundle markers.  It never picks one candidate from
    an ambiguous model response.
    """
    stripped = str(content or "").strip().lstrip("\ufeff")
    if not stripped:
        return "script.raw_source.single_file"
    fenced_count = _count_fenced_blocks(stripped)
    if fenced_count > 1:
        return "script.raw_source.ambiguous_multi_code_blocks"
    if _MULTI_FILE_MARKER_RE.search(stripped) or re.search(r"(?im)^\s*写入文件[:：]", stripped):
        return "script.raw_source.multi_file_bundle"
    if fenced_count == 1:
        body = _extract_only_fenced_block(stripped)
        if body is None:
            return "script.raw_source.ambiguous_script_candidate"
    return None


def is_canonical_script_source_candidate(content: str) -> bool:
    """Return whether content is already a single raw script source candidate."""
    return script_raw_source_candidate_error_id(content) is None and _is_valid_normalized_script_source("", content)


def _drop_common_non_code_lines(text: str) -> str:
    """Remove common chat/file-label prose that models place around scripts."""
    drop_patterns = [
        r"^\s*下面是",
        r"^\s*以下是",
        r"^\s*(?:文件|路径)[:：]",
        r"^\s*写入文件[:：]",
        r"^\s*#+\s*scripts/",
        r"^\s*`?scripts/[^`]+`?\s*$",
    ]
    cleaned: list[str] = []
    for line in text.strip().splitlines():
        if any(re.search(pattern, line, flags=re.IGNORECASE) for pattern in drop_patterns):
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def _looks_like_python_source(text: str) -> bool:
    """Return True for probable Python source without requiring valid syntax yet."""
    stripped = text.strip()
    if not stripped or "```" in stripped or "~~~" in stripped or _MULTI_FILE_MARKER_RE.search(stripped):
        return False
    return bool(re.search(
        r"(?m)^\s*(?:import\s+|from\s+|def\s+|class\s+|if __name__\s*==\s*['\"]__main__['\"]|#!/usr/bin/env python|#)",
        stripped,
    ))


def _extract_probable_python_source(content: str) -> str | None:
    """Extract a raw Python source candidate before syntax validation."""
    stripped = content.strip().lstrip("\ufeff")
    candidates: list[str] = []

    normalized = stripped
    for _ in range(3):
        wrapping = _extract_single_wrapping_fence(normalized)
        if wrapping is None:
            break
        normalized = wrapping.strip()
        candidates.append(normalized)

    only_block = _extract_only_fenced_block(stripped)
    if only_block is not None:
        candidates.append(only_block.strip())

    # Use the first block only when the response does not look like an explicit
    # multi-file bundle.  Multi-file bundles are rejected rather than guessed.
    if not _MULTI_FILE_MARKER_RE.search(stripped) and not re.search(r"(?im)^\s*写入文件[:：]", stripped):
        first_block = _extract_first_fenced_block(stripped)
        if first_block is not None:
            candidates.append(first_block.strip())

    candidates.append(stripped)

    seen: set[str] = set()
    for candidate in candidates:
        cleaned = _drop_common_non_code_lines(candidate)
        if cleaned in seen:
            continue
        seen.add(cleaned)
        if _looks_like_python_source(cleaned):
            return cleaned
    return None


def _normalize_generated_file_content(file_path: str, content: str) -> str:
    """Normalize model output into the requested single file content.

    scripts/**:
    - 保守抽取裸源码；
    - 不接受多文件包。

    SKILL.md:
    - 只剥掉包住整个文件的 ```markdown 外层 fence；
    - 保留正文内部正常 ```bash / ```json fenced block；
    - 不在这里校验 command block，可执行性留给第二轮 E2E。

    references/assets:
    - 继续按普通文件处理。
    """
    if file_path.startswith("scripts/"):
        stripped = content.strip()
        structural_error = script_raw_source_candidate_error_id(stripped)
        if structural_error in {
            "script.raw_source.ambiguous_multi_code_blocks",
            "script.raw_source.multi_file_bundle",
            "script.raw_source.ambiguous_script_candidate",
        }:
            return stripped

        normalized = stripped
        for _ in range(3):
            wrapping_fence = _extract_single_wrapping_fence(normalized)
            if wrapping_fence is None:
                break
            normalized = wrapping_fence.strip()
            if _is_valid_normalized_script_source(file_path, normalized):
                return normalized

        only_block = _extract_only_fenced_block(stripped)
        if only_block is not None:
            normalized = only_block.strip()
            if _is_valid_normalized_script_source(file_path, normalized):
                return normalized

        candidate = stripped if structural_error else _strip_orphan_trailing_fence(stripped)
        if _is_valid_normalized_script_source(file_path, candidate):
            return candidate

        return candidate

    extracted = _extract_target_file_from_bundle(content, file_path)
    candidate = extracted if extracted is not None else content

    if file_path == "SKILL.md":
        return _strip_outer_markdown_fence_for_skill_md(candidate).strip()

    if file_path.startswith("references/"):
        return _strip_outer_markdown_fence_for_reference_md(candidate).strip()

    return _strip_code_fence(candidate)



def _trim_source_to_runtime_entrypoint(file_path: str, content: str, skill_plan_entry: dict[str, Any] | None = None) -> str:
    """Drop leading/trailing prose around a probable runtime source entrypoint."""
    if not file_path.startswith("scripts/"):
        return content.strip()
    plan_entry = _skill_plan_entry_for_file(file_path=file_path, skill_plan_entry=skill_plan_entry)
    text = _strip_orphan_trailing_fence(content.strip().lstrip("\ufeff"))
    lines = text.splitlines()

    start_patterns: list[str]
    end_patterns: list[str]
    if plan_entry.runtime == "node":
        start_patterns = [r"^\s*(?:const|let|var)\s+", r"^\s*function\s+", r"^\s*#!/usr/bin/env\s+node"]
        end_patterns = [r"console\.log\s*\("]
    elif plan_entry.runtime in {"bash", "shell"}:
        start_patterns = [r"^\s*#!/", r"^\s*set\s+-", r"^\s*payload_json=", r"^\s*[A-Za-z_][A-Za-z0-9_]*="]
        end_patterns = [r"^\s*(?:echo|printf|python\s+-c)\b"]
    else:
        start_patterns = [r"^\s*(?:import\s+|from\s+|def\s+|class\s+|#!/usr/bin/env\s+python)"]
        end_patterns = [r"main\s*\(\s*\)"]

    start_idx = 0
    for idx, line in enumerate(lines):
        if any(re.search(pattern, line) for pattern in start_patterns):
            start_idx = idx
            break
    trimmed = lines[start_idx:]

    end_idx = len(trimmed)
    for idx in range(len(trimmed) - 1, -1, -1):
        line = trimmed[idx]
        if any(re.search(pattern, line) for pattern in end_patterns):
            end_idx = idx + 1
            break
    return "\n".join(trimmed[:end_idx]).strip()

_REFERENCE_EXECUTABLE_SCRIPT_CMD_RE = re.compile(
    r"(?m)^\s*(?:python|python3|node|bash|sh)\s+scripts/[A-Za-z0-9_./-]+\b"
)

_MARKDOWN_FENCED_BLOCK_RE = re.compile(
    r"```(?P<lang>[A-Za-z0-9_-]*)\s*\n(?P<body>.*?)```",
    re.DOTALL,
)


def _sanitize_reference_markdown(content: str) -> str:
    """Keep reference examples non-executable.

    references/*.md may contain examples, including code examples.
    But executable shell blocks that call scripts/** must not be preserved as
    ```bash / ```sh / ```shell, because references are documentation resources,
    not workflow sources.
    """
    def repl(match: re.Match[str]) -> str:
        lang = (match.group("lang") or "").strip().lower()
        body = match.group("body") or ""

        if lang in {"bash", "sh", "shell"} and _REFERENCE_EXECUTABLE_SCRIPT_CMD_RE.search(body):
            return "```text\n" + body.strip() + "\n```"

        return match.group(0)

    return _MARKDOWN_FENCED_BLOCK_RE.sub(repl, content)

def _sanitize_generated_file_content(
    file_path: str,
    content: str,
    role: str | None = None,
    skill_plan_entry: dict[str, Any] | None = None,
) -> str:
    """Normalize model output into exactly the requested file content.

    这里只做 normalize/sanitize，不做合同校验。

    原因：
    - references/*.md 需要先 sanitize，再补/规范化 frontmatter，再进入 contract check；
    - 如果这里提前调用 _validate_generated_file_content，会在 frontmatter 修复前误杀；
    - write-file 阶段也不应再次校验，避免前端展示内容与落盘内容不一致。
    """
    if file_path.startswith("scripts/") and script_raw_source_candidate_error_id(content) in {
        "script.raw_source.ambiguous_multi_code_blocks",
        "script.raw_source.multi_file_bundle",
        "script.raw_source.ambiguous_script_candidate",
    }:
        sanitized = content.strip()
    else:
        sanitized = _normalize_generated_file_content(file_path, content)
        sanitized = _trim_source_to_runtime_entrypoint(
            file_path,
            sanitized,
            skill_plan_entry=skill_plan_entry,
        )

    if file_path.startswith("references/") or role == "reference":
        sanitized = _sanitize_reference_markdown(sanitized)

    return sanitized


def _platform_skill_md_command_sections(
    *,
    skill_name: str,
    blueprint_text: str,
    responsibility_graph: Any = None,
) -> list[str]:
    """Build SKILL.md command sections from platform-owned interface facts.

    The model is deliberately not involved in this operation.  Script argv keys
    come from the generated script guard/read contract, while values come from
    frozen graph bindings, SkillPlan bindings, or frozen defaults.  An
    unresolved required argv is an upstream planning error rather than an
    invitation for the SKILL.md writer to guess a value.
    """
    parsed = parse_blueprint([{"role": "assistant", "content": blueprint_text}])
    entries = [
        entry for entry in (parsed.skill_plan.files if parsed.skill_plan else [])
        if getattr(entry, "file_type", "") == "script"
    ]
    skill_dir = settings.skills_path / skill_name
    sections: list[str] = []

    for entry in entries:
        script_path = str(entry.path).replace("\\", "/")
        script_file = skill_dir / script_path
        if not script_file.is_file():
            # Generation order can legitimately place SKILL.md before a script.
            # In that case the frozen SkillPlan remains the only available
            # upstream contract; never fall back to model-authored commands.
            command = render_script_command_from_skill_plan(entry)
        else:
            source = script_file.read_text(encoding="utf-8")
            function_context = build_function_execution_context(
                graph=responsibility_graph,
                target_file=script_path,
            )
            planned_command = render_script_command_from_skill_plan(entry)
            snapshot = build_command_alignment_snapshot(
                script_path=script_path,
                script_content=source,
                command=planned_command,
                function_execution_context=function_context,
                script_defaults=dict(getattr(entry, "default_values", {}) or {}),
            )
            planned_signature = _command_signature(planned_command, script_path) or {}
            planned_payload = planned_signature.get("json_payload") or {}
            if not isinstance(planned_payload, dict):
                planned_payload = {}

            confirmed = dict(snapshot.get("confirmed_bindings") or {})
            defaults = dict(snapshot.get("frozen_defaults") or {})
            required = set(snapshot.get("required_target_keys") or [])
            payload: dict[str, Any] = {}
            for key in snapshot.get("target_keys") or []:
                if key in confirmed:
                    payload[key] = "{{" + str(confirmed[key]) + "}}"
                elif key in defaults:
                    payload[key] = defaults[key]
                elif key in planned_payload:
                    # command_args/input_binding is frozen by the upstream
                    # SkillPlan.  Preserve its native JSON value verbatim.
                    payload[key] = planned_payload[key]
                elif key in required:
                    raise ValueError(
                        f"upstream command binding missing for {script_path}: {key}"
                    )
            command = render_command_payload(
                script_path,
                str(entry.runtime),
                payload,
                dict(snapshot.get("actual_argv_schema", {}).get("expected_types", {}) or {}),
            )

        role = str(getattr(entry, "role", "") or "script")
        inputs = ", ".join(str(value) for value in (getattr(entry, "inputs", []) or [])) or "无"
        outputs = ", ".join(str(value) for value in (getattr(entry, "outputs", []) or [])) or "无"
        defaults = dict(getattr(entry, "default_values", {}) or {})
        defaults_line = (
            f'- defaults: {json.dumps(defaults, ensure_ascii=False, sort_keys=True)}\n\n'
            if defaults else ""
        )
        portable_output_mapping = {}
        if responsibility_graph is not None:
            portable_output_mapping = project_script_interface_contract(
                responsibility_graph, script_path,
            )["platform_output_mapping"]
        output_mapping_record = render_runtime_output_mapping(
            script_path, portable_output_mapping, build_platform_io_contract(),
        )
        sections.append(
            f"### `{script_path}`\n\n"
            f"- role: `{role}`\n"
            f"- inputs: {inputs}\n"
            f"- outputs: {outputs}\n\n"
            f"{defaults_line}"
            f"{output_mapping_record}\n"
            "<!-- generated_by=contract_renderer -->\n"
            "```bash\n"
            f"{command}\n"
            "```"
        )
    return sections


def _materialize_platform_skill_md_commands(
    content: str,
    *,
    skill_name: str,
    blueprint_text: str,
    responsibility_graph: Any = None,
) -> str:
    """Replace every model-authored script command with backend output."""
    text = str(content or "").strip()
    start_marker = "<!-- platform-command-blocks:start -->"
    end_marker = "<!-- platform-command-blocks:end -->"
    text = re.sub(
        re.escape(start_marker) + r".*?" + re.escape(end_marker),
        "",
        text,
        flags=re.DOTALL,
    ).strip()
    # Source ownership is compiler metadata.  Discard any marker supplied by
    # model-authored Markdown and recreate it only alongside rendered blocks.
    text = re.sub(
        r"(?m)^\s*<!--\s*generated_by=contract_renderer\s*-->\s*\n?",
        "",
        text,
    ).strip()
    blocks = parse_skill_md_bash_command_blocks(text)
    for block in reversed(blocks):
        text = text[:block.start] + text[block.end:]
    sections = _platform_skill_md_command_sections(
        skill_name=skill_name,
        blueprint_text=blueprint_text,
        responsibility_graph=responsibility_graph,
    )
    if not sections:
        return text.strip()
    generated = (
        f"{start_marker}\n"
        "## 运行命令\n\n"
        + "\n\n".join(sections)
        + f"\n{end_marker}"
    )
    return text.rstrip() + "\n\n" + generated + "\n"


def _validate_materialized_platform_skill_md_commands(
    content: str,
    *,
    skill_name: str,
    blueprint_text: str,
) -> None:
    """Lightly check command presence, renderer ownership, and script paths."""
    parsed = parse_blueprint([{"role": "assistant", "content": blueprint_text}])
    entries = [
        entry for entry in (parsed.skill_plan.files if parsed.skill_plan else [])
        if getattr(entry, "file_type", "") == "script"
    ]
    blocks = parse_skill_md_bash_command_blocks(content)
    blocks_by_path: dict[str, list[Any]] = {}
    for block in blocks:
        if block.script_path:
            blocks_by_path.setdefault(block.script_path, []).append(block)

    for entry in entries:
        script_path = str(entry.path).replace("\\", "/")
        matching = blocks_by_path.get(script_path, [])
        if len(matching) != 1:
            raise ValueError(
                f"platform command materialization expected one block for {script_path}, got {len(matching)}"
            )
        source_prefix = content[:matching[0].start].rstrip()
        if not source_prefix.endswith("<!-- generated_by=contract_renderer -->"):
            raise ValueError(
                f"platform command block is missing renderer ownership metadata: {script_path}"
            )
        script_file = settings.skills_path / skill_name / script_path
        if not script_file.is_file():
            raise ValueError(f"cannot validate platform command; script is missing: {script_path}")

def _strip_outer_markdown_fence_for_skill_md(content: str) -> str:
    """Strip only one outer markdown fence around a whole SKILL.md file.

    只处理模型把整个 SKILL.md 包成：

    ```markdown
    ---
    name: ...
    ---
    ...
    ```

    的情况。

    不处理正文内部的 ```bash / ```json block，
    避免误删 SKILL.md 内部正常 fenced code block。
    """
    text = (content or "").strip().lstrip("\ufeff")
    if not text.startswith(("```", "~~~")):
        return content

    lines = text.splitlines()
    if len(lines) < 3:
        return content

    opening = lines[0].strip()
    open_match = re.match(r"^(`{3,}|~{3,})(?:markdown|md)?\s*$", opening, flags=re.I)
    if not open_match:
        return content

    fence = open_match.group(1)
    fence_char = fence[0]
    fence_len = len(fence)

    closing = lines[-1].strip()
    if not re.fullmatch(rf"{re.escape(fence_char)}{{{fence_len},}}\s*", closing):
        return content

    body = "\n".join(lines[1:-1]).strip()
    return body + "\n"


def _strip_outer_markdown_fence_for_reference_md(content: str) -> str:
    """Strip a provably complete markdown wrapper around a reference file.

    Unlike the generic fence stripper, this deliberately leaves incomplete
    wrappers untouched.  Reference bodies may legitimately contain fenced
    examples, so normalization must not guess which fence closes the wrapper.
    """
    text = (content or "").strip().lstrip("\ufeff")
    lines = text.splitlines()
    if len(lines) < 3:
        return content

    opening = re.fullmatch(r"(?P<fence>`{3,}|~{3,})(?:markdown|md)\s*", lines[0].strip(), flags=re.I)
    if not opening:
        return content

    outer_fence = opening.group("fence")
    stack: list[tuple[str, int]] = [(outer_fence[0], len(outer_fence))]
    for line in lines[1:]:
        fence_match = re.fullmatch(r"(?P<fence>`{3,}|~{3,})(?P<info>[^`]*)", line.strip())
        if not fence_match:
            continue
        fence = fence_match.group("fence")
        info = fence_match.group("info").strip()
        if not info and stack and fence[0] == stack[-1][0] and len(fence) >= stack[-1][1]:
            stack.pop()
        else:
            stack.append((fence[0], len(fence)))

    closing = lines[-1].strip()
    if stack or not re.fullmatch(rf"{re.escape(outer_fence[0])}{{{len(outer_fence)},}}\s*", closing):
        return content

    return "\n".join(lines[1:-1]).strip() + "\n"

def _strip_code_fence(content: str) -> str:
    """Strip wrapping code-fence markers that a model may output despite instructions.

    Handles:
      ```python\\n<code>\\n```
      ```\\n<code>\\n```
      ~~~~\\n<code>\\n~~~~
    """
    stripped = content.strip()
    # Opening fence with optional language tag
    m_open = re.match(r"^(`{3,}|~{3,})[^\n]*\n", stripped)
    if m_open:
        fence_char = stripped[0]
        rest = stripped[m_open.end():]
        close_pat = re.compile(r"\n" + re.escape(fence_char) + r"{3,}\s*$")
        m_close = close_pat.search(rest)
        if m_close:
            return rest[: m_close.start()].strip()
        return rest.strip()
    return stripped




def _script_generation_skeleton(
    file_path: str,
    purpose: str,
    blueprint_text: str,
    *,
    role: str | None = None,
    skill_plan_entry: dict[str, Any] | None = None,
) -> str:
    """Return protocol-only scaffolds; tool calls come from Tool Registry snippets."""
    plan_entry = _skill_plan_entry_for_file(
        file_path=file_path,
        purpose=purpose,
        blueprint_text=blueprint_text,
        role=role,
        skill_plan_entry=skill_plan_entry,
    )
    input_keys = [key for key in (plan_entry.inputs or []) if isinstance(key, str) and key.strip()]
    output_keys = [key for key in (plan_entry.outputs or []) if isinstance(key, str) and key.strip()]

    planned_interface_hint = (
        "Current planned interface:\n"
        f"inputs: {json.dumps(input_keys, ensure_ascii=False)}\n"
        f"outputs: {json.dumps(output_keys, ensure_ascii=False)}\n"
        "Prefer these input names for strict_json_argv_guard and these output names "
        "for the final stdout object. Do not copy generic field names from examples.\n"
    )

    component_hint = getattr(plan_entry, "component_hint", "") or getattr(plan_entry, "role", "")
    helper_hint = f"# component_hint: {component_hint}\n"

    if plan_entry.runtime == "node":
        return (
            planned_interface_hint +
            "协议骨架（只约束 argv/run/stdout；具体工具调用必须来自 Tool Registry snippets/function cards）：\n"
            + helper_hint +
            "const payload = process.argv[2] ? JSON.parse(process.argv[2]) : {};\n"
            "function run(payload) {\n"
            "  // TODO: implement the canonical contract using selected tools or real local logic.\n"
            "  // Return an object containing every required stdout field.\n"
            "  return {};\n"
            "}\n"
            "console.log(JSON.stringify(run(payload)));"
        )

    if plan_entry.runtime in {"bash", "shell"}:
        helper = "import json,sys; json.loads(sys.argv[1] or '{}'); print(json.dumps({}))"
        return (
            planned_interface_hint +
            "协议骨架（只约束 $1 JSON argv 与 stdout JSON；具体工具调用必须来自 Tool Registry snippets/function cards）：\n"
            + helper_hint +
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "payload_json=${1:-'{}'}\n"
            f"python -c {shlex.quote(helper)} \"$payload_json\""
        )

    return (
        planned_interface_hint +
        "协议骨架（只约束 parse_args/run/main/stdout JSON；具体工具调用必须来自 Tool Registry snippets/function cards）：\n"
        + helper_hint +
        "import json\n"
        "import sys\n\n"
        "from backend.services.runtime_tools import strict_json_argv_guard\n\n"
        "def parse_args() -> dict:\n"
        "    if len(sys.argv) < 2:\n"
        "        raise ValueError('missing JSON argv')\n"
        "    payload = json.loads(sys.argv[1])\n"
        "    return strict_json_argv_guard(payload, {\n"
        "        # Fill this spec using the planned input names shown above whenever applicable.\n"
        "        # Use {} for true no-input scripts.\n"
        "        # Key names must come from the current script contract/SKILL.md command,\n"
        "        # never from placeholder examples such as input_text.\n"
        "    })\n\n"
        "def run(args: dict) -> dict:\n"
        "    # TODO: implement the canonical contract using selected tools or real local logic.\n"
        "    # Only read values from args after parse_args validation; use args['required_key'] for required values.\n"
        "    # Return an object containing every required stdout field.\n"
        "    return {}\n\n"
        "def main() -> None:\n"
        "    print(json.dumps(run(parse_args()), ensure_ascii=False))\n\n"
        "if __name__ == '__main__':\n"
        "    main()"
    )


def _creator_kernel_reference_context() -> str:
    """Load small Creator prompt context from kernel references.

    These references are advisory generation context only; SKILL.md protocol and
    generated file contracts remain unchanged.
    """
    kernel_dir = Path(__file__).resolve().parents[2] / "kernel"
    candidates = [
        kernel_dir / "references" / "best-practices.md",
        kernel_dir / "references" / "workflows.md",
        kernel_dir / "references" / "output-patterns.md",
        kernel_dir / "SKILL.md",
    ]
    chunks: list[str] = []
    for path in candidates:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            rel = path.relative_to(kernel_dir.parent)
            chunks.append(f"### INTERNAL-ONLY {rel}\n{text[:1800]}")
    return "\n\n".join(chunks)




def _command_argv_contract_for_script(file_path: str, blueprint_text: str, plan_entry: SkillPlanEntry) -> dict[str, Any]:
    """Best-effort command argv contract for first-round script generation."""
    command_template = _script_command_template(file_path, blueprint_text, plan_entry)
    argv: dict[str, Any] = {}
    try:
        parts = shlex.split(command_template)
    except Exception:
        parts = []
    for part in reversed(parts):
        text = str(part or "").strip()
        if not (text.startswith("{") and text.endswith("}")):
            continue
        try:
            parsed = json.loads(text)
        except Exception:
            continue
        if isinstance(parsed, dict):
            argv = parsed
            break
    return {
        "command_template": command_template,
        "argv_template": argv,
        "argv_keys": sorted(str(key) for key in argv.keys()),
    }


def _stable_unique(values: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _tool_binding_from_resolution(implementation_resolution: ImplementationResolution) -> dict[str, Any]:
    tools = list(implementation_resolution.available_tools or []) + list(implementation_resolution.selected_tools or [])
    primary_tool_ids: list[str] = []
    allowed_helper_imports: list[str] = []
    allowed_import_paths: list[str] = []
    allowed_function_imports: list[str] = []
    dependencies: list[str] = []
    for tool in tools:
        if getattr(tool, "tool_id", ""):
            primary_tool_ids.append(str(tool.tool_id))
        import_path = str(getattr(tool, "import_path", "") or "").strip()
        function_name = str(getattr(tool, "function_name", "") or "").strip()
        if import_path:
            allowed_import_paths.append(import_path)
        if import_path and function_name:
            allowed_function_imports.append(function_name)
            allowed_function_imports.append(f"{import_path}.{function_name}")
            if import_path == "backend.services.runtime_tools":
                allowed_helper_imports.append(function_name)
        dependencies.extend(str(dep) for dep in (getattr(tool, "dependencies", []) or []))
    dependencies.extend(str(dep) for dep in (implementation_resolution.declared_dependencies or []))
    return {
        "primary_tool_ids": _stable_unique(primary_tool_ids),
        "allowed_helper_imports": _stable_unique(allowed_helper_imports),
        "allowed_import_paths": _stable_unique(allowed_import_paths),
        "allowed_function_imports": _stable_unique(allowed_function_imports),
        "dependencies": _stable_unique(dependencies),
    }


def _merge_tool_binding_summary(explicit: dict[str, Any] | None, derived: dict[str, Any]) -> dict[str, Any]:
    explicit = explicit if isinstance(explicit, dict) else {}
    merged = dict(explicit)
    for key, derived_values in derived.items():
        existing = explicit.get(key)
        if isinstance(existing, list):
            merged[key] = _stable_unique([*existing, *derived_values])
        elif existing not in (None, "", []):
            merged[key] = existing
        else:
            merged[key] = _stable_unique(derived_values)
    return merged


def _ensure_python_script_core_binding(binding: dict[str, Any], plan_entry: SkillPlanEntry) -> dict[str, Any]:
    if str(getattr(plan_entry, "runtime", "") or "").lower() != "python":
        return binding
    merged = dict(binding)
    merged["primary_tool_ids"] = _stable_unique([*(merged.get("primary_tool_ids") or []), "script_argv_guard"])
    available_tools = list(merged.get("available_tools") or [])
    if not any(
        isinstance(tool, dict)
        and (
            tool.get("function_name") == "strict_json_argv_guard"
            or tool.get("tool_id") == "script_argv_guard"
            or tool.get("capability_name") == "script_argv_guard"
        )
        for tool in available_tools
    ):
        available_tools.append({
            "tool_id": "script_argv_guard",
            "capability_name": "script_argv_guard",
            "function_name": "strict_json_argv_guard",
            "import_path": "backend.services.runtime_tools",
            "input_schema": {},
            "output_schema": {},
        })
    merged["available_tools"] = available_tools
    merged["allowed_helper_imports"] = _stable_unique([*(merged.get("allowed_helper_imports") or []), "strict_json_argv_guard"])
    return merged

def _string_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    raw = value if isinstance(value, list) else [value]
    out: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _tool_ids_from_binding_summary(binding: dict[str, Any]) -> list[str]:
    if not isinstance(binding, dict):
        return []
    out: list[str] = []
    for key in ("primary_tool_ids", "allowed_tool_ids", "secondary_tool_ids"):
        for item in _string_list(binding.get(key)):
            if item not in out:
                out.append(item)
    return out


def _snippet_to_dict(snippet: Any) -> dict[str, Any]:
    if hasattr(snippet, "model_dump"):
        return snippet.model_dump(mode="json")
    if hasattr(snippet, "__dict__"):
        return dict(snippet.__dict__)
    if isinstance(snippet, dict):
        return dict(snippet)
    return {"text": str(snippet)}

def _script_responsibility_requirements_payload(
    *,
    file_path: str,
    requirements: Any = None,
) -> list[dict[str, Any]]:
    """Project the current script FunctionItem into prompt-visible payload.

    FunctionItem is the current script's complete executable responsibility
    closure. The existing responsibility_requirements payload key transports
    this FunctionItem for compatibility. No business semantics are inferred here.

    The function only filters the already-compiled responsibility contract by
    target_file and serializes it for the script production model.
    """

    payload: list[dict[str, Any]] = []

    for raw in requirements or []:
        try:
            if isinstance(
                raw,
                FunctionItem,
            ):
                item = raw

            elif isinstance(
                raw,
                dict,
            ):
                item = FunctionItem(
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

        serialized = function_item_prompt_payload(item)

        if str(
            item.id or ""
        ).strip():
            serialized[
                "requirement_id"
            ] = item.id

        payload.append(
            serialized
        )

    return payload

def _build_capability_guidance(
    function_execution_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Build prompt-visible capability guidance.

    Capability declarations are hints for implementation choice.
    They are not execution requirements.

    This projection intentionally does not modify
    FunctionItem or SkillPlan contracts.
    """

    context = (
        function_execution_context
        if isinstance(function_execution_context, dict)
        else {}
    )

    function_item = context.get(
        "function_item",
        {}
    )

    if not isinstance(function_item, dict):
        function_item = {}

    capabilities = (
            function_item.get(
                "required_capabilities",
                []
            )
            or []
    )

    capability_names = [
        str(item).strip()
        for item in capabilities
        if str(item).strip()
    ]

    return {
        "declared_capabilities": capability_names,

        "semantic_role": (
            "implementation_hint"
        ),

        "interpretation": (
            "Capabilities describe available "
            "implementation directions. "
            "They do not define mandatory "
            "tool execution."
        ),

        "rules": [
            (
                "A capability declaration does not "
                "create an additional responsibility."
            ),
            (
                "A capability declaration does not "
                "require invoking a matching tool."
            ),
            (
                "Use a tool only when its function "
                "contract directly contributes to "
                "the current FunctionItem responsibility."
            ),
            (
                "Do not add external calls only "
                "because a capability name exists."
            ),
        ],
    }

def _build_interface_semantics(
    *,
    plan_entry: SkillPlanEntry,
    canonical_contract: Any,
    function_execution_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build the prompt-visible runtime interface projection for code generation.

    This function does not create a new contract or redesign any interface.
    It only projects already-authoritative facts from:
    - SkillPlan inputs/outputs
    - runtime input bindings
    - ResponsibilityGraph incoming/outgoing edges
    - platform terminal sink schemas

    The projection preserves two different facts:
    - semantic/logical port identity
    - concrete runtime representation required at the receiving boundary

    No business keywords, filename heuristics, or name-similarity inference
    are used.
    """

    context = (
        function_execution_context
        if isinstance(function_execution_context, dict)
        else {}
    )

    platform_contract = build_platform_io_contract()

    def _schema_type_values(schema: dict[str, Any]) -> set[str]:
        """
        Return explicitly declared JSON-schema type values.

        This is representation-only normalization. It does not infer
        semantic type from names or descriptions.
        """
        raw_type = schema.get("type")

        if isinstance(raw_type, str):
            value = raw_type.strip().lower()
            return {value} if value else set()

        if isinstance(raw_type, list):
            return {
                str(value).strip().lower()
                for value in raw_type
                if str(value or "").strip()
            }

        return set()

    def _representation_facts(
        schema: dict[str, Any],
        *,
        declared_type: Any = None,
    ) -> dict[str, Any]:
        """
        Project generic representation facts without inventing structure.

        Cardinality is derived only from explicit representation evidence:
        JSON Schema array/items or an explicitly declared list/array type.
        """

        schema = dict(schema or {})
        type_values = _schema_type_values(schema)

        declared_type_text = str(
            declared_type or ""
        ).strip()

        effective_type = (
            declared_type_text
            or (
                next(iter(type_values))
                if len(type_values) == 1
                else ""
            )
            or "unknown"
        )

        normalized_declared_type = (
            declared_type_text
            .strip()
            .lower()
            .replace(" ", "")
        )

        explicit_many = (
            "array" in type_values
            or isinstance(schema.get("items"), dict)
            or normalized_declared_type == "array"
            or normalized_declared_type == "list"
            or normalized_declared_type.startswith("array[")
            or normalized_declared_type.startswith("list[")
        )

        has_known_single_type = bool(
            (
                type_values
                - {
                    "array",
                    "null",
                }
            )
            or (
                normalized_declared_type
                and normalized_declared_type
                not in {
                    "unknown",
                    "unspecified",
                    "array",
                    "list",
                }
                and not normalized_declared_type.startswith(
                    (
                        "array[",
                        "list[",
                    )
                )
            )
        )

        if explicit_many:
            cardinality = "many"
        elif has_known_single_type:
            cardinality = "single"
        else:
            cardinality = "unknown"

        nullable = bool(
            schema.get("nullable") is True
            or "null" in type_values
        )

        item_schema = (
            dict(schema.get("items"))
            if isinstance(
                schema.get("items"),
                dict,
            )
            else {}
        )

        return {
            "type": effective_type,
            "cardinality": cardinality,
            "nullable": nullable,
            "item_schema": item_schema,
        }

    input_bindings: dict[str, dict[str, Any]] = {}

    bindings = (
        getattr(
            plan_entry,
            "input_binding",
            None,
        )
        or getattr(
            plan_entry,
            "command_arg_bindings",
            None,
        )
        or (
            plan_entry.runtime_contract.get(
                "input_binding"
            )
            if isinstance(
                plan_entry.runtime_contract,
                dict,
            )
            else None
        )
        or []
    )

    for item in bindings:
        if not isinstance(item, dict):
            continue

        key = (
            item.get("argv_key")
            or item.get("name")
            or item.get("to_field")
        )

        if key:
            input_bindings[
                str(key)
            ] = dict(item)

    incoming_edges = [
        dict(edge)
        for edge in (
            context.get("incoming_edges")
            or []
        )
        if isinstance(edge, dict)
    ]

    outgoing_edges = [
        dict(edge)
        for edge in (
            context.get("outgoing_edges")
            or []
        )
        if isinstance(edge, dict)
    ]

    incoming_by_input: dict[
        str,
        list[dict[str, Any]],
    ] = {}

    for edge in incoming_edges:
        target_input = str(
            edge.get("to_input")
            or ""
        ).strip()

        if target_input:
            incoming_by_input.setdefault(
                target_input,
                [],
            ).append(edge)

    outgoing_by_output: dict[
        str,
        list[dict[str, Any]],
    ] = {}

    for edge in outgoing_edges:
        source_output = str(
            edge.get("from_output")
            or ""
        ).strip()

        if source_output:
            outgoing_by_output.setdefault(
                source_output,
                [],
            ).append(edge)

    inputs: list[dict[str, Any]] = []

    for raw_input in (
        canonical_contract.inputs
        or []
    ):
        if isinstance(raw_input, dict):
            name = str(
                raw_input.get("name")
                or ""
            ).strip()
            schema = dict(raw_input)
        else:
            name = str(
                raw_input
                or ""
            ).strip()
            schema = {
                "name": name,
            }

        if not name:
            continue

        binding = dict(
            input_bindings.get(
                name,
                {},
            )
        )

        graph_edges = list(
            incoming_by_input.get(
                name,
                [],
            )
        )

        graph_sources: list[
            dict[str, Any]
        ] = []

        for edge in graph_edges:
            graph_sources.append(
                {
                    "from_node": str(
                        edge.get(
                            "from_node"
                        )
                        or ""
                    ),
                    "from_output": str(
                        edge.get(
                            "from_output"
                        )
                        or ""
                    ),
                    "to_input": str(
                        edge.get(
                            "to_input"
                        )
                        or ""
                    ),
                    "constraints": [
                        dict(value)
                        for value in (
                            edge.get(
                                "constraints"
                            )
                            or []
                        )
                        if isinstance(
                            value,
                            dict,
                        )
                    ],
                }
            )

        declared_type = (
            binding.get("value_type")
            or binding.get("type")
            or schema.get("type")
        )

        representation = (
            _representation_facts(
                schema,
                declared_type=declared_type,
            )
        )

        if "required" in binding:
            required = bool(
                binding.get("required")
            )
        elif isinstance(
            schema.get("required"),
            bool,
        ):
            required = bool(
                schema.get("required")
            )
        else:
            required = True

        source_ref = (
            binding.get("source")
            or binding.get("from_field")
            or ""
        )

        source_kind = (
            binding.get("source_kind")
            or (
                "responsibility_graph"
                if graph_sources
                else "runtime_binding"
            )
        )

        inputs.append(
            {
                "name": name,
                "type": representation[
                    "type"
                ],
                "cardinality": representation[
                    "cardinality"
                ],
                "nullable": representation[
                    "nullable"
                ],
                "item_schema": representation[
                    "item_schema"
                ],
                "required": required,
                "binding_status": (
                    "resolved"
                    if binding
                    or graph_sources
                    else "unresolved"
                ),
                "source": {
                    "kind": source_kind,
                    "ref": str(
                        source_ref
                        or ""
                    ),
                    "graph_sources": (
                        graph_sources
                    ),
                },
                "binding": binding,
                "schema": schema,
            }
        )

    outputs: list[dict[str, Any]] = []

    for raw_output in (
        canonical_contract.outputs
        or []
    ):
        if isinstance(raw_output, dict):
            name = str(
                raw_output.get("name")
                or ""
            ).strip()
            declared_schema = dict(
                raw_output
            )
        else:
            name = str(
                raw_output
                or ""
            ).strip()
            declared_schema = {
                "name": name,
            }

        if not name:
            continue

        edges = list(
            outgoing_by_output.get(
                name,
                [],
            )
        )

        downstream_contracts: list[
            dict[str, Any]
        ] = []

        terminal_schemas: list[
            dict[str, Any]
        ] = []

        consumers: list[str] = []

        for edge in edges:
            target_node = str(
                edge.get("to_node")
                or ""
            ).strip()

            target_input = str(
                edge.get("to_input")
                or ""
            ).strip()

            if (
                target_node
                and target_node
                not in consumers
            ):
                consumers.append(
                    target_node
                )

            downstream_contract: dict[
                str,
                Any,
            ] = {
                "target_node": target_node,
                "target_input": target_input,
                "constraints": [
                    dict(value)
                    for value in (
                        edge.get(
                            "constraints"
                        )
                        or []
                    )
                    if isinstance(
                        value,
                        dict,
                    )
                ],
            }

            if (
                target_node
                == "platform_output_node"
                and target_input
            ):
                sink = (
                    get_platform_output_sink(
                        platform_contract,
                        target_input,
                    )
                )

                if isinstance(
                    sink,
                    dict,
                ):
                    value_schema = (
                        sink.get(
                            "value_schema"
                        )
                    )

                    downstream_contract[
                        "platform_sink"
                    ] = {
                        key: value
                        for key, value
                        in sink.items()
                        if key
                        != "value_schema"
                    }

                    if isinstance(
                        value_schema,
                        dict,
                    ):
                        projected_schema = dict(
                            value_schema
                        )

                        downstream_contract[
                            "value_schema"
                        ] = projected_schema

                        terminal_schemas.append(
                            projected_schema
                        )

            downstream_contracts.append(
                downstream_contract
            )

        runtime_schema = dict(
            declared_schema
        )

        if terminal_schemas:
            unique_terminal_schemas = {
                json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
                for value in terminal_schemas
            }

            # Do not choose between conflicting frozen terminal schemas.
            # The deterministic contract validator/materializer owns that
            # conflict. For one unambiguous terminal representation, expose
            # it directly to the code model.
            if (
                len(
                    unique_terminal_schemas
                )
                == 1
            ):
                terminal_schema = dict(
                    terminal_schemas[0]
                )

                semantic_description = (
                    declared_schema.get(
                        "description"
                    )
                )

                runtime_schema = {
                    **runtime_schema,
                    **terminal_schema,
                }

                if semantic_description:
                    runtime_schema[
                        "description"
                    ] = (
                        semantic_description
                    )

        representation = (
            _representation_facts(
                runtime_schema,
                declared_type=runtime_schema.get(
                    "type"
                ),
            )
        )

        outputs.append(
            {
                "name": name,

                # Concrete representation expected from this script.
                "type": representation[
                    "type"
                ],
                "cardinality": representation[
                    "cardinality"
                ],
                "nullable": representation[
                    "nullable"
                ],
                "item_schema": representation[
                    "item_schema"
                ],

                # Compatibility field retained for existing prompt consumers.
                "consumer": (
                    consumers
                    or [
                        "platform_output"
                    ]
                ),

                # Full frozen downstream facts. These are the important
                # addition: code generation no longer sees only a node name.
                "downstream_contracts": (
                    downstream_contracts
                ),

                "terminal_output": bool(
                    terminal_schemas
                ),

                # Preserve both logical/semantic declaration and concrete
                # receiving-boundary representation instead of conflating them.
                "schema": declared_schema,
                "runtime_schema": (
                    runtime_schema
                ),
            }
        )

    return {
        "purpose": (
            getattr(
                plan_entry,
                "purpose",
                "",
            )
            or ""
        ),
        "inputs": inputs,
        "outputs": outputs,
        "authority": [
            "SkillPlan.input_binding",
            "canonical_contract",
            "ResponsibilityGraph incoming_edges",
            "ResponsibilityGraph outgoing_edges",
            "platform output sink value_schema",
        ],
        "rule": (
            "Logical field names identify ports; they do not determine runtime "
            "representation. Preserve the declared semantic value while satisfying "
            "the concrete receiving-boundary schema. For terminal outputs, the "
            "platform sink value_schema is the authoritative runtime representation. "
            "Do not infer scalar/list/object structure from names, descriptions, "
            "examples, or business conventions. Do not rename or redesign ports."
        ),
    }

def _tool_function_card_from_available_tool(tool: dict[str, Any]) -> str:
    function_name = str(tool.get("function_name") or "").strip()
    import_path = str(tool.get("import_path") or "").strip()
    tool_id = str(tool.get("tool_id") or "").strip()
    return "\n".join([
        f"Tool: {tool_id}",
        f"Function: {function_name}",
        f"Import: from {import_path} import {function_name}",
        "Input schema:",
        json.dumps(tool.get("input_schema") or {}, ensure_ascii=False, sort_keys=True),
        "Output schema:",
        json.dumps(tool.get("output_schema") or {}, ensure_ascii=False, sort_keys=True),
        "Return contract:",
        str(tool.get("return_contract") or "Returns a JSON-serializable value matching output_schema."),
        "Example call:",
        str(tool.get("call_template") or f"from {import_path} import {function_name}\nresult = {function_name}(...)").strip(),
        "Runtime: python_script",
    ])


def _filter_snippets_to_available_callables(
    snippets: list[dict[str, Any]],
    available_tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    callable_names = {
        str(tool.get("function_name") or "").strip()
        for tool in available_tools
        if isinstance(tool, dict) and str(tool.get("function_name") or "").strip()
    }
    if not callable_names:
        return []
    out: list[dict[str, Any]] = []
    for snippet in snippets or []:
        if not isinstance(snippet, dict):
            continue
        searchable = "\n".join(
            str(snippet.get(key) or "")
            for key in ("formatted", "code", "description", "title", "return_rule")
        )
        searchable += "\n" + json.dumps(snippet.get("applies_to") or {}, ensure_ascii=False, sort_keys=True)
        searchable += "\n" + json.dumps(snippet.get("requires") or [], ensure_ascii=False, sort_keys=True)
        if any(name in searchable for name in callable_names):
            out.append(snippet)
    return out


def _available_tool_index_for_prompt(
    available_tools: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Return the model-visible tool index without stale tool details."""

    result: list[dict[str, str]] = []
    for tool in available_tools or []:
        if not isinstance(tool, dict):
            continue
        tool_id = str(tool.get("tool_id") or "").strip()
        capability_name = str(tool.get("capability_name") or "").strip()
        function_name = str(tool.get("function_name") or "").strip()
        if not capability_name and "." in tool_id:
            capability_name = tool_id.split(".", 1)[0].strip()
        if not function_name and "." in tool_id:
            function_name = tool_id.rsplit(".", 1)[-1].strip()
        if not tool_id or not function_name:
            continue
        result.append({
            "tool_id": tool_id,
            "capability_name": capability_name,
            "function_name": function_name,
        })
    return result

def _registry_tool_from_available_index(item: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve one available_tools index row to a Registry function fact."""

    if not isinstance(item, dict):
        return None

    function_name = str(item.get("function_name") or "").strip()
    tool_id = str(item.get("tool_id") or "").strip()
    capability_name = str(item.get("capability_name") or "").strip()
    if not capability_name and "." in tool_id:
        capability_name = tool_id.split(".", 1)[0].strip()
    if not capability_name:
        capability_name = tool_id.strip()
    if not function_name and "." in tool_id:
        function_name = tool_id.rsplit(".", 1)[-1].strip()
    if not capability_name or not function_name:
        return None

    capability = get_tool_capability(capability_name)
    if capability is None:
        return None

    for function in getattr(capability, "functions", []) or []:
        registry_function_name = str(getattr(function, "function_name", "") or "").strip()
        if registry_function_name != function_name:
            continue
        import_path = str(getattr(function, "import_path", "") or "").strip()
        if not import_path:
            return None
        example_call = str(getattr(function, "example_call", "") or "").strip()
        return {
            "tool_id": tool_id or f"{capability_name}.{function_name}",
            "capability_name": capability_name,
            "description": str(getattr(function, "when_to_use", "") or getattr(function, "short_description", "") or getattr(capability, "display_name", "") or capability_name),
            "short_description": str(getattr(function, "short_description", "") or ""),
            "when_to_use": str(getattr(function, "when_to_use", "") or ""),
            "function_name": function_name,
            "import_path": import_path,
            "call_template": example_call or f"from {import_path} import {function_name}\nresult = {function_name}(...)",
            "signature": str(getattr(function, "signature", "") or ""),
            "input_schema": getattr(function, "input_schema", None) or {},
            "output_schema": getattr(function, "output_schema", None) or {},
            "return_contract": str(getattr(function, "return_contract", "") or ""),
            "example_return": str(getattr(function, "example_return", "") or ""),
            "example_stdout": str(getattr(function, "example_stdout", "") or ""),
            "common_mistakes": list(getattr(function, "common_mistakes", []) or []),
            "usage_policy": str(getattr(function, "usage_policy", "") or getattr(capability, "usage_policy", "") or ""),
            "required_env": list(getattr(function, "required_env", []) or getattr(capability, "required_env", []) or []),
            "required_secrets": list(getattr(function, "required_secrets", []) or getattr(capability, "required_secrets", []) or []),
            "artifact_outputs": list(getattr(function, "artifact_outputs", []) or getattr(capability, "artifact_outputs", []) or []),
            "side_effects": list(getattr(function, "side_effects", []) or getattr(capability, "side_effects", []) or []),
        }
    return None


def _available_tool_cards_from_binding(
    binding: dict[str, Any],
) -> tuple[
    list[dict[str, Any]],
    list[str],
    list[str],
]:
    """Resolve authorized available_tools indexes into Registry facts/cards."""

    resolved_tools: list[dict[str, Any]] = []
    tool_function_cards: list[str] = []
    selected_tool_names: list[str] = []

    bound_available_tools = binding.get("available_tools") if isinstance(binding, dict) else []
    for item in bound_available_tools if isinstance(bound_available_tools, list) else []:
        resolved_tool = _registry_tool_from_available_index(item)
        if not resolved_tool:
            continue
        capability_name = str(resolved_tool.get("capability_name") or "").strip()
        if capability_name and capability_name not in selected_tool_names:
            selected_tool_names.append(capability_name)
        resolved_tools.append(resolved_tool)
        tool_function_cards.append(_tool_function_card_from_available_tool(resolved_tool))

    return (resolved_tools, tool_function_cards, selected_tool_names)

def build_available_tool_context(
    current_file_tool_binding: dict[str, Any],
    *,
    role: str = "",
    file_path: str = "",
    max_snippets: int = 8,
    failure_layer: str | None = None,
    error_text: str | None = None,
) -> dict[str, Any]:
    """Resolve all model-visible tool context from one binding index.

    ``current_file_tool_binding["available_tools"]`` is the sole tool index.
    Cards, snippets, compatibility allowed_* fields, and selected names are
    derived only from that index via Registry projection helpers.
    """

    binding = dict(current_file_tool_binding or {})
    raw_available_tools = (
        binding.get("available_tools")
        if isinstance(binding.get("available_tools"), list)
        else []
    )
    prompt_tool_index = _available_tool_index_for_prompt([
        dict(tool)
        for tool in raw_available_tools
        if isinstance(tool, dict)
    ])
    binding["available_tools"] = prompt_tool_index

    unresolved_indexes = [
        item
        for item in prompt_tool_index
        if _registry_tool_from_available_index(item) is None
    ]
    if unresolved_indexes:
        raise ValueError(
            "BOUND_AVAILABLE_TOOL_REGISTRY_RESOLUTION_FAILED: "
            + json.dumps(unresolved_indexes, ensure_ascii=False, default=str)
        )

    (
        available_tools,
        tool_function_cards,
        selected_tool_names,
    ) = _available_tool_cards_from_binding(binding)

    capability_ids = [
        tool_name
        for tool_name in selected_tool_names
        if get_tool_capability(tool_name) is not None
    ]
    snippets = _filter_snippets_to_available_callables(
        resolve_tool_snippets_for_context(
            role=role or "",
            capabilities=list(dict.fromkeys(capability_ids)),
            tool_names=list(dict.fromkeys(capability_ids)),
            file_path=file_path,
            failure_layer=failure_layer,
            error_text=error_text,
            max_snippets=max_snippets,
        ),
        available_tools,
    )

    allowed_import_paths = _stable_unique([
        tool.get("import_path")
        for tool in available_tools
        if isinstance(tool, dict)
    ])
    allowed_function_imports = _stable_unique([
        tool.get("function_name")
        for tool in available_tools
        if isinstance(tool, dict)
    ])
    allowed_helper_imports = _stable_unique([
        tool.get("function_name")
        for tool in available_tools
        if isinstance(tool, dict)
    ])

    return {
        "available_tools": binding["available_tools"],
        "resolved_tools": available_tools,
        "tool_function_cards": tool_function_cards,
        "tool_snippets": snippets,
        "tool_snippet_prompt": tool_snippet_prompt(snippets),
        "selected_tool_names": selected_tool_names,
        "allowed_import_paths": allowed_import_paths,
        "allowed_function_imports": allowed_function_imports,
        "allowed_helper_imports": allowed_helper_imports,
    }

def _project_terminal_sink_schema_to_stdout(
    stdout_schema: dict[str, Any],
    *,
    function_execution_context: dict[str, Any] | None,
    platform_contract: dict[str, Any],
) -> dict[str, Any]:
    projected = dict(stdout_schema or {})

    raw_properties = projected.get("properties")
    properties = {
        str(key): dict(value) if isinstance(value, dict) else {}
        for key, value in (
            raw_properties.items()
            if isinstance(raw_properties, dict)
            else []
        )
    }

    context = (
        function_execution_context
        if isinstance(function_execution_context, dict)
        else {}
    )

    outgoing_edges = [
        dict(edge)
        for edge in (context.get("outgoing_edges") or [])
        if isinstance(edge, dict)
    ]

    schemas_by_output: dict[str, list[dict[str, Any]]] = {}

    for edge in outgoing_edges:
        if str(edge.get("to_node") or "") != "platform_output_node":
            continue

        output_name = str(edge.get("from_output") or "").strip()
        sink_name = str(edge.get("to_input") or "").strip()

        if not output_name or output_name not in properties or not sink_name:
            continue

        sink = get_platform_output_sink(
            platform_contract,
            sink_name,
        )
        if not isinstance(sink, dict):
            continue

        value_schema = sink.get("value_schema")
        if not isinstance(value_schema, dict) or not value_schema:
            continue

        schemas_by_output.setdefault(
            output_name,
            [],
        ).append(dict(value_schema))

    for output_name, schemas in schemas_by_output.items():
        unique = {
            json.dumps(
                schema,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            for schema in schemas
        }

        if len(unique) > 1:
            raise ValueError(
                "conflicting terminal sink schemas for stdout field: "
                f"{output_name}"
            )

        sink_schema = dict(schemas[0])
        existing = dict(properties.get(output_name) or {})

        existing_type = str(existing.get("type") or "").strip()
        sink_type = str(sink_schema.get("type") or "").strip()

        if (
            existing_type
            and sink_type
            and existing_type != sink_type
        ):
            raise ValueError(
                "stdout field contract conflicts with terminal sink: "
                f"{output_name}"
            )

        description = existing.get("description")

        properties[output_name] = {
            **existing,
            **sink_schema,
        }

        if description:
            properties[output_name]["description"] = description

    projected["properties"] = properties
    return projected

def _script_local_contract_payload(
    *,
    file_path: str,
    purpose: str,
    plan_entry: SkillPlanEntry,
    stdout_schema: dict[str, Any],
    requirements: Any = None,
    responsibility_graph: Any = None,
    function_execution_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one code model's local script contract.

    Current File Tool Binding is the authorization truth.

    Rich callable tool contracts are projected dynamically from Registry at
    prompt-construction time.
    """

    canonical_contract = (
        compile_canonical_file_contract(
            plan_entry,
            stdout_schema,
        )
    )

    responsibility_requirements = (
        _script_responsibility_requirements_payload(
            file_path=file_path,
            requirements=requirements,
        )
    )
    planned_must_do = list(plan_entry.must_do or [])
    planned_must_not_do = list(plan_entry.must_not_do or [])

    for item in responsibility_requirements:
        if planned_must_do:
            item["must_do"] = planned_must_do
        if planned_must_not_do:
            item["must_not_do"] = planned_must_not_do
    implementation_resolution = (
        resolve_implementation(
            plan_entry,
            canonical_contract,
        )
    )

    command_argv_contract = (
        _command_argv_contract_for_script(
            file_path,
            "",
            plan_entry,
        )
    )

    tool_binding_summary: dict[
        str,
        Any,
    ] = {}

    if isinstance(
        plan_entry.runtime_contract,
        dict,
    ):
        raw_binding = (
            plan_entry.runtime_contract.get(
                "tool_binding_summary"
            )
        )

        if isinstance(
            raw_binding,
            dict,
        ):
            tool_binding_summary = dict(
                raw_binding
            )

    tool_binding_summary = (
        _ensure_python_script_core_binding(
            tool_binding_summary,
            plan_entry,
        )
    )

    if function_execution_context is None:
        function_execution_context = build_function_execution_context(
            graph=responsibility_graph,
            target_file=file_path,
            current_file_tool_binding=tool_binding_summary,
            fallback_function_item=(responsibility_requirements[0] if responsibility_requirements else {}),
        )
    else:
        function_execution_context = dict(function_execution_context)

    # Runtime argv provenance is frozen by the Interface Contract. The code
    # generator may consume this projection but must never reconstruct a source
    # from an argv key, variable name, filename, or description.
    # Interface fields are projected exclusively from the graph.  SkillPlan and
    # source-code inspection remain implementation inputs, never competing
    # interface authorities.
    graph_projection = None
    if responsibility_graph is not None:
        graph_projection = project_script_interface_contract(
            responsibility_graph, file_path,
        )
        stdout_schema = graph_projection["stdout_schema"]
        command_argv_contract = {
            "command_template": "python " + file_path + " '" + json.dumps(graph_projection["command_payload"], ensure_ascii=False, separators=(",", ":")) + "'",
            "argv_template": graph_projection["command_payload"],
            "argv_keys": list(graph_projection["argv_schema"]["properties"]),
            "argv_schema": graph_projection["argv_schema"],
            "source": "responsibility_graph_interface_contract",
        }

    interface_semantics = _build_interface_semantics(
        plan_entry=plan_entry,
        canonical_contract=canonical_contract,
        function_execution_context=function_execution_context,
    )
    platform_contract = build_platform_io_contract()

    if graph_projection is None:
        stdout_schema = _project_terminal_sink_schema_to_stdout(
            stdout_schema,
            function_execution_context=function_execution_context,
            platform_contract=platform_contract,
        )
    local_function_item = function_execution_context.get(
        "function_item"
    )

    if isinstance(local_function_item, dict):
        local_function_item = dict(local_function_item)
    else:
        local_function_item = {}

    if planned_must_do:
        local_function_item["must_do"] = list(
            planned_must_do
        )

    if planned_must_not_do:
        local_function_item["must_not_do"] = list(
            planned_must_not_do
        )

    if local_function_item:
        function_execution_context[
            "function_item"
        ] = local_function_item

    projection_gaps = (
        _bound_callable_tool_contract_projection_gaps(
            tool_binding_summary
        )
    )

    if projection_gaps:
        raise ValueError(
            "BOUND_CALLABLE_TOOL_CONTRACT_MISSING: "
            "Current File Tool Binding contains "
            "authorized callable tools whose Registry "
            "function contracts cannot be projected "
            "into the code-model prompt. "
            + json.dumps(
                projection_gaps,
                ensure_ascii=False,
                default=str,
            )
        )

    tool_context = build_available_tool_context(
        tool_binding_summary,
        role=plan_entry.role or "",
        file_path=file_path,
        max_snippets=8,
    )
    available_tools = tool_context["available_tools"]
    resolved_tools = tool_context["resolved_tools"]
    tool_function_cards = tool_context["tool_function_cards"]
    tool_snippets = tool_context["tool_snippets"]
    prompt_tool_binding_summary = dict(tool_binding_summary)
    prompt_tool_binding_summary["available_tools"] = available_tools
    prompt_tool_binding_summary["allowed_import_paths"] = tool_context["allowed_import_paths"]
    prompt_tool_binding_summary["allowed_function_imports"] = tool_context["allowed_function_imports"]
    prompt_tool_binding_summary["allowed_helper_imports"] = tool_context["allowed_helper_imports"]
    prompt_runtime_contract = dict(plan_entry.runtime_contract or {})
    prompt_runtime_contract["tool_binding_summary"] = dict(prompt_tool_binding_summary)

    # This is a prompt-only projection, not a SkillPlan/schema mutation.  Show
    # the code model that the planned ports and their bindings are resolved so
    # a short ``inputs`` list cannot be mistaken for an invitation to invent a
    # second caller protocol.
    declared_bindings = list(plan_entry.input_binding or []) or list(
        plan_entry.command_arg_bindings or []
    )
    binding_by_name = {
        str(item.get("argv_key") or item.get("name") or item.get("to_field") or ""): item
        for item in declared_bindings
        if isinstance(item, dict)
    }
    runtime_input_ports = []
    graph_inputs = ([p for p in graph_interface_contract(responsibility_graph)["input_ports"] if p["consumer"] == file_path] if graph_projection is not None else canonical_contract.inputs)
    for raw_input in graph_inputs:
        input_name = str(raw_input.get("name") if isinstance(raw_input, dict) else raw_input)
        binding = binding_by_name.get(input_name, {})
        declared_type = (
            binding.get("value_type")
            or binding.get("type")
            or (raw_input.get("type") if isinstance(raw_input, dict) else None)
            or "unspecified"
        )
        runtime_input_ports.append({
            "name": input_name,
            "declared_schema": raw_input,
            "binding": binding,
            "type": declared_type,
            "binding_status": "resolved",
        })
    capability_guidance = (
        _build_capability_guidance(
            function_execution_context
        )
    )
    if graph_projection is not None:
        validate_graph_interface_projection(
            responsibility_graph,
            file_path,
            {
                "argv_schema": command_argv_contract["argv_schema"],
                "stdout_schema": stdout_schema,
                "runtime_binding": graph_projection["runtime_binding"],
                "platform_output_mapping": graph_projection["platform_output_mapping"],
            },
        )
    return {
        "file_path": file_path,
        "runtime": plan_entry.runtime,
        "language": plan_entry.language,
        "script_goal": purpose,
        "inputs": graph_inputs if graph_projection is not None else canonical_contract.inputs,
        "outputs": ([p for p in graph_interface_contract(responsibility_graph)["output_ports"] if p["producer"] == file_path] if graph_projection is not None else canonical_contract.outputs),
        "interface_semantics": interface_semantics,
        "responsibility_requirements": (
            responsibility_requirements
        ),
        "function_item_graph_context": function_execution_context,
        "capability_guidance": capability_guidance,
        "function_execution_context": function_execution_context,
        "functional_requirements": canonical_contract.functional_requirements,
        "side_effects": canonical_contract.side_effects,
        "upstream_dependencies": canonical_contract.upstream_dependencies,
        "downstream_consumers": canonical_contract.downstream_consumers,
        "available_tools": available_tools,
        "resolved_tools": resolved_tools,
        "tool_function_cards": (
            tool_function_cards
        ),
        "tool_snippets": tool_snippets,
        "tool_snippet_prompt": (
            tool_snippet_prompt(
                tool_snippets
            )
        ),
        "current_file_tool_binding": (
            prompt_tool_binding_summary
        ),
        "allowed_helper_imports": (
            prompt_tool_binding_summary.get(
                "allowed_helper_imports",
                [],
            )
        ),
        "resource_refs": (
            canonical_contract.required_resources
        ),
        "required_resources": canonical_contract.required_resources,
        "output_contract": {
            "stdout_schema": stdout_schema,
            "artifact_contract": (
                canonical_contract
                .artifact_contract
            ),
        },
        "platform_io_contract": platform_contract,
        "platform_io_rules": (
            platform_io_contract_prompt_text()
        ),
        "coverage_requirements": (
            plan_entry.runtime_contract
            or {}
        ).get(
            "coverage_requirements",
            {},
        ),
        "runtime_contract": (
            prompt_runtime_contract
        ),
        "runtime_binding_context": {
            "inputs": runtime_input_ports,
            "input_binding": graph_projection["runtime_binding"] if graph_projection is not None else declared_bindings,
            "authority": "ResponsibilityGraph Interface Contract" if graph_projection is not None else "SkillPlan/runtime_contract/input_binding",
            "runtime_provenance": [binding["runtime_provenance"] for binding in (graph_projection["runtime_binding"] if graph_projection is not None else []) if binding.get("runtime_provenance")],
            "provenance_rule": "Use only declared runtime_provenance; never infer a source from variable names.",
        },
        "command_argv_contract": (
            command_argv_contract
        ),
        "runtime_envelope": {
            "description": (
                "Runtime envelope is only the external transport layer. "
                "It is not a script input schema and must never appear "
                "as an additional wrapper in SKILL.md command JSON."
            ),

            "generic_fields": [],

            "rules": [
                "Do not generate payload/data/options/request wrappers.",
                "Only pass values required by strict_json_argv_schema.",
                "Preserve declared JSON value boundaries."
            ],
            "smoke_note": (
                "Smoke inputs may include real "
                "runtime files or resources. "
                "The script should consume runtime "
                "argv rather than embedding trial "
                "values in business logic."
            ),
        },
        "rules": [
            (
                "Use script_composition: combine "
                "validated argv inputs, local logic, "
                "standard library, and useful "
                "available_tools to satisfy the "
                "current script responsibility."
            ),

            (
                "available_tools and Current File "
                "Tool Binding describe the callable "
                "tools authorized for the current Skill."
            ),

            (
                "Available tools are implementation "
                "candidates, not mandatory execution "
                "requirements. Prefer a suitable hinted "
                "tool when its real contract directly helps, "
                "but selecting zero tools is acceptable when "
                "local logic, the standard library, or an "
                "allowed dependency fully satisfies the "
                "responsibility contract."
            ),

            (
                "Do not call a tool only because the "
                "capability appears in FunctionItem, "
                "required_capabilities, or available_tools."
            ),

            (
                "Capability declarations describe "
                "possible implementation capabilities. "
                "They do not override must_do, "
                "must_not_do, output contracts, runtime "
                "contracts, or deterministic local logic."
            ),

            (
                "Deterministic local implementation, "
                "standard library usage, or existing "
                "runtime logic is valid when it satisfies "
                "the responsibility contract."
            ),

            (
                "Never create fake implementations "
                "when the responsibility contract "
                "explicitly requires an external "
                "capability or tool behavior."
            )
        ],
        "implementation_resolution": {
            "mode": (
                implementation_resolution.mode
            ),
            "required_evidence": (
                implementation_resolution
                .required_evidence
            ),
            "reason": (
                "Implementation composition evidence "
                "only. Callable tool permissions are "
                "represented by "
                "current_file_tool_binding and "
                "available_tools."
            ),
        },
    }


def _creator_file_generation_messages(task_content: str, *, system_rule: str | None = None) -> list[dict]:
    """Return Creator generation messages with the concrete task in user role."""
    rule = system_rule or "你是 Creator 文件内容生成器。只遵守用户消息中的当前文件生成任务；只输出目标文件内容。"
    return [
        {"role": "system", "content": rule},
        {"role": "user", "content": task_content},
    ]


def _ensure_user_visible_task_message(messages: list[dict]) -> list[dict]:
    """Guarantee same-model retries always include a user-visible task message."""
    if any(isinstance(message, dict) and message.get("role") == "user" and str(message.get("content") or "").strip() for message in messages):
        return messages
    task_content = "\n\n".join(
        str(message.get("content") or "")
        for message in messages
        if isinstance(message, dict) and str(message.get("content") or "").strip()
    )
    return _creator_file_generation_messages(task_content)


def _message_role_counts(messages: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "unknown")
        counts[role] = counts.get(role, 0) + 1
    return counts


def _message_role_chars(messages: list[dict], role: str) -> int:
    return sum(
        len(str(message.get("content") or ""))
        for message in messages
        if isinstance(message, dict) and message.get("role") == role
    )

def _existing_script_argv_context_for_skill_md(
    *,
    skill_name: str,
    declared_paths: set[str] | list[str],
    blueprint_text: str = "",
    conversation_history: list[dict] | None = None,
    responsibility_graph: Any = None,
    e2e_verified_bindings_by_script: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    """Collect already generated script input JSON and local graph facts.

    This is advisory context for SKILL.md command block generation.
    It does not infer, rewrite, normalize, or repair business input field names
    from SkillPlan/ResponsibilityGraph; it only transports existing structured facts.
    """
    try:
        skill_dir = settings.skills_path / skill_name
    except Exception:
        return ""

    entries_by_path: dict[str, Any] = {}
    ordered_script_paths: list[str] = []
    try:
        parsed = parse_blueprint([{"role": "assistant", "content": blueprint_text}])
        if getattr(parsed, "skill_plan", None):
            for entry in parsed.skill_plan.files:
                if getattr(entry, "file_type", "") == "script":
                    entries_by_path[str(entry.path)] = entry
                    ordered_script_paths.append(str(entry.path))
    except Exception:
        entries_by_path = {}
        ordered_script_paths = []

    platform_context = build_creator_external_input_context(messages=conversation_history or [])
    declared_prior_stdout_by_path: dict[str, list[str]] = {}
    prior_outputs: set[str] = set()
    for path in ordered_script_paths:
        declared_prior_stdout_by_path[path] = sorted(prior_outputs)
        entry = entries_by_path.get(path)
        prior_outputs.update(str(item) for item in (getattr(entry, "outputs", []) or []) if str(item or "").strip())

    items: list[dict[str, Any]] = []
    for raw_path in sorted(str(path) for path in declared_paths or []):
        script_path = raw_path.replace("\\", "/").strip()
        if not script_path.startswith("scripts/") or not script_path.endswith(".py"):
            continue

        abs_path = skill_dir / script_path
        if not abs_path.is_file():
            continue

        content = ""
        try:
            content = abs_path.read_text(encoding="utf-8")
            schema = extract_python_strict_argv_schema(content)
        except Exception as exc:
            schema = {"error": f"{type(exc).__name__}: {exc}"}

        run_analysis: dict[str, Any] = {}
        try:
            run_analysis = _python_run_args_analysis(content)
        except Exception:
            run_analysis = {}

        try:
            function_execution_context = build_function_execution_context(
                graph=responsibility_graph,
                target_file=script_path,
            )
        except Exception as exc:
            function_execution_context = {"error": f"{type(exc).__name__}: {exc}"}

        command = ""
        try:
            entry = entries_by_path.get(script_path) or _skill_plan_entry_for_file(file_path=script_path, blueprint_text=blueprint_text)
            command = render_script_command_from_skill_plan(entry)
        except Exception:
            command = ""

        snapshot = build_command_alignment_snapshot(
            script_path=script_path,
            script_content=content,
            command=command,
            platform_input_fields=platform_context,
            prior_stdout_fields=declared_prior_stdout_by_path.get(script_path, []),
            function_execution_context=function_execution_context if isinstance(function_execution_context, dict) else {},
            script_defaults=dict(getattr(entry, "default_values", {}) or {}),
            e2e_verified_bindings=(e2e_verified_bindings_by_script or {}).get(script_path, {}),
        )

        items.append({
            "script_path": script_path,

            "command_alignment_snapshot": snapshot,

            "strict_json_argv_schema": schema,

            "argv_value_boundary_contract": {
                "authority": "strict_json_argv_schema",
                "rules": [
                    "argv key names must match exactly",
                    "argv JSON value types must match exactly",
                    "array values must remain JSON arrays",
                    "object values must remain JSON objects",
                    "scalar values must remain scalar",
                    "no wrapper objects are allowed",
                    "no adapter fields such as data/items/files/value are allowed unless declared in schema"
                ]
            },
            "run_args_analysis": run_analysis,
            "function_execution_context": function_execution_context,
            "declared_prior_stdout_fields": declared_prior_stdout_by_path.get(script_path, []),
            "note": (
                "Shared fact snapshot for both SKILL.md writer and judge. "
                "confirmed_bindings are frozen read-only source authority and must be preserved exactly; candidate_bindings are non-authoritative; "
                "available_sources is a candidate domain only for unresolved_target_keys; "
                "do not match by name similarity, hard-code by role/script name, or invent business fields."
            ),
        })

    if not items:
        return ""

    return (
        "已生成脚本输入 JSON 事实（command_alignment_snapshot 是 Writer/Judge 共享字段对齐快照，来自 strict_json_argv_guard / run(args) AST 和 FunctionItem 图谱局部上下文）：\n"
        + json.dumps(items, ensure_ascii=False, indent=2, default=str)
    )

def _bound_callable_tool_contract_projection_gaps(
    binding: dict[str, Any],
) -> list[dict[str, Any]]:
    """Detect available_tools indexes that cannot resolve to Registry facts."""

    gaps: list[dict[str, Any]] = []
    raw_available_tools = (
        binding.get("available_tools")
        if isinstance(binding, dict) and isinstance(binding.get("available_tools"), list)
        else []
    )
    for item in _available_tool_index_for_prompt([
        tool for tool in raw_available_tools if isinstance(tool, dict)
    ]):
        if _registry_tool_from_available_index(item) is None:
            gaps.append({
                "tool_id": item.get("tool_id"),
                "capability_name": item.get("capability_name"),
                "function_name": item.get("function_name"),
                "reason": "available_tools index cannot be resolved from Tool Registry",
            })

    return gaps

def _build_script_generate_file_prompt_variant(
    *,
    file_path: str,
    skill_name: str,
    purpose: str,
    blueprint_text: str,
    role: str | None,
    skill_plan_entry: dict[str, Any] | None,
    requirements: Any = None,
    responsibility_graph: Any = None,
    function_execution_context: dict[str, Any] | None = None,
    variant: str,
) -> list[dict]:
    """Build script-only prompts using progressively smaller local contracts.

    Scripts must not receive the full blueprint, creator UI copy, global kernel
    docs, or E2E/platform workflow text. The platform owns invocation/stdout
    parsing; the model only implements this one file's internals.

    Current File Tool Binding is the single source of truth for callable tools.
    ``implementation_resolution`` is implementation evidence only and must not
    be used as a tool-permission or available-tool source.
    """
    plan_entry = _skill_plan_entry_for_file(
        file_path=file_path,
        purpose=purpose,
        blueprint_text=blueprint_text,
        role=role,
        skill_plan_entry=skill_plan_entry,
    )

    stdout_schema = _script_stdout_schema_for_entry(
        plan_entry
    )
    if (
            isinstance(skill_plan_entry, dict)
            and isinstance(
        skill_plan_entry.get(
            "tool_binding_summary"
        ),
        dict,
    )
    ):
        runtime_contract = dict(
            plan_entry.runtime_contract or {}
        )

        runtime_contract[
            "tool_binding_summary"
        ] = dict(
            skill_plan_entry[
                "tool_binding_summary"
            ]
        )

        object.__setattr__(
            plan_entry,
            "runtime_contract",
            runtime_contract,
        )
    local_contract = (
        _script_local_contract_payload(
            file_path=file_path,
            purpose=purpose,
            plan_entry=plan_entry,
            stdout_schema=stdout_schema,
            requirements=requirements,
            responsibility_graph=responsibility_graph,
            function_execution_context=function_execution_context,
        )
    )

    implementation_payload = (
        local_contract.get(
            "implementation_resolution"
        )
        if isinstance(
            local_contract.get(
                "implementation_resolution"
            ),
            dict,
        )
        else {}
    )

    implementation_mode = str(
        implementation_payload.get("mode")
        or "script_composition"
    )

    if implementation_mode == "unresolved":
        # unresolved 只能说明没有召回到完整专用工具方案，
        # 不能说明脚本无法通过本地组合实现。
        logger.warning(
            "[Creator][script_generation_contract]"
            "[implementation_unresolved_downgraded] "
            "file_path=%s reason=%s",
            file_path,
            str(
                implementation_payload.get("reason")
                or ""
            ),
        )

        implementation_mode = (
            "script_composition"
        )

        implementation_payload[
            "mode"
        ] = "script_composition"

        implementation_payload[
            "unresolved_advisory"
        ] = (
            implementation_payload.get("reason")
            or (
                "no specialized tool resolution; "
                "use script composition"
            )
        )

        local_contract[
            "implementation_resolution"
        ] = implementation_payload

    script_skeleton_text = ""

    if variant in {
        "standard",
        "simplified",
    }:
        script_skeleton_text = (
            _script_generation_skeleton(
                file_path,
                purpose,
                "",
                role=plan_entry.role,
                skill_plan_entry=(
                    skill_plan_entry
                ),
            )
        )

    # Current File Tool Binding-derived top-level contract fields are the
    # callable-tool truth. Do not read available_tools from
    # implementation_resolution.
    selected_tools_payload = (
        local_contract.get("available_tools")
        if isinstance(
            local_contract.get(
                "available_tools"
            ),
            list,
        )
        else []
    )

    tool_function_cards = (
        local_contract.get(
            "tool_function_cards"
        )
        if isinstance(
            local_contract.get(
                "tool_function_cards"
            ),
            list,
        )
        else []
    )

    tool_snippets = (
        local_contract.get("tool_snippets")
        if isinstance(
            local_contract.get(
                "tool_snippets"
            ),
            list,
        )
        else []
    )

    current_binding = (
        local_contract.get(
            "current_file_tool_binding"
        )
        if isinstance(
            local_contract.get(
                "current_file_tool_binding"
            ),
            dict,
        )
        else {}
    )

    available_tool_ids = [
        str(tool.get("tool_id") or "")
        for tool in selected_tools_payload
        if (
            isinstance(tool, dict)
            and str(
                tool.get("tool_id") or ""
            ).strip()
        )
    ]

    allowed_import_paths = list(
        current_binding.get(
            "allowed_import_paths"
        )
        or []
    )

    allowed_function_imports = list(
        current_binding.get(
            "allowed_function_imports"
        )
        or []
    )

    binding_dependencies = list(
        current_binding.get("dependencies")
        or []
    )

    allowed_helper_imports = list(
        current_binding.get(
            "allowed_helper_imports"
        )
        or local_contract.get(
            "allowed_helper_imports"
        )
        or []
    )

    # Keep the complete backend contract intact. This view exists only for this
    # one model call and removes a representation only when its exact source is
    # rendered in a dedicated prompt section below.
    prompt_contract_view = dict(local_contract)
    graph_context = local_contract.get("function_item_graph_context")
    execution_context = local_contract.get("function_execution_context")
    if json.dumps(graph_context, ensure_ascii=False, sort_keys=True, default=str) == json.dumps(
        execution_context, ensure_ascii=False, sort_keys=True, default=str
    ):
        prompt_contract_view.pop("function_execution_context", None)

    # The binding is rendered once in its own hard-constraint section. Its
    # available-tools index and helper allow-lists therefore remain visible
    # there, rather than as duplicate top-level contract fields.
    prompt_contract_view.pop("current_file_tool_binding", None)
    binding_available_tools = current_binding.get("available_tools")
    if json.dumps(prompt_contract_view.get("available_tools"), ensure_ascii=False, sort_keys=True, default=str) == json.dumps(
        binding_available_tools, ensure_ascii=False, sort_keys=True, default=str
    ):
        prompt_contract_view.pop("available_tools", None)
    for key in (
        "allowed_helper_imports",
        "allowed_import_paths",
        "allowed_function_imports",
    ):
        if key in current_binding:
            prompt_contract_view.pop(key, None)

    runtime_contract_view = prompt_contract_view.get("runtime_contract")
    if isinstance(runtime_contract_view, dict):
        runtime_contract_view = dict(runtime_contract_view)
        if json.dumps(runtime_contract_view.get("tool_binding_summary"), ensure_ascii=False, sort_keys=True, default=str) == json.dumps(
            current_binding, ensure_ascii=False, sort_keys=True, default=str
        ):
            runtime_contract_view.pop("tool_binding_summary", None)
        prompt_contract_view["runtime_contract"] = runtime_contract_view

    resolved_tools = local_contract.get("resolved_tools")
    resolved_tools = resolved_tools if isinstance(resolved_tools, list) else []
    compact_code_tools = []
    for tool in resolved_tools:
        if not isinstance(tool, dict):
            continue
        compact_tool = {
            "tool_id": tool.get("tool_id"),
            "capability_name": tool.get("capability_name"),
            "function_name": tool.get("function_name"),
            "import_path": tool.get("import_path"),
            "purpose": (
                tool.get("short_description")
                or tool.get("when_to_use")
                or tool.get("description")
                or ""
            ),
            "signature": tool.get("signature"),
            "input_schema": tool.get("input_schema") or {},
            "output_schema": tool.get("output_schema") or {},
            "return_contract": tool.get("return_contract"),
            "artifact_outputs": tool.get("artifact_outputs") or [],
            "side_effects": tool.get("side_effects") or [],
            "example_call": tool.get("example_call") or tool.get("call_template"),
            "example_return": tool.get("example_return"),
            "example_stdout": tool.get("example_stdout"),
            "common_mistakes": tool.get("common_mistakes") or [],
        }
        if tool.get("usage_policy"):
            compact_tool["usage_policy"] = tool["usage_policy"]
        compact_code_tools.append(compact_tool)

    # Full Registry records, cards, and raw snippets remain in local_contract.
    # The producer sees one compact callable contract and one formatted snippet
    # representation instead.
    for key in (
        "resolved_tools",
        "tool_function_cards",
        "tool_snippets",
        "tool_snippet_prompt",
    ):
        prompt_contract_view.pop(key, None)

    logger.info(
        "[Creator][script_generation_contract] "
        "file_path=%s "
        "inputs=%s "
        "outputs=%s "
        "output_contract=%s "
        "resource_refs=%s "
        "mode=%s "
        "available_tools=%s "
        "tool_function_cards_count=%d "
        "tool_snippets_count=%d "
        "required_evidence=%s "
        "allowed_imports=%s "
        "declared_dependencies=%s "
        "reason=%s "
        "current_file_binding.allowed_helper_imports=%s "
        "allowed_import_paths=%s "
        "allowed_function_imports=%s "
        "runtime_import_guard_result.success=%s "
        "runtime_import_guard_result.error_type=%s",
        file_path,
        json.dumps(
            local_contract.get("inputs")
            or [],
            ensure_ascii=False,
        ),
        json.dumps(
            local_contract.get("outputs")
            or [],
            ensure_ascii=False,
        ),
        json.dumps(
            local_contract.get(
                "output_contract"
            )
            or {},
            ensure_ascii=False,
            sort_keys=True,
        ),
        json.dumps(
            local_contract.get(
                "resource_refs"
            )
            or [],
            ensure_ascii=False,
            sort_keys=True,
        ),
        implementation_mode,
        json.dumps(
            available_tool_ids,
            ensure_ascii=False,
        ),
        len(tool_function_cards),
        len(tool_snippets),
        json.dumps(
            implementation_payload.get(
                "required_evidence"
            )
            or [],
            ensure_ascii=False,
        ),
        json.dumps(
            allowed_import_paths,
            ensure_ascii=False,
        ),
        json.dumps(
            binding_dependencies,
            ensure_ascii=False,
        ),
        str(
            implementation_payload.get("reason")
            or ""
        ),
        json.dumps(
            allowed_helper_imports,
            ensure_ascii=False,
        ),
        json.dumps(
            allowed_import_paths,
            ensure_ascii=False,
        ),
        json.dumps(
            allowed_function_imports,
            ensure_ascii=False,
        ),
        None,
        None,
    )

    instruction = [
        (
            f'你正在为 Skill 包 "{skill_name}" '
            f"生成单个脚本文件：{file_path}。"
        ),
        (
            "目标：生成一个可运行的单文件脚本，"
            "只实现当前文件职责，输出源码本身。"
        ),
        (
            "脚本文件必须读取一个 JSON object argv，"
            "并向 stdout 输出一个 JSON object。"
        ),
        (
            """
            CURRENT SCRIPT RESPONSIBILITY CONTRACT
            
            Before writing code, freeze the responsibility boundary.
            
            The only authority for what this script should implement is:
            
            1. FunctionItem.must_do
            2. FunctionItem.constraints
            3. Incoming ResponsibilityEdges
            4. Outgoing ResponsibilityEdges
            
            Do not expand responsibility because:
            - tool exists
            - capability exists
            - blueprint mentions related actions
            - filename suggests another role
            
            Every generated line of business logic must serve current FunctionItem.
            """
        ),
        (
            "Generated scripts must strictly follow the declared input/output contracts.\n"
            "The contract schema is the only source of truth.\n"
            "Do not infer data structures from variable names, task descriptions, examples, "
            "or common programming assumptions.\n"
            "Runtime contracts are authoritative implementation interfaces."
            "Input variable names, descriptions, and common programming conventions are not"
            "sources of truth for runtime value structure."
            "The generated script must interpret every runtime value according to the"
            "declared contract structure."
            "Do not simplify, reinterpret, or replace a declared runtime structure with an"
            "assumed alternative representation."
            "The implementation should adapt its internal logic to the runtime contract;"
            "the implementation must not redefine the runtime contract."
        ),
        (
            """
            INTERFACE SEMANTICS AUTHORITY
        
            interface_semantics is the semantic projection of the runtime interface.
        
            When implementing inputs:
            1. Use interface_semantics.inputs to understand where runtime values come from.
            2. Do not infer meaning from field names.
            3. Do not create synthetic runtime values.
            4. Do not replace runtime-bound inputs with demo files, placeholders, examples,
               or locally generated samples.
        
            When implementing outputs:
            1. Use interface_semantics.outputs to understand downstream responsibility.
            2. The output object must preserve the declared output contract.
            3. Do not rename fields or create alternative output schemas.
        
            interface_semantics explains the existing runtime contract.
            It does not authorize redesigning the contract.
            """
        ),
        _RUNTIME_BINDING_AUTHORITY_PROMPT,
        (
            "Python scripts/*.py 必须 import 并调用 "
            "strict_json_argv_guard；"
            "它用于声明脚本第一轮 argv 接口，"
            "并在 E2E 中暴露调用映射问题。"
        ),
        (
            "strict_json_argv_guard spec 由当前脚本实现真实需要的"
            "入口参数决定；spec、run(args)、main()/入口逻辑必须内部一致。"
        ),
        (
            "run(args) 只能读取 strict_json_argv_guard 返回的 args；"
            "guard required key 必须被 run(args) 消费。"
        ),
        (
            "参数校验应在核心逻辑前完成；缺失、空值、类型错误或"
            "未知参数应 fail-fast，不输出成功 JSON。"
        ),
        (
            "无输入脚本也应调用 "
            "strict_json_argv_guard(payload, {})，保持统一入口协议。"
        ),
        (
            "当前脚本结构化合同中的 inputs 是该脚本已规划的运行时输入接口。"
            "生成 strict_json_argv_guard 时，应优先直接沿用这些 input 字段名，"
            "不要无必要地重命名、创建同义字段或使用更泛化的字段替代。"
            "脚本内部局部变量名、helper 参数名和 Tool 调用参数名可以自由设计，"
            "不要求与外部接口字段同名。"
        ),
        (
            "command_argv_contract、SKILL.md command、E2E repair trace "
            "如果存在，表示已有运行映射证据；第一轮生成可参考，"
            "但脚本 guard/run(args) 自洽性优先。"
        ),
        (
            "SKILL.md block 应参考脚本 guard spec 形成调用模板；"
            "如果 E2E 发现 block 与 guard 不一致，优先修复运行映射。"
            "只有脚本 guard、run(args)、stdout 或职责实现本身不成立时"
            "才修脚本。"
        ),
        (
            "平台 root/envelope 字段是运行来源，"
            "不是脚本必须采用的参数名；"
            "脚本可以在 guard 后做局部变量转换。"
        ),
        (
            "optional/default/config 参数可以由脚本默认逻辑处理；"
            "required 参数必须由 guard 声明并由 run(args) 实际使用。"
        ),
        (
            "stdout JSON 不得包含 error 字段；"
            "必须覆盖 output_contract.stdout_schema.required 中的字段，"
            "字段名逐字一致且值非空。"
        ),
        (
            "available_tools/custom_tools 的返回值是中间结果；"
            "最终 run(args) 返回的 dict 必须按 "
            "output_contract.stdout_schema.required 组织。"
        ),
        (
            "调用 Tool 前必须按顺序读取当前 compact Tool contract："
            "1. import_path/function_name；2. signature；3. input_schema；"
            "4. return_contract/output_schema；5. example_call；"
            "6. example_return；7. common_mistakes。"
            "不得根据函数名、模型常识或其它 Tool 的示例猜测参数或返回字段。"
        ),
        (
            "Tool helper return 只是中间结果；output_contract.stdout_schema "
            "才是当前 Script 的最终 stdout contract。不得从 Script stdout required fields "
            "反推 Tool 必须返回同名字段；先按 Tool return contract 读取结果，再在脚本本地映射 stdout。"
        ),
        (
            "如果工具返回结构与 stdout_schema.required 不一致，"
            "脚本需要在本地完成语义映射、聚合或格式整理；"
            "只有字段名和语义都满足 required schema 时才能直接转发。"
        ),
        (
            "核心输入必须影响核心输出或产物内容；"
            "不要用空对象、空文件、固定示例或无关默认值绕过职责。"
        ),
        (
            "只根据轻量上下文实现：script_goal、semantic inputs/outputs、"
            "responsibility_requirements、coverage_requirements、"
            "available_tools、compact_code_tools、tool_snippet_prompt、"
            "required_resources、output_contract、"
            "runtime_envelope、rules。"
        ),
        (
            "function_item_graph_context 是当前脚本的共享局部责任图上下文，Producer 和 Judge 使用同一 payload。"
            "FunctionItem describes what the current script owns. Incoming ResponsibilityEdges describe what upstream responsibilities must provide to the current script. Outgoing ResponsibilityEdges describe what the current script must make available to downstream responsibilities. Required edge constraints must be preserved when implementing the current FunctionItem."
            "responsibility_requirements 是当前文件已经编译完成的职责合同，"
            "也是后续单文件职责审查所依据的责任事实。"
            "实现当前脚本时必须完成其中 must_do，遵守 must_not_do；"
            "constraints 是当前文件拥有的开放 responsibility requirements，"
            "所有 required=true constraints 都必须实现；"
            "根据完整 constraint object 理解其语义，不存在固定 constraint vocabulary，"
            "不要忽略不认识的 constraint；"
            "inputs/outputs 表达当前脚本与上下游之间已规划的接口语义。"
            "对外接口应尽量保持一致：strict_json_argv_guard 的 argv keys "
            "优先沿用 inputs；stdout 交付字段应与 outputs / output_contract 保持一致。"
            "脚本内部局部变量、helper 参数和 Tool 参数仍由实现自由决定。"
        ),
        (
            "coverage_requirements 是职责覆盖约束，不是 argv/stdout 字段；"
            "它用于判断脚本是否覆盖声明的输入来源、输入格式、核心动作、"
            "输出形态、参考读取和最终交付义务。"
        ),
        (
            "如果 coverage_requirements 声明多个输入变体，"
            "脚本应实现通用解析或分发逻辑，使核心职责覆盖这些变体。"
        ),
        (
            "如果 coverage_requirements 声明多种输出形态，"
            "stdout 必须通过 output_contract.stdout_schema.required "
            "中的字段交付对应非空结果。"
        ),
        (
            "required_resources 是 Frozen SkillPlan 对当前脚本已确认资源依赖的唯一权威投影。"
            "非空时实现必须按当前职责合理消费这些资源；不得创造不存在的 resource path，"
            "不得从完整 Blueprint prose 重新发现资源，不得把未声明文件当依赖，也不得修改 FilePlan。"
            "Backend 不规定读取或语义消费方式。"
        ),
        (
            "coverage_requirements 只作为职责约束，"
            "不得生成 coverage:*、covered:* 或 "
            "declared_requirement_terms 等运行时字段。"
        ),
        (
            "raw role/capability 只能作为 hint，"
            "不能覆盖 purpose、output_contract、tool binding 和脚本实际职责。"
        ),
        (
            "统一按 script_composition 生成脚本："
            "代码模型根据功能目标组合 argv 输入、本地逻辑、"
            "标准库和 available_tools。"
        ),
        (
            "执行顺序：1. Read script_goal; 2. Read responsibility_requirements.must_do; "
            "3. Read responsibility_requirements.must_not_do; 4. Read responsibility_requirements.constraints; 5. Determine the current file's responsibility closure; "
            "6. Only then inspect available_tools; 7. Prefer a suitable hinted tool whose real function contract directly helps, "
            "but select zero tools when local logic, the standard library, or an allowed dependency can fully implement that responsibility."
        ),
        (
            "available_tools 是 Skill-wide authorized candidate pool，不是当前脚本的责任所有权清单。"
            "Tool availability does not imply responsibility ownership, does not mean the current script should call that Tool, "
            "and cannot expand the current file's responsibility boundary."
        ),
        (
            "Do not perform an extra core action merely because a callable Tool for that action is available. "
            "A Tool may only be used when its real function contract directly contributes to a must_do responsibility of the current file. "
            "If a Tool would create a separate core product assigned to another script, do not call it from the current file."
        ),
        (
            "当前脚本可以定义局部 helper，使用标准库或已允许依赖"
            "完成字段适配、内容组织、格式转换、文件处理和产物组装。"
        ),
        (
            "工具、helper、标准库的组合方式由脚本实现决定；"
            "后续 import、dependency、调用、stdout 或 artifact 问题"
            "由静态检查和 E2E 暴露。"
        ),
        (
            "如果生成 artifact，产物路径、文件名和 helper 返回值"
            "应遵循 output_contract/artifact_contract/"
            "runtime_envelope 中的约定。"
        ),
        (
            "如果当前脚本需要外部文件、上传资源或运行时资源，"
            "应从 JSON argv 或 runtime envelope 中读取；"
            "试运行输入只用于验证处理能力，不应写成业务常量。"
        ),
        (
            "Do not assume that the process working directory equals the Skill root. "
            "When accessing declared references/assets at runtime, resolve the dependency consistently with the current script location, the Skill-relative declared path, and supplied runtime workspace facts. "
            "Do not depend on launcher cwd accidentally matching the Skill root."
        ),
        (
            "第一轮生成时应尽量保持当前 Script contract、argv、stdout "
            "和已知上下游字段一致。E2E probe 用于观察真实执行映射并发现"
            "生成阶段未预见的问题，不是主动偏离当前已知接口的理由。"
        ),
        f"prompt_variant: {variant}",
        "当前文件结构化合同：",
        json.dumps(
            prompt_contract_view,
            ensure_ascii=False,
            indent=2,
        ),
        (
            "Current Skill Tool Pool / Current File Tool Binding"
            "（硬约束）："
        ),
        json.dumps(
            local_contract.get(
                "current_file_tool_binding"
            )
            or {
                "allowed_helper_imports": (
                    local_contract.get(
                        "allowed_helper_imports",
                        [],
                    )
                )
            },
            ensure_ascii=False,
            indent=2,
        ),
        (
            "工具导入边界：只能导入 Current File Tool Binding "
            "中允许的 runtime_tools helper、custom tool import path "
            "或 function；标准库和已允许依赖可用于本地实现。"
        ),
        (
            "工具使用边界：先确定 responsibility closure，再选择真正服务 must_do 的工具；"
            "没有合适工具时使用标准库或已允许依赖完成本地逻辑；"
            "无法完成时返回清晰 blocker。"
        ),
        "Code-callable Tool Contracts（完整授权集合的紧凑调用合同）：",
        json.dumps(
            compact_code_tools,
            ensure_ascii=False,
            indent=2,
        ),
        (
            "动态工具 Snippet 指南（从 registry/manifest 读取，"
            "不硬编码工具名）："
        ),
        (
            "Tool contract 决定真实函数调用方式；Snippet 仅为辅助示例，"
            "不得覆盖 signature、input_schema 或 return_contract。Snippet 缺失时仍以 compact contract 为准。"
        ),
        str(
            local_contract.get(
                "tool_snippet_prompt"
            )
            or "当前脚本可用工具 Snippets: 无"
        ),
    ]

    if variant == "standard":
        instruction.append(
            "不注入完整蓝图、kernel 文档或额外工具清单；"
            "只使用上面的轻量上下文。"
        )

    if script_skeleton_text:
        instruction.extend([
            (
                "固定脚本骨架 / 动态协议骨架"
                "（根据当前 outputs 生成；"
                "输出时应补全为可运行源码）："
            ),
            script_skeleton_text,
        ])

    if variant == "minimal":
        instruction.append(
            "极简要求：返回可运行脚本源码，"
            "import strict_json_argv_guard，"
            "在入口解析 sys.argv[1] 后调用 "
            "strict_json_argv_guard(payload, spec)，"
            "run() 只使用返回的 args，真实处理输入，"
            "成功时打印满足 stdout_schema 的 JSON object。"
        )

    prompt_text = "\n\n".join(instruction)
    prompt_contract_text = json.dumps(prompt_contract_view, ensure_ascii=False, indent=2)
    binding_text = json.dumps(
        local_contract.get("current_file_tool_binding")
        or {"allowed_helper_imports": local_contract.get("allowed_helper_imports", [])},
        ensure_ascii=False,
        indent=2,
    )
    compact_code_tools_text = json.dumps(compact_code_tools, ensure_ascii=False, indent=2)
    formatted_snippets_text = str(local_contract.get("tool_snippet_prompt") or "当前脚本可用工具 Snippets: 无")
    skeleton_chars = len(script_skeleton_text)
    logger.info(
        "[Creator][script_prompt_telemetry] file_path=%s final_prompt_chars=%d "
        "fixed_instruction_chars=%d prompt_contract_view_chars=%d "
        "responsibility_requirements_chars=%d function_graph_context_chars=%d coverage_requirements_chars=%d "
        "runtime_contract_chars=%d platform_io_contract_chars=%d platform_io_rules_chars=%d runtime_envelope_chars=%d "
        "available_tool_index_chars=%d full_resolved_tools_chars=%d compact_code_tools_chars=%d "
        "tool_function_cards_chars=%d raw_tool_snippets_chars=%d formatted_tool_snippets_chars=%d "
        "current_file_tool_binding_chars=%d script_skeleton_chars=%d "
        "resolved_tool_count=%d compact_code_tool_count=%d tool_cards_count=%d snippet_count=%d",
        file_path, len(prompt_text),
        len(prompt_text) - len(prompt_contract_text) - len(binding_text) - len(compact_code_tools_text) - len(formatted_snippets_text) - skeleton_chars,
        len(prompt_contract_text),
        len(json.dumps(local_contract.get("responsibility_requirements", []), ensure_ascii=False, default=str)),
        len(json.dumps(local_contract.get("function_item_graph_context", {}), ensure_ascii=False, default=str)),
        len(json.dumps(local_contract.get("coverage_requirements", {}), ensure_ascii=False, default=str)),
        len(json.dumps(prompt_contract_view.get("runtime_contract", {}), ensure_ascii=False, default=str)),
        len(json.dumps(local_contract.get("platform_io_contract", {}), ensure_ascii=False, default=str)),
        len(str(local_contract.get("platform_io_rules") or "")),
        len(json.dumps(local_contract.get("runtime_envelope", {}), ensure_ascii=False, default=str)),
        len(json.dumps(selected_tools_payload, ensure_ascii=False, default=str)),
        len(json.dumps(resolved_tools, ensure_ascii=False, default=str)),
        len(compact_code_tools_text),
        len("\n\n---\n\n".join(tool_function_cards) if tool_function_cards else "无"),
        len(json.dumps(tool_snippets, ensure_ascii=False, default=str)),
        len(formatted_snippets_text), len(binding_text), skeleton_chars,
        len(resolved_tools), len(compact_code_tools), len(tool_function_cards), len(tool_snippets),
    )

    return _creator_file_generation_messages(
        prompt_text,
        system_rule=(
            "你是 Creator 脚本文件生成器。"
            "只输出单个目标脚本源码；"
            "禁止解释、Markdown fence 或多文件包。"
        ),
    )

def _build_generate_file_prompt(
    file_path: str,
    skill_name: str,
    purpose: str,
    blueprint_text: str,
    conversation_history: list[dict],
    role: str | None = None,
    skill_plan_entry: dict[str, Any] | None = None,
    requirements: Any = None,
    responsibility_graph: Any = None,
    function_execution_context: dict[str, Any] | None = None,
    e2e_verified_bindings_by_script: Mapping[str, Mapping[str, str]] | None = None,
) -> list[dict]:
    """Build a minimal generation prompt for a single Skill file."""

    ext = Path(file_path).suffix.lower()
    lang = _LANG_LABELS.get(ext, "文本")

    if file_path.startswith("scripts/"):
        return (
            _build_script_generate_file_prompt_variant(
                file_path=file_path,
                skill_name=skill_name,
                purpose=purpose,
                blueprint_text=blueprint_text,
                role=role,
                skill_plan_entry=(
                    skill_plan_entry
                ),
                requirements=requirements,
                responsibility_graph=responsibility_graph,
                function_execution_context=function_execution_context,
                variant="standard",
            )
        )

    clean_blueprint_text = _clean_blueprint_for_file_prompt(blueprint_text)
    declared_paths = _authoritative_blueprint_skill_paths(blueprint_text)
    declared_paths_text = "\n".join(f"- {path}" for path in declared_paths) or "- （蓝图未显式列出资源文件）"

    plan_entry = _skill_plan_entry_for_file(
        file_path=file_path,
        purpose=purpose,
        blueprint_text=blueprint_text,
        role=role,
        skill_plan_entry=skill_plan_entry,
    )

    generated_file_contract_text = _build_generated_file_contract_text(
        file_path,
        blueprint_text,
        purpose,
        role=role,
        skill_plan_entry=skill_plan_entry,
    )

    script_skeleton_text = (
        _script_generation_skeleton(
            file_path,
            purpose,
            blueprint_text,
            role=plan_entry.role,
            skill_plan_entry=skill_plan_entry,
        )
        if file_path.startswith("scripts/")
        else ""
    )

    if file_path == "SKILL.md":
        instruction = (
            f'你正在为 Skill 包 "{skill_name}" 生成 SKILL.md 的语义文档。\n\n'
            "只输出单个 SKILL.md 文件内容，不要解释或外层 Markdown fence。\n"
            "文件必须以仅含 name 与 description 的 YAML frontmatter 开始：\n"
            "---\n"
            f"name: {skill_name}\n"
            "description: <一句话说明本 Skill 的用途>\n"
            "---\n\n"
            "LLM 只负责用户可读的语义内容：使用说明、参数含义、输出说明、"
            "脚本职责与调用顺序，以及已声明 references/assets 的用途。\n"
            "禁止生成、复制或修复任何 bash/shell fenced block、python/node/bash 执行命令、"
            "argv JSON、placeholder 或参数拼接示例。命令区是平台根据 Graph Contract、"
            "SkillPlan 与 Script Contract 生成的编译产物，会在模型返回后确定性注入。\n"
            "可以在自然语言中提及已声明脚本路径，但不要描述命令行拼装方式。\n"
            "若 authoritative SkillPlan 声明了 default_values，必须在对应参数说明中写出其真实默认值；"
            "没有默认值时不要编造。平台会把已声明默认值确定性写入命令参数。\n"
            "不要复制 Creator 创建流程、内部合同、确认清单或平台实现细节。\n"
            "只使用 authoritative SkillPlan 已声明的文件，不要从 prose 或示例发明文件。\n\n"
            "Markdown 结构要完整：frontmatter 后至少包含一个正文标题和可执行流程说明。\n\n"
            f"蓝图声明的文件路径：\n{declared_paths_text}\n\n"
            f"已确认蓝图：\n\n{clean_blueprint_text}"
        )

    elif file_path.startswith("scripts/"):
        local_contract = _script_local_contract_payload(
            file_path=file_path,
            purpose=purpose,
            plan_entry=plan_entry,
            stdout_schema=_script_stdout_schema_for_entry(plan_entry),
            function_execution_context=function_execution_context,
        )
        instruction = (
            f'你正在为 Skill 包 "{skill_name}" 生成单个脚本文件：{file_path}。\n\n'
            "只输出完整可运行源码本身；禁止 Markdown fence、解释、文件名标题或多文件输出。\n"
            "第一轮只修当前脚本；不要修改或重规划上下游链路，第二轮 E2E 才修整链路。\n"
            "生成前必须严格基于以下上下文生成脚本："
            "script_goal、inputs、outputs、available_tools、resource_refs、output_contract、rules、"
            "upstream_data_contract、downstream_data_contract、responsibility_edges。"
            "统一按 script_composition 理解：根据功能目标组合 argv 输入、本地逻辑和 available_tools；available_tools 只做候选召回，不是最终裁决。\n"
            "工具/helper 如何组合不作为第一轮 hard gate；如 import/dependency、调用、stdout 或 artifact 失败，再修当前脚本。\n"
            "平台 IO 硬规则：OUTPUT_DIR 本身就是最终输出目录；禁止 OUTPUT_DIR/outputs；禁止 os.path.join(OUTPUT_DIR, \"outputs\") 或 os.path.join(output_dir, \"outputs\")；禁止 replace(\"/tmp/\", \"outputs/\")。\n"
            "helper filename 硬规则：artifact helper 的 filename 只传 basename；禁止 filename=full_path 或 filename=absolute_path。\n"
            "helper 返回硬规则：优先 return result 或原样转发 helper 返回的 artifact/stdout 字段；不要手动重写 helper 返回路径。\n"
            "references/assets 只能作为 resource_refs/asset_refs 读取，不能作为 dependencies、allowed_imports 或 pip install 依赖。\n"
            "生成后第一轮只校验协议 + 运行 + 产物：argv JSON、入口、stdout JSON object、required outputs、artifact_created、import/dependency 和危险系统操作。\n\n"
            "轻量脚本上下文：\n"
            f"{json.dumps(local_contract, ensure_ascii=False, indent=2)}\n\n"
            f"固定脚本骨架（仅约束入口/JSON stdout；输出时补全为可运行源码）：\n{script_skeleton_text}"
            "数据流闭环规则："
            "1. 禁止在脚本中创建未声明的业务输入字段。"
            "2. 禁止通过固定字段名猜测用户数据结构，例如 id、name、date 等。"
            "3. 如果脚本需要某个业务参数，该参数必须来自："
            "(a) 平台输入绑定；"
            "(b) ResponsibilityGraph incoming edge；"
            "(c) 用户明确输入。"
            "4. 如果上述来源不存在，必须通过脚本逻辑自动推断，而不是硬编码默认字段。"
        )

    elif file_path.startswith("references/"):
        instruction = (
            f'你正在为 Skill 包 "{skill_name}" 生成 {file_path} 参考资料文件。\n\n'
            f"职责说明：{purpose}\n\n"
            "要求：\n"
            "1. 只输出 Markdown 文档内容，不要额外的说明文字。\n"
            "2. 不要在文档外套 ``` 代码块。\n"
            "3. 内容应是有实际指导价值的参考资料，不是对参考资料的再描述。\n"
            "生成前请先隐式检查以下 reference 合同，最终输出必须逐项满足：\n"
            f"{generated_file_contract_text}\n\n"
            f"蓝图声明的文件路径：\n{declared_paths_text}\n\n"
            f"以下是已确认的蓝图（参考资料职责说明见 references/ 部分）：\n\n{clean_blueprint_text}"
        )

    elif file_path.startswith("assets/"):
        instruction = (
            f'你正在为 Skill 包 "{skill_name}" 生成 {file_path} 资源文件。\n\n'
            f"职责说明：{purpose}\n\n"
            "要求：\n"
            f"1. 只输出 {lang} 格式的文件内容，不要任何说明文字。\n"
            "2. 不要用 ``` 代码块包裹输出。\n"
            "生成前请先隐式检查以下 asset 合同，最终输出必须逐项满足：\n"
            f"{generated_file_contract_text}\n\n"
            f"蓝图声明的文件路径：\n{declared_paths_text}\n\n"
            f"以下是已确认的蓝图：\n\n{clean_blueprint_text}"
        )

    else:
        instruction = (
            f'你正在为 Skill 包 "{skill_name}" 生成 {file_path} 文件。\n\n'
            f"职责说明：{purpose}\n\n"
            "要求：直接输出文件内容，不要任何解释，不要 Markdown 代码块包裹。\n\n"
            f"蓝图声明的文件路径：\n{declared_paths_text}\n\n"
            f"蓝图：\n\n{clean_blueprint_text}"
        )

    messages: list[dict] = _creator_file_generation_messages(instruction)

    if file_path.startswith("scripts/"):
        return messages

    for msg in conversation_history[-_MAX_HISTORY_TURNS:]:
        if not isinstance(msg, dict) or msg.get("role") not in {"user", "assistant"}:
            continue
        content = str(msg.get("content") or "")
        if msg.get("role") == "assistant" and _CREATOR_FLOW_LEAK_RE.search(content):
            continue
        messages.append({**msg, "content": _clean_blueprint_for_file_prompt(content)})

    return messages



def _local_blueprint_text_for_path(path: str, blueprint_text: str, *, window: int = 600) -> str:
    text = blueprint_text or ""
    idx = text.find(path)
    if idx < 0:
        return ""
    return text[max(0, idx - window): min(len(text), idx + len(path) + window)]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

__all__ = [name for name in globals() if not name.startswith("__")]
