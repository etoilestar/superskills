import pytest

from backend.services.creator import repair
from backend.services.creator.repair import (
    CreatorDiffProposal,
    CreatorRepairScope,
    _apply_exact_replace_patch,
    _validate_repair_diff_scope,
    _request_and_apply_repair_patch,
)


def _proposal(target_file="SKILL.md", old="", new=""):
    return CreatorDiffProposal(target_file=target_file, reason="test", edits=[{"old": old, "new": new}])


def test_exact_match_success_uses_original_logic():
    original = "alpha\nbeta\ngamma\n"
    candidate, stats = _apply_exact_replace_patch(
        original_content=original,
        proposal=_proposal(old="beta", new="BETA"),
        expected_target_file="SKILL.md",
    )

    assert candidate == "alpha\nBETA\ngamma\n"
    assert stats["applied"][0]["fallback_type"] == "exact"


def test_normalized_fallback_handles_punctuation_and_whitespace():
    original = "标题： “你好”  \n下一行\n"
    candidate, stats = _apply_exact_replace_patch(
        original_content=original,
        proposal=_proposal(old='标题: "你好" 下一行', new="标题：您好\n下一行"),
        expected_target_file="SKILL.md",
    )

    assert candidate == "标题：您好\n下一行\n"
    assert stats["applied"][0]["fallback_type"] == "normalized_exact"
    assert stats["applied"][0]["similarity"] == 1.0


def test_approximate_substring_fallback_succeeds_for_unique_high_similarity_span():
    original = "# Skill\n\nUse `scripts/run.py` to read input, validate payload, and write the final report.\n"
    old = "Use `scripts/run.py` to read inputs, validate payload, and write final report."
    candidate, stats = _apply_exact_replace_patch(
        original_content=original,
        proposal=_proposal(old=old, new="Use `scripts/run.py` to validate input and write a final report."),
        expected_target_file="SKILL.md",
    )

    assert "validate input and write a final report" in candidate
    assert stats["applied"][0]["fallback_type"] == "approximate_substring"
    assert stats["applied"][0]["similarity"] >= 0.88
    assert "original_model_old_excerpt" in stats["applied"][0]


def test_approximate_substring_rejects_multiple_high_similarity_candidates():
    original = (
        "First block: use the parser to validate payload and write the final report.\n"
        "Second block: use the parser to validate payload and write the final report.\n"
    )
    old = "use the parser to validate payload and write final report"

    with pytest.raises(ValueError, match="approximate substring .*拒绝"):
        _apply_exact_replace_patch(
            original_content=original,
            proposal=_proposal(old=old, new="replacement"),
            expected_target_file="SKILL.md",
        )


def test_scripts_python_does_not_use_approximate_substring():
    original = "def run(payload):\n    return {'result': payload}\n"
    old = "def run(data):\n    return {'result': data}\n"

    with pytest.raises(ValueError, match="不启用 approximate substring"):
        _apply_exact_replace_patch(
            original_content=original,
            proposal=_proposal(target_file="scripts/main.py", old=old, new="def run(payload):\n    return {'ok': payload}\n"),
            expected_target_file="scripts/main.py",
        )


def test_fallback_still_respects_changed_line_count_scope():
    original = "# Skill\n\nUse `scripts/run.py` to read input, validate payload, and write the final report.\n"
    old = "Use `scripts/run.py` to read inputs, validate payload, and write final report."
    proposal = _proposal(old=old, new="line1\nline2\nline3\nline4")
    scope = CreatorRepairScope(phase="test", repair_type="test", target_file="SKILL.md", max_changed_lines=1)

    with pytest.raises(ValueError, match="修改行数超过"):
        _validate_repair_diff_scope(proposal=proposal, current_content=original, scope=scope)


@pytest.mark.asyncio
async def test_repeated_unapplicable_proposal_is_rejected_without_third_retry(monkeypatch):
    calls = 0
    proposal = _proposal(target_file="scripts/main.py", old="missing exact old", new="replacement")

    async def fake_request(**kwargs):
        nonlocal calls
        calls += 1
        return proposal

    monkeypatch.setattr(repair, "_request_repair_diff_proposal", fake_request)

    scope = CreatorRepairScope(phase="test", repair_type="test", target_file="scripts/main.py")
    with pytest.raises(ValueError, match="REPEATED_UNAPPLICABLE_PROPOSAL"):
        await _request_and_apply_repair_patch(
            model="test-model",
            file_path="scripts/main.py",
            current_content="print('hello')\n",
            failure_text="failure",
            scope=scope,
            task_context="ctx",
            target_rule="rule",
            patch_retry_limit=3,
        )

    assert calls == 2


def test_old_lines_new_lines_patch_parses_to_exact_replace():
    from backend.services.creator.repair import _extract_json_or_diff_proposal

    proposal = _extract_json_or_diff_proposal(
        '{"target_file":"SKILL.md","edits":[{"old_lines":["alpha","beta"],"new_lines":["alpha","BETA"]}]}',
        expected_target_file="SKILL.md",
    )

    assert proposal.edits == [{"old": "alpha\nbeta", "new": "alpha\nBETA"}]


def test_normalized_span_mapping_trims_spans_with_surrounding_whitespace():
    original = "\n\n  标题： “你好”  \n下一行\n  "
    candidate, stats = _apply_exact_replace_patch(
        original_content=original,
        proposal=_proposal(old='标题: "你好" 下一行', new="标题：您好\n下一行"),
        expected_target_file="SKILL.md",
    )

    assert candidate == "\n\n  标题：您好\n下一行\n  "
    assert stats["applied"][0]["fallback_type"] == "normalized_exact"
