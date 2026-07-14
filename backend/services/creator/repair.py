"""Repair scope, diff application, and generated-file repair helpers."""

from .common import *  # noqa: F403
from collections.abc import Mapping, Sequence


def _failure_layer_from_error_text(error_text: str) -> str | None:
    text = (error_text or "").lower()
    if "stdout" in text and "json" in text:
        return "stdout_not_json"
    if "wrapped" in text or "value is object" in text or "expected string" in text:
        return "final_platform_output_value_invalid"
    if "artifact_missing" in text or "does not exist" in text or "missing artifact" in text:
        return "artifact_missing"
    if "artifact_invalid" in text or "invalid artifact" in text:
        return "artifact_invalid"
    if "helper" in text and ("failed" in text or "error" in text):
        return "helper_call_failed"
    if "exit" in text or "traceback" in text:
        return "script_exit"
    return None

@dataclass(frozen=True)
class CreatorRepairScope:
    """Creator 局部 diff 修复权限域。

    注意：
    - 不写平台 IO 字段词表；
    - 不做 argv/stdout 字段穷举；
    - 不做 fake/mock 词表拦截；
    - 平台 IO 兼容性直接交给现有 smoke / sandbox / E2E 真实试运行；
    - 本 scope 只负责 diff 结构安全：单文件、可应用、修改量受控。
    """

    phase: str
    repair_type: str
    target_file: str
    max_changed_lines: int = 160
    allow_format_repair: bool = False
    allow_tool_explore: bool = True
    notes: tuple[str, ...] = ()

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "repair_type": self.repair_type,
            "target_file": self.target_file,
            "max_changed_lines": self.max_changed_lines,
            "allow_format_repair": self.allow_format_repair,
            "allow_tool_explore": self.allow_tool_explore,
            "notes": list(self.notes),
        }


@dataclass
class CreatorDiffProposal:
    """Normalized in-memory source repair proposal.

    主 transport 是 literal OLD/NEW exact_replace envelope。

    Transport parser 会把 literal source text 直接归一化为：

    {
        "old": "当前目标文件中的真实旧片段",
        "new": "准备写回目标文件的真实新片段",
    }

    apply 层不关心 proposal 最初来自：
    - literal exact_replace envelope；
    - legacy exact_replace JSON；
    - unified diff compatibility path。

    apply 层只消费已经归一化后的 target_file / edits / diff。
    """

    target_file: str
    reason: str
    diff: str = ""
    edits: list[dict[str, str]] = field(
        default_factory=list
    )
    raw: dict[str, Any] | None = None
    mode: str = "exact_replace"


def _strip_diff_path_prefix(path: str) -> str:
    value = str(path or "").strip().strip('"').strip("'")
    if value.startswith("a/") or value.startswith("b/"):
        value = value[2:]
    return value.replace("\\", "/")

def _extract_first_json_object_text(text: str) -> str | None:
    """Extract the first balanced JSON object from a noisy model response.

    只做格式抽取，不做业务判断。
    允许模型前后多写解释时仍能抽出 JSON。
    如果模型返回完整代码而不是 JSON，则返回 None。
    """

    source = str(text or "")
    start = source.find("{")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escape = False

    for index in range(start, len(source)):
        ch = source[index]

        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]

    return None


def _strip_outer_code_fence(text: str) -> str:
    value = str(text or "").strip()

    fence_match = re.fullmatch(
        r"```(?:json|diff|patch|text|python|markdown|md)?\s*(.*?)```",
        value,
        flags=re.S | re.I,
    )
    if fence_match:
        return fence_match.group(1).strip()

    return value


def _looks_like_unified_diff(diff_text: str) -> bool:
    value = str(diff_text or "").strip()
    lines = value.splitlines()

    has_old = any(line.startswith("--- ") for line in lines)
    has_new = any(line.startswith("+++ ") for line in lines)
    has_hunk = any(line.startswith("@@ ") for line in lines)

    return has_old and has_new and has_hunk

def _normalize_source_patch_prompt_text(
    text: str,
) -> str:
    """Normalize legacy source-patch wording before model prompting.

    这里只迁移 Creator 自己的 source patch transport 表述。

    不推断：
    - 业务字段；
    - argv key；
    - stdout key；
    - capability；
    - tool；
    - target content。
    """
    value = str(text or "")

    replacements = (
        (
            "优先输出 edits old_lines/new_lines "
            "exact_replace patch",
            "优先输出 literal OLD/NEW exact_replace "
            "patch envelope",
        ),
        (
            "old_lines/new_lines（兼容 old/new）",
            "literal OLD/NEW",
        ),
        (
            "old_lines/new_lines exact_replace",
            "literal OLD/NEW exact_replace",
        ),
        (
            "exact_replace JSON patch",
            "literal exact_replace patch envelope",
        ),
        (
            "old_lines 必须包含",
            "OLD 必须包含",
        ),
        (
            "纳入 old_lines",
            "纳入 OLD",
        ),
        (
            "new_lines",
            "NEW",
        ),
        (
            "old_lines",
            "OLD",
        ),
    )

    for old, new in replacements:
        value = value.replace(old, new)

    return value


def _build_literal_patch_boundary(
    *,
    current_content: str,
    file_path: str,
) -> str:
    """Build a marker token not present in current target content."""
    path_token = re.sub(
        r"[^A-Za-z0-9]+",
        "_",
        str(file_path or ""),
    ).strip("_").upper()

    path_token = (
        path_token or "TARGET"
    )[-40:]

    base = f"CREATOR_PATCH_{path_token}"
    candidate = base
    index = 1

    source = str(current_content or "")

    while candidate in source:
        index += 1
        candidate = f"{base}_{index}"

    return candidate


def _literal_patch_marker(
    boundary: str,
    kind: str,
) -> str:
    return f"<<<{kind}:{boundary}>>>"


def _literal_patch_protocol_example(
    *,
    file_path: str,
    boundary: str,
) -> str:
    """Return a format-only literal patch example."""
    return (
        f"{_literal_patch_marker(boundary, 'CREATOR_PATCH')}\n"
        f"TARGET_FILE: {file_path}\n"
        "REASON: 只修复当前真实失败\n"
        "\n"
        f"{_literal_patch_marker(boundary, 'EDIT')}\n"
        f"{_literal_patch_marker(boundary, 'OLD')}\n"
        "python scripts/example.py "
        "'{\"topic\": \"{{topic}}\"}'\n"
        f"{_literal_patch_marker(boundary, 'NEW')}\n"
        "python scripts/example.py "
        "'{\"topic\": \"{{text_content}}\"}'\n"
        f"{_literal_patch_marker(boundary, 'END_EDIT')}\n"
        "\n"
        f"{_literal_patch_marker(boundary, 'END_PATCH')}"
    )


def _find_literal_patch_marker(
    text: str,
    marker: str,
    *,
    start: int = 0,
) -> re.Match[str] | None:
    """Find one literal-patch marker as a standalone line."""
    pattern = re.compile(
        rf"(?m)^[ \t]*"
        rf"{re.escape(marker)}"
        rf"[ \t]*(?:\r?\n|$)"
    )

    return pattern.search(
        str(text or ""),
        max(0, start),
    )


def _remove_one_protocol_terminal_newline(
    text: str,
) -> str:
    """Remove exactly one envelope separator newline.

    marker 必须独占一行。

    因此 literal payload 与下一个 marker 之间会存在一次
    protocol separator newline。

    这里只删除一次，不 strip 其它空白，避免修改真实源码。
    """
    value = str(text or "")

    if value.endswith("\r\n"):
        return value[:-2]

    if value.endswith("\n"):
        return value[:-1]

    return value


def _extract_literal_exact_replace_proposal(
    text: str,
    *,
    expected_target_file: str,
    boundary: str,
) -> CreatorDiffProposal | None:
    """Parse one literal OLD/NEW exact-replace patch envelope.

    OLD 和 NEW 是目标文件 literal source text。

    不调用 json.loads。
    不做 JSON string unescape。
    不修复 escape。
    不猜模型意图。
    """
    source = str(text or "").strip()

    if not boundary:
        return None

    patch_marker = _literal_patch_marker(
        boundary,
        "CREATOR_PATCH",
    )

    edit_marker = _literal_patch_marker(
        boundary,
        "EDIT",
    )

    old_marker = _literal_patch_marker(
        boundary,
        "OLD",
    )

    new_marker = _literal_patch_marker(
        boundary,
        "NEW",
    )

    end_edit_marker = _literal_patch_marker(
        boundary,
        "END_EDIT",
    )

    end_patch_marker = _literal_patch_marker(
        boundary,
        "END_PATCH",
    )

    if (
        "<<<CREATOR_PATCH:" in source
        and patch_marker not in source
    ):
        raise ValueError(
            "literal patch boundary 不匹配："
            f"expected={boundary!r}"
        )

    patch_start = _find_literal_patch_marker(
        source,
        patch_marker,
    )

    if patch_start is None:
        return None

    patch_end = _find_literal_patch_marker(
        source,
        end_patch_marker,
        start=patch_start.end(),
    )

    if patch_end is None:
        raise ValueError(
            "literal exact_replace patch 缺少 "
            f"{end_patch_marker}"
        )

    if source[
        :patch_start.start()
    ].strip():
        raise ValueError(
            "literal exact_replace patch 前存在额外输出；"
            "只能返回 patch envelope。"
        )

    if source[
        patch_end.end():
    ].strip():
        raise ValueError(
            "literal exact_replace patch 后存在额外输出；"
            "只能返回 patch envelope。"
        )

    body = source[
        patch_start.end():
        patch_end.start()
    ]

    first_edit = _find_literal_patch_marker(
        body,
        edit_marker,
    )

    if first_edit is None:
        raise ValueError(
            "literal exact_replace patch 没有 EDIT。"
        )

    header = body[
        :first_edit.start()
    ]

    target_match = re.search(
        r"(?m)^[ \t]*"
        r"TARGET_FILE:"
        r"[ \t]*(.+?)[ \t]*\r?$",
        header,
    )

    if target_match is None:
        raise ValueError(
            "literal exact_replace patch "
            "缺少 TARGET_FILE。"
        )

    target_file = _strip_diff_path_prefix(
        target_match.group(1)
    )

    if target_file != expected_target_file:
        raise ValueError(
            "literal exact_replace target_file 不匹配："
            f"expected={expected_target_file!r}, "
            f"actual={target_file!r}"
        )

    reason_match = re.search(
        r"(?m)^[ \t]*"
        r"REASON:"
        r"[ \t]*(.*?)[ \t]*\r?$",
        header,
    )

    reason = (
        reason_match.group(1).strip()
        if reason_match
        else "literal exact_replace proposal"
    )

    edits: list[
        dict[str, str]
    ] = []

    cursor = first_edit.start()

    while cursor < len(body):
        if not body[
            cursor:
        ].strip():
            break

        edit_start = _find_literal_patch_marker(
            body,
            edit_marker,
            start=cursor,
        )

        if edit_start is None:
            raise ValueError(
                "literal exact_replace patch "
                "EDIT 结构不完整。"
            )

        if body[
            cursor:
            edit_start.start()
        ].strip():
            raise ValueError(
                "literal exact_replace EDIT "
                "之间存在非法内容。"
            )

        old_start = _find_literal_patch_marker(
            body,
            old_marker,
            start=edit_start.end(),
        )

        if old_start is None:
            raise ValueError(
                "literal exact_replace EDIT "
                "缺少 OLD marker。"
            )

        if body[
            edit_start.end():
            old_start.start()
        ].strip():
            raise ValueError(
                "EDIT 与 OLD marker "
                "之间存在非法内容。"
            )

        new_start = _find_literal_patch_marker(
            body,
            new_marker,
            start=old_start.end(),
        )

        if new_start is None:
            raise ValueError(
                "literal exact_replace EDIT "
                "缺少 NEW marker。"
            )

        end_edit = _find_literal_patch_marker(
            body,
            end_edit_marker,
            start=new_start.end(),
        )

        if end_edit is None:
            raise ValueError(
                "literal exact_replace EDIT "
                "缺少 END_EDIT marker。"
            )

        old = (
            _remove_one_protocol_terminal_newline(
                body[
                    old_start.end():
                    new_start.start()
                ]
            )
        )

        new = (
            _remove_one_protocol_terminal_newline(
                body[
                    new_start.end():
                    end_edit.start()
                ]
            )
        )

        if not old:
            raise ValueError(
                "literal exact_replace OLD 不能为空。"
            )

        edits.append({
            "old": old,
            "new": new,
        })

        cursor = end_edit.end()

    if not edits:
        raise ValueError(
            "literal exact_replace patch "
            "没有有效 edits。"
        )

    return CreatorDiffProposal(
        target_file=target_file,
        reason=reason,
        edits=edits,
        raw={
            "target_file": target_file,
            "reason": reason,
            "transport": (
                "literal_exact_replace"
            ),
            "boundary": boundary,
            "edit_count": len(edits),
        },
        mode="exact_replace",
    )

def _format_diff_response_violation(
    error: Exception,
    raw_text: str,
    *,
    file_path: str,
    boundary: str,
) -> str:
    example = _literal_patch_protocol_example(
        file_path=file_path,
        boundary=boundary,
    )

    return (
        "FORMAT_VIOLATION：上一次输出不是可接受的 "
        "literal repair patch envelope。\n"
        f"解析错误："
        f"{type(error).__name__}: {error}\n\n"
        "重新输出 literal OLD/NEW exact_replace patch。\n"
        "OLD 和 NEW 是目标文件字面源码，不是 JSON string。\n"
        "不要 JSON encode OLD/NEW；"
        "不要为了 transport 给源码中的双引号增加反斜杠。\n"
        "例如目标文件中是 \"，"
        "OLD/NEW 中仍然直接写 \"；"
        "只有目标文件本身真的包含反斜杠时才写反斜杠。\n"
        "{{placeholder}} 是普通 literal source text，"
        "原样复制。\n"
        "必须继续使用完全相同的 boundary。\n"
        "禁止输出完整文件、JSON patch、unified diff、"
        "Markdown fence 或 envelope 外解释。\n\n"
        "协议形态示例（只展示格式，不可照抄示例源码）：\n"
        f"{example}\n\n"
        "上一次输出片段：\n"
        f"{str(raw_text or '')[:4000]}"
    )

class CreatorRepairProposalParseError(ValueError):
    """Structured parse error for repair proposal UI/events."""

    def __init__(
        self,
        message: str,
        *,
        parser_error: str = "",
        last_output_excerpt: str = "",
        diff_extraction_attempted: bool = False,
        lines_fallback_attempted: bool = False,
    ) -> None:
        super().__init__(message)
        self.parser_error = parser_error or message
        self.last_output_excerpt = str(last_output_excerpt or "")[:4000]
        self.diff_extraction_attempted = diff_extraction_attempted
        self.lines_fallback_attempted = lines_fallback_attempted


_PATCH_SCHEMA_KEYS = {
    "target_file", "reason", "edits", "old", "new", "old_lines", "new_lines", "diff", "unified_diff"
}


def _extract_patch_like_json_text(text: str) -> str | None:
    """Return a likely patch-proposal object text without accepting arbitrary JSON."""
    raw = str(text or "")
    first = _extract_first_json_object_text(raw)
    if first and any(f'"{key}"' in first for key in _PATCH_SCHEMA_KEYS):
        return first
    fenced = re.search(r"```(?:json|text)?\s*({[\s\S]*?})\s*```", raw, re.I)
    if fenced and any(f'"{key}"' in fenced.group(1) for key in _PATCH_SCHEMA_KEYS):
        return fenced.group(1)
    return None


def _extract_diff_payload_from_malformed_patch_text(text: str) -> str | None:
    """Conservatively recover only unified-diff payloads from malformed patch proposals."""
    raw = str(text or "")
    for match in re.finditer(r"```(?:diff|patch)?\s*([\s\S]*?)```", raw, re.I):
        candidate = match.group(1).strip()
        if _looks_like_unified_diff(candidate):
            return candidate

    key_match = re.search(r'"(?:diff|unified_diff)"\s*:\s*', raw)
    search_area = raw[key_match.end():] if key_match else raw
    lines = search_area.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("--- ")), None)
    if start is None:
        return None
    diff_lines: list[str] = []
    for line in lines[start:]:
        stripped = line.rstrip()
        if diff_lines and re.match(r'^\s*[,}]\s*$', stripped):
            break
        diff_lines.append(stripped.rstrip('"').rstrip("\\n"))
    candidate = "\n".join(diff_lines).strip()
    return candidate if _looks_like_unified_diff(candidate) else None


def _reject_tool_pool_patch_if_frozen(parsed: dict[str, Any], *, allow_tool_explore: bool) -> None:
    """Raise if the model response includes tool_pool_patch.add_tool_requests and exploration is frozen."""
    if allow_tool_explore:
        return
    patch = parsed.get("tool_pool_patch")
    if isinstance(patch, dict) and patch.get("add_tool_requests"):
        raise ValueError(
            "E2E repair scope: allow_tool_explore=False; "
            "tool_pool_patch.add_tool_requests is strictly forbidden in this phase. "
            "Fix cross-step IO issues only; do not request new tools."
        )


def _coerce_patch_schema_fields(parsed: dict[str, Any], *, expected_target_file: str, allow_tool_explore: bool = True) -> CreatorDiffProposal:
    """Coerce and validate only CreatorDiffProposal schema fields."""
    _reject_tool_pool_patch_if_frozen(parsed, allow_tool_explore=allow_tool_explore)
    allowed = {key: parsed.get(key) for key in _PATCH_SCHEMA_KEYS if key in parsed}
    target_file = _strip_diff_path_prefix(allowed.get("target_file") or expected_target_file)
    if target_file != expected_target_file:
        raise ValueError(
            f"patch target_file 不匹配：expected={expected_target_file!r}, actual={target_file!r}"
        )
    reason = str(allowed.get("reason") or parsed.get("summary") or "").strip()
    edits = allowed.get("edits")
    if isinstance(edits, list) and edits:
        normalized_edits: list[dict[str, str]] = []
        for index, edit in enumerate(edits):
            if not isinstance(edit, dict):
                raise ValueError(f"edits[{index}] 必须是 object。")
            old = edit.get("old")
            new = edit.get("new")
            if old is None and isinstance(edit.get("old_lines"), list):
                if not all(isinstance(line, str) for line in edit["old_lines"]):
                    raise ValueError(f"edits[{index}].old_lines 必须是字符串数组。")
                old = "\n".join(edit["old_lines"])
            if new is None and isinstance(edit.get("new_lines"), list):
                if not all(isinstance(line, str) for line in edit["new_lines"]):
                    raise ValueError(f"edits[{index}].new_lines 必须是字符串数组。")
                new = "\n".join(edit["new_lines"])
            if not isinstance(old, str) or not old:
                raise ValueError(f"edits[{index}].old/old_lines 必须是非空字符串。")
            if not isinstance(new, str):
                raise ValueError(f"edits[{index}].new/new_lines 必须是字符串。")
            normalized_edits.append({"old": old, "new": new})
        return CreatorDiffProposal(target_file=target_file, reason=reason, edits=normalized_edits, raw=allowed, mode="exact_replace")

    diff = str(allowed.get("diff") or allowed.get("unified_diff") or "").strip()
    diff = _strip_outer_code_fence(diff)
    if diff:
        if not _looks_like_unified_diff(diff):
            raise ValueError("JSON 中的 diff 不是 unified diff。")
        old_path, new_path = _unified_diff_target_files(diff)
        if new_path != expected_target_file or old_path not in {expected_target_file, new_path}:
            raise ValueError(f"diff target_file 不匹配：expected={expected_target_file!r}, actual={new_path!r}")
        return CreatorDiffProposal(target_file=target_file, reason=reason, diff=diff, raw=allowed, mode="unified_diff")
    raise ValueError("修复模型返回 JSON，但没有 edits，也没有 diff/unified_diff。")

def _extract_json_or_diff_proposal(
    text: str,
    *,
    expected_target_file: str,
    allow_tool_explore: bool = True,
    literal_boundary: str | None = None,
) -> CreatorDiffProposal:
    """Parse a Creator source-repair proposal.

    Main transport:
    - literal OLD/NEW exact-replace envelope

    Compatibility fallbacks:
    - legacy exact_replace JSON
    - single-file unified diff
    """
    raw_text = str(text or "").strip()

    stripped = _strip_outer_code_fence(
        raw_text
    )

    parser_errors: list[str] = []

    parsed: dict[
        str,
        Any,
    ] | None = None

    diff_extraction_attempted = False
    lines_fallback_attempted = False

    # ---------------------------------------------------------
    # Pass 1: literal OLD/NEW exact_replace
    # ---------------------------------------------------------

    if literal_boundary:
        try:
            literal_proposal = (
                _extract_literal_exact_replace_proposal(
                    raw_text,
                    expected_target_file=(
                        expected_target_file
                    ),
                    boundary=literal_boundary,
                )
            )

        except Exception as exc:
            raise CreatorRepairProposalParseError(
                "修复模型返回了 malformed literal "
                "exact_replace patch envelope。",
                parser_error=(
                    "literal_exact_replace: "
                    f"{type(exc).__name__}: {exc}"
                ),
                last_output_excerpt=raw_text,
                diff_extraction_attempted=False,
                lines_fallback_attempted=False,
            ) from exc

        if literal_proposal is not None:
            return literal_proposal

    # ---------------------------------------------------------
    # Pass 2: legacy strict JSON compatibility
    # ---------------------------------------------------------

    try:
        maybe_json = json.loads(
            stripped
        )

        if isinstance(
            maybe_json,
            dict,
        ):
            parsed = maybe_json

        else:
            parser_errors.append(
                "top-level JSON must be object; "
                f"actual="
                f"{type(maybe_json).__name__}"
            )

    except json.JSONDecodeError as exc:
        parser_errors.append(
            f"{type(exc).__name__}: {exc}"
        )

    # ---------------------------------------------------------
    # Pass 3: extracted legacy patch-like JSON
    # ---------------------------------------------------------

    if parsed is None:
        json_text = (
            _extract_patch_like_json_text(
                raw_text
            )
        )

        if json_text:
            try:
                maybe_json = json.loads(
                    json_text
                )

                if isinstance(
                    maybe_json,
                    dict,
                ):
                    parsed = maybe_json

                else:
                    parser_errors.append(
                        "extracted patch JSON "
                        "must be object"
                    )

            except json.JSONDecodeError as exc:
                parser_errors.append(
                    "extracted JSON: "
                    f"{type(exc).__name__}: {exc}"
                )

    if isinstance(
        parsed,
        dict,
    ):
        lines_fallback_attempted = True

        return _coerce_patch_schema_fields(
            parsed,
            expected_target_file=(
                expected_target_file
            ),
            allow_tool_explore=(
                allow_tool_explore
            ),
        )

    # ---------------------------------------------------------
    # Pass 4: malformed JSON carrying recoverable diff
    # ---------------------------------------------------------

    diff_extraction_attempted = True

    recovered_diff = (
        _extract_diff_payload_from_malformed_patch_text(
            raw_text
        )
    )

    if recovered_diff:
        old_path, new_path = (
            _unified_diff_target_files(
                recovered_diff
            )
        )

        if (
            new_path != expected_target_file
            or old_path not in {
                expected_target_file,
                new_path,
            }
        ):
            raise ValueError(
                "recovered diff target_file 不匹配："
                f"expected="
                f"{expected_target_file!r}, "
                f"actual={new_path!r}"
            )

        return CreatorDiffProposal(
            target_file=expected_target_file,
            reason=(
                "recovered unified diff proposal"
            ),
            diff=recovered_diff,
            raw={
                "target_file": (
                    expected_target_file
                ),
                "diff": recovered_diff,
            },
            mode="unified_diff",
        )

    # ---------------------------------------------------------
    # Pass 5: raw unified diff compatibility
    # ---------------------------------------------------------

    raw_diff = stripped

    fence_match = re.search(
        r"```(?:diff|patch)?\s*(.*?)```",
        raw_text,
        re.S | re.I,
    )

    if fence_match:
        raw_diff = (
            fence_match
            .group(1)
            .strip()
        )

    if not _looks_like_unified_diff(
        raw_diff
    ):
        raise CreatorRepairProposalParseError(
            "修复模型没有返回 literal exact_replace "
            "patch envelope，也没有返回兼容的 "
            "legacy patch JSON 或 raw unified diff。",
            parser_error=(
                " | ".join(parser_errors)
                or (
                    "no supported patch "
                    "proposal found"
                )
            ),
            last_output_excerpt=raw_text,
            diff_extraction_attempted=(
                diff_extraction_attempted
            ),
            lines_fallback_attempted=(
                lines_fallback_attempted
            ),
        )

    old_path, new_path = (
        _unified_diff_target_files(
            raw_diff
        )
    )

    if new_path != expected_target_file:
        raise ValueError(
            "raw diff target_file 不匹配："
            f"expected={expected_target_file!r}, "
            f"actual={new_path!r}"
        )

    if old_path not in {
        expected_target_file,
        new_path,
    }:
        raise ValueError(
            "raw diff old file 不匹配："
            f"expected={expected_target_file!r}, "
            f"actual={old_path!r}"
        )

    return CreatorDiffProposal(
        target_file=expected_target_file,
        reason="raw unified diff proposal",
        diff=raw_diff,
        raw=None,
        mode="unified_diff",
    )


def _unified_diff_target_files(diff_text: str) -> tuple[str, str]:
    old_path = ""
    new_path = ""

    for line in str(diff_text or "").splitlines():
        if line.startswith("--- "):
            old_path = _strip_diff_path_prefix(line[4:].split("\t", 1)[0].strip())
        elif line.startswith("+++ "):
            new_path = _strip_diff_path_prefix(line[4:].split("\t", 1)[0].strip())
            break

    if not old_path or not new_path:
        raise ValueError("unified diff 缺少 --- / +++ 文件头。")

    if old_path == "/dev/null" or new_path == "/dev/null":
        raise ValueError("局部修复不允许通过 diff 新增或删除文件。")

    return old_path, new_path



_NORMALIZE_TRANSLATION = str.maketrans({
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "，": ",", "。": ".", "：": ":", "；": ";", "！": "!", "？": "?",
    "（": "(", "）": ")", "【": "[", "】": "]", "［": "[", "］": "]",
    "｛": "{", "｝": "}", "《": "<", "》": ">", "、": ",",
    "—": "-", "–": "-", "－": "-", "…": "...",
})


def _normalize_text_with_spans(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Normalize text for conservative matching while retaining original spans."""

    normalized: list[str] = []
    spans: list[tuple[int, int]] = []
    pending_space_start: int | None = None
    pending_space_end: int | None = None

    def flush_space() -> None:
        nonlocal pending_space_start, pending_space_end
        if pending_space_start is None or pending_space_end is None:
            return
        if normalized and normalized[-1] != " ":
            normalized.append(" ")
            spans.append((pending_space_start, pending_space_end))
        pending_space_start = None
        pending_space_end = None

    for pos, ch in enumerate(text or ""):
        if ch.isspace():
            if pending_space_start is None:
                pending_space_start = pos
            pending_space_end = pos + 1
            continue
        flush_space()
        mapped = ch.translate(_NORMALIZE_TRANSLATION)
        for mapped_ch in mapped:
            normalized.append(mapped_ch)
            spans.append((pos, pos + 1))

    while normalized and normalized[0] == " ":
        normalized.pop(0)
        spans.pop(0)
    while normalized and normalized[-1] == " ":
        normalized.pop()
        spans.pop()
    return "".join(normalized), spans


def _find_unique_normalized_span(content: str, old: str) -> tuple[int, int] | None:
    norm_content, spans = _normalize_text_with_spans(content)
    norm_old, _ = _normalize_text_with_spans(old)
    if not norm_old:
        return None
    starts = [m.start() for m in re.finditer(re.escape(norm_old), norm_content)]
    if len(starts) != 1:
        return None
    norm_start = starts[0]
    norm_end = norm_start + len(norm_old) - 1
    return spans[norm_start][0], spans[norm_end][1]


def _is_approximate_replace_allowed(target_file: str) -> bool:
    """Return whether approximate replacement may be attempted for a decoded text target.

    Patch safety is intentionally independent of file extension.  Callers have
    already constrained the repair to one target_file and supplied decoded text;
    uniqueness, similarity, scope limits, real-diff checks, and post-apply
    validators provide the safety boundary.
    """
    return bool(_strip_diff_path_prefix(target_file))


def _is_markdown_file(target_file: str) -> bool:
    path = _strip_diff_path_prefix(target_file).lower()
    return path == "skill.md" or path.startswith("references/") or path.endswith((".md", ".markdown"))


def _markdown_fence_ranges(content: str) -> list[tuple[int, int, str]]:
    """Return fenced code block char ranges with info string.

    Kept for Markdown-specific validators/diagnostics.  Fuzzy patch permission
    must not depend on these ranges or on any extension allow/deny list.
    """
    ranges: list[tuple[int, int, str]] = []
    offset = 0
    open_start: int | None = None
    open_info = ""
    for raw_line in (content or "").splitlines(keepends=True):
        line_start = offset
        stripped = raw_line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            if open_start is None:
                open_start = line_start
                open_info = stripped[3:].strip().lower()
            elif stripped.startswith(marker):
                ranges.append((open_start, line_start + len(raw_line), open_info))
                open_start = None
                open_info = ""
        offset += len(raw_line)
    if open_start is not None:
        ranges.append((open_start, len(content or ""), open_info))
    return ranges


def _span_inside_command_fence(content: str, start: int, end: int) -> bool:
    for fence_start, fence_end, info in _markdown_fence_ranges(content):
        if start >= fence_start and end <= fence_end and _is_shell_fence_info(info):
            return True
    return False


def _span_crosses_markdown_boundary(content: str, start: int, end: int) -> bool:
    """Diagnostic helper retained for optional Markdown validators only."""
    snippet = content[start:end]
    if re.search(r"(?m)^#{1,6}\s+", snippet):
        return True
    crossings = 0
    for fence_start, fence_end, _info in _markdown_fence_ranges(content):
        if start < fence_start < end or start < fence_end < end:
            crossings += 1
    return crossings > 0


def _line_boundary_span(content: str, start: int, end: int) -> tuple[int, int]:
    line_start = content.rfind("\n", 0, start) + 1
    line_end = content.find("\n", end)
    if line_end < 0:
        line_end = len(content)
    else:
        line_end += 1
    return line_start, line_end


def _ensure_block_text(text: str, *, before: bool = False) -> str:
    value = str(text or "")
    if not value:
        return ""
    if before:
        if not value.endswith("\n"):
            value += "\n"
    else:
        if not value.startswith("\n"):
            value = "\n" + value
        if not value.endswith("\n"):
            value += "\n"
    return value


def _patch_match_policy_for_file(target_file: str, old: str) -> str:
    """Compatibility label for stats; not a permission policy."""
    return "generic_text"


def _resolve_patch_span(
    *,
    content: str,
    old: str,
    target_file: str,
    edit_index: int,
) -> dict[str, Any]:
    """Resolve old text to one safe replacement span using generic text matching.

    All decoded target_file text follows the same sequence: exact, normalized,
    approximate/fuzzy, then whole-file fallback when the proposal clearly
    describes the full current file.  Extension-specific allow/deny lists are
    deliberately not used here.
    """
    count = content.count(old)
    if count == 1:
        start = content.find(old)
        return {
            "start": start,
            "end": start + len(old),
            "fallback_type": "exact",
            "similarity": None,
            "matched_excerpt": old,
            "policy": "generic_text",
        }
    if count > 1:
        raise ValueError(
            f"edits[{edit_index}].old 在当前文件中匹配了 {count} 次。"
            "请提供更长 old 片段，保证唯一匹配。"
        )

    normalized_span = _find_unique_normalized_span(content, old)
    if normalized_span is not None:
        start, end = normalized_span
        return {
            "start": start,
            "end": end,
            "fallback_type": "normalized_exact",
            "similarity": 1.0,
            "matched_excerpt": _excerpt(content, start, end),
            "policy": "generic_text",
        }

    approx = _find_approximate_substring_span(content, old)
    if approx.get("accepted"):
        start = int(approx["start"])
        end = int(approx["end"])

        if _strip_diff_path_prefix(target_file).lower() == "skill.md":
            starts_at_line_boundary = start == 0 or content[start - 1] == "\n"
            ends_at_line_boundary = end == len(content) or content[end] == "\n"

            if _span_crosses_markdown_boundary(content, start, end):
                raise ValueError(
                    f"edits[{edit_index}] 的 fuzzy 匹配"
                    "跨越了 Markdown 结构边界，"
                    "请提供更准确的 OLD。"
                )

            if "\n" in old and not (
                starts_at_line_boundary
                and ends_at_line_boundary
            ):
                raise ValueError(
                    f"edits[{edit_index}] 的多行 fuzzy 匹配"
                    "没有覆盖完整行，"
                    "请提供更准确的 OLD。"
                )

        return {
            "start": start,
            "end": end,
            "fallback_type": "fuzzy_window",
            "similarity": float(approx["similarity"]),
            "matched_excerpt": str(approx.get("matched_excerpt") or ""),
            "policy": "generic_text",
            "second_similarity": float(approx.get("second_similarity") or 0),
        }

    full_similarity = difflib.SequenceMatcher(
        None,
        _normalize_text_with_spans(old)[0],
        _normalize_text_with_spans(content)[0],
        autojunk=False,
    ).ratio()
    if full_similarity >= 0.92:
        return {
            "start": 0,
            "end": len(content),
            "fallback_type": "full_file_fallback",
            "similarity": full_similarity,
            "matched_excerpt": _excerpt(content, 0, len(content)),
            "policy": "generic_text",
        }

    raise ValueError(
        f"edits[{edit_index}].old 在当前文件中没有 exact/normalized/fuzzy 唯一可靠匹配。"
        f"reason={approx.get('reason')}; "
        f"similarity={float(approx.get('similarity') or 0):.3f}; "
        f"second_similarity={float(approx.get('second_similarity') or 0):.3f}; "
        f"full_file_similarity={full_similarity:.3f}; "
        "请提供更长唯一 old，或提交与当前全文高度一致的 full-file fallback。\n"
        "最相近候选原文片段如下，可在下一轮直接复制为 old：\n"
        "```text\n"
        f"{approx.get('matched_excerpt') or ''}\n"
        "```"
    )


def _extract_approximate_anchors(query: str) -> list[str]:
    patterns = [
        r"(?:[\w.-]+/)+[\w.-]+",
        r"`([^`]{3,120})`",
        r"https?://[^\s)\]>'\"]+",
        r"[\"']([^\"']{6,120})[\"']",
        r"[\u4e00-\u9fff]{4,}",
        r"[A-Za-z0-9_./:-]{8,}",
    ]
    anchors: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, query or ""):
            value = next((g for g in match.groups() if g), match.group(0))
            if value and value not in anchors:
                anchors.append(value)
    return anchors[:20]


def _excerpt(text: str, start: int, end: int, limit: int = 600) -> str:
    value = (text or "")[max(0, start):min(len(text or ""), end)]
    if len(value) <= limit:
        return value
    half = max(1, limit // 2)
    return value[:half] + "\n…\n" + value[-half:]


def _find_approximate_substring_span(content: str, query: str) -> dict[str, Any]:
    norm_content, content_spans = _normalize_text_with_spans(content)
    norm_query, _ = _normalize_text_with_spans(query)
    if not norm_content or not norm_query:
        return {"accepted": False, "reason": "empty_normalized_query_or_content"}

    q_len = len(norm_query)
    candidate_ranges: set[tuple[int, int]] = set()
    matcher = difflib.SequenceMatcher(None, norm_query, norm_content, autojunk=False)
    for block in matcher.get_matching_blocks():
        if block.size < max(4, min(20, q_len // 8)):
            continue
        base = block.b - block.a
        for delta in (-q_len // 10, 0, q_len // 10):
            start = max(0, base + delta)
            end = min(len(norm_content), start + q_len)
            if end > start:
                candidate_ranges.add((start, end))

    for anchor in _extract_approximate_anchors(query):
        norm_anchor, _ = _normalize_text_with_spans(anchor)
        if not norm_anchor:
            continue
        anchor_offset = norm_query.find(norm_anchor)
        if anchor_offset < 0:
            continue
        search_from = 0
        while True:
            pos = norm_content.find(norm_anchor, search_from)
            if pos < 0:
                break
            base = pos - anchor_offset
            for scale in (0.8, 1.0, 1.2):
                length = max(1, int(q_len * scale))
                start = max(0, base - max(0, length - q_len) // 2)
                end = min(len(norm_content), start + length)
                candidate_ranges.add((start, end))
            search_from = pos + 1

    scored: list[dict[str, Any]] = []
    for start, end in candidate_ranges:
        cand = norm_content[start:end]
        similarity = difflib.SequenceMatcher(None, norm_query, cand, autojunk=False).ratio()
        if start < len(content_spans) and end - 1 < len(content_spans):
            orig_start, orig_end = content_spans[start][0], content_spans[end - 1][1]
            scored.append({
                "start": orig_start,
                "end": orig_end,
                "similarity": similarity,
                "matched_excerpt": _excerpt(content, orig_start, orig_end),
            })

    if not scored:
        return {"accepted": False, "reason": "no_candidate_substring"}

    scored.sort(key=lambda item: item["similarity"], reverse=True)
    best = scored[0]

    def substantially_overlaps(left: dict[str, Any], right: dict[str, Any]) -> bool:
        overlap = max(0, min(left["end"], right["end"]) - max(left["start"], right["start"]))
        shortest = max(1, min(left["end"] - left["start"], right["end"] - right["start"]))
        return overlap / shortest >= 0.8

    distinct_best_spans = [
        item for item in scored
        if item["similarity"] >= best["similarity"] - 1e-9 and not substantially_overlaps(best, item)
    ]
    second_similarity = next((item["similarity"] for item in scored[1:] if not substantially_overlaps(best, item)), 0.0)
    best["second_similarity"] = second_similarity
    best["candidate_count"] = len(scored)

    if best["similarity"] < 0.88:
        best["accepted"] = False
        best["reason"] = "best_similarity_below_threshold"
    elif second_similarity and best["similarity"] - second_similarity < 0.05:
        best["accepted"] = False
        best["reason"] = "best_second_best_margin_too_small"
    elif distinct_best_spans:
        best["accepted"] = False
        best["reason"] = "matched_span_not_unique"
    else:
        best["accepted"] = True
        best["reason"] = "accepted"
    return best

class CreatorRepairNoopPatch(ValueError):
    """Raised when a proposal contains no effective edits and should not consume normal retries."""

def _locate_failure_evidence_span(content: str, evidence: str) -> tuple[int, int, str] | None:
    evidence = str(evidence or "").strip()
    if not evidence:
        return None
    count = content.count(evidence)
    if count == 1:
        start = content.find(evidence)
        return start, start + len(evidence), "exact"
    span = _find_unique_normalized_span(content, evidence)
    if span is not None:
        return span[0], span[1], "normalized_exact"
    approx = _find_approximate_substring_span(content, evidence)
    if approx.get("accepted"):
        return int(approx["start"]), int(approx["end"]), "approximate_substring"
    return None


def _build_deterministic_patch_from_failure(
    *,
    failure: Mapping[str, Any],
    current_content: str,
    target_file: str,
) -> CreatorDiffProposal | None:
    """Build a schema-safe micro patch from structured failure fields only."""
    failure_target = str(failure.get("target_file") or failure.get("target") or target_file)
    if failure_target not in {target_file, "SKILL.md"} and not failure_target.startswith(f"{target_file}:"):
        return None
    repair_ops = failure.get("repair_ops")
    if isinstance(repair_ops, Mapping):
        ops = [repair_ops]
    elif isinstance(repair_ops, Sequence) and not isinstance(repair_ops, (str, bytes)):
        ops = [op for op in repair_ops if isinstance(op, Mapping)]
    else:
        return None
    edits: list[dict[str, str]] = []
    for repair_op in ops:
        op = str(repair_op.get("op") or "").lower()
        if op not in {"replace", "delete", "append_after", "append_before"}:
            continue
        anchor = repair_op.get("anchor") or repair_op.get("evidence")
        if not isinstance(anchor, str) or not anchor:
            continue
        located = _locate_failure_evidence_span(current_content, anchor)
        if located is None:
            continue
        start, end, _kind = located
        if _is_markdown_file(target_file) and op in {"append_after", "append_before"}:
            start, end = _line_boundary_span(current_content, start, end)
        old = current_content[start:end]
        if op == "replace":
            new = repair_op.get("replacement") if "replacement" in repair_op else repair_op.get("new")
        elif op == "delete":
            new = ""
        elif op == "append_after":
            new = old.rstrip("\n") + _ensure_block_text(str(repair_op.get("text") or "")) if _is_markdown_file(target_file) else old + str(repair_op.get("text") or "")
        else:
            new = _ensure_block_text(str(repair_op.get("text") or ""), before=True) + old.lstrip("\n") if _is_markdown_file(target_file) else str(repair_op.get("text") or "") + old
        if isinstance(new, str) and old != new:
            edits.append({"old": old, "new": new})
    if not edits:
        return None
    return CreatorDiffProposal(
        target_file=target_file,
        reason="deterministic micro patch from structured failure repair_ops",
        edits=edits,
        raw={"target_file": target_file, "edits": edits},
        mode="exact_replace",
    )


def _apply_deterministic_micro_patch_if_safe(
    *,
    failures: Sequence[Mapping[str, Any]] | None,
    current_content: str,
    scope: CreatorRepairScope,
) -> tuple[CreatorDiffProposal, str, dict[str, Any]] | None:
    all_edits: list[dict[str, str]] = []
    skipped: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for failure in failures or []:
        proposal = _build_deterministic_patch_from_failure(
            failure=failure,
            current_content=current_content,
            target_file=scope.target_file,
        )
        if proposal is None:
            unresolved.append({"target": failure.get("target") or failure.get("target_file"), "reason": "no_applicable_repair_ops"})
            continue
        for edit in proposal.edits:
            if edit in all_edits:
                skipped.append({"reason": "duplicate", "old_chars": len(edit.get("old", ""))})
                continue
            if edit.get("old") == edit.get("new"):
                skipped.append({"reason": "noop", "old_chars": len(edit.get("old", ""))})
                continue
            all_edits.append(edit)
    if not all_edits:
        return None
    batched = CreatorDiffProposal(
        target_file=scope.target_file,
        reason="batched deterministic micro patches from structured failure repair_ops",
        edits=all_edits,
        raw={"target_file": scope.target_file, "edits": all_edits},
        mode="exact_replace",
    )
    candidate, stats = _validate_repair_diff_scope(
        proposal=batched,
        current_content=current_content,
        scope=scope,
    )
    stats["mode"] = "deterministic_micro_patch_batch"
    stats["repair_ops"] = {
        "applied": len(stats.get("applied") or []),
        "unresolved": len(unresolved),
        "skipped": len(skipped) + int(stats.get("skipped_noop_count") or 0),
        "unresolved_items": unresolved[:20],
        "skipped_items": skipped[:20],
    }
    return batched, candidate, stats


def _apply_exact_replace_patch(
    *,
    original_content: str,
    proposal: CreatorDiffProposal,
    expected_target_file: str,
) -> tuple[str, dict[str, Any]]:
    """Apply exact old/new replacement patch safely.

    系统级原则：
    - target_file 必须匹配；
    - old 必须非空；
    - old 必须唯一匹配；
    - 单个 no-op edit 不应让整轮 repair 崩溃；
    - 如果所有 edits 都是 no-op，仍然失败；
    - 应用后必须产生真实 diff。
    """

    if proposal.target_file != expected_target_file:
        raise ValueError(
            f"patch target_file 不匹配：expected={expected_target_file!r}, actual={proposal.target_file!r}"
        )

    if not proposal.edits:
        raise ValueError("exact_replace patch 必须包含非空 edits。")

    candidate = original_content
    applied: list[dict[str, Any]] = []
    skipped_noop: list[dict[str, Any]] = []

    for index, edit in enumerate(proposal.edits):
        old = edit.get("old", "")
        new = edit.get("new", "")

        if not isinstance(old, str) or not old:
            raise ValueError(f"edits[{index}].old 必须是非空字符串。")

        if not isinstance(new, str):
            raise ValueError(f"edits[{index}].new 必须是字符串。")

        if old == new:
            skipped_noop.append({
                "index": index,
                "old_chars": len(old),
                "reason": "old 与 new 完全相同，已跳过该 no-op edit。",
            })
            continue

        resolved = _resolve_patch_span(
            content=candidate,
            old=old,
            target_file=expected_target_file,
            edit_index=index,
        )
        replace_start = int(resolved["start"])
        replace_end = int(resolved["end"])
        fallback_type = str(resolved.get("fallback_type") or "exact")
        similarity = resolved.get("similarity")
        matched_excerpt = str(resolved.get("matched_excerpt") or old)
        original_span = candidate[replace_start:replace_end]
        if original_span == new:
            skipped_noop.append({
                "index": index,
                "old_chars": len(old),
                "reason": "匹配到的原文 span 与 new 完全相同，已跳过该 no-op edit。",
            })
            continue

        if expected_target_file == "SKILL.md" and fallback_type == "fuzzy_window":
            if original_span.startswith("\n") and not new.startswith("\n"):
                raise ValueError("SKILL.md fuzzy replacement 丢失了开头换行")

            if original_span.endswith("\n") and not new.endswith("\n"):
                raise ValueError("SKILL.md fuzzy replacement 丢失了结尾换行")

        candidate = candidate[:replace_start] + new + candidate[replace_end:]
        applied.append({
            "index": index,
            "old_chars": len(old),
            "new_chars": len(new),
            "fallback_type": fallback_type,
            "similarity": similarity,
            "matched_excerpt": matched_excerpt[:1000],
            "original_model_old_excerpt": old[:1000],
            "match_policy": resolved.get("policy"),
        })

    if not applied:
        raise CreatorRepairNoopPatch(
            "proposal_noop：exact_replace patch 没有任何真实 edit。"
            "所有 edits 都是 no-op，old 与 new 完全相同。"
            "请提交会真实改变当前失败内容的 patch。"
        )

    diff = "".join(
        difflib.unified_diff(
            original_content.splitlines(keepends=True),
            candidate.splitlines(keepends=True),
            fromfile=f"a/{expected_target_file}",
            tofile=f"b/{expected_target_file}",
        )
    )

    if not diff.strip():
        raise ValueError(
            "exact_replace patch 应用后没有产生任何变化。"
            "请检查 old/new 是否完全相同，或是否修改了与当前失败无关的片段。"
        )

    changed_line_count = sum(
        1
        for line in diff.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )

    return candidate, {
        "mode": "exact_replace",
        "edit_count": len(applied),
        "skipped_noop_count": len(skipped_noop),
        "applied": applied,
        "skipped_noop": skipped_noop,
        "changed_line_count": changed_line_count,
        "generated_diff_chars": len(diff),
        "generated_diff_excerpt": diff[:2000],
    }


def _apply_single_file_unified_diff(
    *,
    original_content: str,
    diff_text: str,
    expected_target_file: str,
) -> tuple[str, dict[str, Any]]:
    """Apply a single-file unified diff in memory.

    不调用系统 patch。
    不允许多文件修改。
    不在这里判断平台 IO。
    平台 IO 由后续 smoke / E2E sandbox 试运行判断。
    """

    old_path, new_path = _unified_diff_target_files(diff_text)

    if new_path != expected_target_file:
        raise ValueError(
            f"diff target_file 不匹配：expected={expected_target_file!r}, actual={new_path!r}"
        )

    if old_path not in {expected_target_file, new_path}:
        raise ValueError(
            f"diff old file 不匹配：expected={expected_target_file!r}, actual={old_path!r}"
        )

    original_lines = (original_content or "").splitlines()
    diff_lines = str(diff_text or "").splitlines()

    output_lines: list[str] = []
    source_index = 0
    added = 0
    deleted = 0
    saw_hunk = False

    hunk_re = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")

    i = 0
    while i < len(diff_lines):
        line = diff_lines[i]
        match = hunk_re.match(line)

        if not match:
            i += 1
            continue

        saw_hunk = True
        old_start = int(match.group(1))
        hunk_source_index = old_start - 1

        if hunk_source_index < source_index:
            raise ValueError("diff hunk 重叠或顺序错误。")

        output_lines.extend(original_lines[source_index:hunk_source_index])
        source_index = hunk_source_index
        i += 1

        while i < len(diff_lines):
            hunk_line = diff_lines[i]

            if hunk_re.match(hunk_line):
                break

            if hunk_line.startswith("--- ") or hunk_line.startswith("+++ "):
                break

            if hunk_line.startswith("\\"):
                i += 1
                continue

            if not hunk_line:
                raise ValueError("diff hunk 中存在缺少前缀的空行；空上下文行必须以空格开头。")

            prefix = hunk_line[0]
            body = hunk_line[1:]

            if prefix == " ":
                if source_index >= len(original_lines) or original_lines[source_index] != body:
                    raise ValueError(
                        "diff context 不匹配，拒绝应用。"
                        f"line={source_index + 1}, expected={body!r}"
                    )

                output_lines.append(original_lines[source_index])
                source_index += 1

            elif prefix == "-":
                if source_index >= len(original_lines) or original_lines[source_index] != body:
                    raise ValueError(
                        "diff delete 行不匹配，拒绝应用。"
                        f"line={source_index + 1}, expected={body!r}"
                    )

                source_index += 1
                deleted += 1

            elif prefix == "+":
                output_lines.append(body)
                added += 1

            else:
                raise ValueError(f"diff hunk 行前缀非法：{hunk_line[:80]!r}")

            i += 1

    if not saw_hunk:
        raise ValueError("unified diff 没有 @@ hunk。")

    output_lines.extend(original_lines[source_index:])

    candidate = "\n".join(output_lines)
    if original_content.endswith("\n"):
        candidate += "\n"

    return candidate, {
        "old_path": old_path,
        "new_path": new_path,
        "added_lines": added,
        "deleted_lines": deleted,
        "changed_line_count": added + deleted,
    }


def _diff_hunks_to_exact_replace_edits(diff_text: str, *, original_content: str) -> list[dict[str, str]]:
    """Convert single-file unified-diff hunks to exact_replace edits.

    This intentionally ignores hunk line numbers and relies on the existing
    exact/normalized/approximate matching policy in _apply_exact_replace_patch.
    Pure insertions use adjacent context for a unique insertion anchor.
    """
    _unified_diff_target_files(diff_text)
    diff_lines = str(diff_text or "").splitlines()
    hunk_re = re.compile(r"^@@\s+-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@")
    edits: list[dict[str, str]] = []
    i = 0
    while i < len(diff_lines):
        if not hunk_re.match(diff_lines[i]):
            i += 1
            continue
        i += 1
        hunk: list[tuple[str, str]] = []
        while i < len(diff_lines) and not hunk_re.match(diff_lines[i]):
            line = diff_lines[i]
            if line.startswith(("--- ", "+++ ")):
                break
            if line.startswith("\\"):
                i += 1
                continue
            if not line:
                raise ValueError("diff hunk 中存在缺少前缀的空行；无法转换为 exact_replace。")
            if line[0] not in {" ", "-", "+"}:
                raise ValueError(f"diff hunk 行前缀非法：{line[:80]!r}")
            hunk.append((line[0], line[1:]))
            i += 1

        old_lines = [body for prefix, body in hunk if prefix in {" ", "-"}]
        new_lines = [body for prefix, body in hunk if prefix in {" ", "+"}]
        deleted = [body for prefix, body in hunk if prefix == "-"]
        added = [body for prefix, body in hunk if prefix == "+"]
        if deleted:
            edits.append({"old": "\n".join(old_lines), "new": "\n".join(new_lines)})
            continue
        if added:
            # Pure insertion: replace a unique context span by context+added.
            context_lines = [body for prefix, body in hunk if prefix == " "]
            if not context_lines:
                raise ValueError("纯新增 hunk 缺少上下文，无法唯一定位插入点。")
            context = "\n".join(context_lines)
            if original_content.count(context) != 1 and _find_unique_normalized_span(original_content, context) is None:
                raise ValueError("纯新增 hunk 上下文无法唯一定位，拒绝转换。")
            insert_at = next((idx for idx, (prefix, _body) in enumerate(hunk) if prefix == "+"), len(hunk))
            before = "\n".join(body for prefix, body in hunk[:insert_at] if prefix == " ")
            after = "\n".join(body for prefix, body in hunk[insert_at:] if prefix == " ")
            new_parts = []
            if before:
                new_parts.append(before)
            new_parts.extend(added)
            if after:
                new_parts.append(after)
            edits.append({"old": context, "new": "\n".join(new_parts)})
    if not edits:
        raise ValueError("unified diff 没有可转换的 hunk edit。")
    return edits


def _apply_unified_diff_or_convert_to_exact(
    *,
    original_content: str,
    proposal: CreatorDiffProposal,
    expected_target_file: str,
) -> tuple[str, dict[str, Any]]:
    try:
        candidate, stats = _apply_single_file_unified_diff(
            original_content=original_content,
            diff_text=proposal.diff,
            expected_target_file=expected_target_file,
        )
        stats["mode"] = "unified_diff"
        return candidate, stats
    except Exception as diff_exc:
        edits = _diff_hunks_to_exact_replace_edits(proposal.diff, original_content=original_content)
        exact = CreatorDiffProposal(
            target_file=expected_target_file,
            reason=f"converted from unified diff after apply failed: {diff_exc}",
            edits=edits,
            raw={"target_file": expected_target_file, "edits": edits},
            mode="exact_replace",
        )
        candidate, stats = _apply_exact_replace_patch(
            original_content=original_content,
            proposal=exact,
            expected_target_file=expected_target_file,
        )
        stats["mode"] = "unified_diff_to_exact_replace"
        stats["unified_diff_apply_error"] = str(diff_exc)[:1000]
        return candidate, stats



_PLATFORM_IO_FORBIDDEN_PATCH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("os.path.join(...OUTPUT_DIR..., 'outputs')", re.compile(r"os\.path\.join\([^\n)]*OUTPUT_DIR[^\n)]*[,][^\n)]*['\"]outputs['\"]", re.I)),
    ("os.path.join(output_dir, 'outputs')", re.compile(r"os\.path\.join\([^\n)]*\boutput_dir\b[^\n)]*[,][^\n)]*['\"]outputs['\"]", re.I)),
    (".replace('/tmp/', 'outputs/')", re.compile(r"\.replace\(\s*['\"]/tmp/['\"]\s*,\s*['\"]outputs/['\"]\s*\)", re.I)),
    ("filename=full_path", re.compile(r"\bfilename\s*=\s*full_path\b", re.I)),
    ("filename=absolute_path", re.compile(r"\bfilename\s*=\s*absolute_path\b", re.I)),
)


def _platform_io_patch_violation(before: str, after: str) -> str | None:
    """Return violation name when a candidate newly introduces forbidden platform IO anti-patterns."""
    before_text = str(before or "")
    after_text = str(after or "")
    for label, pattern in _PLATFORM_IO_FORBIDDEN_PATCH_PATTERNS:
        if pattern.search(after_text) and not pattern.search(before_text):
            return label
    return None


def _skill_md_block_repair_constraints_from_notes(scope: CreatorRepairScope) -> dict[str, Any] | None:
    for note in scope.notes or ():
        text = str(note or "")
        prefix = "skill_md_block_repair_constraint:"
        if not text.startswith(prefix):
            continue
        try:
            data = json.loads(text[len(prefix):])
            return data if isinstance(data, dict) else None
        except Exception:
            return None
    return None


def _validate_skill_md_block_repair_proposal(
    *,
    proposal: CreatorDiffProposal,
    constraints: dict[str, Any],
) -> None:
    block_text = str(constraints.get("block_text") or "")
    script_path = str(constraints.get("script_path") or "")
    if not block_text or not script_path:
        raise ValueError("skill_md_block_repair_constraint 缺少 block_text/script_path。")
    if proposal.mode != "exact_replace":
        raise ValueError("SKILL.md single-block repair only accepts exact_replace OLD/NEW patches.")
    if len(proposal.edits or []) != 1:
        raise ValueError("SKILL.md single-block repair must contain exactly one edit.")
    edit = proposal.edits[0]
    old = edit.get("old", "")
    new = edit.get("new", "")
    if old != block_text:
        raise ValueError("SKILL.md single-block repair OLD must exactly equal the failed command block text.")
    if new == old:
        raise CreatorRepairNoopPatch("SKILL.md single-block repair rejected no-op OLD/NEW patch.")
    if script_path.replace('\\', '/') not in str(new).replace('\\', '/'):
        raise ValueError("SKILL.md single-block repair NEW must still invoke the same script_path.")
    if "```" in str(new) or "~~~" in str(new):
        raise ValueError("SKILL.md single-block repair NEW must replace only the block body, not fence markers or prose.")

def _validate_repair_diff_scope(
    *,
    proposal: CreatorDiffProposal,
    current_content: str,
    scope: CreatorRepairScope,
) -> tuple[str, dict[str, Any]]:
    """Validate and apply repair proposal.

    主路径：
    - exact_replace old_lines/new_lines（兼容 old/new）

    兜底：
    - unified diff

    注意：
    不做平台字段词表校验。
    不做 fake/mock 词表校验。
    不穷举 argv/stdout 字段。
    平台 IO 是否兼容，由现有 sandbox / smoke / E2E 试运行决定。
    """

    if proposal.target_file != scope.target_file:
        raise ValueError(
            f"repair proposal target_file 越权：expected={scope.target_file!r}, actual={proposal.target_file!r}"
        )

    block_constraints = _skill_md_block_repair_constraints_from_notes(scope)
    if block_constraints is not None:
        _validate_skill_md_block_repair_proposal(proposal=proposal, constraints=block_constraints)

    if proposal.mode == "exact_replace":
        candidate, stats = _apply_exact_replace_patch(
            original_content=current_content,
            proposal=proposal,
            expected_target_file=scope.target_file,
        )

    elif proposal.mode == "unified_diff":
        candidate, stats = _apply_unified_diff_or_convert_to_exact(
            original_content=current_content,
            proposal=proposal,
            expected_target_file=scope.target_file,
        )

    else:
        raise ValueError(f"未知 repair proposal mode：{proposal.mode}")

    violation = _platform_io_patch_violation(current_content, candidate)
    if violation:
        raise ValueError(
            "platform_io_contract_violation: repair patch newly introduces forbidden platform IO pattern: "
            f"{violation}. OUTPUT_DIR is already the final outputs dir; helper filename must be basename; "
            "do not rewrite helper-returned artifact paths."
        )

    changed_line_count = int(stats.get("changed_line_count") or 0)
    if changed_line_count > scope.max_changed_lines:
        raise ValueError(
            "repair patch 修改行数超过当前修复域上限："
            f"{changed_line_count} > {scope.max_changed_lines}"
        )

    stats["post_apply_validators"] = _run_registered_repair_text_validators(
        target_file=scope.target_file,
        before=current_content,
        after=candidate,
        scope=scope,
    )

    return candidate, stats


def _run_registered_repair_text_validators(
    *,
    target_file: str,
    before: str,
    after: str,
    scope: CreatorRepairScope,
) -> list[str]:
    """Run optional registered post-apply text validators for repair patches.

    Missing specialized validators are not a rejection condition.  Generic patch
    safety remains target_file equality, unique matching, similarity/margin,
    changed-line scope, and real diff; smoke/sandbox/E2E continue after this.
    """
    ran: list[str] = []
    if scope.allow_format_repair:
        return ran

    validators: list[tuple[str, Callable[[], bool], Callable[[], None]]] = [
        (
            "hard_format_regression",
            lambda: target_file == "SKILL.md" or target_file.startswith("references/") or _is_markdown_file(target_file),
            lambda: __import__("backend.services.creator.contracts", fromlist=["validate_no_hard_format_regression"]).validate_no_hard_format_regression(
                target_file,
                before,
                after,
                require_frontmatter=(target_file == "SKILL.md"),
            ),
        ),
        (
            "skill_md_shell_json_argv",
            lambda: target_file == "SKILL.md",
            lambda: (_ for _ in ()).throw(ValueError(
                "localized patch introduced backslash-escaped shell JSON argv inside a bash fenced block; "
                "合法 shell JSON argv 不允许被改写成带反斜杠的 argv。"
            )) if _skill_md_has_backslash_escaped_json_argv(after) else None,
        ),
    ]
    for name, applies, validate in validators:
        if applies():
            validate()
            ran.append(name)
    return ran


def _skill_md_has_backslash_escaped_json_argv(content: str) -> bool:
    """Detect escaped JSON argv only on standard scripts/*.py shell commands.

    This intentionally ignores ordinary shell snippets (echo/sed/etc.) and
    non-standard/multiline shell text. Command-contract repair owns closed bash
    blocks whose JSON argv is otherwise invalid.
    """
    from .contracts import _command_signature

    for info, body in _iter_markdown_fenced_blocks(content):
        if not _is_shell_fence_info(info):
            continue
        lines = [line.strip() for line in str(body or "").splitlines() if line.strip()]
        if len(lines) != 1:
            continue
        command = lines[0]
        try:
            parts = shlex.split(command, posix=True)
        except ValueError:
            continue
        if len(parts) != 3:
            continue
        runner = Path(parts[0]).name
        script_path = parts[1].replace("\\", "/").strip()
        argv = parts[2].strip()
        if runner not in {"python", "python3"}:
            continue
        if not script_path.startswith("scripts/") or Path(script_path).suffix.lower() != ".py":
            continue

        command_sig = _command_signature(command, script_path)
        if not command_sig or command_sig.get("arg_mode") != "invalid_json_arg":
            continue
        if argv.startswith("{") and argv.endswith("}") and any(token in argv for token in ('\\"', "\\{", "\\}")):
            return True

    return False


def _compact_messages_for_repair_context(
    messages: list[dict],
    *,
    max_chars: int = 12000,
) -> str:
    parts: list[str] = []

    for message in messages[-8:]:
        role = str(message.get("role") or "")
        content = str(message.get("content") or "")
        if not content.strip():
            continue

        parts.append(f"\n[{role}]\n{content[-3000:]}")

    return "\n".join(parts)[-max_chars:]


def _sandbox_io_contract_text_for_creator() -> str:
    """Describe the existing sandbox IO contract without field-name hardcoding.

    这里只给模型解释现有 sandbox 的通用运行方式：
    - Action schema
    - JSON argv
    - placeholder
    - stdout JSON object
    - context merge
    - final output / artifact 由现有 sandbox 校验

    不在 Creator repair 层重写平台字段白名单。
    """

    return (
        "现有 sandbox IO 协议如下：\n"
        "1. SKILL.md 中的 shell fenced command block 会被解析成 Action schema；references/*.md 只读按需加载，不作为执行步骤。\n"
        "2. 每个 command 调用 scripts/...，脚本参数是一个 JSON argv object。\n"
        "3. command JSON keys 需要与 Action schema inputs / optional_inputs 对齐。\n"
        "4. {{placeholder}} 从 workflow context 中解析，解析不到会触发 dataflow_mismatch。\n"
        "5. 每个脚本 stdout 必须是 JSON object；后端会 json.loads(stdout)，非 object 失败。\n"
        "6. 每步 stdout 会 merge 到 workflow context，后续步骤可以引用其字段。\n"
        "7. 最后一步必须通过现有 sandbox final output / artifact 校验；Creator 不另写字段词表。\n"
        "8. references/*.md 是参考资料，不是 E2E 执行源。\n"
    )


async def _request_repair_diff_proposal(
    *,
    model: str,
    file_path: str,
    current_content: str,
    failure_text: str,
    scope: CreatorRepairScope,
    task_context: str,
    target_rule: str,
    format_retry_limit: int = 2,
) -> CreatorDiffProposal:
    """Ask coding model for one literal localized source patch."""
    boundary = _build_literal_patch_boundary(
        current_content=current_content,
        file_path=file_path,
    )

    example = _literal_patch_protocol_example(
        file_path=file_path,
        boundary=boundary,
    )

    scope_payload = scope.to_prompt_dict()

    scope_payload["notes"] = [
        _normalize_source_patch_prompt_text(
            note
        )
        for note in (
            scope_payload.get("notes")
            or []
        )
    ]

    normalized_target_rule = (
        _normalize_source_patch_prompt_text(
            target_rule
        )
    )

    messages = [
        {
            "role": "system",
            "content": (
                "你是 superskills Creator 的局部修复代码模型。\n"
                "你只能输出 literal exact_replace patch envelope；"
                "不能输出 JSON patch、unified diff、"
                "Markdown 解释或完整文件。\n"
                "OLD 和 NEW 是目标文件 literal source text，"
                "不是 JSON string。\n"
                "因此源码中的双引号、单引号、反斜杠、正则、"
                "shell JSON argv、{{placeholder}} "
                "必须按目标文件真实内容原样输出。\n"
                "禁止为了 patch transport 增加 JSON escape；"
                "禁止把普通 \" 改写成 \\\"。\n"
                "本轮只允许修复 target_file；"
                "不要新增文件、删除文件或修改其它文件。\n"
                "不要在 Creator repair 层重新定义平台 IO。"
                "收到 basic_format/python_compile_error/"
                "markdown_basic_format_error 时，只修格式；"
                "不要修改工具选择、argv schema、"
                "stdout 字段或业务职责。"
                "只有 sandbox/E2E 错误才修 "
                "workflow/argv/stdout/artifact 链路。"
                "平台兼容性由后续 sandbox / smoke / "
                "E2E 真实试运行判断。\n"
            ),
        },
        {
            "role": "user",
            "content": (
                f"目标文件：{file_path}\n\n"
                "RepairScope：\n"
                f"{json.dumps(scope_payload, ensure_ascii=False, indent=2)}\n\n"
                "本轮修复规则：\n"
                f"{normalized_target_rule}\n\n"
                "sandbox IO 前置协议：\n"
                f"{_sandbox_io_contract_text_for_creator()}\n\n"
                "真实失败来源：\n"
                f"{failure_text[-12000:]}\n\n"
                "上下文：\n"
                f"{task_context[-20000:]}\n\n"
                "当前目标文件完整内容：\n"
                "```text\n"
                f"{current_content}\n"
                "```\n\n"
                "只返回 literal patch envelope。\n"
                "协议形态示例仅展示 transport 格式，"
                "示例源码不可照抄：\n"
                f"{example}\n\n"
                "要求：\n"
                f"1. 必须原样使用 boundary={boundary}；"
                "所有 marker 必须独占一行。\n"
                f"2. TARGET_FILE 必须严格等于 {file_path}。\n"
                "3. OLD 必须从当前目标文件逐字复制；"
                "OLD 是源码，不是 JSON string，"
                "不做 JSON escaping。\n"
                "4. NEW 是最终写回目标文件的真实源码；"
                "NEW 也不是 JSON string，"
                "不做 transport escaping。\n"
                "5. shell JSON argv 中原文是 \"，"
                "OLD/NEW 就直接写 \"；"
                "原文没有反斜杠时不得添加反斜杠。\n"
                "6. {{placeholder}} 原样保留；"
                "不要把双花括号当成 patch JSON 错误。\n"
                "7. OLD 必须在当前文件中形成唯一可靠匹配；"
                "优先复制完整失败行或更长连续局部片段。\n"
                "8. NEW 必须产生真实语义变化；"
                "OLD 与 NEW 完全相同的 no-op 禁止提交。\n"
                "9. 每个 EDIT 只修改一个连续局部片段；"
                "确有多个独立局部修改时可以输出多个 EDIT。\n"
                "10. 删除内容时允许 NEW 为空。\n"
                "11. 不要输出完整文件；"
                "不要输出 ```json、```diff "
                "或其它 Markdown fence；"
                "不要输出 envelope 外文字。\n"
            ),
        },
    ]

    last_error: Exception | None = None
    last_text = ""

    for attempt in range(
        1,
        max(1, format_retry_limit) + 1,
    ):
        text = await complete_chat_once(
            messages,
            model,
        )

        last_text = text

        try:
            return _extract_json_or_diff_proposal(
                text,
                expected_target_file=file_path,
                allow_tool_explore=(
                    scope.allow_tool_explore
                ),
                literal_boundary=boundary,
            )

        except Exception as exc:
            last_error = exc

            logger.warning(
                "[Creator][repair_patch]"
                "[format_violation] "
                "file=%s model=%s attempt=%d/%d error=%s",
                file_path,
                model,
                attempt,
                format_retry_limit,
                exc,
            )

            if attempt >= format_retry_limit:
                break

            messages.append({
                "role": "assistant",
                "content": (
                    str(text or "")[:6000]
                ),
            })

            messages.append({
                "role": "user",
                "content": (
                    _format_diff_response_violation(
                        exc,
                        text,
                        file_path=file_path,
                        boundary=boundary,
                    )
                ),
            })

    last_error_type = (
        type(last_error).__name__
        if last_error
        else "Unknown"
    )

    raise CreatorRepairProposalParseError(
        "修复模型连续没有返回合法 literal "
        "patch proposal，已拒绝应用。\n"
        f"target_file={file_path}\n"
        f"last_error={last_error_type}: "
        f"{last_error}\n"
        "last_output_excerpt:\n"
        f"{str(last_text or '')[:4000]}",
        parser_error=str(
            last_error or ""
        ),
        last_output_excerpt=last_text,
        diff_extraction_attempted=bool(
            getattr(
                last_error,
                "diff_extraction_attempted",
                False,
            )
        ),
        lines_fallback_attempted=bool(
            getattr(
                last_error,
                "lines_fallback_attempted",
                False,
            )
        ),
    )

async def _request_and_apply_repair_patch(
    *,
    model: str,
    file_path: str,
    current_content: str,
    failure_text: str,
    scope: CreatorRepairScope,
    task_context: str,
    target_rule: str,
    patch_retry_limit: int = 3,
) -> tuple[
    CreatorDiffProposal,
    str,
    dict[str, Any],
]:
    """Request, apply, and retry one localized source patch.

    This layer only owns:
    - proposal transport/parse feedback;
    - literal OLD/NEW matching feedback;
    - no-op feedback;
    - runtime traceback priority feedback.

    Business correctness remains owned by smoke/sandbox/E2E reruns.
    """
    runtime_priority_note = ""

    if any(
        marker in str(
            failure_text or ""
        )
        for marker in (
            "Traceback",
            "stderr=",
            "exit_code=",
            "脚本试运行失败",
            "SyntaxError:",
            "ValueError:",
            "TypeError:",
            "NameError:",
            "ModuleNotFoundError:",
            "ImportError:",
        )
    ):
        runtime_priority_note = (
            "RUNTIME_TRACEBACK_PRIORITY："
            "这是 smoke/trial run 真实运行失败。"
            "修复时必须优先依据 raw stderr Traceback、"
            "exit_code、报错源码行和异常类型。"
            "validator 的解释只作为辅助说明；"
            "如果 validator 解释与 Traceback 冲突，"
            "以 Traceback 为准。"
            "不要修改与 Traceback 无关的位置。"
        )

    argv_probe_note = ""

    if (
        scope.phase == "workflow_e2e"
        and any(
            marker
            in f"{failure_text}\n{task_context}"
            for marker in (
                "argv_schema_error",
                "strict_json_argv_guard",
            )
        )
    ):
        argv_probe_note = (
            "ARGV_SCHEMA_REPAIR_ALIGNMENT："
            "strict_json_argv_guard 是探针，"
            "不是默认修复目标；"
            "禁止只 patch guard schema。"
            "修复要对齐 SKILL.md block 调用、"
            "script entry、script core；"
            "不能删除业务参数/功能覆盖面。"
        )

    extra_notes = "\n\n".join(
        note
        for note in (
            runtime_priority_note,
            argv_probe_note,
        )
        if note
    )

    accumulated_failure = (
        failure_text
        + (
            "\n\n" + extra_notes
            if extra_notes
            else ""
        )
    )

    accumulated_context = (
        task_context
        + (
            "\n\n" + extra_notes
            if extra_notes
            else ""
        )
    )

    last_error: Exception | None = None
    last_proposal_excerpt = ""

    failed_proposal_counts: dict[
        str,
        int,
    ] = {}

    for attempt in range(
        1,
        max(1, patch_retry_limit) + 1,
    ):
        proposal_signature: str | None = None

        try:
            proposal = (
                await _request_repair_diff_proposal(
                    model=model,
                    file_path=file_path,
                    current_content=current_content,
                    failure_text=accumulated_failure,
                    scope=scope,
                    task_context=accumulated_context,
                    target_rule=target_rule,
                    format_retry_limit=2,
                )
            )

            # 注意：
            # signature 使用 normalized semantic patch。
            #
            # 不使用 proposal.raw。
            # 不使用 reason。
            # 不使用 literal boundary。
            #
            # 防止模型仅修改 transport 序列化方式或 reason
            # 来绕过 repeated proposal detection。
            proposal_signature_payload = {
                "target_file": (
                    proposal.target_file
                ),
                "mode": proposal.mode,
                "diff": proposal.diff,
                "edits": proposal.edits,
            }

            last_proposal_excerpt = (
                json.dumps(
                    {
                        "normalized_patch": (
                            proposal_signature_payload
                        ),
                        "reason": proposal.reason,
                        "transport": (
                            proposal.raw or {}
                        ).get(
                            "transport"
                        ),
                    },
                    ensure_ascii=False,
                    default=str,
                )[:5000]
            )

            proposal_signature = json.dumps(
                proposal_signature_payload,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )

            if (
                failed_proposal_counts.get(
                    proposal_signature,
                    0,
                )
                >= 1
            ):
                repeated_excerpt = ""

                if proposal.edits:
                    old_text = str(
                        proposal
                        .edits[0]
                        .get("old")
                        or ""
                    )

                    approx = (
                        _find_approximate_substring_span(
                            current_content,
                            old_text,
                        )
                    )

                    repeated_excerpt = str(
                        approx.get(
                            "matched_excerpt"
                        )
                        or ""
                    )

                raise ValueError(
                    "REPEATED_UNAPPLICABLE_PROPOSAL："
                    "连续两轮 normalized patch 完全相同，"
                    "且上一轮不可应用。"
                    "修改 reason、boundary 或 transport "
                    "序列化方式不算新的 patch。"
                    "请直接复制下面最相近候选原文片段"
                    "作为新的 OLD，或提供更长唯一 OLD。\n"
                    "```text\n"
                    f"{repeated_excerpt}\n"
                    "```"
                )

            candidate, diff_stats = (
                _validate_repair_diff_scope(
                    proposal=proposal,
                    current_content=current_content,
                    scope=scope,
                )
            )

            diff_stats[
                "repair_patch_attempt"
            ] = attempt

            return (
                proposal,
                candidate,
                diff_stats,
            )

        except Exception as exc:
            last_error = exc

            if proposal_signature is not None:
                failed_proposal_counts[
                    proposal_signature
                ] = (
                    failed_proposal_counts.get(
                        proposal_signature,
                        0,
                    )
                    + 1
                )

            is_markdown_hard_format_regression = (
                (
                    file_path == "SKILL.md"
                    or file_path.startswith(
                        "references/"
                    )
                    or _is_markdown_file(
                        file_path
                    )
                )
                and (
                    "hard_format_regression"
                    in str(exc)
                    or "markdown.fences.unclosed"
                    in str(exc)
                    or "markdown.fences.bash_unclosed"
                    in str(exc)
                    or "markdown.frontmatter.unclosed"
                    in str(exc)
                    or "model_patch_allowed"
                    in str(exc)
                )
            )

            if (
                is_markdown_hard_format_regression
                and scope.phase == "workflow_e2e"
            ):
                last_failure = (
                    "PATCH_CANDIDATE_FORMAT_REGRESSED："
                    "原始 Markdown 格式已通过，"
                    "但候选 patch 造成 hard_format_regression，"
                    "已拒绝且未修改原文件。\n"
                    "继续生成更小的 content-only patch："
                    "不要修 frontmatter；"
                    "不要修 code fence；"
                    "不要新增/删除 ``` 行；"
                    "只修改失败命令那一行；"
                    "OLD 必须包含当前文件中的完整真实命令行；"
                    "不要把 ```bash 和闭合 ``` 纳入 OLD，"
                    "除非完整包含闭合 fence。\n"
                    f"validator_error={exc}"
                )

                accumulated_failure = (
                    failure_text
                    + "\n\n"
                    + last_failure
                )

                accumulated_context = (
                    task_context
                    + "\n\n"
                    + last_failure
                )

                continue

            if is_markdown_hard_format_regression:
                raise

            # repeated semantic patch 才直接停止。
            #
            # CreatorRepairNoopPatch 不再在这里 break，
            # 必须进入下面的 NO_OP_PATCH_REJECTED feedback。
            if (
                "REPEATED_UNAPPLICABLE_PROPOSAL"
                in str(exc)
            ):
                break

            logger.warning(
                "[Creator][repair_patch]"
                "[apply_or_parse_failed] "
                "file=%s model=%s "
                "attempt=%d/%d error=%s",
                file_path,
                model,
                attempt,
                patch_retry_limit,
                exc,
            )

            if attempt >= patch_retry_limit:
                break

            is_noop_patch = (
                isinstance(
                    exc,
                    CreatorRepairNoopPatch,
                )
                or "proposal_noop"
                in str(exc)
                or "no-op"
                in str(exc)
                or "没有产生任何变化"
                in str(exc)
                or "old 与 new 完全相同"
                in str(exc)
            )

            no_op_note = ""

            if is_noop_patch:
                no_op_note = (
                    "NO_OP_PATCH_REJECTED："
                    "上一轮 patch 在解析为真实 OLD/NEW 后"
                    "没有产生目标文件变化。"
                    "不能提交 OLD 与 NEW 完全相同的 edit。"
                    "也不能只改变 patch transport 的 boundary、"
                    "reason 或转义写法。"
                    "NEW 必须真实改变当前失败内容。"
                    "如果目标是在文件末尾补充内容，"
                    "让 OLD 选中当前文件末尾的一段真实文本，"
                    "NEW 在该片段基础上追加缺失内容。"
                )

            apply_feedback = (
                "\n\n"
                "PATCH_APPLY_OR_PARSE_FAILED："
                "上一轮 patch 没有被后端接受。\n"
                f"失败类型："
                f"{type(exc).__name__}\n"
                f"失败原因：{exc}\n\n"
                f"{runtime_priority_note}\n\n"
                f"{no_op_note}\n\n"
                "请重新输出 literal OLD/NEW exact_replace "
                "patch envelope。"
                "不要输出完整文件、JSON patch "
                "或 unified diff。\n"
                "OLD 必须从当前目标文件逐字复制，"
                "且只出现一次。\n"
                "NEW 必须真实改变当前失败内容。\n"
                "OLD/NEW 中的引号和反斜杠"
                "是目标文件字面内容；"
                "不要为了 transport 添加 JSON escape。\n"
                "如果 OLD 没匹配，"
                "复制更准确的当前文件片段。\n"
                "如果 OLD 匹配多次，"
                "提供更长 OLD 片段。\n\n"
                "上一轮 normalized proposal 摘要：\n"
                "```text\n"
                f"{last_proposal_excerpt}\n"
                "```\n"
            )

            accumulated_failure = (
                failure_text
                + apply_feedback
            )

            accumulated_context = (
                task_context
                + apply_feedback
            )

    if isinstance(
        last_error,
        CreatorRepairProposalParseError,
    ):
        raise CreatorRepairProposalParseError(
            "修复模型连续提出无法解析的 patch，"
            "已停止本轮 repair。\n"
            f"target_file={file_path}\n"
            f"last_error={last_error}",
            parser_error=(
                last_error.parser_error
            ),
            last_output_excerpt=(
                last_error.last_output_excerpt
            ),
            diff_extraction_attempted=(
                last_error
                .diff_extraction_attempted
            ),
            lines_fallback_attempted=(
                last_error
                .lines_fallback_attempted
            ),
        )

    last_error_type = (
        type(last_error).__name__
        if last_error
        else "Unknown"
    )

    raise ValueError(
        "修复模型连续提出无法解析或无法应用的 patch，"
        "已停止本轮 repair。\n"
        f"target_file={file_path}\n"
        f"last_error={last_error_type}: "
        f"{last_error}\n"
    )

async def _repair_generated_file_with_feedback(
    *,
    prompt_messages: list[dict],
    model: str,
    file_path: str,
    previous_content: str,
    validation_error: str,
    targeted_repair: str = "",
    contract_text: str = "",
    passed_checks_text: str = "",
    failed_checks_text: str = "",
    repair_mode: str = "minimal_edit",
    skill_plan_entry: dict[str, Any] | None = None,
    import_guard_result: dict[str, Any] | None = None,
    current_file_binding: dict[str, Any] | None = None,
    tool_pool_summary: dict[str, Any] | None = None,
    function_execution_context: dict[str, Any] | None = None,
    patch_retry_limit: int = 3,
) -> str:
    """First-round single-file repair using local patch.

    第一轮职责：
    - 模型负责判断当前文件是否完成责任内功能；
    - smoke / trial run 负责判断代码是否真的能跑通；
    - 本函数只让模型提出局部 patch，并在内存中 apply patch 得到 candidate content；
    - 不允许模型整文件覆盖；
    - 不在 repair 层穷举平台 IO 字段，平台 IO 交给现有 smoke / sandbox 校验；
    - patch 解析/apply 失败时，会反馈给写代码模型重试，而不是直接报错。
    """

    current_content = previous_content or ""
    is_script = file_path.startswith("scripts/")

    if is_script:
        has_noncanonical_source_shape = (
            "```" in current_content
            or "~~~" in current_content
            or _MULTI_FILE_MARKER_RE.search(current_content) is not None
        )
        if has_noncanonical_source_shape or any(
            error_id in str(validation_error or "")
            for error_id in {
                "script.raw_source.single_file",
                "script.raw_source.ambiguous_multi_code_blocks",
                "script.raw_source.multi_file_bundle",
                "script.raw_source.ambiguous_script_candidate",
            }
        ):
            raise ValueError(
                "scripts/* raw_source format failure must not enter repair_patch; "
                "regenerate a single canonical script source instead."
            )

    if file_path == "SKILL.md" or file_path.startswith("references/") or Path(file_path).suffix.lower() in {".md", ".markdown"}:
        from .contracts import detect_markdown_hard_format_failures

        hard_format_failures = detect_markdown_hard_format_failures(
            file_path,
            current_content,
            require_frontmatter=(file_path == "SKILL.md"),
        )
        if hard_format_failures:
            raise ValueError(
                "hard_format failure must not enter localized patch repair; full rewrite required:\n"
                + json.dumps(hard_format_failures, ensure_ascii=False, default=str)
            )

    block_repair_constraint_note = ""
    if file_path == "SKILL.md" and "skill_md.command_block." in str(failed_checks_text or ""):
        match = re.search(r"details: (\{[^\n]*\})", failed_checks_text or "")
        if not match:
            raise ValueError("SKILL.md command block repair missing structured block constraint details; refusing file-level fallback repair.")
        try:
            details = json.loads(match.group(1))
        except Exception as exc:
            raise ValueError("SKILL.md command block repair constraint details are not valid JSON; refusing file-level fallback repair.") from exc
        if not isinstance(details, dict):
            raise ValueError("SKILL.md command block repair constraint details must be an object; refusing file-level fallback repair.")
        block_text = str(details.get("block_text") or details.get("current_block") or "")
        script_path = str(details.get("script_path") or "")
        if not block_text or not script_path:
            raise ValueError("SKILL.md command block repair constraint missing script_path/block_text; refusing file-level fallback repair.")
        block_repair_constraint_note = "skill_md_block_repair_constraint:" + json.dumps({
            "script_path": script_path,
            "block_text": block_text,
            "block_start": details.get("block_start"),
            "block_end": details.get("block_end"),
            "block_ordinal": details.get("block_ordinal"),
        }, ensure_ascii=False, sort_keys=True)


    scope_notes = [
        "第一轮只修当前文件。",
        "模型功能校验判断责任是否完成；smoke/trial run 判断代码是否通过。",
        "平台兼容性直接交给现有 sandbox/smoke 校验，不在 repair 层做字段词表判断。",
        "优先输出 edits old_lines/new_lines exact_replace patch，不要输出完整文件。",
    ]
    if block_repair_constraint_note:
        scope_notes.append(block_repair_constraint_note)

    scope = CreatorRepairScope(
        phase="module_functional_smoke",
        repair_type=repair_mode or "localized_patch",
        target_file=file_path,
        max_changed_lines=220 if repair_mode == "strict_contract_rewrite" else 160,
        notes=tuple(scope_notes),
    )

    plan_entry = (
        _skill_plan_entry_for_file(file_path=file_path, skill_plan_entry=skill_plan_entry)
        if is_script
        else None
    )
    repair_language = plan_entry.language if plan_entry is not None else language_for_path(file_path)
    repair_runtime = plan_entry.runtime if plan_entry is not None else runtime_for_language(
        repair_language,
        file_type_for_path(file_path),
    )

    if is_script:
        tool_context = (
            _creator_tool_context_for_script(
                file_path=file_path,
                skill_plan_entry=plan_entry,
                failure_layer=(
                    _failure_layer_from_error_text(
                        validation_error
                    )
                ),
                error_text=validation_error,
                include_snippets=True,
                rediscover_for_repair=False,
                repair_context={
                    "target_file": file_path,
                    "script_content": current_content,
                    "structured_failure": {
                        "validation_error": (
                            validation_error
                        ),
                        "targeted_repair": targeted_repair,
                        "failed_checks": (
                            failed_checks_text
                        ),
                    },
                    "runtime_contract": getattr(
                        plan_entry,
                        "runtime_contract",
                        None,
                    ),
                    "artifact_contract": getattr(
                        plan_entry,
                        "artifact_contract",
                        None,
                    ),
                    "command_argv_contract": getattr(
                        plan_entry,
                        "command_template",
                        None,
                    ),
                },
                current_file_binding=(
                        current_file_binding or {}
                ),
            )
            if plan_entry is not None
            else ""
        )

        repair_snippet_text = ""

        if plan_entry is not None:
            bound_tool_ids: list[str] = []

            for key in (
                    "primary_tool_ids",
                    "secondary_tool_ids",
                    "allowed_tool_ids",
            ):
                for tool_id in (
                                       current_file_binding or {}
                               ).get(key, []) or []:
                    tool_id = str(tool_id or "").strip()

                    if (
                            tool_id
                            and tool_id not in bound_tool_ids
                    ):
                        bound_tool_ids.append(tool_id)

            snippets = resolve_tool_snippets_for_context(
                role=plan_entry.role,
                capabilities=bound_tool_ids,
                tool_names=bound_tool_ids,
                file_path=file_path,
                failure_layer=(
                    _failure_layer_from_error_text(
                        validation_error
                    )
                ),
                error_text=(
                        validation_error
                        + "\n"
                        + current_content[-6000:]
                ),
                max_snippets=5,
            )

            repair_snippet_text = (
                tool_snippet_prompt(snippets)
            )

        guard_success = bool((import_guard_result or {}).get("success")) if isinstance(import_guard_result, dict) else False
        failure_layer = _failure_layer_from_error_text(validation_error)
        should_repair_tools = "runtime_import_guard" in str(failure_layer or "") or "runtime_import_guard" in str(validation_error or "")
        target_rule = (
            "第一轮单文件修复。\n"
            "如果 failure_text/coarse_failure_kind 是 basic_format/python_compile_error/markdown_basic_format_error，"
            "只修当前候选基础格式；不要修改工具选择、argv schema、stdout 字段或业务职责。\n"
            "只修当前脚本文件，不改 SKILL.md，不改其它脚本，不改 references/assets。\n"
            "只有 failure source/layer 是 runtime_import_guard 时，才优先修不可用工具或 helper。\n"
            "如果 runtime_import_guard 已通过，不要删除 helper import，不要把合法 helper 调用改成本地占位逻辑，不要替换成另一个未绑定 helper。\n"
            "优先使用 Current File Tool Binding 绑定的 helper；没有可用 helper 时用当前脚本本地逻辑、Python 标准库或已允许/已安装的安全依赖实现职责，不要猜 runtime_tools 函数。\n"
            "不要只改字段名；不要为了通过校验返回空结果或伪造成功。\n"
            "优先输出 edits old_lines/new_lines exact_replace patch。不要输出完整文件。"
            + (
                "\n工具导入已通过后端确定性检查，本轮不要修改工具导入。"
                if guard_success and not should_repair_tools
                else "\n本轮 runtime_import_guard 未通过或失败来源为 runtime_import_guard；允许按 Current File Tool Binding 修复 import/helper。"
            )
        )

        extra_context = (
            f"runtime={repair_runtime}, language={repair_language}\n\n"
            "SkillPlanEntry：\n"
            f"{json.dumps(skill_plan_entry or {}, ensure_ascii=False, default=str)[:6000]}\n\n"
            "Current File Tool Binding（硬约束）：\n"
            f"{json.dumps(current_file_binding or {}, ensure_ascii=False, default=str)[:8000]}\n\n"
            "Current Tool Pool Summary（含 scored candidates / primary / fallback / denied / missing）：\n"
            f"{json.dumps(tool_pool_summary or {}, ensure_ascii=False, default=str)[:10000]}\n\n"
            "Runtime Import Guard Result（如果存在，必须先修复该硬错误；不要把 forbidden helper 替换成另一个未绑定 helper；需要平台 helper 时请求 tool_pool_patch.add_tool_requests；若任务可由标准库/允许依赖完成，则改为本地实现，不导入 runtime_tools）：\n"
            f"{json.dumps(import_guard_result or {}, ensure_ascii=False, default=str)[:8000]}\n\n"
            "Canonical Function Execution Context（Writer/Judge/Repair 共享，优先来自当前真实 ToolPool file binding）：\n"
            f"{json.dumps(function_execution_context or {}, ensure_ascii=False, default=str)[:12000]}\n\n"
            "Tool Registry / Snippet 上下文：\n"
            f"{tool_context}\n\n"
            + (
                "本次失败涉及以下工具；请优先按相关 Tool Snippet 修复，不要继续猜工具调用方式：\n"
                f"{repair_snippet_text}\n"
                if repair_snippet_text
                else ""
            )
        )

    elif file_path == "SKILL.md":
        target_rule = (
            "第一轮 SKILL.md 修复。\n"
            "当前文件已通过 hard format gate；本轮不是 Markdown 全局格式修复。\n"
            "只修当前失败相关的小节内容、职责描述、蓝图对齐或命令合同问题。\n"
            "不要整文件重写。\n"
            "不要修改 frontmatter 边界。\n"
            "不要修改 fenced block 开闭结构。\n"
            "不要把 proposal JSON 转义写进目标文件。\n"
            "不要在这里做第二轮 E2E 跨模块字段推断；那属于 workflow E2E。\n"
            "平台 IO 与 sandbox 模式对齐，由后续验证执行判断。\n"
            "优先输出 edits old_lines/new_lines exact_replace patch。不要输出完整 SKILL.md。"
        )
        extra_context = ""

    elif file_path.startswith("references/"):
        target_rule = (
            "第一轮 reference 修复。\n"
            "当前文件已通过 hard format gate；本轮不是 Markdown 全局格式修复。\n"
            "reference 是参考资料，不是执行源。\n"
            "只修当前 reference 文件中的失败区域。\n"
            "不要修改 frontmatter 边界或 fenced block 开闭结构。\n"
            "不要把 proposal JSON 转义写进目标文件。\n"
            "不要添加可执行 workflow。\n"
            "优先输出 edits old_lines/new_lines exact_replace patch。不要输出完整文件。"
        )
        extra_context = ""

    else:
        target_rule = (
            "第一轮单文件修复。\n"
            "只修当前文件。\n"
            "优先输出 edits old_lines/new_lines exact_replace patch。不要输出完整文件。"
        )
        extra_context = ""

    prompt_context_summary = (
        "已省略原始 Writer prompt 中可能过期的工具上下文；本轮以 Canonical Function Execution Context 和 Current File Tool Binding 为准。"
        if is_script and isinstance(function_execution_context, dict)
        else _compact_messages_for_repair_context(prompt_messages)
    )

    task_context = "\n".join([
        "原始生成上下文摘要：",
        prompt_context_summary,
        "",
        "后端定向修复提示：",
        targeted_repair or "无",
        "",
        "文件合同摘要：",
        contract_text[-8000:] if contract_text else "无",
        "",
        "已通过检查，必须尽量保持：",
        passed_checks_text[-5000:] if passed_checks_text else "无",
        "",
        "未通过检查，本轮只修这些：",
        failed_checks_text[-5000:] if failed_checks_text else "无",
        "",
        "额外上下文：",
        extra_context,
    ])

    logger.info(
        "[Creator][model] phase=repair_patch.request file=%s model=%s repair_mode=%s previous_chars=%d",
        file_path,
        model,
        repair_mode,
        len(current_content),
    )

    structured_failures: list[Mapping[str, Any]] = []
    parsed_failure_json = _parse_validator_json_object(failed_checks_text)
    if isinstance(parsed_failure_json, dict):
        maybe = parsed_failure_json.get("failures") or parsed_failure_json.get("failed_checks")
        if isinstance(maybe, list):
            structured_failures = [item for item in maybe if isinstance(item, Mapping)]
    else:
        try:
            maybe = json.loads(failed_checks_text) if failed_checks_text else None
            if isinstance(maybe, list):
                structured_failures = [item for item in maybe if isinstance(item, Mapping)]
        except Exception:
            structured_failures = []

    deterministic = _apply_deterministic_micro_patch_if_safe(
        failures=structured_failures,
        current_content=current_content,
        scope=scope,
    )
    if deterministic is not None:
        _proposal, candidate, diff_stats = deterministic
    else:
        _proposal, candidate, diff_stats = await _request_and_apply_repair_patch(
            model=model,
            file_path=file_path,
            current_content=current_content,
            failure_text=validation_error,
            scope=scope,
            task_context=task_context,
            target_rule=target_rule,
            patch_retry_limit=patch_retry_limit,
        )

    logger.info(
        "[Creator][model] phase=repair_patch.applied file=%s model=%s repair_mode=%s diff_stats=%s",
        file_path,
        model,
        repair_mode,
        json.dumps(diff_stats, ensure_ascii=False, default=str)[:3000],
    )

    return candidate

def _parse_validator_json_object(text: str) -> dict | None:
    stripped = (text or "").strip()
    if stripped.startswith("```json"):
        stripped = stripped[7:].strip()
    if stripped.startswith("```"):
        stripped = stripped[3:].strip()
    if stripped.endswith("```"):
        stripped = stripped[:-3].strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _deterministic_failed_check_ids(failed_checks_text: str) -> set[str]:
    ids: set[str] = set()
    for match in re.finditer(r"^-\s+([^\s]+)\s+target=", failed_checks_text or "", re.M):
        ids.add(match.group(1))
    return ids


def _filter_validator_failed_checks(failed_checks: list[Any], failed_checks_text: str) -> list[Any]:
    """Keep only model failed_checks backed by deterministic failed checks."""
    allowed_ids = _deterministic_failed_check_ids(failed_checks_text)
    if not allowed_ids:
        return []
    filtered: list[Any] = []
    for check in failed_checks:
        if not isinstance(check, dict):
            continue
        check_id = str(check.get("id") or "")
        if check_id in allowed_ids:
            filtered.append(check)
    return filtered


_MISSING_SKILL_SCRIPT_BLOCK_RE = re.compile(
    r"SKILL\.md 缺少调用 (?P<script>scripts/[A-Za-z0-9_./-]+) 的可执行 Markdown 命令块"
)
_MISSING_SKILL_REFERENCE_RE = re.compile(
    r"SKILL\.md 缺少对参考资料 (?P<reference>references/[A-Za-z0-9_./-]+) 的引用"
)


def _targeted_generated_file_repair_instructions(*, file_path: str, deterministic_error: str) -> str:
    """Return deterministic, actionable instructions for recurring validation failures."""
    error_text = deterministic_error or ""
    if "生成内容为空" in error_text or "内容为空" in error_text or "empty" in error_text.lower():
        if file_path == "SKILL.md":
            return (
                "当前 SKILL.md 生成内容为空。"
                "这不是可保存状态，必须让模型重新输出非空 SKILL.md 文件内容。"
                "只需生成最小可用 SKILL.md：frontmatter、用途说明、真实脚本调用说明、资源说明和最终产物说明。"
                "如果包含 scripts/*.py，bash fenced block 内必须是一条真实可执行脚本命令，不能是 JSON 伪命令。"
            )

        if file_path.startswith("references/"):
            return (
                "当前 reference 文件生成内容为空。"
                "这不是可保存状态，必须让模型返修为非空 Markdown 参考资料。"
                "只需补齐当前 reference 自身内容：YAML frontmatter、Markdown 标题、与 purpose 对应的参考规则/格式/示例/质量标准。"
                "不要写聊天确认语，不要写 Creator 流程，不要生成其它文件内容。"
            )

        if file_path.startswith("scripts/"):
            return (
                "当前脚本生成内容为空。"
                "这不是可保存状态，必须让模型返修为非空、可解析的单文件源码。"
                "只实现当前脚本职责，保留 JSON argv 入口和 stdout JSON 输出要求。"
                "脚本必须使用 mandatory helper strict_json_argv_guard：从 backend.services.runtime_tools 正确 import，在核心逻辑前对实际使用 argv 调用 strict_json_argv_guard(payload, spec)；不强制固定字段常量名，"
                "拒绝 unknown/missing/empty/type 错误；required 参数禁止靠内部默认值补齐，"
                "可选/defaulted 参数必须在入口 guard spec 中显式声明；"
                "parse_args 必须返回已校验参数，run() 只能使用已校验参数。"
                "禁止保留 input_text/example placeholder、ellipsis、set(...)、{...}、TODO schema 或 placeholder schema。"
            )

        if file_path.startswith("assets/"):
            return (
                "当前 asset 文件生成内容为空。"
                "这不是可保存状态，必须让模型返修为非空静态资源内容；"
                "如果该 asset 是用户上传素材，则不应走模型生成链路。"
            )

    if "generated_pool_forbidden_import" in error_text or "generated_unknown_runtime_tool_import" in error_text:
        return (
            "当前脚本导入了未绑定或不存在的 backend.services.runtime_tools helper，这是硬错误。"
            "不要把 forbidden helper 替换成另一个未绑定 helper，不要伪造 helper 名称。"
            "如果确实需要平台 helper，应请求 tool_pool_patch.add_tool_requests 并等待工具池绑定后再 import。"
            "如果没有平台 helper，但任务可由 Python 标准库或允许的第三方依赖完成，请改为本地实现，不要 import runtime_tools。"
            "TXT 读取使用 open()。DOCX 可用 zipfile + xml.etree.ElementTree 读取 word/document.xml 作为 fallback。"
            "PDF 若没有 extract_pdf_text/pypdf/pdfplumber 等已绑定 helper或可用依赖，则返回明确 blocker，不要假装支持。"
        )

    if "stdout_required_outputs_missing" in error_text:
        return (
            "当前脚本 stdout 缺少 required_outputs。不要改 SKILL.md；不要为了适配所有输入参数重写逻辑。"
            "只修改当前脚本 run()/main() 的输出组织。必须返回 deterministic error 中 missing 列出的字段，且值非空；"
            "保留已有核心业务逻辑和宽松 JSON argv 读取，不要把修复方向转向输入参数重命名。"
            "补齐字段时必须保持 output provenance：字段值必须来自 argv JSON、上游 stdout、reference/assets、"
            "工具结果、模型结果或确定性计算。不得用固定示例值、固定模板、无关默认值或与输入无关的常量凑 required_outputs。"
            "如果当前脚本需要开放式写作或结构化内容生成，应让文本模型返回包含 required_outputs 的 JSON object，"
            "或从模型结果中解析/组织出这些字段；不要只把模型结果放到 text 后再硬编码其它字段。"
        )

    if file_path == "SKILL.md":
        if (
            "workflow_missing" in error_text
            or "没有可执行 bash/sh/shell 命令块" in error_text
            or "缺少调用" in error_text
            or "script_command.exists" in error_text
            or "command_block.fenced_exists" in error_text
            or "fenced code block" in error_text
        ):
            return (
                "按严格 Markdown 执行规范修复 SKILL.md："
                "蓝图真实规划的每个 scripts/ 文件必须有一个标准、独立、无缩进的 ```bash fenced code block；"
                "每个 block 内只放一条命令；命令必须直接调用 scripts/ 路径；"
                "脚本路径后必须传入 json.loads 可解析的 JSON object argv；"
                "所有动态占位符必须作为 JSON 字符串值出现。"
                "不要使用 '''bash；不要只写行内 scripts/*.py；不要把示例/反例路径当成真实脚本。"
            )

        if "frontmatter" in error_text:
            return (
                "修复 SKILL.md YAML frontmatter：文件开头必须是 --- / name / description / ---；"
                "不要求文件末尾追加 ---。只修文件开头 frontmatter。"
            )

        if "蓝图意图不一致" in error_text or "intent" in error_text or "workflow" in error_text or "file_plan" in error_text:
            return (
                "按模型审查意见最小修复 SKILL.md：必须覆盖蓝图真实规划任务、真实 scripts、真实 references、真实 assets、workflow 顺序和最终产物类型；第一轮不要证明内部 stdout/placeholder 闭环；"
                "真实脚本必须使用 ```bash fenced code block；"
                "JSON 配置或 stdout 示例必须使用 ```json fenced code block；"
                "不要把示例/反例路径当成真实文件。"
            )

        return (
            "修复 SKILL.md：保持其作为最终 Skill 使用说明；覆盖蓝图真实任务和 workflow；"
            "删除 Creator 创建流程；确保真实脚本命令块使用 ```bash fence，JSON 示例使用 ```json fence。"
        )

    if file_path.startswith("scripts/"):
        lower_error = error_text.lower()

        if (
            "script_functional.artifact_evidence.missing" in error_text
            or "artifact_evidence.missing" in error_text
            or "stdout 声明产物" in error_text
        ):
            return (
                "当前失败属于文件产物证据失败，只能针对真正具有 artifact/file/path 语义的 stdout 字段修复。"
                "不要把普通业务 stdout 字段改造成文件路径；不要为了通过 artifact 校验把业务数据写成无意义文件。"
                "如果失败字段确实是 pdf_path/image_path/docx_path/pptx_path/html_path/file_paths/file_outputs 等产物字段，"
                "请只修当前脚本的真实文件写入路径和 stdout 路径映射，确保返回路径对应的文件真实存在且内容非空。"
                "如果当前脚本只输出普通业务数据，则应保持普通 JSON 字段，并等待后端 artifact 字段语义修复；不要改 SKILL.md、SkillPlan 或其它脚本。"
            )

        if (
            "script_functional" in error_text
            or "职责" in error_text
            or "responsibility" in lower_error
            or "content_responsibility" in lower_error
        ):
            return (
                "当前失败属于第一轮当前脚本自身语义职责失败，不是 script_smoke 运行失败，也不是 E2E 字段链路失败。"
                "第一轮修复只补当前脚本缺失的语义输入消费、语义产物生成或明确无效内容；"
                "只修当前脚本中校验信息指出的函数、行号或代码区域；"
                "保留已经通过的 import、parse_args/main 入口、strict JSON argv guard、stdout 字段名和文件输出协议；不要把修复变成固定字段名改名；"
                "如果缺少 mandatory guard，必须只修当前脚本：添加 strict_json_argv_guard import，在 parse_args/入口中调用 strict_json_argv_guard(payload, spec)，spec 根据 run() 真实使用参数填写；不要内联 helper，不要改 SKILL.md，不要引入脱离核心逻辑的全局字段词表；"
                "不要一刀切删除 .get/default；但 required 参数不得用 .get(..., default) 或 .get(...) or default 继续执行，"
                "可选/defaulted 参数必须在入口 guard spec 中显式声明；默认值可以由 guard spec 的 default 或 SKILL.md command JSON 明确提供，但不要破坏合法 optional/defaulted 逻辑；"
                "禁止保留 input_text/example placeholder、ellipsis、set(...)、{...}、TODO schema 或 placeholder schema；"
                "不得改 SKILL.md、其它脚本或 SkillPlan；不得进入全量重写；"
                "不得通过 try/except 吞错后输出假成功、固定模板、空值或 mock 数据。"
                "核心 stdout 字段必须具有 provenance：来自 argv JSON、上游 stdout、reference/assets、工具结果、模型结果或确定性计算。"
                "如果脚本调用了模型/工具，模型/工具结果必须参与核心 declared_stdout_fields 或文件产物内容构造；"
                "不得只调用工具生成一个 text，然后硬编码其它业务字段。"
            )

        if (
            "stdout JSON 不得包含 error 字段" in error_text
            or "stdout JSON 至少需要一个非空字段" in error_text
            or "stdout={}" in error_text
            or '"pdf_path": ""' in error_text
            or "'pdf_path': ''" in error_text
        ):
            return (
                "不要通过 try/except 输出 error、{}、空 pdf_path 或空 file_paths 来绕过试运行。"
                "必须修复导致异常的真实代码路径，并输出真实存在的文件路径或真实业务 stdout 数据。"
                "如果当前脚本生成文件产物，请修复真实文件创建路径并返回平台可消费的文件字段。"
                "如果当前脚本输出普通业务字段，请确保字段值来自输入、上游 stdout、reference/assets、工具结果、模型结果或确定性计算，"
                "不得返回固定示例值或无关默认值。"
            )

        if "Markdown 代码块或多文件包" in error_text or "script.raw_source.single_file" in error_text:
            return (
                "本轮必须把上一次内容改成单个裸脚本源码：删除所有 ``` fence、```python/```text 标签、"
                "文件路径标题、写入文件标签、解释性文字和多文件包内容；最终响应第一个字符应是脚本源码字符。"
            )

        if "forbidden_image_generation" in error_text or "调用了图片生成 helper" in error_text:
            return (
                "第一轮不因 required/optional/allowed_capabilities 工具路线阻断。"
                "如调用导致 import/运行/stdout/artifact 失败，repair 只能修当前脚本，不能修改 SkillPlan、capability 声明或 workflow。"
            )

        if (
            "script.required_capabilities.called" in error_text
            or "未调用这些 required_capabilities" in error_text
            or "没有调用这些 required_capabilities" in error_text
        ):
            return (
                "按当前脚本的轻量 script_composition 上下文修复脚本闭环："
                "根据 script_goal、inputs、outputs、available_tools、resource_refs、output_contract、rules 组合工具与本地逻辑；"
                "available_tools 只是候选，最终由单文件 evidence/stdout/artifact 校验和 E2E 数据流校验共同验证。"
                "禁止返回固定 template-only 文本、placeholder、空对象或空路径；蓝图和 SKILL.md 确定后只能修当前脚本。"
                "如果 required outputs 是普通业务 stdout 字段，它们必须来自输入、上游 stdout、工具结果、模型结果或确定性计算，"
                "不能用固定示例值凑字段。"
            )

        if "试运行" in error_text or "JSON 参数" in error_text or "合法 Python" in error_text:
            return (
                "按脚本合同修复：保持单文件源码，修正语法/参数解析/运行错误；"
                "如果 SKILL.md 命令传 JSON，脚本必须读取 sys.argv[1] 并 json.loads，stdout 输出结构化 JSON，至少包含一个非空字段；"
                "字段名由现有 SKILL.md 变量消费关系决定，不要修改蓝图或 SKILL.md。"
                "不要通过 try/except 输出 error、{}、空路径来掩盖真实异常；必须修复导致试运行失败的代码。"
                "修复后输出字段不能是无关固定示例值；核心业务字段必须由输入、上游 stdout、reference/assets、工具结果、模型结果或确定性计算推导。"
            )

    if file_path.startswith("assets/"):
        if "contract 未通过" in error_text or "asset" in error_text or "JSON" in error_text:
            return (
                "按 asset 合同修复：只输出当前资源文件内容；确保非空、JSON 可解析，"
                "删除 Creator 流程、多文件包和运行时代码。"
            )

    if file_path.startswith("references/"):
        if (
            "contract 未通过" in error_text
            or "reference." in error_text
            or "frontmatter" in error_text
            or "Markdown" in error_text
            or "参考资料" in error_text
            or "多文件包" in error_text
            or "Creator" in error_text
        ):
            return (
                "按 reference 合同修复：当前文件必须是一份正式 Markdown 参考资料文档。"
                "必须包含 YAML frontmatter，且 title/description 非空；"
                "frontmatter 顶层只允许 title、description、source、license、metadata。"
                "正文必须包含 Markdown 标题，并提供可复用的规则、示例、约束、格式说明、风格要求或质量标准。"
                "不要输出聊天式澄清问题、确认选项、状态说明或计划询问。"
                "不要添加脚本命令块，不要重新定义 workflow、role、inputs、outputs 或 capabilities。"
                "删除 Creator 流程、写入文件标签和多文件包；"
                "reference 正文可以提到相关脚本路径，但不能把其它文件的完整内容打包进来。"
            )

    return ""

def _file_done_error_sse(
    *,
    file_path: str,
    role: str | None = None,
    error: str,
    error_type: str = "generation_error",
    content: str | None = None,
    recoverable: bool = True,
) -> str:
    """Terminal SSE for failed-but-editable draft state.

    生成/校验/修复失败不是 success，但普通文件必须保持可操作：
    - 前端可以打开编辑；
    - 可以重新生成；
    - 可以写入草稿；
    - 不能因为失败状态变灰、禁用。
    """
    safe_content = content if isinstance(content, str) else ""
    editable = True

    return _sse({
        "type": "file_done",

        # 不再给 failed/error 作为普通可恢复失败的终态。
        "status": "needs_repair",
        "validation_status": "needs_repair",
        "next_file_status": "needs_repair",

        "success": False,
        "file_path": file_path,
        "role": role,
        "error_type": error_type,
        "error": str(error or "生成失败"),
        "message": str(error or "生成失败"),
        "recoverable": bool(recoverable),
        "needs_repair": True,
        "terminal": True,
        "done": True,

        # 关键：失败也带回草稿，且明确可编辑。
        "content": safe_content,
        "draft_content": safe_content,
        "content_chars": len(safe_content),

        "editable": editable,
        "disabled": False,
        "can_edit": editable,
        "can_retry": True,
        "can_write_draft": True,
        "can_skip": True,

        "next_phase": "paused",
        "draft_available": True,
    })

def _safe_validator_localizations(
    validator_data: dict[str, Any],
    *,
    file_path: str,
) -> list[dict[str, Any]]:
    """Extract current-file localization hints from validator output.

    validator 不能新增失败范围，只能解释后端 deterministic_error。
    这里只保留当前文件的定位字段和 minimal_edit，不传全量 issues /
    failed_checks / repair_instructions 给 repair 模型。
    """
    raw_items = validator_data.get("localization")
    if not isinstance(raw_items, list):
        return []

    safe_items: list[dict[str, Any]] = []

    for item in raw_items:
        if not isinstance(item, dict):
            continue

        failed_file = str(item.get("failed_file") or "").strip()
        if failed_file and failed_file != file_path:
            continue

        safe_item = {
            "failed_file": failed_file or file_path,
            "failed_function": item.get("failed_function"),
            "line_region": item.get("line_region"),
            "reason": item.get("reason"),
            "minimal_edit": item.get("minimal_edit"),
            "allowed_scope": item.get("allowed_scope"),
            "forbidden_scope": item.get("forbidden_scope"),
        }

        safe_item = {
            key: value
            for key, value in safe_item.items()
            if value not in (None, "", [], {})
        }

        if safe_item:
            safe_items.append(safe_item)

    return safe_items

async def _run_generated_file_validator_round(
    *,
    file_path: str,
    content: str,
    deterministic_error: str,
    requested_model: str,
    targeted_repair: str = "",
    contract_text: str = "",
    passed_checks_text: str = "",
    failed_checks_text: str = "",
    repair_mode: str = "minimal_edit",
) -> dict:
    """Ask the validator model for advisory localization only.

    后端 deterministic checks / trial run 是唯一裁决来源。

    微调原则：
    - 不新增后台格式词表；
    - 不由后台硬判断哪些是 Markdown 格式类问题；
    - validator 模型如果认为当前 deterministic_error 已经是后端结构化格式/合同问题，
      必须返回 delegate_to_backend_contract=true；
    - 一旦 delegate_to_backend_contract=true，后端不再采纳 localization，
      repair 只依据 deterministic_error / failed_checks_text。
    """
    if not str(deterministic_error or "").strip():
        return {
            "passed": True,
            "issues": [],
            "failed_checks": [],
            "localization": [],
            "preserve": [],
            "repair_instructions": "",
            "delegate_to_backend_contract": False,
            "model": "",
        }

    route = route_model(
        VALIDATOR_TASK,
        requested_model=requested_model,
        reason=f"creator generated file validation: {file_path}",
    )
    _log_creator_model_usage(
        phase="validator.route",
        file_path=file_path,
        route=route,
        extra=f"requested_generation_model={requested_model} repair_mode={repair_mode} content_chars={len(content)}",
    )

    messages = [
        {
            "role": "system",
            "content": (
                "你是 Creator 生成文件校验模型，只输出严格 JSON object。\n\n"

                "你不决定 passed/failed；后端 deterministic checks、ContractCheckResult、"
                "trial run 和 E2E 才是唯一裁决来源。\n\n"

                "你的职责只有两个：\n"
                "1. 如果 deterministic_error 是代码运行、职责实现、异常栈、导入失败、stdout 构造等"
                "需要定位到当前源码区域的问题，你可以给 localization，帮助 coder 做局部 patch。\n"
                "2. 如果 deterministic_error 已经是后端结构化合同/格式问题，必须把它交还给后端合同判断："
                "delegate_to_backend_contract=true，localization=[]，repair_instructions=''。\n\n"

                "特别注意 references/*.md：\n"
                "- references/*.md 是正式 Markdown 参考资料文件；\n"
                "- 如果 failed_checks_text / deterministic_error 已经来自后端 reference 合同检查，"
                "并且后端已经给出 target、expected、minimal_edit，"
                "你不得再重新解释 reference 的 Markdown 文档格式、frontmatter、metadata、正文结构、"
                "fenced block 或资源边界问题；\n"
                "- 这种情况必须返回 delegate_to_backend_contract=true；\n"
                "- 后续 patch 将直接依据后端 ContractCheckResult 修复 reference 文件。\n\n"

                "同样适用于 SKILL.md：\n"
                "- 如果 SKILL.md 的 Markdown/frontmatter/fenced command/resource 合同错误已经由后端结构化失败项给出，"
                "你也必须 delegate_to_backend_contract=true。\n\n"

                "重要边界：\n"
                "- 禁止新增 failed_checks。\n"
                "- 禁止发明 deterministic_error 以外的问题。\n"
                "- 禁止猜输入字段。\n"
                "- 禁止要求修改 SkillPlan、capability、workflow、其它脚本或 E2E。\n"
                "- 禁止把 SKILL.md 或 references/*.md 的文档格式/合同问题重新解释成业务问题。\n"
                "- 如果 failed_checks_text 已经给出后端结构化 check id、target、expected、minimal_edit，"
                "你应优先 delegate_to_backend_contract=true，让后端合同结果直接驱动 patch。\n\n"

                "返回 JSON object，格式：\n"
                "{\n"
                "  \"passed\": false,\n"
                "  \"delegate_to_backend_contract\": true|false,\n"
                "  \"delegate_reason\": \"如果 delegate=true，说明为什么应交给后端合同判断\",\n"
                "  \"issues\": [],\n"
                "  \"failed_checks\": [],\n"
                "  \"localization\": [\n"
                "    {\n"
                "      \"failed_file\": \"当前文件路径\",\n"
                "      \"failed_function\": \"函数名、代码区域、stdout construction 等\",\n"
                "      \"line_region\": \"行号或最小代码区域\",\n"
                "      \"reason\": \"为什么该区域导致 deterministic_error\",\n"
                "      \"minimal_edit\": \"只修复 deterministic_error 的最小修改建议\",\n"
                "      \"allowed_scope\": \"只允许改哪里\",\n"
                "      \"forbidden_scope\": \"不能改哪里\"\n"
                "    }\n"
                "  ],\n"
                "  \"preserve\": [],\n"
                "  \"repair_instructions\": \"只针对 deterministic_error 的局部修改指令\"\n"
                "}\n\n"

                "如果 delegate_to_backend_contract=true：\n"
                "- issues 必须为空数组；\n"
                "- failed_checks 必须为空数组；\n"
                "- localization 必须为空数组；\n"
                "- repair_instructions 必须为空字符串。\n"
            ),
        },
        {
            "role": "user",
            "content": (
                f"目标文件：{file_path}\n\n"
                "后端 deterministic_error，这是唯一真实失败来源：\n"
                f"{deterministic_error}\n\n"
                + (f"当前文件合同摘要：\n{contract_text[-4000:]}\n\n" if contract_text else "")
                + (f"后端结构化 failed_checks_text，只能解释这些，不能新增：\n{failed_checks_text}\n\n" if failed_checks_text else "")
                + (f"本轮修复模式：{repair_mode}\n\n" if repair_mode else "")
                + (f"后端确定性修复边界：\n{targeted_repair}\n\n" if targeted_repair else "")
                + "当前文件内容（尾部截断，仅供定位）：\n"
                "```text\n"
                f"{content[-12000:]}\n"
                "```\n\n"
                "请判断：\n"
                "A. 如果这是后端合同/格式类问题，尤其是 SKILL.md 或 references/*.md 的后端结构化合同失败，"
                "返回 delegate_to_backend_contract=true，不要给 localization，不要再解释 Markdown 文档格式。\n"
                "B. 如果这是代码运行、职责实现或源码局部逻辑问题，返回 delegate_to_backend_contract=false，"
                "并给出 localization。\n"
                "C. 不管哪种情况，passed 都必须是 false，因为后端已经确认本轮失败。\n"
            ),
        },
    ]

    try:
        text = await complete_chat_once(messages, route.model)
        logger.info(
            "[Creator][model] phase=validator.response file=%s model=%s chars=%d repair_mode=%s",
            file_path,
            route.model,
            len(text or ""),
            repair_mode,
        )
    except Exception as exc:
        logger.warning("Creator file validator failed; using deterministic feedback: %s", exc)
        return {
            "passed": False,
            "issues": [],
            "failed_checks": [],
            "localization": [],
            "preserve": [],
            "repair_instructions": "",
            "delegate_to_backend_contract": True,
            "delegate_reason": (
                "validator 不可用；后端将直接使用 deterministic_error / failed_checks_text。"
            ),
            "model": route.model,
        }

    data = _parse_validator_json_object(text)
    if not isinstance(data, dict) or not data:
        return {
            "passed": False,
            "issues": [],
            "failed_checks": [],
            "localization": [],
            "preserve": [],
            "repair_instructions": "",
            "delegate_to_backend_contract": True,
            "delegate_reason": (
                "validator 未返回合法 JSON；后端将直接使用 deterministic_error / failed_checks_text。"
            ),
            "model": route.model,
        }

    delegate_to_backend_contract = bool(data.get("delegate_to_backend_contract"))

    if delegate_to_backend_contract:
        return {
            "passed": False,
            "issues": [],
            "failed_checks": [],
            "localization": [],
            "preserve": [],
            "repair_instructions": "",
            "delegate_to_backend_contract": True,
            "delegate_reason": str(
                data.get("delegate_reason")
                or "validator 判定该失败应交给后端 deterministic contract checks。"
            ),
            "model": route.model,
            "raw_validator_report": data,
        }

    issues = data.get("issues") if isinstance(data.get("issues"), list) else []
    raw_failed_checks = data.get("failed_checks") if isinstance(data.get("failed_checks"), list) else []
    failed_checks = _filter_validator_failed_checks(raw_failed_checks, failed_checks_text)

    instructions = str(data.get("repair_instructions") or data.get("feedback") or "")
    filtered_issues, filtered_instructions = _filter_validator_model_call_misjudgements(
        file_path=file_path,
        deterministic_error=deterministic_error,
        failed_checks_text=failed_checks_text,
        issues=issues,
        instructions=instructions,
    )

    localization = _safe_validator_localizations(data, file_path=file_path)

    deterministic_failed = bool(str(deterministic_error or "").strip())
    return {
        "passed": not deterministic_failed,
        "issues": filtered_issues if deterministic_failed else [],
        "failed_checks": failed_checks if deterministic_failed else [],
        "localization": localization if deterministic_failed else [],
        "preserve": [str(item) for item in data.get("preserve", [])] if isinstance(data.get("preserve"), list) else [],
        "repair_instructions": filtered_instructions if deterministic_failed else "",
        "delegate_to_backend_contract": False,
        "delegate_reason": "",
        "model": route.model,
        "raw_validator_report": data,
    }


def _filter_validator_model_call_misjudgements(
    *,
    file_path: str,
    deterministic_error: str,
    failed_checks_text: str,
    issues: list[Any],
    instructions: str,
) -> tuple[list[str], str]:
    """Ignore model-invented blocking issues.

    The backend has already produced structured checks and filtered model
    failed_checks to deterministic ids. Free-form model issues are kept out of
    blocking/repair decisions so the model can explain existing results without
    inventing new failure items.
    """
    return [], instructions or deterministic_error


def _format_file_validator_feedback(
    deterministic_error: str,
    validator_report: dict,
    targeted_repair: str = "",
    file_path: str | None = None,
) -> str:
    """Build safe repair feedback.

    原则：
    - deterministic_error 是唯一真实失败来源；
    - validator 不能新增失败范围；
    - 如果 validator 自己判断应 delegate_to_backend_contract，
      则完全不采纳 localization / repair_instructions；
    - 不使用后台词表判断格式类问题；
    - SKILL.md 和 references/*.md 的格式/合同类问题都可通过
      delegate_to_backend_contract 交回后端 ContractCheckResult。
    """
    parts = [
        "后端确定性校验/试运行错误，这是本轮唯一需要修复的真实失败：",
        str(deterministic_error or ""),
    ]

    if targeted_repair:
        parts.extend([
            "",
            "后端确定性修复边界：",
            str(targeted_repair),
        ])

    delegate_to_backend_contract = (
        isinstance(validator_report, dict)
        and bool(validator_report.get("delegate_to_backend_contract"))
    )

    if delegate_to_backend_contract:
        parts.extend([
            "",
            "validator 委托说明：",
            str(
                validator_report.get("delegate_reason")
                or "validator 判定该问题应由后端 deterministic contract checks 直接裁决。"
            ),
            "",
            "处理方式：",
            "本轮不采纳 validator localization、issues、failed_checks 或 repair_instructions。",
            "patch 模型只能依据 deterministic_error、后端 failed_checks_text 和 targeted_repair 做最小修改。",
            "如果目标文件是 references/*.md，则直接依据 reference ContractCheckResult 的 target、expected、minimal_edit 修复。",
        ])

    safe_localizations: list[dict[str, Any]] = []

    if not delegate_to_backend_contract and isinstance(validator_report, dict):
        raw_localization = validator_report.get("localization")
        if isinstance(raw_localization, list):
            for item in raw_localization:
                if not isinstance(item, dict):
                    continue

                failed_file = str(item.get("failed_file") or "").strip()
                if file_path and failed_file and failed_file != file_path:
                    continue

                safe_item = {
                    "failed_file": failed_file or file_path or "",
                    "failed_function": item.get("failed_function"),
                    "line_region": item.get("line_region"),
                    "semantic_failure": item.get("semantic_failure") or item.get("reason"),
                    "minimal_edit": item.get("minimal_edit"),
                    "allowed_scope": item.get("allowed_scope"),
                    "forbidden_scope": item.get("forbidden_scope"),
                }
                if item.get("interface_notes"):
                    safe_item["interface_notes_advisory_only"] = "present but omitted from repair goals"

                safe_item = {
                    key: value
                    for key, value in safe_item.items()
                    if value not in (None, "", [], {})
                }

                if safe_item:
                    safe_localizations.append(safe_item)

    if safe_localizations:
        parts.extend([
            "",
            "校验模型对上述真实失败的定位信息，仅用于帮助修复 deterministic_error，不得新增失败范围：",
            json.dumps(safe_localizations, ensure_ascii=False, indent=2, default=str),
        ])

    advisory_only_report = (
        isinstance(validator_report, dict)
        and (validator_report.get("passed") is True or validator_report.get("failure_type") in {"none", None, ""})
        and not validator_report.get("issues")
    )
    repair_text = str(validator_report.get("repair_instructions") or "").strip() if isinstance(validator_report, dict) else ""
    issues_for_repair = validator_report.get("issues") if isinstance(validator_report, dict) else []
    has_structured_blocking_issue = any(
        isinstance(item, dict)
        and str(item.get("requirement_id") or "").strip()
        and isinstance(item.get("missing_evidence"), list)
        and bool(item.get("missing_evidence"))
        for item in (issues_for_repair if isinstance(issues_for_repair, list) else [])
    )
    failure_type = str(validator_report.get("failure_type") or "") if isinstance(validator_report, dict) else ""
    if failure_type in {"script_requirement_validator_error", "script_requirement_validator_incomplete", "validator_error", "validator_incomplete"}:
        advisory_only_report = True
    if (
        not delegate_to_backend_contract
        and isinstance(validator_report, dict)
        and repair_text
        and not advisory_only_report
        and has_structured_blocking_issue
    ):
        parts.extend([
            "",
            "校验模型给出的辅助 repair_instructions，仅作为定位参考，不得覆盖 deterministic_error：",
            repair_text,
        ])

    parts.extend([
        "",
        "严格修复规则：",
        "1. 只修复上面的 deterministic_error。",
        "2. validator localization 只能作为定位和 minimal_edit 参考，不能作为新的失败来源。",
        "3. 不得根据 validator 自行扩展问题范围。",
        "4. 不得修改其它文件、SkillPlan、workflow、上下游脚本或 E2E 映射。",
        "5. 如果 validator 已经 delegate_to_backend_contract=true，不得采纳 validator 的任何 localization。",
        "6. 如果 localization 和 deterministic_error 冲突，以 deterministic_error 为准。",
        "7. 对 Markdown 文件，只修改 deterministic_error 指向的 frontmatter、小节、列表项、段落或 fenced block；保留未失败区域。",
        "8. 对 references/*.md，如果后端 ContractCheckResult 已经给出 reference 合同失败项，直接按该失败项修；不要让 validator 重新定义 reference 格式规则。",
    ])

    return "\n".join(parts)

def _normalize_responsibility_review_issues(
    review: dict[str, Any],
    *,
    file_path: str,
) -> list[dict[str, Any]]:
    """Normalize first-round script responsibility-review issues.

    只服务第一轮 generate-file：
    - 判断当前脚本是否完成 SkillPlanEntry 定义的自身职责；
    - 不处理最终 SKILL.md workflow block；
    - 不处理 E2E step 参数映射；
    - 不处理跨脚本 dataflow。
    """

    if not isinstance(review, dict):
        return []

    raw_items: list[Any] = []

    blocking = review.get("blocking_issues")
    if isinstance(blocking, list):
        raw_items.extend(blocking)

    issues = review.get("issues")
    if isinstance(issues, list):
        raw_items.extend(issues)

    if not raw_items and review.get("passed") is False:
        raw_items.append({
            "issue_type": "content_responsibility",
            "scope": "current_file_only",
            "severity": "error",
            "problem": str(
                review.get("problem")
                or review.get("reason")
                or review.get("message")
                or "职责审查模型判定当前脚本没有完成自身内容职责。"
            ),
            "evidence": str(
                review.get("evidence")
                or "模型返回 passed=false，但没有提供结构化 blocking issue。"
            ),
            "minimal_edit": str(
                review.get("minimal_edit")
                or review.get("repair_instructions")
                or "只修改当前脚本中未完成内容职责的业务逻辑区域。"
            ),
        })

    normalized: list[dict[str, Any]] = []

    for raw in raw_items:
        if not isinstance(raw, dict):
            if review.get("passed") is False:
                raw = {
                    "issue_type": "content_responsibility",
                    "scope": "current_file_only",
                    "severity": "error",
                    "problem": str(raw),
                    "evidence": str(raw),
                    "minimal_edit": "只修改当前脚本中未完成内容职责的业务逻辑区域。",
                }
            else:
                continue

        issue_type = str(raw.get("issue_type") or raw.get("type") or "").strip()
        scope = str(raw.get("scope") or raw.get("category") or "").strip()
        failed_file = str(raw.get("failed_file") or raw.get("target_file") or file_path).strip()
        failure_layer = str(raw.get("failure_layer") or raw.get("layer") or raw.get("layer_type") or "").strip()
        semantic_failure = str(raw.get("semantic_failure") or raw.get("problem") or raw.get("reason") or "").strip()
        severity = str(raw.get("severity") or "").strip().lower()

        if severity in {"note", "info", "advisory"}:
            continue

        if failed_file != file_path:
            continue
        if scope and scope not in {"current_file", "current_file_only", "current file", "current-file"}:
            continue
        if failure_layer and failure_layer not in {"responsibility", "semantic_responsibility"}:
            continue
        if not semantic_failure:
            continue

        problem = str(
            raw.get("semantic_failure")
            or raw.get("problem")
            or raw.get("message")
            or raw.get("reason")
            or raw.get("description")
            or "当前脚本没有完成自身内容职责。"
        ).strip()

        evidence = str(
            raw.get("evidence")
            or raw.get("source_evidence")
            or raw.get("why")
            or raw.get("details")
            or problem
        ).strip()

        minimal_edit = str(
            raw.get("minimal_edit")
            or raw.get("repair_goal")
            or raw.get("fix")
            or raw.get("suggested_fix")
            or review.get("repair_instructions")
            or "只修改当前脚本中未完成内容职责的业务逻辑区域。"
        ).strip()

        raw_issue_id = str(raw.get("id") or raw.get("issue_id") or "").strip()
        normalized_issue_id = raw_issue_id if raw_issue_id in {"tool_contract_mismatch", "tool_support_insufficient"} else "script_functional.responsibility"

        normalized.append({
            "id": normalized_issue_id,
            "failed_file": file_path,
            "failed_function": str(
                raw.get("function")
                or raw.get("failed_function")
                or "run"
            ),
            "code_region": str(
                raw.get("line_region")
                or raw.get("code_region")
                or "current file business logic"
            ),
            "reason": problem or "当前脚本没有完成自身内容职责。",
            "minimal_edit": minimal_edit or "只修改当前脚本中未完成内容职责的业务逻辑区域。",
            "allowed_scope": str(
                raw.get("allowed_scope")
                or "只允许修改当前脚本中未完成内容职责的业务逻辑区域。"
            ),
            "forbidden_scope": str(
                raw.get("forbidden_scope")
                or "不得修改 SKILL.md、workflow、其它脚本、stdout schema、argv 协议、artifact 校验或 E2E 映射。"
            ),
            "details": {
                "issue_type": issue_type or "content_responsibility",
                "scope": scope or "current_file_only",
                "evidence": evidence or problem,
                "raw": raw,
            },
        })

    return normalized


_CURRENT_FILE_SCOPE_VALUES = {"current_file", "current_file_only", "current file", "current-file"}
_RESPONSIBILITY_LAYER_VALUES = {"responsibility", "semantic_responsibility"}


def _is_structured_semantic_responsibility_blocker(item: Any, file_path: str) -> bool:
    """Return True only for a current-file semantic responsibility blocker.

    This predicate is deliberately structural: it does not inspect business
    words, field names, extensions, or concrete skill cases.
    """
    if not isinstance(item, dict):
        return False

    failed_file = str(item.get("failed_file") or item.get("target_file") or file_path).strip()
    if failed_file != file_path:
        return False

    scope = str(item.get("scope") or item.get("repair_scope") or "").strip().lower()
    if scope not in _CURRENT_FILE_SCOPE_VALUES:
        return False

    failure_layer = str(item.get("failure_layer") or item.get("layer") or item.get("layer_type") or "").strip().lower()
    if failure_layer not in _RESPONSIBILITY_LAYER_VALUES:
        return False

    if not str(item.get("semantic_failure") or "").strip():
        return False

    repair_target = str(item.get("repair_target_file") or item.get("minimal_edit_target_file") or "").strip()
    if repair_target and repair_target != file_path:
        return False

    change_requests = item.get("change_requests")
    if isinstance(change_requests, list):
        for request in change_requests:
            if not isinstance(request, dict):
                continue
            target = str(request.get("target_file") or request.get("file") or file_path).strip()
            if target != file_path:
                return False

    return True


def _is_structured_missing_required_evidence(item: dict[str, Any], required_ids: set[str], *, file_path: str) -> bool:
    """Backend-owned blocking predicate for first-round responsibility clues.

    Do not use severity/warning/advisory wording as the gate. A model item is
    actionable only when it structurally points at a required requirement and
    reports missing evidence that can be repaired in the current file.
    """
    if not isinstance(item, dict):
        return False
    rid = str(item.get("requirement_id") or "").strip()
    if not rid or (required_ids and rid not in required_ids):
        return False
    if str(item.get("evidence_level") or "").strip().lower() != "missing":
        return False
    missing = item.get("missing_evidence")
    if not isinstance(missing, list) or not missing:
        return False
    if not _is_structured_semantic_responsibility_blocker(item, file_path):
        return False
    # Scope is normalized by the backend when producing the repair issue; do
    # not infer blocking from advisory/severity/scope wording.
    return True


def _is_blocking_requirement_check(item: dict[str, Any], required_ids: set[str], *, file_path: str) -> bool:
    return _is_structured_missing_required_evidence(item, required_ids, file_path=file_path)


def _coerce_requirement_items(requirements: Any) -> list[RequirementItem]:
    items: list[RequirementItem] = []
    for raw in requirements or []:
        try:
            if isinstance(raw, RequirementItem):
                items.append(raw)
            elif isinstance(raw, dict):
                items.append(RequirementItem(**raw))
        except Exception:
            continue
    return items


def _parse_requirement_review_result(data: dict[str, Any], *, requirements: list[RequirementItem], file_path: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {"passed": True, "failure_type": "script_requirement_validator_error", "issues": [], "advisory_notes": [{"id": "script_requirement_validator_error", "failed_file": file_path, "reason": "review JSON is not an object", "allowed_scope": "do not repair business files"}]}
    advisory_notes = list(data.get("advisory_notes") or []) if isinstance(data.get("advisory_notes"), list) else []
    if data.get("passed") is False and (isinstance(data.get("blocking_issues"), list) or isinstance(data.get("issues"), list)):
        direct_issues = _normalize_responsibility_review_issues(data, file_path=file_path)
        if direct_issues:
            return {"passed": False, "failure_type": "script_requirement_failed", "issues": direct_issues, "checks": [], "advisory_notes": advisory_notes, "repair_instructions": str(data.get("repair_instructions") or ""), "raw_review": data}
    checks = data.get("checks")
    required_ids = {r.id for r in requirements if r.required}
    if not isinstance(checks, list):
        return {"passed": True, "failure_type": "script_requirement_validator_incomplete", "issues": [], "checks": [], "advisory_notes": [*advisory_notes, {"id": "script_requirement_validator_incomplete", "failed_file": file_path, "reason": "review missing checks[]", "allowed_scope": "do not repair business files"}], "raw_review": data}
    seen = {str(c.get("requirement_id") or "") for c in checks if isinstance(c, dict)}
    missing = sorted(required_ids - seen)
    if missing:
        return {"passed": True, "failure_type": "script_requirement_validator_incomplete", "issues": [], "checks": checks, "advisory_notes": [*advisory_notes, {"id": "script_requirement_validator_incomplete", "failed_file": file_path, "reason": "review did not cover all required requirements", "missing_requirement_ids": missing, "allowed_scope": "do not repair business files"}], "raw_review": data}
    blocking: list[dict[str, Any]] = []
    seen_blockers: set[tuple[str, tuple[str, ...], str]] = set()
    for check in checks:
        if not isinstance(check, dict):
            continue
        rid = str(check.get("requirement_id") or "").strip()
        if rid not in required_ids:
            if check not in advisory_notes:
                advisory_notes.append(check)
            continue
        if not _is_blocking_requirement_check(check, required_ids, file_path=file_path):
            continue
        missing_evidence = check.get("missing_evidence") if isinstance(check.get("missing_evidence"), list) else []
        semantic_failure = str(check.get("semantic_failure") or check.get("reason") or check.get("problem") or "Required requirement lacks implementation evidence.")
        key = (rid, tuple(str(item) for item in missing_evidence), semantic_failure)
        if key in seen_blockers:
            continue
        seen_blockers.add(key)
        blocking.append({
            "id": "script_requirement_failed",
            "requirement_id": rid,
            "failed_file": file_path,
            "failed_function": "current script",
            "code_region": str(check.get("code_region") or check.get("target_file") or file_path),
            "reason": semantic_failure,
            "semantic_failure": semantic_failure,
            "missing_evidence": missing_evidence,
            "minimal_edit": str(check.get("minimal_edit") or "Add the smallest implementation evidence for this requirement in the current file."),
            "allowed_scope": "current file only",
            "forbidden_scope": "Do not modify SKILL.md, workflow mapping, field names only, or other files.",
            "details": {"check": {k: v for k, v in check.items() if k != "interface_notes"}, "interface_notes_advisory": check.get("interface_notes") if isinstance(check.get("interface_notes"), list) else []},
        })
    if blocking:
        return {"passed": False, "failure_type": "script_requirement_failed", "issues": blocking, "checks": checks, "advisory_notes": advisory_notes, "repair_instructions": str(data.get("repair_instructions") or ""), "raw_review": data}
    return {"passed": True, "failure_type": "", "issues": [], "checks": checks, "advisory_notes": advisory_notes, "repair_instructions": "", "raw_review": data}

def _detect_script_responsibility_static_blockers(
    script_content: str,
    skill_plan_entry: SkillPlanEntry,
    requirements: Any,
) -> list[dict[str, Any]]:
    """Conservative first-round check for required input -> core product paths.

    This is intentionally small: it only blocks when there is no AST evidence that
    declared/semantic required inputs are read and flow into a generic constructed
    product/helper/output path. Field aliases, variable names, and extra stdout
    metadata are not treated as failures.
    """
    req_items = [req for req in _coerce_requirement_items(requirements) if getattr(req, "required", False)]
    if not req_items or not str(script_content or "").strip():
        return []
    # First-round responsibility validation must be behavioral, not name-based.
    # SkillPlan.inputs/outputs and requirement semantic_inputs/semantic_outputs
    # are semantic responsibility slots, not literal key requirements. Do not
    # require them to appear as string literals, dict keys, variable names, or
    # stdout keys. E2E owns interface/schema alignment; this static fallback only
    # looks for a semantic input/tool signal flowing into constructed
    # product/helper/output behavior.

    try:
        tree = ast.parse(script_content or "")
    except SyntaxError:
        return []

    input_roots = {"payload", "argv", "args", "input", "inputs", "data", "context", "params", "config", "options"}
    structural_core_names = {"blocks", "sections", "items", "pages", "document", "documents", "rows", "table", "tables", "content", "contents", "result", "results", "artifact", "artifacts", "output", "outputs"}
    core_names = structural_core_names
    helper_terms = tuple(sorted(structural_core_names))

    aliases: set[str] = set()
    core_vars: set[str] = set()
    helper_result_vars: set[str] = set()
    has_input_read = False
    has_tainted_core = False
    has_core_structure = False
    has_core_output = False
    has_tool_call = False

    def _name(n: ast.AST) -> str:
        if isinstance(n, ast.Name):
            return n.id
        if isinstance(n, ast.Attribute):
            return n.attr
        return ""

    def _call_name(n: ast.AST) -> str:
        if isinstance(n, ast.Call):
            return _name(n.func).lower()
        return ""

    def _contains_input_read(n: ast.AST) -> bool:
        for child in ast.walk(n):
            if isinstance(child, ast.Name) and (child.id in input_roots or child.id in aliases):
                return True
            if isinstance(child, ast.Attribute) and child.attr in input_roots:
                return True
            if isinstance(child, ast.Subscript):
                root = _name(child.value)
                if root in input_roots or root in aliases:
                    return True
        return False

    def _is_core_expr(n: ast.AST) -> bool:
        call = _call_name(n)
        if call and any(term in call for term in helper_terms):
            return True
        if isinstance(n, (ast.List, ast.Dict, ast.Tuple, ast.JoinedStr)):
            return True
        return any(isinstance(child, ast.Name) and child.id in core_names for child in ast.walk(n))

    def _is_tainted(n: ast.AST) -> bool:
        return _contains_input_read(n) or any(isinstance(child, ast.Name) and child.id in core_vars for child in ast.walk(n))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in node.args.args:
                if arg.arg:
                    aliases.add(arg.arg)
        if isinstance(node, ast.Assign):
            if _contains_input_read(node.value):
                has_input_read = True
                for target in node.targets:
                    name = _name(target)
                    if name:
                        aliases.add(name)
            if _is_core_expr(node.value) and _is_tainted(node.value):
                has_tainted_core = True
                for target in node.targets:
                    name = _name(target)
                    if name:
                        core_vars.add(name)
                        if _call_name(node.value):
                            helper_result_vars.add(name)
            if _is_core_expr(node.value):
                has_core_structure = True
            if _call_name(node.value):
                has_tool_call = True
        elif isinstance(node, ast.AnnAssign):
            value = node.value
            if value is not None and _contains_input_read(value):
                has_input_read = True
                name = _name(node.target)
                if name:
                    aliases.add(name)
            if value is not None and _is_core_expr(value) and _is_tainted(value):
                has_tainted_core = True
                name = _name(node.target)
                if name:
                    core_vars.add(name)
            if value is not None and _is_core_expr(value):
                has_core_structure = True
            if value is not None and _call_name(value):
                has_tool_call = True
        elif isinstance(node, ast.Call):
            has_tool_call = True
            if _contains_input_read(node):
                has_input_read = True
            call = _call_name(node)
            if call and any(term in call for term in helper_terms) and _is_tainted(node):
                has_tainted_core = True
                has_core_output = True
        elif isinstance(node, ast.Return):
            if node.value is not None and (_is_tainted(node.value) or any(isinstance(child, ast.Name) and child.id in helper_result_vars for child in ast.walk(node.value))):
                has_core_output = True
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = _call_name(node.value)
            if call == "print" and (_is_tainted(node.value) or any(isinstance(child, ast.Name) and child.id in helper_result_vars for child in ast.walk(node.value))):
                has_core_output = True

    weak_evidence = has_input_read and (has_tainted_core or has_core_output)
    functional_evidence = (
        (has_input_read or has_tool_call)
        and (has_core_structure or has_tool_call or has_tainted_core)
        and has_core_output
    )
    if weak_evidence or functional_evidence:
        return []

    requirement_id = req_items[0].id
    failed_file = getattr(skill_plan_entry, "path", "") or req_items[0].target_file
    return [{
        "id": "semantic_responsibility_missing",
        "requirement_id": requirement_id,
        "failed_file": failed_file,
        "failed_function": "current script",
        "code_region": "run() input and product construction path",
        "reason": "Current script lacks conservative static evidence that semantic inputs or registered tool/model results participate in the current file's core semantic product construction.",
        "missing_evidence": ["semantic input or tool/model result participates in core product construction", "constructed semantic product/helper result is returned or printed"],
        "minimal_edit": "只修改当前脚本 run() 中缺失的语义输入消费和语义产物构造逻辑；不要为了通过第一轮而改成固定字段名。",
        "allowed_scope": "current script only",
    }]


def _detect_stub_implementations(
    script_content: str,
    skill_plan_entry: Any,
) -> list[dict[str, Any]]:
    """Detect empty-shell functions and empty conditional branches.

    Checks for:
    - Functions/methods whose body consists only of ``pass``, ``...``, a bare
      ``raise NotImplementedError``, or a docstring with no real logic.
    - ``if``/``elif``/``else`` branches that consist only of ``pass`` or ``...``.

    These are treated as unfulfilled responsibilities and trigger re-exploration
    of the tool library rather than direct repair so the judgment model can
    evaluate them with an updated tool pool.
    """
    issues: list[dict[str, Any]] = []
    try:
        tree = ast.parse(script_content or "")
    except SyntaxError:
        return issues

    file_path = getattr(skill_plan_entry, 'path', '') or (
        skill_plan_entry.get('path', '') if isinstance(skill_plan_entry, dict) else ''
    )

    def _real_stmts(stmts: list[ast.stmt]) -> list[ast.stmt]:
        """Strip leading docstring constants; return remaining real statements."""
        result = list(stmts)
        if result and isinstance(result[0], ast.Expr) and isinstance(result[0].value, ast.Constant) and isinstance(result[0].value.value, str):
            result = result[1:]
        return result

    def _is_stub_body(stmts: list[ast.stmt]) -> bool:
        real = _real_stmts(stmts)
        if not real:
            return True  # empty or docstring-only
        if len(real) == 1:
            s = real[0]
            if isinstance(s, ast.Pass):
                return True
            # bare Ellipsis: ...
            if isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and s.value.value is ...:
                return True
            if isinstance(s, ast.Raise):
                exc = s.exc
                if exc is not None:
                    name = ''
                    if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
                        name = exc.func.id
                    elif isinstance(exc, ast.Name):
                        name = exc.id
                    if 'NotImplemented' in name or 'TODO' in name:
                        return True
        return False

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Skip dunder methods and private helpers
            if node.name.startswith('__') and node.name.endswith('__'):
                continue
            if _is_stub_body(node.body):
                issues.append({
                    'id': 'stub_implementation',
                    'requirement_id': '',
                    'failed_file': file_path,
                    'failed_function': node.name,
                    'code_region': f'def {node.name}() line {node.lineno}',
                    'reason': (
                        f"Function '{node.name}' has an empty-shell implementation "
                        '(only pass/ellipsis/raise NotImplementedError). '
                        'This indicates unfulfilled responsibility; consider re-exploring '
                        'the tool library for a suitable helper.'
                    ),
                    'missing_evidence': ['real implementation of function body'],
                    'minimal_edit': (
                        f"Implement the actual business logic inside '{node.name}'; "
                        'if a platform helper is needed, request it via the tool pool '
                        'rather than leaving an empty shell.'
                    ),
                })
            else:
                # Check for empty conditional branches inside the function
                for child in ast.walk(node):
                    if isinstance(child, ast.If):
                        for branch_stmts, branch_label in [
                            (child.body, 'if branch'),
                            (child.orelse, 'else/elif branch'),
                        ]:
                            if branch_stmts and _is_stub_body(branch_stmts):
                                issues.append({
                                    'id': 'stub_branch',
                                    'requirement_id': '',
                                    'failed_file': file_path,
                                    'failed_function': node.name,
                                    'code_region': f'{branch_label} inside {node.name} near line {child.lineno}',
                                    'reason': (
                                        f"A {branch_label} inside '{node.name}' is an empty shell "
                                        '(only pass/ellipsis). This may leave a responsibility unmet.'
                                    ),
                                    'missing_evidence': [f'real logic in {branch_label}'],
                                    'minimal_edit': (
                                        f"Fill the {branch_label} inside '{node.name}' with the required logic."
                                    ),
                                })
    return issues


def _runtime_tool_contract_static_blockers(
    script_content: str,
    skill_plan_entry: SkillPlanEntry,
    requirements: Any = None,
) -> list[dict[str, Any]]:
    """Deterministically validate runtime_tools helper imports/calls.

    This check must use the same file-binding contract as runtime_import_guard.
    It must not re-derive allowed tools from role, required_capabilities, or
    natural-language responsibility hints.
    """
    if not str(script_content or "").strip():
        return []

    try:
        tree = ast.parse(script_content or "")
    except SyntaxError:
        return []

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

    runtime_prefix = "backend.services.runtime_tools"
    imported_helpers: dict[str, str] = {}
    runtime_module_aliases: dict[str, str] = {}
    imported_modules: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = str(node.module or "")
            if module == runtime_prefix or module.startswith(runtime_prefix + "."):
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    imported_helpers[alias.asname or alias.name] = alias.name
                    imported_modules.add(module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = str(alias.name or "")
                if name == runtime_prefix or name.startswith(runtime_prefix + "."):
                    imported_modules.add(name)
                    runtime_module_aliases[alias.asname or name.rsplit(".", 1)[-1]] = name

    if not imported_helpers and not imported_modules:
        return []

    runtime_contract = getattr(skill_plan_entry, "runtime_contract", None)
    if not isinstance(runtime_contract, dict):
        runtime_contract = {}

    binding = runtime_contract.get("tool_binding_summary")
    if not isinstance(binding, dict):
        binding = {}

    allowed_tool_ids: set[str] = set()
    allowed_tool_ids.update(_string_list(binding.get("allowed_tool_ids")))
    allowed_tool_ids.update(_string_list(binding.get("primary_tool_ids")))
    allowed_tool_ids.update(_string_list(binding.get("secondary_tool_ids")))

    # Legacy runtime_contract selected/allowed tool ids are allowed only as
    # deterministic resolver output, not as natural-language role/capability text.
    allowed_tool_ids.update(_string_list(runtime_contract.get("selected_tools")))
    allowed_tool_ids.update(_string_list(runtime_contract.get("allowed_tools")))
    allowed_tool_ids.update(_string_list(runtime_contract.get("tool_names")))

    allowed_helpers: set[str] = set(_string_list(binding.get("allowed_helper_imports")))
    allowed_helpers.update(_string_list(runtime_contract.get("allowed_helper_imports")))

    allowed_import_paths: set[str] = set(_string_list(binding.get("allowed_import_paths")))
    allowed_import_paths.update(_string_list(runtime_contract.get("allowed_imports")))

    allowed_function_imports: set[str] = set(_string_list(binding.get("allowed_function_imports")))
    allowed_import_paths.update(allowed_function_imports)

    try:
        from backend.services.runtime_tools import __all__ as runtime_tools_all  # type: ignore
        exported_runtime_helpers = {str(item) for item in runtime_tools_all}
    except Exception:
        exported_runtime_helpers = set()

    all_caps = list_tool_capabilities()
    known_tool_names = {
        str(getattr(cap, "name", "") or "").strip()
        for cap in all_caps
        if str(getattr(cap, "name", "") or "").strip()
    }
    allowed_tool_ids = {tool_id for tool_id in allowed_tool_ids if tool_id in known_tool_names}

    helper_to_tools: dict[str, set[str]] = {}
    helper_to_imports: dict[str, set[str]] = {}

    for cap in all_caps:
        cap_name = str(getattr(cap, "name", "") or "").strip()
        if not cap_name:
            continue

        for helper in list(getattr(cap, "helper_imports", []) or []):
            helper_name = str(helper or "").strip()
            if helper_name:
                helper_to_tools.setdefault(helper_name, set()).add(cap_name)

        for fn in list(getattr(cap, "functions", []) or []):
            function_name = str(getattr(fn, "function_name", "") or "").strip()
            import_path = str(getattr(fn, "import_path", "") or "").strip()
            if function_name:
                helper_to_tools.setdefault(function_name, set()).add(cap_name)
                if import_path:
                    helper_to_imports.setdefault(function_name, set()).add(import_path)

    # Expand allowed helpers/imports from explicitly bound tools.
    for cap in all_caps:
        cap_name = str(getattr(cap, "name", "") or "").strip()
        if cap_name not in allowed_tool_ids:
            continue

        allowed_helpers.update(
            str(item).strip()
            for item in (getattr(cap, "helper_imports", []) or [])
            if str(item or "").strip()
        )

        for fn in list(getattr(cap, "functions", []) or []):
            function_name = str(getattr(fn, "function_name", "") or "").strip()
            import_path = str(getattr(fn, "import_path", "") or "").strip()
            if function_name:
                allowed_helpers.add(function_name)
            if import_path:
                allowed_import_paths.add(import_path)
                if function_name:
                    allowed_import_paths.add(f"{import_path}.{function_name}")

    allowlist_present = bool(
        allowed_helpers
        or allowed_import_paths
        or allowed_function_imports
        or allowed_tool_ids
    )

    issues: list[dict[str, Any]] = []
    req_items = _coerce_requirement_items(requirements) or _coerce_requirement_items(getattr(skill_plan_entry, "requirements", []))
    requirement_id = req_items[0].id if req_items else ""
    failed_file = getattr(skill_plan_entry, "path", "") or (req_items[0].target_file if req_items else "")

    def helper_known(helper_name: str) -> bool:
        return (
            helper_name in exported_runtime_helpers
            or helper_name in helper_to_tools
            or helper_name in allowed_helpers
        )

    def helper_allowed(helper_name: str, imported_module: str = "") -> bool:
        if not allowlist_present:
            return True
        if helper_name in allowed_helpers:
            return True
        if f"{runtime_prefix}.{helper_name}" in allowed_import_paths:
            return True
        if imported_module and imported_module in allowed_import_paths:
            return True
        helper_imports = helper_to_imports.get(helper_name) or set()
        if helper_imports & allowed_import_paths:
            return True
        if any(f"{path}.{helper_name}" in allowed_import_paths for path in helper_imports):
            return True
        return False

    for local_name, helper_name in sorted(imported_helpers.items()):
        module_ok = (
            runtime_prefix in imported_modules
            or any(module == runtime_prefix or module.startswith(runtime_prefix + ".") for module in imported_modules)
        )
        known = helper_known(helper_name)
        allowed = helper_allowed(helper_name, runtime_prefix)

        if not known or not module_ok or not allowed:
            issues.append({
                "id": "tool_contract_mismatch",
                "requirement_id": requirement_id,
                "failed_file": failed_file,
                "failed_function": local_name,
                "code_region": f"runtime_tools import/call: {helper_name}",
                "reason": (
                    "Imported runtime helper is not exported by runtime_tools or registered/bound tool metadata."
                    if not known or not module_ok
                    else "Imported runtime helper is not allowed by current_file_tool_binding."
                ),
                "missing_evidence": ["runtime helper must be present in current_file_tool_binding.allowed_helper_imports"],
                "minimal_edit": (
                    "Use only helpers allowed by Current File Tool Binding, or implement the responsibility "
                    "without importing backend.services.runtime_tools."
                ),
                "allowed_scope": "current script only",
                "details": {
                    "helper": helper_name,
                    "binding_allowed_tool_ids": sorted(allowed_tool_ids),
                    "binding_allowed_helpers": sorted(allowed_helpers),
                    "binding_allowed_import_paths": sorted(allowed_import_paths),
                    "known_tools_for_helper": sorted(helper_to_tools.get(helper_name) or []),
                },
            })

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not isinstance(node.func.value, ast.Name) or node.func.value.id not in runtime_module_aliases:
            continue

        helper_name = str(node.func.attr or "")
        module_name = runtime_module_aliases.get(node.func.value.id, "")

        known = helper_known(helper_name)
        allowed = helper_allowed(helper_name, module_name)

        if not known or not allowed:
            issues.append({
                "id": "tool_contract_mismatch",
                "requirement_id": requirement_id,
                "failed_file": failed_file,
                "failed_function": helper_name,
                "code_region": f"runtime_tools call: {node.func.value.id}.{helper_name}",
                "reason": (
                    "Called runtime helper is not exported by runtime_tools or registered/bound tool metadata."
                    if not known
                    else "Called runtime helper is not allowed by current_file_tool_binding."
                ),
                "missing_evidence": ["runtime helper must be present in current_file_tool_binding.allowed_helper_imports"],
                "minimal_edit": (
                    "Use only helpers allowed by Current File Tool Binding, or implement the responsibility "
                    "without importing backend.services.runtime_tools."
                ),
                "allowed_scope": "current script only",
                "details": {
                    "helper": helper_name,
                    "binding_allowed_tool_ids": sorted(allowed_tool_ids),
                    "binding_allowed_helpers": sorted(allowed_helpers),
                    "binding_allowed_import_paths": sorted(allowed_import_paths),
                    "known_tools_for_helper": sorted(helper_to_tools.get(helper_name) or []),
                },
            })

    return issues


def detect_error_stdout_bypass(script_content: str, requirements: list[RequirementItem], expected_outputs: list[str] | None = None) -> list[dict[str, Any]]:
    if not requirements:
        return []
    text = str(script_content or "")
    if re.search(r"except\s+Exception[^:]*:([\s\S]{0,500}?)(return|print)\s*\(?\s*\{[^}]*['\"]error['\"]", text):
        return [{"id": "fake_success_or_error_stdout_bypass", "requirement_id": requirements[0].id, "failed_file": requirements[0].target_file, "failed_function": "exception handler", "code_region": "except Exception", "reason": "catch Exception returns only an error object without clear expected output evidence.", "missing_evidence": ["expected outputs are not preserved on the error bypass path"], "minimal_edit": "Return/print expected output evidence or re-raise failures instead of treating error-only stdout as success."}]
    return []



def _python_static_evidence(script_content: str) -> dict[str, Any]:
    evidence = {
        "input_reads": False,
        "generic_builders": False,
        "output_writes": False,
        "called_functions": set(),
        "string_literals": set(),
    }
    try:
        tree = ast.parse(script_content or "")
    except Exception:
        return evidence
    generic_container_names = {"blocks", "items", "sections", "pages", "slides", "rows", "options", "config", "parameters", "styles"}
    generic_input_names = {"payload", "input", "inputs", "fields", "options", "config", "parameters"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id in generic_input_names:
                evidence["input_reads"] = True
            if node.id in generic_container_names:
                evidence["generic_builders"] = True
        elif isinstance(node, ast.Attribute):
            if node.attr in generic_input_names:
                evidence["input_reads"] = True
            if node.attr in generic_container_names:
                evidence["generic_builders"] = True
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                evidence["called_functions"].add(func.id)
            elif isinstance(func, ast.Attribute):
                evidence["called_functions"].add(func.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value.strip().lower()
            if text:
                evidence["string_literals"].add(text)
        elif isinstance(node, (ast.Return, ast.Expr)):
            dump = ast.dump(node).lower()
            if "print" in dump or "return" in dump:
                evidence["output_writes"] = True
    return evidence


def _requirement_terms(requirement: RequirementItem) -> list[str]:
    terms: list[str] = []
    for value in [requirement.description, *requirement.required_components, *requirement.semantic_outputs]:
        text = str(value or "").strip().lower()
        if text:
            terms.append(text)
    for constraint in requirement.constraints or []:
        for value in (getattr(constraint, "name", ""), getattr(constraint, "value", "")):
            text = str(value or "").strip().lower()
            if text:
                terms.append(text)
    return terms


def _term_has_evidence(term: str, evidence: dict[str, Any]) -> bool:
    literals = evidence.get("string_literals") or set()
    # Requirement-derived terms are the only semantic probes; no business word list.
    compact = term[:80]
    return any(compact in literal or literal in compact for literal in literals if literal)

def detect_required_component_coverage(script_content: str, requirements: list[RequirementItem]) -> list[dict[str, Any]]:
    if not requirements or not str(script_content or "").strip():
        return []
    text = str(script_content or "")
    evidence = _python_static_evidence(text)
    issues=[]
    for req in requirements:
        if not req.required or not req.required_components:
            continue
        component_terms = [str(item or "").strip().lower() for item in req.required_components if str(item or "").strip()]
        has_semantic_literal = any(_term_has_evidence(term, evidence) for term in component_terms)
        has_core_path = bool(evidence.get("generic_builders") and evidence.get("output_writes") and (evidence.get("input_reads") or has_semantic_literal))
        if len(text.strip()) < 120 or not has_core_path:
            issues.append({"id":"script_requirement_failed","requirement_id":req.id,"failed_file":req.target_file,"failed_function":"current script","code_region":"file","reason":"No conservative static evidence of an input-read → generic construction → output path for required components.","missing_evidence":["input_reads + generic_builders + output_writes"],"minimal_edit":"Add the required component into a generic constructed object and return/print it from the current script."})
    return issues


def detect_required_constraint_application(script_content: str, requirements: list[RequirementItem]) -> list[dict[str, Any]]:
    if not requirements or not str(script_content or "").strip():
        return []
    evidence = _python_static_evidence(script_content)
    issues: list[dict[str, Any]] = []
    for req in requirements:
        required_constraints = [c for c in (req.constraints or []) if getattr(c, "required", True) and str(getattr(c, "source", "") or "") in {"user_explicit", "blueprint", "inferred"}]
        if not req.required or not required_constraints:
            continue
        has_constraint_landing = bool(evidence.get("generic_builders"))
        if not has_constraint_landing:
            first = required_constraints[0]
            issues.append({"id":"script_requirement_failed","requirement_id":req.id,"failed_file":req.target_file,"failed_function":"current script","code_region":"styles/options/config/parameters","reason":"No conservative static evidence of any styles/options/config/parameters landing for required constraints.","missing_evidence":[f"constraint_landing:{getattr(first, 'name', '') or getattr(first, 'kind', '')}"],"minimal_edit":"Apply required constraints through a generic styles/options/config/parameters structure or equivalent builder argument."})
    return issues


def detect_requirement_evidence_static(script_content: str, requirements: list[RequirementItem], expected_outputs: list[str] | None = None) -> list[dict[str, Any]]:
    issues=[]
    # Keep only deterministic structural bypass checks here. Core responsibility
    # flow is handled by _detect_script_responsibility_static_blockers so field
    # names, variable names, and extra stdout metadata remain advisory-only.
    issues.extend(detect_error_stdout_bypass(script_content, requirements, expected_outputs))
    return issues


def _script_responsibility_schema_error(data: Any, *, file_path: str) -> str:
    if not isinstance(data, dict) or not data:
        return "职责审查模型未返回 JSON object。"
    if "passed" not in data:
        return "职责审查模型 JSON 缺少 required bool 字段 passed。"
    if not isinstance(data.get("passed"), bool):
        return "职责审查模型 JSON 字段 passed 必须是 bool。"
    if "blocking_issues" not in data:
        return "职责审查模型 JSON 缺少 required list 字段 blocking_issues。"
    if not isinstance(data.get("blocking_issues"), list):
        return "职责审查模型 JSON 字段 blocking_issues 必须是 list。"
    if "advisory_notes" in data and not isinstance(data.get("advisory_notes"), list):
        return "职责审查模型 JSON 字段 advisory_notes 必须是 list。"
    if "repair_instructions" in data and not isinstance(data.get("repair_instructions"), str):
        return "职责审查模型 JSON 字段 repair_instructions 必须是 string。"
    if data.get("passed") is True and data.get("blocking_issues"):
        return "职责审查模型返回 passed=true 但 blocking_issues 非空。"
    return ""


def _script_responsibility_validator_failure(
    *,
    file_path: str,
    reason: str,
    raw: Any,
    model: str,
) -> dict[str, Any]:
    return {
        "passed": False,
        "issues": [{
            "id": "script_responsibility.validator_schema_invalid",
            "failed_file": file_path,
            "failed_function": "responsibility_review",
            "code_region": "review",
            "reason": reason or "职责审查模型输出 schema invalid。",
            "minimal_edit": "不是脚本内容错误；请重试或切换 validator 模型。",
            "allowed_scope": "do not repair business files for validator response format",
            "details": {"raw": str(raw or "")[:1000]},
        }],
        "advisory_notes": [],
        "repair_instructions": "职责审查模型输出格式/协议连续失败，不能放行当前脚本。",
        "failure_type": "script_requirement_validator_incomplete",
        "model": model,
    }

async def _run_script_responsibility_review(
    *,
    file_path: str,
    script_content: str,
    skill_plan_entry: SkillPlanEntry,
    requirements: Any = None,
    deterministic_issues: list[dict[str, Any]] | None = None,
    requested_model: str | None = None,
    review_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """First-round script responsibility review.

    第一轮只判断当前脚本源码是否覆盖自身负责的语义任务。

    不判断：
    - 脚本能不能实际运行；
    - JSON argv 是否正确；
    - stdout 字段名是否正确；
    - artifact 是否真实存在；
    - 上下游字段映射；
    - 具体输入输出变量名；
    - 最终 E2E 闭环。

    Backend 在 Model Judge 前只保留客观工具合同事实检查；
    pass/空壳/变量名/AST 结构是否完成职责交给 Model Judge。
    """

    req_items = _coerce_requirement_items(requirements) or _coerce_requirement_items(getattr(skill_plan_entry, "requirements", []))
    deterministic_issues = deterministic_issues or []
    review_context = review_context if isinstance(review_context, dict) else {}
    current_file_tool_binding = (
        review_context.get(
            "current_file_tool_binding"
        )
    )

    if not isinstance(
        current_file_tool_binding,
        dict,
    ):
        current_file_tool_binding = {}

    if (
        str(
            getattr(
                skill_plan_entry,
                "runtime",
                "",
            )
            or ""
        ).strip().lower()
        == "python"
    ):
        current_file_tool_binding = dict(
            current_file_tool_binding
        )

        for key, value in (
            (
                "allowed_tool_ids",
                "script_argv_guard",
            ),
            (
                "primary_tool_ids",
                "script_argv_guard",
            ),
            (
                "allowed_helper_imports",
                "strict_json_argv_guard",
            ),
        ):
            values = [
                str(item).strip()
                for item in (
                    current_file_tool_binding.get(
                        key
                    )
                    or []
                )
                if str(item or "").strip()
            ]

            if value not in values:
                values.append(value)

            current_file_tool_binding[
                key
            ] = values

    provided_function_execution_context = review_context.get("function_execution_context")
    if isinstance(provided_function_execution_context, dict):
        function_execution_context = dict(provided_function_execution_context)
        authorized_tool_contracts = list(
            function_execution_context.get(
                "authorized_tool_contracts"
            )
            or []
        )
        seen_tool_ids = {
            str(contract.get("tool_id") or "").strip()
            for contract in authorized_tool_contracts
            if isinstance(contract, dict)
        }
        for contract in tool_contracts_from_binding(
            current_file_tool_binding or {}
        ):
            tool_id = str(contract.get("tool_id") or "").strip()
            if tool_id and tool_id in seen_tool_ids:
                continue
            if tool_id:
                seen_tool_ids.add(tool_id)
            authorized_tool_contracts.append(contract)
        function_execution_context["authorized_tool_contracts"] = authorized_tool_contracts
    else:
        function_execution_context = build_function_execution_context(
            graph=review_context.get("requirement_graph"),
            target_file=file_path,
            current_file_tool_binding=current_file_tool_binding,
            fallback_function_item=(function_item_prompt_payload(req_items[0]) if req_items else {}),
        )
    authorized_tool_contracts = list(function_execution_context.get("authorized_tool_contracts") or [])
    if deterministic_issues:
        logger.info(
            "[Creator]"
            "[script_responsibility]"
            "[tool_contract_context] %s",
            json.dumps(
                {
                    "event": (
                        "script_responsibility_"
                        "tool_contract_context"
                    ),
                    "file_path": file_path,
                    "authorized_tool_ids": [
                        contract.get("tool_id")
                        for contract
                        in authorized_tool_contracts
                    ],
                    "tool_contract_count": len(
                        authorized_tool_contracts
                    ),
                    "callable_functions": [
                        (
                            f"{contract.get('tool_id')}."
                            f"{function.get('function_name')}"
                        )
                        for contract
                        in authorized_tool_contracts
                        for function
                        in (
                                contract.get(
                                    "functions"
                                )
                                or []
                        )
                        if isinstance(
                            function,
                            dict,
                        )
                           and str(
                            function.get(
                                "function_name"
                            )
                            or ""
                        ).strip()
                    ],
                },
                ensure_ascii=False,
                default=str,
            ),
        )
        return {
            "passed": False,
            "issues": deterministic_issues,
            "repair_instructions": "按确定性工具合同/功能责任检查结果修复当前脚本源码。",
            "failure_type": "script_requirement_failed",
            "model": "deterministic",
        }

    route = route_model(
        VALIDATOR_TASK,
        requested_model=requested_model,
        reason=f"creator first-round script responsibility review: {file_path}",
    )
    _log_creator_model_usage(
        phase="script_responsibility.route",
        file_path=file_path,
        route=route,
        model=requested_model,
    )

    short_contract = str(getattr(skill_plan_entry, "purpose", "") or "").strip()
    blueprint_text = str(review_context.get("blueprint_text") or review_context.get("blueprint") or "").strip()
    workflow_allocation_summary = str(review_context.get("workflow_allocation_summary") or "").strip()
    trial_stdout = review_context.get("trial_stdout_json", review_context.get("trial_stdout", ""))
    artifact_info = review_context.get("artifact_info", review_context.get("artifact_paths", []))
    graph_context = function_execution_context
    req_payload = [graph_context.get("function_item") or function_item_prompt_payload(item) for item in req_items]

    messages = [
        {
            "role": "system",
            "content": (
                "你是 Creator 第一轮单脚本职责审查模型，只输出严格 JSON object。\n\n"

                "你只判断当前 scripts/** 源码是否覆盖自身负责的语义任务；也就是只判断当前脚本是否完成自身职责、是否完成 purpose 短合同表达的职责。"
                "不要判断其它文件、workflow、字段名、审美或充分性细节；不要求固定字段名。\n\n"

                "语义职责槽位参考：purpose、requirements、workflow_allocation_summary 只用于判断当前脚本自身职责是否完成。\n"
                "Judge checks whether the current script implements its FunctionItem. Reference files may only serve as dependency/resource evidence for the current FunctionItem; SKILL.md, references/**, and assets/** do not own executable workflow responsibilities.\n\n"
                "核心原则（图谱式可观察边界）：\n"
                "- 当前脚本的语义职责以 current script FunctionItem（通过现有 requirements/responsibility_requirements payload 传输）的 purpose、must_do、must_not_do 和 constraints 为准；inputs/outputs 只是接口提示。\n"
                "- FunctionItem describes what the current script owns. Incoming ResponsibilityEdges describe what upstream responsibilities must provide to the current script. Outgoing ResponsibilityEdges describe what the current script must make available to downstream responsibilities. Required edge constraints must be checked against the current FunctionItem implementation.\n"
                "- requirements.constraints 是当前文件拥有的开放责任约束。\n"
                "- 所有 required=true constraints 都必须检查实现证据。\n"
                "- 根据完整 constraint object 理解约束语义。\n"
                "- 不存在固定 constraint vocabulary。\n"
                "- 不得忽略不认识的 constraint。\n"
                "- 不得重新创造 current script FunctionItem 中不存在的 constraint。\n"
                "- 只有 scripts/*.py 或平台真实 runtime 能力可以承担运行链路闭环；SKILL.md、references/*.md、assets/** 只能提供说明、规范或资源上下文，不能承担运行时字段转换或产物生成。\n"
                "- 不写脚本类型词表，不按 role 名称、文件名、字段名或固定业务词表判责。\n"
                "- 一个脚本只能被要求完成或验证它能从输入、依赖、工具和声明能力中实际完成/验证的职责。\n"
                "- 默认内容、空内容、纯占位内容、明显模板化内容只能作为兜底健壮性，不能替代核心职责实现。\n"
                "- 不做质量、审美、风格、充分性细评；blocking_issues 只能描述当前文件在可观察边界内缺失的职责和最小实现边界。\n\n"
                "空壳检查（重点）：\n"
                "- 必须重点检查空壳函数（函数体仅含 pass / ... / raise NotImplementedError）和空壳分支（if/elif/else 仅含 pass / ...）。\n"
                "- 空壳函数/空壳分支是责任未完成的直接证据；必须在 blocking_issues 中明确指出每一个空壳位置，指明函数名、行号区域和应实现的职责。\n"
                "- 不得以'结构完整'或'有导入语句'为由跳过空壳检查。\n\n"
                "已授权工具合同理解规则：\n"
                "- 当前文件已授权工具合同来自 Tool Registry，是已绑定工具用途、真实 callable function、import、signature、输入 schema、输出 schema、return contract、artifact outputs、side effects、example 和 common mistakes 的事实源。\n"
                "- 当源码调用已授权工具函数时，必须按照工具合同理解函数真实行为和返回值；不得只根据函数名、变量名或自然语言猜测。\n"
                "- 例如合同声明函数返回 str，就必须按 str 理解；不得假定返回 dict 或存在合同未声明的字段。\n"
                "- 例如合同声明函数返回 image_path/file_outputs，应检查源码是否按该真实返回合同消费结果，而不是根据变量名猜测图片已经生成。\n"
                "- 工具合同只用于理解源码语义和判断当前职责是否真实使用已有能力；不得借此新增工具、授权工具或要求 ToolPool 外工具。\n"
                "- 如果当前源码没有调用某个已授权工具，不得因为工具已授权就假定其效果已经发生。\n"
                "- 如果源码调用工具，但返回值没有进入当前职责要求的结果或 artifact，不得仅凭存在 tool call 判定职责完成。\n\n"
                "工具合同判断规则：你必须根据 Current File ToolPool contracts 与完整源码判断工具使用事实。"
                "如果源码调用的平台工具不在当前 ToolPool 合同中，输出 blocking issue id=tool_contract_mismatch。"
                "如果当前 ToolPool 缺少完成 FunctionItem 所需能力，输出 blocking issue id=tool_support_insufficient。"
                "如果工具已提供但源码没有正确使用导致职责未完成，输出普通 semantic blocking issue。"
                "Backend 只确认工具事实是否真实，不根据模块名、函数名、角色或 capability 映射替你判断工具语义。\n\n"

                "返回 JSON object：\n"
                "{\n"
                "  \"passed\": true|false,\n"
                "  \"blocking_issues\": [\n"
                "    {\n"
                "      \"issue_type\": \"responsibility_weakened|semantic_source_missing|semantic_action_incomplete|semantic_delivery_incomplete|semantic_constraint_dropped|collection_boundary_lost|aggregation_boundary_lost\",\n"
                "      \"scope\": \"current_file_only\",\n"
                "      \"failure_layer\": \"responsibility\",\n"
                "      \"severity\": \"error\",\n"
                "      \"failed_file\": \"当前脚本路径\",\n"
                "      \"semantic_failure\": \"当前文件未完成的语义职责；只有这里能作为 blocking\",\n"
                "      \"function\": \"相关函数或区域\",\n"
                "      \"line_region\": \"相关源码区域\",\n"
                "      \"problem\": \"为什么没有完成当前脚本自身职责\",\n"
                "      \"evidence\": \"引用源码说明\",\n"
                "      \"expected\": \"当前脚本应完成的职责\",\n"
                "      \"minimal_edit\": \"只修改当前脚本职责实现区域\"\n"
                "    }\n"
                "  ],\n"
                "  \"advisory_notes\": [],\n"
                "  \"repair_instructions\": \"...\"\n"
                "}\n"
            ),
        },
        {
            "role": "user",
            "content": (
                f"目标脚本：{file_path}\n\n"
                "原始 blueprint_text：\n"
                f"{blueprint_text[:8000]}\n\n"

                "当前文件 purpose 短合同：\n"
                f"{short_contract}\n\n"

                "workflow_allocation_summary：\n"
                f"{workflow_allocation_summary[:4000]}\n\n"

                "SkillPlanEntry：\n"
                f"{json.dumps({k: getattr(skill_plan_entry, k, '') for k in ('path', 'purpose', 'role', 'component_hint')}, ensure_ascii=False, default=str)[:8000]}\n\n"

                "当前文件 FunctionItem graph context（Producer/Judge shared payload）：\n"
                f"{json.dumps(graph_context, ensure_ascii=False, default=str)[:8000]}\n\n"

                "当前文件 requirements / must_do：\n"

                f"{json.dumps(req_payload, ensure_ascii=False, default=str)[:8000]}\n\n"
                "当前文件已授权工具合同"
                "（来自 Current File Tool Binding "
                "中的 tool_id 回查 Tool Registry）：\n"
                f"{json.dumps(authorized_tool_contracts,ensure_ascii=False,default=str,)[:16000]}\n\n"
                "脚本试运行输出（如本阶段尚未运行则为空或说明未提供）：\n"
                f"{json.dumps(trial_stdout, ensure_ascii=False, default=str)[:4000]}\n\n"

                "脚本生成的产物信息（如本阶段尚未运行则为空或说明未提供）：\n"
                f"{json.dumps(artifact_info, ensure_ascii=False, default=str)[:4000]}\n\n"

                "额外上下文：\n"
                f"{json.dumps(review_context, ensure_ascii=False, default=str)[:4000]}\n\n"

                "当前脚本源码（带行号）：\n"
                f"{_numbered_source(script_content)[-16000:]}\n\n"

                "审查要求：\n"
                "1. 只判断当前脚本是否完成 purpose 短合同和 current script FunctionItem。\n"
                "2. 不要判断其它非职责问题，不要按字段名/变量名/固定函数名/脚本类型词表判错。\n"
                "3. 检查脚本是否保持自己可观察的输入关系，并交付 current script FunctionItem 要求的输出/产物。\n"
                "4. 不要要求当前脚本验证无法从输入、依赖、工具或声明能力中观察的信息。\n"
                "5. requirements.constraints 是开放责任约束；所有 required=true constraints 都必须检查实现证据。\n"
                "6. 不得忽略不认识的 constraint，也不得重新创造 current script FunctionItem 中不存在的 constraint。\n"
                "7. 检查源码调用的平台工具是否存在于 Current File ToolPool contracts；ToolPool 外工具使用输出 tool_contract_mismatch，工具不足输出 tool_support_insufficient。不得要求删除工具调用并改成本地假实现。\n"
            ),
        },
    ]
    logger.info("[Creator][script_responsibility][start] %s", json.dumps({
        "event": "script_responsibility_start",
        "file_path": file_path,
        "has_workflow_allocation_summary": bool(workflow_allocation_summary),
        "requirement_count": len(req_items),
    }, ensure_ascii=False, default=str))

    last_text = ""
    for review_attempt in range(3):
        try:
            active_messages = messages if review_attempt == 0 else [
                *messages,
                {
                    "role": "user",
                    "content": (
                        "请将上一轮审查结论改写为约定 JSON object。\n"
                        "JSON 只能表达当前脚本职责是否完成。\n"
                        f"上一轮输出片段：{last_text[:1200]}"
                    ),
                },
            ]
            text = await complete_chat_once(active_messages, route.model)
            last_text = str(text or "")
        except Exception as exc:
            logger.warning(
                "[Creator][script_responsibility][review_unavailable] file=%s error=%s",
                file_path,
                exc,
            )
            return {
                "passed": False,
                "issues": [{
                    "id": "script_responsibility.review_unavailable",
                    "failed_file": file_path,
                    "failed_function": "responsibility_review",
                    "code_region": "review",
                    "reason": f"职责审查模型不可用：{type(exc).__name__}: {exc}",
                    "minimal_edit": "不是脚本内容错误；请重试或切换 validator 模型。",
                    "allowed_scope": "不要自动修改脚本。",
                    "forbidden_scope": "不得因为 validator 不可用而判定脚本通过。",
                }],
                "repair_instructions": "职责审查模型不可用，不能放行当前脚本。",
                "failure_type": "script_requirement_validator_error",
                "model": route.model,
            }

        data = _parse_validator_json_object(last_text)
        schema_error = _script_responsibility_schema_error(data, file_path=file_path)
        if schema_error:
            if review_attempt < 2:
                last_text = json.dumps({"schema_error": schema_error, "raw": data if isinstance(data, dict) else last_text}, ensure_ascii=False, default=str)
                continue
            return _script_responsibility_validator_failure(
                file_path=file_path,
                reason=schema_error,
                raw=data if isinstance(data, dict) else last_text,
                model=route.model,
            )

        data.setdefault("advisory_notes", [])
        data.setdefault("repair_instructions", "")
        break

    data = data if isinstance(data, dict) else {}

    blocking = data.get("blocking_issues")
    blocking_issues = blocking if isinstance(blocking, list) else []

    if data.get("passed") is False:
        issues = _normalize_responsibility_review_issues(data, file_path=file_path)
        if not issues:
            issues = [{
                "id": "script_responsibility.failed",
                "failed_file": file_path,
                "failed_function": "current script",
                "code_region": "current file responsibility logic",
                "reason": str(
                    data.get("problem")
                    or data.get("reason")
                    or data.get("message")
                    or "职责审查模型判定当前脚本没有完成自身职责。"
                ),
                "minimal_edit": str(
                    data.get("repair_instructions")
                    or "只修改当前脚本中未完成职责的业务逻辑区域。"
                ),
                "allowed_scope": "只允许修改当前脚本职责实现区域。",
                "forbidden_scope": "不得修改 SKILL.md、workflow、字段映射、stdout schema、artifact 或其它脚本。",
                "details": {
                    "raw_review": data,
                    "blocking_issues": blocking_issues,
                },
            }]
        logger.info("[Creator][script_responsibility][failed] %s", json.dumps({
            "event": "script_responsibility_failed",
            "file_path": file_path,
            "model": route.model,
            "issue_count": len(issues),
            "failure_types": sorted({
                str(issue.get("issue_type") or issue.get("id") or "")
                for issue in issues
                if isinstance(issue, dict)
            }),
        }, ensure_ascii=False, default=str))
        return {
            "passed": False,
            "issues": issues,
            "repair_instructions": str(
                data.get("repair_instructions")
                or "只修改当前脚本中未完成职责的业务逻辑区域。"
            ),
            "model": route.model,
            "advisory_notes": data.get("advisory_notes") if isinstance(data.get("advisory_notes"), list) else [],
        }
    logger.info("[Creator][script_responsibility][result] %s", json.dumps({
        "event": "script_responsibility_result",
        "file_path": file_path,
        "model": route.model,
        "passed": True,
    }, ensure_ascii=False, default=str))
    return {
        "passed": True,
        "issues": [],
        "repair_instructions": "",
        "model": route.model,
        "advisory_notes": data.get("advisory_notes") if isinstance(data.get("advisory_notes"), list) else [],
    }

async def _run_reference_semantic_review(
    *,
    file_path: str,
    content: str,
    purpose: str = "",
    blueprint_context: str = "",
    dependent_context: Any = None,
    requested_model: str | None = None,
) -> dict[str, Any]:
    """Model-owned semantic review for references/*.md.

    Backend format checks must run before this helper. This helper never uses
    lexical overlap, body length, keyword lists, CJK bigrams, or static backend
    heuristics to decide semantic success.
    """
    route = route_model(
        VALIDATOR_TASK,
        requested_model=requested_model,
        reason=f"creator first-round reference semantic review: {file_path}",
    )
    messages = [
        {
            "role": "system",
            "content": (
                "你是 Creator 第一轮 reference 语义审查模型，只输出严格 JSON object。\n"
                "Backend 已经完成 Markdown/frontmatter/fence/single-file 格式检查；你只判断语义职责。\n"
                "不要使用 token overlap、长度阈值、关键词词表、CJK bigram、字段名匹配或固定章节名作为裁决依据。\n"
                "只判断当前 references/*.md 是否完成自己的参考资料职责、是否跑题、是否只是空话/TODO/placeholder、"
                "是否错误写成 Creator 创建流程、是否错误承担 executable script 职责。\n"
                "不要判断 workflow 字段名、argv/stdout 字段、工具选择或 E2E 运行。\n"
                "返回 JSON object: {\"passed\": true|false, \"issues\": [], \"repair_instructions\": \"...\"}."
            ),
        },
        {
            "role": "user",
            "content": (
                f"目标 reference：{file_path}\n\n"
                f"当前 FilePlan purpose / file-local responsibility：\n{purpose[:4000]}\n\n"
                f"Blueprint 中相关上下文：\n{(blueprint_context or '')[:8000]}\n\n"
                "引用/依赖当前 reference 的文件职责摘要（如有）：\n"
                f"{json.dumps(dependent_context or {}, ensure_ascii=False, default=str)[:8000]}\n\n"
                "当前 reference content：\n<<<REFERENCE\n"
                f"{(content or '')[:16000]}\n"
                "REFERENCE\n"
            ),
        },
    ]
    last_text = ""
    for attempt in range(3):
        active_messages = messages if attempt == 0 else [
            *messages,
            {"role": "user", "content": f"上一轮不是合法 JSON，请只返回约定 JSON object。上一轮：{last_text[:1000]}"},
        ]
        text = await complete_chat_once(active_messages, route.model)
        last_text = str(text or "")
        data = _parse_validator_json_object(last_text)
        if isinstance(data, dict) and data:
            passed = bool(data.get("passed"))
            issues = data.get("issues") if isinstance(data.get("issues"), list) else data.get("blocking_issues")
            issues = issues if isinstance(issues, list) else []
            if not passed and not issues:
                issues = [{
                    "id": "reference_semantic.failed",
                    "failed_file": file_path,
                    "reason": str(data.get("reason") or data.get("problem") or "Reference semantic judge failed this file."),
                    "minimal_edit": str(data.get("repair_instructions") or "Patch only this reference's semantic content."),
                }]
            return {
                "passed": passed,
                "issues": issues,
                "repair_instructions": str(data.get("repair_instructions") or ""),
                "model": route.model,
                "raw_review": data,
            }
    return {
        "passed": False,
        "issues": [{
            "id": "reference_semantic.validator_incomplete",
            "failed_file": file_path,
            "reason": "Reference semantic judge did not return valid JSON.",
            "minimal_edit": "Retry semantic review; do not treat backend as semantic authority.",
        }],
        "repair_instructions": "Reference semantic judge unavailable/incomplete.",
        "failure_type": "reference_semantic_validator_incomplete",
        "model": route.model,
        "raw_review": last_text[:1000],
    }

__all__ = [name for name in globals() if not name.startswith("__")]


TOOL_POOL_REPAIR_RULES = """Repair may only import backend.services.runtime_tools helpers listed in current_file_binding.allowed_helper_imports. Do not replace a forbidden helper with another unbound helper. If a pool-external platform helper is needed, emit tool_pool_patch.add_tool_requests; patches must pass tool_pool_gate before code may import the helper. If the task can be implemented with Python standard library or allowed third-party dependencies, do that without importing runtime_tools. Repair must not add script files or let references/assets use runtime tools. Import guard errors are hard constraints."""

E2E_REPAIR_NO_TOOL_EXPLORE_RULE = """E2E repair phase: tool library exploration is strictly prohibited. Do NOT emit tool_pool_patch.add_tool_requests or request any new tool. E2E repair must only fix cross-module interface issues (command payloads, argv/stdout alignment, workflow dataflow) using the existing allowed tool pool. If a missing standard library package is discovered during E2E execution, surface it as a missing_stdlib_request in the response instead of requesting tool exploration."""
