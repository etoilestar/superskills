"""Repair scope, diff application, and generated-file repair helpers."""

from .common import *  # noqa: F403


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
    notes: tuple[str, ...] = ()

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "repair_type": self.repair_type,
            "target_file": self.target_file,
            "max_changed_lines": self.max_changed_lines,
            "notes": list(self.notes),
        }


@dataclass
class CreatorDiffProposal:
    """Repair proposal.

    主路径是 exact_replace：
    {
      "target_file": "scripts/x.py",
      "reason": "...",
      "edits": [
        {"old": "当前文件中逐字复制的旧片段", "new": "替换后的新片段"}
      ]
    }

    兜底兼容 unified diff：
    {
      "target_file": "scripts/x.py",
      "reason": "...",
      "diff": "--- a/scripts/x.py\n+++ b/scripts/x.py\n@@ ..."
    }
    """

    target_file: str
    reason: str
    diff: str = ""
    edits: list[dict[str, str]] = field(default_factory=list)
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


def _format_diff_response_violation(error: Exception, raw_text: str) -> str:
    return (
        "FORMAT_VIOLATION：上一次输出不是可接受的 repair diff proposal。\n"
        f"解析错误：{type(error).__name__}: {error}\n\n"
        "你必须重新输出严格 JSON object，且只包含 target_file、reason、diff。\n"
        "diff 必须是 single-file unified diff，必须包含 ---、+++、@@ hunk。\n"
        "禁止输出完整文件源码。\n"
        "禁止输出 Markdown 解释。\n"
        "禁止新增、删除或修改其它文件。\n\n"
        "上一次输出片段如下：\n"
        "```text\n"
        f"{str(raw_text or '')[:4000]}\n"
        "```"
    )

def _extract_json_or_diff_proposal(
    text: str,
    *,
    expected_target_file: str,
) -> CreatorDiffProposal:
    """Parse model repair proposal.

    优先接受 exact_replace JSON：

    {
      "target_file": "scripts/x.py",
      "reason": "...",
      "edits": [
        {"old": "...", "new": "..."}
      ]
    }

    兜底接受 unified diff，但不推荐让 Qwen 主路径写 diff。
    """

    raw_text = str(text or "").strip()
    stripped = _strip_outer_code_fence(raw_text)

    parsed: dict[str, Any] | None = None

    try:
        maybe_json = json.loads(stripped)
        if isinstance(maybe_json, dict):
            parsed = maybe_json
    except json.JSONDecodeError:
        parsed = None

    if parsed is None:
        json_text = _extract_first_json_object_text(raw_text)
        if json_text:
            try:
                maybe_json = json.loads(json_text)
                if isinstance(maybe_json, dict):
                    parsed = maybe_json
            except json.JSONDecodeError:
                parsed = None

    if isinstance(parsed, dict):
        target_file = _strip_diff_path_prefix(parsed.get("target_file") or expected_target_file)
        if target_file != expected_target_file:
            raise ValueError(
                f"patch target_file 不匹配：expected={expected_target_file!r}, actual={target_file!r}"
            )

        reason = str(parsed.get("reason") or parsed.get("summary") or "").strip()

        edits = parsed.get("edits")
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

            return CreatorDiffProposal(
                target_file=target_file,
                reason=reason,
                edits=normalized_edits,
                raw=parsed,
                mode="exact_replace",
            )

        diff = str(parsed.get("diff") or parsed.get("unified_diff") or "").strip()
        diff = _strip_outer_code_fence(diff)

        if diff:
            if not _looks_like_unified_diff(diff):
                raise ValueError(
                    "JSON 中的 diff 不是 unified diff。"
                    "如果使用 diff，必须包含 ---、+++、@@。"
                    "更推荐使用 edits old/new exact_replace 格式。"
                )

            old_path, new_path = _unified_diff_target_files(diff)

            if new_path != expected_target_file:
                raise ValueError(
                    f"diff target_file 不匹配：expected={expected_target_file!r}, actual={new_path!r}"
                )

            if old_path not in {expected_target_file, new_path}:
                raise ValueError(
                    f"diff old file 不匹配：expected={expected_target_file!r}, actual={old_path!r}"
                )

            return CreatorDiffProposal(
                target_file=target_file,
                reason=reason,
                diff=diff,
                raw=parsed,
                mode="unified_diff",
            )

        raise ValueError(
            "修复模型返回 JSON，但没有 edits，也没有 diff/unified_diff。"
            "请使用 edits old/new exact_replace 格式。"
        )

    raw_diff = stripped
    fence_match = re.search(r"```(?:diff|patch)?\s*(.*?)```", raw_text, re.S | re.I)
    if fence_match:
        raw_diff = fence_match.group(1).strip()

    if not _looks_like_unified_diff(raw_diff):
        raise ValueError(
            "修复模型没有返回 exact_replace JSON，也没有返回 raw unified diff。"
            "如果输出的是完整源码，必须拒绝并要求模型重新输出 edits old/new patch。"
        )

    old_path, new_path = _unified_diff_target_files(raw_diff)

    if new_path != expected_target_file:
        raise ValueError(
            f"raw diff target_file 不匹配：expected={expected_target_file!r}, actual={new_path!r}"
        )

    if old_path not in {expected_target_file, new_path}:
        raise ValueError(
            f"raw diff old file 不匹配：expected={expected_target_file!r}, actual={old_path!r}"
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
    path = _strip_diff_path_prefix(target_file).lower()
    if path.startswith("scripts/") and path.endswith(".py"):
        return False
    return path.endswith((".md", ".markdown", ".txt", ".text", ".rst", ".yaml", ".yml", ".json"))


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

        count = candidate.count(old)
        fallback_type = "exact"
        similarity: float | None = None
        matched_excerpt = old
        replace_start: int | None = None
        replace_end: int | None = None

        if count == 1:
            replace_start = candidate.find(old)
            replace_end = replace_start + len(old)
        elif count > 1:
            raise ValueError(
                f"edits[{index}].old 在当前文件中匹配了 {count} 次。"
                "请提供更长 old 片段，保证唯一匹配。"
            )
        else:
            normalized_span = _find_unique_normalized_span(candidate, old)
            if normalized_span is not None:
                replace_start, replace_end = normalized_span
                fallback_type = "normalized_exact"
                similarity = 1.0
                matched_excerpt = _excerpt(candidate, replace_start, replace_end)
            elif _is_approximate_replace_allowed(expected_target_file):
                approx = _find_approximate_substring_span(candidate, old)
                if not approx.get("accepted"):
                    raise ValueError(
                        f"edits[{index}].old 在当前文件中没有 exact/normalized 匹配，"
                        "approximate substring 置信度不足，已拒绝自动替换。"
                        f"reason={approx.get('reason')}; "
                        f"similarity={float(approx.get('similarity') or 0):.3f}; "
                        f"second_similarity={float(approx.get('second_similarity') or 0):.3f}; "
                        "最相近候选原文片段如下，可在下一轮直接复制为 old：\n"
                        "```text\n"
                        f"{approx.get('matched_excerpt') or ''}\n"
                        "```"
                    )
                replace_start = int(approx["start"])
                replace_end = int(approx["end"])
                fallback_type = "approximate_substring"
                similarity = float(approx["similarity"])
                matched_excerpt = str(approx.get("matched_excerpt") or "")
            else:
                raise ValueError(
                    f"edits[{index}].old 在当前文件中没有匹配。"
                    "当前文件类型只允许 exact 或 normalized exact，不启用 approximate substring 自动替换。"
                    "请从当前文件逐字复制更准确的 old 片段。"
                )

        assert replace_start is not None and replace_end is not None
        original_span = candidate[replace_start:replace_end]
        if original_span == new:
            skipped_noop.append({
                "index": index,
                "old_chars": len(old),
                "reason": "匹配到的原文 span 与 new 完全相同，已跳过该 no-op edit。",
            })
            continue
        candidate = candidate[:replace_start] + new + candidate[replace_end:]
        applied.append({
            "index": index,
            "old_chars": len(old),
            "new_chars": len(new),
            "fallback_type": fallback_type,
            "similarity": similarity,
            "matched_excerpt": matched_excerpt[:1000],
            "original_model_old_excerpt": old[:1000],
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


def _validate_repair_diff_scope(
    *,
    proposal: CreatorDiffProposal,
    current_content: str,
    scope: CreatorRepairScope,
) -> tuple[str, dict[str, Any]]:
    """Validate and apply repair proposal.

    主路径：
    - exact_replace old/new

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

    if proposal.mode == "exact_replace":
        candidate, stats = _apply_exact_replace_patch(
            original_content=current_content,
            proposal=proposal,
            expected_target_file=scope.target_file,
        )

    elif proposal.mode == "unified_diff":
        candidate, stats = _apply_single_file_unified_diff(
            original_content=current_content,
            diff_text=proposal.diff,
            expected_target_file=scope.target_file,
        )
        stats["mode"] = "unified_diff"

    else:
        raise ValueError(f"未知 repair proposal mode：{proposal.mode}")

    changed_line_count = int(stats.get("changed_line_count") or 0)
    if changed_line_count > scope.max_changed_lines:
        raise ValueError(
            "repair patch 修改行数超过当前修复域上限："
            f"{changed_line_count} > {scope.max_changed_lines}"
        )

    return candidate, stats


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
        "1. SKILL.md / references 中的 shell fenced command block 会被解析成 Action schema。\n"
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
    """Ask coding model for a repair patch proposal.

    优先要求 exact_replace old/new。
    兜底兼容 unified diff。
    """

    messages = [
        {
            "role": "system",
            "content": (
                "你是 superskills Creator 的局部修复代码模型。\n"
                "你只能输出严格 JSON object，不能输出 Markdown 解释。\n"
                "你不能输出完整文件，只能输出 target_file 的局部 patch。\n"
                "优先使用 edits old/new exact_replace 格式，不要手写 unified diff hunk 行号。\n"
                "本轮只允许修复 target_file。\n"
                "不要新增文件、删除文件、修改其它文件。\n"
                "不要在 Creator repair 层重新定义平台 IO。"
                "平台兼容性会由后续 sandbox / smoke / E2E 真实试运行判断。\n"
            ),
        },
        {
            "role": "user",
            "content": (
                f"目标文件：{file_path}\n\n"
                "RepairScope：\n"
                f"{json.dumps(scope.to_prompt_dict(), ensure_ascii=False, indent=2)}\n\n"
                "本轮修复规则：\n"
                f"{target_rule}\n\n"
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
                "只返回严格 JSON object，优先使用如下格式：\n"
                "{\n"
                f"  \"target_file\": \"{file_path}\",\n"
                "  \"reason\": \"为什么这个 patch 只修复当前真实失败\",\n"
                "  \"edits\": [\n"
                "    {\n"
                "      \"old\": \"从当前文件中逐字复制、且唯一出现的旧片段\",\n"
                "      \"new\": \"替换后的新片段\"\n"
                "    }\n"
                "  ]\n"
                "}\n\n"
                "要求：\n"
                "1. old 必须从当前文件逐字复制。\n"
                "2. old 必须唯一出现。\n"
                "3. 不要输出完整文件源码。\n"
                "4. 不要输出 Markdown。\n"
                "5. 不要手写 unified diff，除非你非常确定 hunk 完全正确。\n"
                "6. 多行代码片段建议使用 old_lines/new_lines 字符串数组，后端会用换行 join，避免 JSON 字符串裸换行转义错误。\n"
            ),
        },
    ]

    last_error: Exception | None = None
    last_text = ""

    for attempt in range(1, max(1, format_retry_limit) + 1):
        text = await complete_chat_once(messages, model)
        last_text = text

        try:
            return _extract_json_or_diff_proposal(
                text,
                expected_target_file=file_path,
            )

        except Exception as exc:
            last_error = exc
            logger.warning(
                "[Creator][repair_patch][format_violation] file=%s model=%s attempt=%d/%d error=%s",
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
                "content": str(text or "")[:6000],
            })
            messages.append({
                "role": "user",
                "content": _format_diff_response_violation(exc, text),
            })

    raise ValueError(
        "修复模型连续没有返回合法 patch proposal，已拒绝应用。\n"
        f"target_file={file_path}\n"
        f"last_error={type(last_error).__name__ if last_error else 'Unknown'}: {last_error}\n"
        "last_output_excerpt:\n"
        f"{str(last_text or '')[:4000]}"
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
) -> tuple[CreatorDiffProposal, str, dict[str, Any]]:
    """Request patch, apply patch, and retry on parse/apply failure.

    不做业务规则判断。
    只做：
    - 格式失败反馈；
    - old/new 匹配失败反馈；
    - no-op patch 反馈；
    - runtime traceback 优先级反馈。
    """

    runtime_priority_note = ""
    if any(
        marker in str(failure_text or "")
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
            "RUNTIME_TRACEBACK_PRIORITY：这是 smoke/trial run 真实运行失败。"
            "修复时必须优先依据 raw stderr Traceback、exit_code、报错源码行和异常类型。"
            "validator 的解释只作为辅助说明；如果 validator 解释与 Traceback 冲突，以 Traceback 为准。"
            "不要修改与 Traceback 无关的位置。"
        )

    accumulated_failure = failure_text + ("\n\n" + runtime_priority_note if runtime_priority_note else "")
    accumulated_context = task_context + ("\n\n" + runtime_priority_note if runtime_priority_note else "")
    last_error: Exception | None = None
    last_proposal_excerpt = ""
    failed_proposal_counts: dict[str, int] = {}

    for attempt in range(1, max(1, patch_retry_limit) + 1):
        proposal_signature: str | None = None
        try:
            proposal = await _request_repair_diff_proposal(
                model=model,
                file_path=file_path,
                current_content=current_content,
                failure_text=accumulated_failure,
                scope=scope,
                task_context=accumulated_context,
                target_rule=target_rule,
                format_retry_limit=2,
            )

            proposal_signature_payload = proposal.raw if proposal.raw is not None else {
                "target_file": proposal.target_file,
                "reason": proposal.reason,
                "mode": proposal.mode,
                "diff": proposal.diff[:2000],
                "edits": proposal.edits,
            }
            last_proposal_excerpt = json.dumps(
                proposal_signature_payload,
                ensure_ascii=False,
                default=str,
            )[:5000]
            proposal_signature = json.dumps(proposal_signature_payload, ensure_ascii=False, sort_keys=True, default=str)

            if failed_proposal_counts.get(proposal_signature, 0) >= 1:
                repeated_excerpt = ""
                if proposal.edits:
                    old_text = str(proposal.edits[0].get("old") or "")
                    approx = _find_approximate_substring_span(current_content, old_text)
                    repeated_excerpt = str(approx.get("matched_excerpt") or "")
                raise ValueError(
                    "REPEATED_UNAPPLICABLE_PROPOSAL：连续两轮 proposal 完全相同且上一轮不可应用，"
                    "禁止再次提交同一 old。请直接复制下面最相近候选原文片段作为新的 old，或提供更长唯一片段。\n"
                    "```text\n"
                    f"{repeated_excerpt}\n"
                    "```"
                )

            candidate, diff_stats = _validate_repair_diff_scope(
                proposal=proposal,
                current_content=current_content,
                scope=scope,
            )

            diff_stats["repair_patch_attempt"] = attempt
            return proposal, candidate, diff_stats

        except Exception as exc:
            last_error = exc
            if proposal_signature is not None:
                failed_proposal_counts[proposal_signature] = failed_proposal_counts.get(proposal_signature, 0) + 1
            if isinstance(exc, CreatorRepairNoopPatch) or "REPEATED_UNAPPLICABLE_PROPOSAL" in str(exc):
                break

            logger.warning(
                "[Creator][repair_patch][apply_or_parse_failed] file=%s model=%s attempt=%d/%d error=%s",
                file_path,
                model,
                attempt,
                patch_retry_limit,
                exc,
            )

            if attempt >= patch_retry_limit:
                break

            no_op_note = ""
            if (
                "no-op" in str(exc)
                or "没有产生任何变化" in str(exc)
                or "old 与 new 完全相同" in str(exc)
            ):
                no_op_note = (
                    "NO_OP_PATCH_REJECTED：上一轮 patch 没有产生真实变化。"
                    "你不能提交 old 与 new 完全相同的 edit。"
                    "new 必须真实改变当前失败内容。"
                    "如果目标是在文件末尾补充内容，请让 old 选中当前文件末尾的一段真实文本，"
                    "new 在该片段基础上追加缺失内容。"
                )

            apply_feedback = (
                "\n\nPATCH_APPLY_OR_PARSE_FAILED：上一轮 patch 没有被后端接受。\n"
                f"失败类型：{type(exc).__name__}\n"
                f"失败原因：{exc}\n\n"
                f"{runtime_priority_note}\n\n"
                f"{no_op_note}\n\n"
                "请重新输出 exact_replace JSON patch，不要输出完整文件，不要手写 unified diff。\n"
                "old 必须从当前目标文件逐字复制，且只出现一次。\n"
                "new 必须真实改变当前失败内容。\n"
                "如果 old 没匹配，请复制更准确的当前文件片段。\n"
                "如果 old 匹配多次，请提供更长 old 片段。\n\n"
                "上一轮 proposal 摘要：\n"
                "```text\n"
                f"{last_proposal_excerpt}\n"
                "```\n"
            )

            accumulated_failure = failure_text + apply_feedback
            accumulated_context = task_context + apply_feedback

    raise ValueError(
        "修复模型连续提出无法解析或无法应用的 patch，已停止本轮 repair。\n"
        f"target_file={file_path}\n"
        f"last_error={type(last_error).__name__ if last_error else 'Unknown'}: {last_error}\n"
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

    scope = CreatorRepairScope(
        phase="module_functional_smoke",
        repair_type=repair_mode or "localized_patch",
        target_file=file_path,
        max_changed_lines=220 if repair_mode == "strict_contract_rewrite" else 160,
        notes=(
            "第一轮只修当前文件。",
            "模型功能校验判断责任是否完成；smoke/trial run 判断代码是否通过。",
            "平台兼容性直接交给现有 sandbox/smoke 校验，不在 repair 层做字段词表判断。",
            "优先输出 edits old/new exact_replace patch，不要输出完整文件。",
        ),
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
                failure_layer=_failure_layer_from_error_text(validation_error),
                error_text=validation_error,
                include_snippets=True,
            )
            if plan_entry is not None
            else ""
        )

        repair_snippet_text = ""
        if plan_entry is not None:
            snippets = resolve_tool_snippets_for_context(
                role=plan_entry.role,
                capabilities=list(plan_entry.required_capabilities or []) + list(plan_entry.optional_capabilities or []),
                tool_names=[],
                file_path=file_path,
                failure_layer=_failure_layer_from_error_text(validation_error),
                error_text=validation_error + "\n" + current_content[-6000:],
                max_snippets=5,
            )
            repair_snippet_text = tool_snippet_prompt(snippets)

        target_rule = (
            "第一轮单文件修复。\n"
            "只修当前脚本文件，不改 SKILL.md，不改其它脚本，不改 references/assets。\n"
            "模型职责校验只负责判断该脚本有没有完成 SkillPlanEntry.purpose / role / capabilities 内的功能责任。\n"
            "smoke / trial run 才负责判断代码能不能运行、stdout/artifact 是否合规。\n"
            "如果失败来自模型功能职责校验，你可以修当前脚本中未完成职责的局部逻辑，"
            "例如 PDF blocks/styles 组装、图片生成/检索参数、表格构造、内容生成调用、工具结果参与输出等。\n"
            "如果失败来自 smoke/trial run，你只修导致运行失败、stdout 失败或 artifact 失败的局部逻辑。\n"
            "不要为了绕过试运行而返回空结果或伪造成功。\n"
            "平台 IO 是否兼容，由后续现有 smoke/sandbox 试运行判断。\n"
            "优先输出 edits old/new exact_replace patch。不要输出完整文件。"
        )

        extra_context = (
            f"runtime={repair_runtime}, language={repair_language}\n\n"
            "SkillPlanEntry：\n"
            f"{json.dumps(skill_plan_entry or {}, ensure_ascii=False, default=str)[:6000]}\n\n"
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
            "只修当前失败相关的小节、frontmatter 或 fenced block。\n"
            "不要整文件重写。\n"
            "不要在这里做第二轮 E2E 跨模块字段推断；那属于 workflow E2E。\n"
            "平台 IO 与 sandbox 模式对齐，由后续验证执行判断。\n"
            "优先输出 edits old/new exact_replace patch。不要输出完整 SKILL.md。"
        )
        extra_context = ""

    elif file_path.startswith("references/"):
        target_rule = (
            "第一轮 reference 修复。\n"
            "reference 是参考资料，不是执行源。\n"
            "只修当前 reference 文件中的失败区域。\n"
            "不要添加可执行 workflow。\n"
            "优先输出 edits old/new exact_replace patch。不要输出完整文件。"
        )
        extra_context = ""

    else:
        target_rule = (
            "第一轮单文件修复。\n"
            "只修当前文件。\n"
            "优先输出 edits old/new exact_replace patch。不要输出完整文件。"
        )
        extra_context = ""

    task_context = "\n".join([
        "原始生成上下文摘要：",
        _compact_messages_for_repair_context(prompt_messages),
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

    _proposal, candidate, diff_stats = await _request_and_apply_repair_patch(
        model=model,
        file_path=file_path,
        current_content=current_content,
        failure_text=validation_error,
        scope=scope,
        task_context=task_context,
        target_rule=target_rule,
        patch_retry_limit=3,
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
            )

        if file_path.startswith("assets/"):
            return (
                "当前 asset 文件生成内容为空。"
                "这不是可保存状态，必须让模型返修为非空静态资源内容；"
                "如果该 asset 是用户上传素材，则不应走模型生成链路。"
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
                "当前失败属于 script_functional 内容职责闭环失败，不是 script_smoke 运行失败。"
                "只修当前脚本中校验信息指出的函数、行号或代码区域；"
                "保留已经通过的 import、parse_args/main 入口、JSON argv 协议、stdout 字段名和文件输出协议；"
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
                    safe_localizations.append(safe_item)

    if safe_localizations:
        parts.extend([
            "",
            "校验模型对上述真实失败的定位信息，仅用于帮助修复 deterministic_error，不得新增失败范围：",
            json.dumps(safe_localizations, ensure_ascii=False, indent=2, default=str),
        ])

    if (
        not delegate_to_backend_contract
        and isinstance(validator_report, dict)
        and str(validator_report.get("repair_instructions") or "").strip()
    ):
        parts.extend([
            "",
            "校验模型给出的辅助 repair_instructions，仅作为定位参考，不得覆盖 deterministic_error：",
            str(validator_report.get("repair_instructions") or "").strip(),
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

    non_current_file_scopes = {
        "e2e",
        "workflow",
        "cross_file",
        "cross_step",
        "dataflow",
        "stdout_schema",
        "stdout_contract",
        "artifact",
        "path",
        "dependency",
        "import",
        "metadata",
        "skill_md",
        "interface_only",
        "argument_mapping",
        "argument_effect",
    }

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
        severity = str(raw.get("severity") or "").strip().lower()

        if severity in {"note", "info", "advisory"}:
            continue

        lowered = f"{issue_type} {scope}".lower()
        if any(marker in lowered for marker in non_current_file_scopes):
            continue

        problem = str(
            raw.get("problem")
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

        normalized.append({
            "id": "script_functional.responsibility",
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

async def _run_script_responsibility_review(
    *,
    file_path: str,
    script_content: str,
    skill_plan_entry: SkillPlanEntry,
    deterministic_issues: list[dict[str, Any]] | None = None,
    requested_model: str | None = None,
    review_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """First-round script responsibility review.

    第一轮只判断当前脚本源码是否完成自身职责。

    不判断：
    - 脚本能不能实际运行；
    - JSON argv 是否正确；
    - stdout 字段名是否正确；
    - artifact 是否真实存在；
    - 上下游字段映射；
    - 具体输入输出变量名；
    - 最终 E2E 闭环。
    """

    deterministic_issues = deterministic_issues or []
    review_context = review_context if isinstance(review_context, dict) else {}

    if deterministic_issues:
        return {
            "passed": False,
            "issues": deterministic_issues,
            "repair_instructions": "按确定性静态检查结果修复当前脚本源码。",
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

    messages = [
        {
            "role": "system",
            "content": (
                "你是 Creator 第一轮单脚本职责审查模型，只输出严格 JSON object。\n\n"

                "你只判断当前 scripts/** 源码是否完成 SkillPlanEntry 描述的自身职责。"
                "你不是 smoke runner，不是 E2E 审查器，不判断运行环境、argv、stdout、artifact、"
                "字段名映射、上下游 dataflow 或最终产物质量。\n\n"

                "关键边界：\n"
                "- inputs / outputs 中的名称只作为推荐名和语义提示，不是 hard gate；\n"
                "- 不得因为脚本没有逐字使用推荐字段名就判失败；\n"
                "- 不得要求脚本必须使用某种固定输入结构；\n"
                "- 不得要求脚本必须输出某个具体字段名；\n"
                "- 如果脚本通过 payload、统一 input object、配置对象、工具结果、模型结果或等价结构完成同一语义责任，应判通过；\n"
                "- 运行错误、argv 映射、stdout 字段、artifact 存在性由第二轮 E2E 负责。\n\n"

                "可以判失败的情况：\n"
                "1. 当前脚本没有实现 purpose 要求的核心职责；\n"
                "2. 核心输出完全来自固定常量、空壳模板或与输入语义无关；\n"
                "3. 脚本只是协议壳、演示壳、占位壳；\n"
                "4. 工具/模型/本地处理结果没有参与当前脚本的核心职责。\n\n"

                "不能判失败的情况：\n"
                "1. 具体字段名不是推荐名；\n"
                "2. stdout 字段名是否最终匹配；\n"
                "3. 脚本是否能在当前环境运行；\n"
                "4. 上游是否真的产生这些字段；\n"
                "5. artifact 文件是否存在；\n"
                "6. artifact 内容质量、排版质量、主观质量；\n"
                "7. 是否必须调用某个具体工具、模型或函数。\n\n"

                "如果问题只是字段名、stdout、artifact、运行错误或 E2E 数据流问题，"
                "只能放 advisory_notes，不得放 blocking_issues。\n\n"

                "返回格式：\n"
                "{\n"
                "  \"passed\": true|false,\n"
                "  \"blocking_issues\": [\n"
                "    {\n"
                "      \"issue_type\": \"responsibility_not_met\",\n"
                "      \"scope\": \"current_file_only\",\n"
                "      \"severity\": \"error\",\n"
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

                "SkillPlanEntry：\n"
                f"{json.dumps(skill_plan_entry.__dict__, ensure_ascii=False, default=str)[:8000]}\n\n"

                "推荐 inputs（非硬约束）：\n"
                f"{json.dumps(declared_inputs, ensure_ascii=False)}\n\n"

                "推荐 outputs（非硬约束）：\n"
                f"{json.dumps(declared_outputs, ensure_ascii=False)}\n\n"

                "额外上下文：\n"
                f"{json.dumps(review_context, ensure_ascii=False, default=str)[:4000]}\n\n"

                "当前脚本源码（带行号）：\n"
                f"{_numbered_source(script_content)[-16000:]}\n\n"

                "审查要求：\n"
                "1. 只判断当前脚本是否完成自身职责。\n"
                "2. 不要检查运行、argv、stdout、artifact。\n"
                "3. 不要因为字段名和推荐名不一致而失败。\n"
                "4. 如果只是接口映射或运行问题，放 advisory_notes。\n"
                "5. 只有职责本身没有实现，才 passed=false。\n"
            ),
        },
    ]

    try:
        text = await complete_chat_once(messages, route.model)
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
            "model": route.model,
        }

    data = _parse_validator_json_object(text)
    if not isinstance(data, dict) or not data:
        return {
            "passed": False,
            "issues": [{
                "id": "script_responsibility.invalid_json",
                "failed_file": file_path,
                "failed_function": "responsibility_review",
                "code_region": "review",
                "reason": "职责审查模型没有返回合法 JSON object。",
                "minimal_edit": "不是脚本内容错误；请重试或切换 validator 模型。",
                "allowed_scope": "不要自动修改脚本。",
                "forbidden_scope": "不得因为 validator 输出非法而判定脚本通过。",
                "details": {"raw": str(text or "")[:1000]},
            }],
            "repair_instructions": "职责审查模型输出非法，不能放行当前脚本。",
            "model": route.model,
        }

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

    return {
        "passed": True,
        "issues": [],
        "repair_instructions": "",
        "model": route.model,
        "advisory_notes": data.get("advisory_notes") if isinstance(data.get("advisory_notes"), list) else [],
    }

__all__ = [name for name in globals() if not name.startswith("__")]
