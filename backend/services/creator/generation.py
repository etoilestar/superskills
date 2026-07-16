"""Prompt construction, model calls, content normalization, and file-generation helpers."""

from .common import *  # noqa: F403
from .contracts import *  # noqa: F403
from .e2e import *  # noqa: F403
from .repair import *  # noqa: F403

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
    input_keys = list(plan_entry.inputs or ["payload"])
    output_keys = [key for key in (plan_entry.outputs or []) if isinstance(key, str) and key.strip()]
    if not output_keys:
        output_keys = ["text"]

    component_hint = getattr(plan_entry, "component_hint", "") or getattr(plan_entry, "role", "")
    helper_hint = f"# component_hint: {component_hint}\n"

    if plan_entry.runtime == "node":
        return (
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
            "协议骨架（只约束 $1 JSON argv 与 stdout JSON；具体工具调用必须来自 Tool Registry snippets/function cards）：\n"
            + helper_hint +
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "payload_json=${1:-'{}'}\n"
            f"python -c {shlex.quote(helper)} \"$payload_json\""
        )

    return (
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
        "        # Fill this spec with the actual argv keys used by run(args).\n"
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
        and tool.get("function_name") == "strict_json_argv_guard"
        and tool.get("import_path") == "backend.services.runtime_tools"
        for tool in available_tools
    ):
        available_tools.append({
            "tool_id": "script_argv_guard",
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
    binding["available_tools"] = [
        dict(tool)
        for tool in raw_available_tools
        if isinstance(tool, dict)
    ]

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
    tool_binding_summary["allowed_import_paths"] = tool_context["allowed_import_paths"]
    tool_binding_summary["allowed_function_imports"] = tool_context["allowed_function_imports"]
    tool_binding_summary["allowed_helper_imports"] = tool_context["allowed_helper_imports"]

    return {
        "file_path": file_path,
        "runtime": plan_entry.runtime,
        "language": plan_entry.language,
        "script_goal": purpose,
        "inputs": canonical_contract.inputs,
        "outputs": canonical_contract.outputs,
        "responsibility_requirements": (
            responsibility_requirements
        ),
        "function_item_graph_context": function_execution_context,
        "function_execution_context": function_execution_context,
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
            tool_binding_summary
        ),
        "allowed_helper_imports": (
            tool_binding_summary.get(
                "allowed_helper_imports",
                [],
            )
        ),
        "resource_refs": (
            canonical_contract.resource_refs
        ),
        "output_contract": {
            "stdout_schema": stdout_schema,
            "artifact_contract": (
                canonical_contract
                .artifact_contract
            ),
        },
        "platform_io_contract": (
            build_platform_io_contract()
        ),
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
            plan_entry.runtime_contract
            or {}
        ),
        "command_argv_contract": (
            command_argv_contract
        ),
        "runtime_envelope": {
            "description": (
                "Creator/Skill runtime may provide "
                "a generic JSON argv envelope. "
                "Scripts should read the inputs "
                "required by their own guard/run "
                "contract and may receive external "
                "values from the runtime envelope."
            ),
            "generic_fields": [
                "payload",
                "user_request",
                "fields",
                "options",
                "input_files",
                "files",
                "resources",
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
                "tools currently authorized for the "
                "current Skill."
            ),
            (
                "For every available_tools item, "
                "read description, signature, "
                "input_schema, output_schema, "
                "return_contract, artifact_outputs, "
                "side_effects, examples and "
                "common_mistakes before composing "
                "the call."
            ),
            (
                "Only use tools from current_file_tool_binding.available_tools. "
                "Use the function_name, import_path, input_schema and output_schema "
                "provided by that tool pool; do not use tools, imports, functions, "
                "parameters, or output fields outside that pool."
            ),
            (
                "Treat available tools as candidates only after responsibility_requirements are understood."
            ),
            (
                "Standard-library or allowed local "
                "implementation may be used only for "
                "deterministic local transformations "
                "that do not replace a required model "
                "or external-effect capability with "
                "placeholder behavior."
            ),
            (
                "Never replace an available model "
                "generation or artifact-producing "
                "tool with a fixed template, fake "
                "path, simulated result, or filename "
                "string."
            ),
            (
                "Core inputs must participate in the "
                "produced business result or artifact."
            ),
            (
                "References and assets are runtime "
                "resources, not Python dependency "
                "declarations."
            ),
            (
                "First-round generation implements "
                "the current script; cross-step "
                "execution alignment is verified "
                "by E2E."
            ),
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
            script_defaults=(run_analysis or {}).get("defaulted_keys") or (schema or {}).get("defaulted_keys") or [],
            e2e_verified_bindings=(e2e_verified_bindings_by_script or {}).get(script_path, {}),
        )

        items.append({
            "script_path": script_path,
            "command_alignment_snapshot": snapshot,
            "strict_json_argv_schema": schema,
            "run_args_analysis": run_analysis,
            "function_execution_context": function_execution_context,
            "declared_prior_stdout_fields": declared_prior_stdout_by_path.get(script_path, []),
            "note": (
                "Shared fact snapshot for both SKILL.md writer and judge. "
                "confirmed_bindings must be preserved exactly; candidate_bindings may be checked/adjusted; unresolved_target_keys may be bound only to available_sources; "
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
    """Detect authorized callable tools whose contracts are not prompt-visible."""

    gaps: list[dict[str, Any]] = []

    for tool_id in (
        _tool_ids_from_binding_summary(
            binding
        )
    ):
        capability = get_tool_capability(
            tool_id
        )

        if capability is None:
            gaps.append({
                "tool_id": tool_id,
                "reason": (
                    "bound tool_id is missing "
                    "from Tool Registry"
                ),
            })

            continue

        callable_functions = [
            function
            for function in (
                getattr(
                    capability,
                    "functions",
                    [],
                )
                or []
            )
            if str(
                getattr(
                    function,
                    "import_path",
                    "",
                )
                or ""
            ).strip()
            and str(
                getattr(
                    function,
                    "function_name",
                    "",
                )
                or ""
            ).strip()
        ]

        # Capability-only metadata may legitimately
        # expose no callable function.
        if not callable_functions:
            continue

        cards = function_cards_for_tool(
            capability
        )

        if (
            len(cards)
            < len(callable_functions)
        ):
            gaps.append({
                "tool_id": tool_id,
                "reason": (
                    "authorized callable tool has "
                    "functions but not every callable "
                    "function produced a Tool Function Card"
                ),
                "callable_functions": [
                    str(
                        getattr(
                            function,
                            "function_name",
                            "",
                        )
                        or ""
                    )
                    for function
                    in callable_functions
                ],
                "function_card_count": len(
                    cards
                ),
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
            "脚本第一轮可以选择清晰、稳定的 argv key；"
            "SkillPlan/ResponsibilityGraph/workflow allocation/"
            "local_contract 中的 inputs 只提供语义输入提示和 "
            "SKILL.md block 参考，不是 argv key 白名单。"
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
            "available_tools、tool_function_cards、tool_snippets、"
            "tool_snippet_prompt、resource_refs、output_contract、"
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
            "inputs/outputs 表达语义责任，不要求局部变量名或 argv key "
            "与这些文本逐字一致。"
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
            "如果声明需要读取 reference/resource，"
            "脚本应在当前职责范围内读取并消费；"
            "若缺省不影响核心逻辑，应在 guard/default/metadata 中保持自洽。"
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
            "6. Only then inspect available_tools; 7. Select zero or more tools whose real function contracts directly help implement that responsibility."
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
            "第一轮只生成当前脚本；上下游运行映射、placeholder、"
            "command block 与脚本 guard 的最终对齐由 E2E 处理。"
        ),
        f"prompt_variant: {variant}",
        "当前文件结构化合同：",
        json.dumps(
            local_contract,
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
        (
            "动态工具函数卡片（从 registry/manifest 读取，"
            "不硬编码工具名）："
        ),
        (
            "\n\n---\n\n".join(
                tool_function_cards
            )
            if tool_function_cards
            else "无"
        ),
        (
            "动态工具 Snippet 指南（从 registry/manifest 读取，"
            "不硬编码工具名）："
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

    return _creator_file_generation_messages(
        "\n\n".join(instruction),
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
    declared_paths = _extract_declared_skill_paths(blueprint_text)
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

    skill_md_contract_text = generated_file_contract_text if file_path == "SKILL.md" else ""
    skill_md_e2e_authoring_guide = (
        _build_skill_md_e2e_authoring_guide(blueprint_text)
        if file_path == "SKILL.md"
        else ""
    )

    script_argv_context = (
        _existing_script_argv_context_for_skill_md(
            skill_name=skill_name,
            declared_paths=declared_paths,
            blueprint_text=blueprint_text,
            conversation_history=conversation_history,
            responsibility_graph=responsibility_graph,
            e2e_verified_bindings_by_script=e2e_verified_bindings_by_script,
        )
        if file_path == "SKILL.md"
        else ""
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
            f'你正在为 Skill 包 "{skill_name}" 生成 SKILL.md 文件。\n\n'
            "要求：\n"
            "1. 只输出 SKILL.md 的文件内容，不要任何解释，不要 Markdown 代码块包裹。\n"
            "2. 文件必须以 YAML frontmatter 开始，格式严格如下（冒号后有一个空格）：\n"
            "---\n"
            f"name: {skill_name}\n"
            "description: <一句话说明本 Skill 的用途>\n"
            "---\n"
            "3. frontmatter 闭合后，输出 Skill 的核心执行说明（普通 Markdown 正文）。\n"
            "4. SKILL.md 第一轮只生成静态可解析的使用说明、资源说明和脚本命令块；command 映射必须在第一轮根据脚本探针与责任图谱形成可执行闭环，不能把明确 dataflow 错误留给第二轮 E2E。\n"
            "5. 如果蓝图包含 scripts/ 资源，SKILL.md 正文必须为每个真实 scripts/ 路径提供一个标准、独立、无缩进的 ```bash fenced code block。\n"
            "6. 每个 bash fenced code block 内只能有一条脚本命令；命令必须直接调用 scripts/ 路径。脚本路径后必须紧跟一个完整的输入 JSON object，并使用一对 ASCII 单引号包裹整个 JSON object，使其在 shell 中作为脚本路径后的第一个位置参数传入；JSON object 内部的字段名和字符串值必须继续使用标准 JSON 双引号。该输入 JSON 对应 Python 脚本中的 `sys.argv[1]`。\n"
            "6a. 每个 scripts/*.py command block 附近必须写普通 Markdown action schema 声明：role、inputs、outputs；这些是使用说明，不是运行时 hard schema。\n"
            "6b. 输入 JSON key 必须使用对应脚本真实 strict_json_argv_guard schema 中的字段；如果 guard 不完整，再以 run_args_analysis 和 function_execution_context/function_item_graph_context 为事实依据补足，不能自行编造业务字段或别名。\n"
            "6c. 输入字段是脚本入口接口字段，不是平台字段白名单；输入值必须绑定到平台输入、责任图谱 incoming edge、已排序前序 stdout、reference/assets、literal_default、runtime_constant 或脚本默认值中的真实来源。\n"
            "6d. 不要为同一语义输入同时编造多个别名字段；选定一个输入字段后，command block、输入 JSON 说明和正文说明要一致。\n"
            "6e. 必填动态参数不能写成普通示例字符串、字段名字符串或只重复参数名的字符串；动态值必须使用 `{{...}}` placeholder，并且 placeholder 根节点必须存在于允许来源。\n"
            "7. 第一条脚本命令的动态输入只能引用 platform input envelope 中确定存在的来源，或明确的 literal_default、reference_file、asset_file、runtime_constant；不要引用尚未产生的中间 stdout 字段。\n"
            "8. 如果 Skill 需要业务字段，第一条命令应把平台输入 envelope 中的真实占位符传给脚本，由脚本自行解析；不要在第一轮固定无来源中间字段名。\n"
            "9. 后续命令需要使用上游结果时，必须引用责任图谱 incoming edge 或前序 stdout 中真实存在的字段；不得使用普通字段名字符串冒充流转，也不得引用未在平台输入或前序 stdout 中出现的 placeholder 根节点。\n"
            "10. 输入 JSON 必须是标准 JSON；动态值必须作为 JSON 字符串值出现。运行时输入文件 sentinel 只可用于真实平台运行时文件输入；当责任图谱已声明输入来自前序 stdout 时，禁止改写为运行时输入文件 sentinel。reference/assets 文件使用普通相对路径字符串，模型名使用运行时常量字符串。\n"
            "10a. 每个核心执行命令附近必须写 **输入 JSON 说明**；这是提示词级映射说明，不是硬校验 schema。对每个输入字段说明 type、source_kind、source、required、default（如有）。\n"
            "10b. source_kind 只能用通用类别：platform_input、previous_stdout、reference_file、asset_file、literal_default、runtime_constant、script_default。\n"
            "10c. 普通字符串只允许用于明确的 literal_default、runtime_constant、reference_file、asset_file 或脚本默认值；输入值如果是动态值，应能从平台 input envelope、责任图谱 incoming edge 或前序 stdout 解析。\n"
            "11. 若需要数值默认值，直接写固定 JSON 数字；不要把动态数值 placeholder 裸露在 JSON 中。\n"
            "12. 批量处理、列表处理或多文件处理应由对应脚本内部完成，SKILL.md 静态说明中不展开自然语言循环。\n"
            "13. 列表或对象字段必须通过整值占位符传递；不要写成由无来源拆分字段拼接的列表。\n"
            "14. 如果蓝图包含 references/ 资源，SKILL.md 正文必须在“参考资料/资源”小节明确引用每个 references/ 路径，并说明何时读取。\n"
            "15. 不要在输出内容的外侧套 ``` 代码块，但 SKILL.md 正文内部必须按需包含标准 ```bash fenced code block。\n"
            "16. 禁止只写隐式执行描述；必须写明可执行 fenced block。\n"
            "17. 禁止复制 Creator 界面流程、确认清单、点击开始创建/开始生成、系统将自动创建文件等平台创建流程文案。\n"
            "18. 以下宿主 Markdown 执行说明是内部写作约束，只能转化为面向使用者的 Skill 说明，不要逐字复制这些约束或标题。\n"
            "19. 命令中的 JSON key 是当前脚本读取的输入字段；strict_json_argv_schema、run_args_analysis 和 function_execution_context 是生成 command 映射的事实依据，不只是建议参考。\n"
            "20. 不要在第一轮为下游脚本固定无来源中间字段名；placeholder 来源只能来自平台输入、责任图谱 incoming edge、前序 stdout 或明确静态来源。\n"
            "21. 第一轮不要求声明最终 stdout 字段闭环；但每个 command JSON value 的来源闭环必须按脚本探针和责任图谱成立，脚本 stdout 与平台标准输出字段由第二轮 E2E 真实执行验证。\n"
            "22. SKILL.md 必须覆盖蓝图真实规划的任务、真实脚本路径、资源使用、脚本调用顺序（如有）和最终产物类型；不要固定特定中间字段。\n"
            "23. 真实文件计划需要结合蓝图语境判断：目录结构、SkillPlan path、dependencies、references 字段通常是真实文件计划。\n"
            "24. 如果蓝图在禁止隐式执行、示例、反例、例如、比如等语境中提到某个 scripts/*.py、references/*.md 或 assets/*，它只是解释性示例，不应进入最终 SKILL.md，除非它同时出现在目录结构或 SkillPlan path 中。\n"
            "25. 不要为了满足格式而新增蓝图外脚本；只为蓝图真实规划脚本提供命令块。\n"
            f"{_SKILL_MD_MARKDOWN_EXECUTION_GUIDE}\n\n"
            "已生成脚本输入 JSON 上下文：\n"
            f"{script_argv_context or '当前未读取到已生成脚本的 strict_json_argv_guard schema；按当前可用的脚本计划、run_args_analysis 和 function_execution_context 事实生成第一版 command，避免编造无来源字段。'}\n\n"
            "以下 SKILL.md first-round static authoring guide 约束静态格式、平台边界和 command value 来源闭环；明确 dataflow 映射必须在第一轮按脚本探针和责任图谱成立：\n"
            f"{skill_md_e2e_authoring_guide}\n\n"
            "生成前请先隐式检查以下合同，最终输出必须逐项满足；如果合同要求内部 ```bash block，必须在 SKILL.md 正文中写出该 block：\n"
            f"{skill_md_contract_text}\n\n"
            f"蓝图声明的文件路径（必须覆盖对应 scripts/references 要求）：\n{declared_paths_text}\n\n"
            f"以下是已确认的蓝图（已移除 Creator UI 确认文案），你的内容必须与此一致：\n\n{clean_blueprint_text}"
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
            "生成前只使用以下轻量上下文：script_goal、inputs、outputs、available_tools、resource_refs、output_contract、rules。\n"
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
