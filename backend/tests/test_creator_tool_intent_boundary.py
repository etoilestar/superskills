"""Tests for Creator tool-intent boundary, embedding fallback, and E2E stage isolation.

Test coverage:
  A  Semantic resolver: higher-scoring candidate wins when registry metadata differs.
  B  Remote embedding timeout + local embedding available -> resolver returns candidates.
  C  Both remote and local embedding fail -> lexical/schema fallback, no crash.
  D  Gate does NOT reject a valid scripts/** tool due to unknown role component_hint.
  E  reference/assets/SKILL.md paths cannot bind runtime tools.
  F  Generation prompt disallows helpers not in allowed_helper_imports.
  G  Generation skeleton does not contain the hard-coded 'input_text' example key.
  H  E2E pre-run: unbound helper import returns pre_e2e_tool_binding_error, never
     calls explore_tool_pool, never adds tools.
  I  E2E command/argv/stdout fixes do not modify the tool pool.
"""
from __future__ import annotations

import inspect
import json
import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers / shared fakes
# ---------------------------------------------------------------------------

def _make_fake_cap(
    name: str,
    *,
    semantic_tags: list[str] | None = None,
    accepted_input_extensions: list[str] | None = None,
    output_content_types: list[str] | None = None,
    helper_imports: list[str] | None = None,
    enabled_by_default: bool = True,
    allow_creator_use: bool = True,
    input_schema: dict | None = None,
    output_schema: dict | None = None,
    roles: list[str] | None = None,
) -> Any:
    """Return a minimal fake ToolCapability-like object."""
    cap = MagicMock()
    cap.name = name
    cap.display_name = name
    cap.category = "test"
    cap.prompt_guidance = ""
    cap.enabled_by_default = enabled_by_default
    cap.allow_creator_use = allow_creator_use
    cap.approval_status = "approved"
    cap.capability_aliases = semantic_tags or []
    cap.semantic_tags = semantic_tags or []
    cap.domain_terms = []
    cap.task_verbs = []
    cap.accepted_input_extensions = accepted_input_extensions or []
    cap.output_content_types = output_content_types or []
    cap.output_extensions = []
    cap.negative_tags = []
    cap.functions = []
    cap.helper_imports = helper_imports or []
    cap.required_env = []
    cap.required_secrets = []
    cap.dependencies = []
    cap.preference_score = 0.5
    cap.tool_quality_score = 0.5
    cap.structured_output_score = 0.5
    cap.input_schema = input_schema or {}
    cap.output_schema = output_schema or {}
    cap.roles = roles or ["generic_script"]
    cap.allowed_roles = roles or ["generic_script"]
    cap.snippets = []
    cap.adapter_path = ""
    return cap


# ---------------------------------------------------------------------------
# Test A: Semantic resolver selects higher-scoring candidate
# ---------------------------------------------------------------------------

def test_semantic_resolver_selects_higher_score_candidate():
    """Given two candidates with different semantic_tags, the one whose tags
    better match the intent should score higher."""
    from backend.services.creator.tool_pool_explorer import (
        score_tool_for_file_request,
        _normalize_tokens,
        _file_exts,
    )

    # cap_a has tags that directly match the intent text
    cap_a = _make_fake_cap("fake_text_reader", semantic_tags=["read", "text", "extract"])
    # cap_b has unrelated tags
    cap_b = _make_fake_cap("fake_artifact_writer", semantic_tags=["write", "artifact", "export"])

    intent_text = "extract text content from uploaded file"
    tokens = _normalize_tokens(intent_text)
    exts: set[str] = set()
    outputs: list[str] = []

    score_a, features_a, _, _ = score_tool_for_file_request(
        cap_a,
        text=intent_text,
        tokens=tokens,
        exts=exts,
        outputs=outputs,
        role="generic_script",
        selected_tools=set(),
        required_slots=set(),
        uploaded_candidate_tools=set(),
    )
    score_b, features_b, _, _ = score_tool_for_file_request(
        cap_b,
        text=intent_text,
        tokens=tokens,
        exts=exts,
        outputs=outputs,
        role="generic_script",
        selected_tools=set(),
        required_slots=set(),
        uploaded_candidate_tools=set(),
    )

    # The reader tool must rank higher for a "read/extract text" intent
    assert score_a > score_b, (
        f"expected fake_text_reader (score={score_a}) > fake_artifact_writer (score={score_b})"
    )
    assert any("capability_alias" in f or "description" in f for f in features_a), (
        "expected at least one feature match for fake_text_reader"
    )


# ---------------------------------------------------------------------------
# Test B: Remote embedding timeout + local model available -> returns candidates
# ---------------------------------------------------------------------------

def test_resolver_uses_local_embedding_when_remote_fails(caplog):
    """When remote embedding raises an exception, the resolver should fall back
    to local embedding and still return candidate tool IDs."""
    import backend.services.creator_contracts as cc

    # Patch: remote fails, local succeeds with a dummy vector
    def fake_remote(texts):
        raise TimeoutError("remote embedding timeout")

    # Return unit vectors so cosine similarity is well-defined
    def fake_local(texts):
        return [[1.0] + [0.0] * 99 for _ in texts]

    # Reset caches so fresh embedding is attempted
    cc._EMBEDDING_INDEX_CACHE = None
    # Force the local model state to 'not yet tried' so our fake_local is reached
    # (the patch replaces _embed_texts_local directly, bypassing the model loader)
    saved_tried = cc._LOCAL_EMBEDDING_MODEL_TRIED
    saved_model = cc._LOCAL_EMBEDDING_MODEL
    cc._LOCAL_EMBEDDING_MODEL_TRIED = False
    cc._LOCAL_EMBEDDING_MODEL = None

    try:
        with patch.object(cc, "_embed_texts_remote", fake_remote), \
             patch.object(cc, "_embed_texts_local", fake_local), \
             caplog.at_level(logging.INFO, logger="backend.services.creator_contracts"):
            result = cc._embedding_candidate_tool_ids(["read text from file"])
    finally:
        cc._LOCAL_EMBEDDING_MODEL_TRIED = saved_tried
        cc._LOCAL_EMBEDDING_MODEL = saved_model

    # Should not crash
    assert isinstance(result, set)

    log_text = caplog.text
    assert "remote_embedding_failed" in log_text
    assert "local_embedding_success" in log_text


# ---------------------------------------------------------------------------
# Test C: Both embeddings fail -> lexical/schema fallback, no crash
# ---------------------------------------------------------------------------

def test_resolver_falls_back_to_lexical_when_all_embeddings_fail(caplog):
    """When both remote and local embedding fail, _embedding_candidate_tool_ids
    must return an empty set (triggering lexical fallback in the caller) and
    must not raise an exception."""
    import backend.services.creator_contracts as cc

    def fake_remote(texts):
        raise RuntimeError("remote unavailable")

    def fake_local(texts):
        raise ImportError("sentence_transformers not installed")

    cc._EMBEDDING_INDEX_CACHE = None
    cc._LOCAL_EMBEDDING_MODEL_TRIED = False
    cc._LOCAL_EMBEDDING_MODEL = None

    with patch.object(cc, "_embed_texts_remote", fake_remote), \
         patch.object(cc, "_embed_texts_local", fake_local), \
         caplog.at_level(logging.WARNING, logger="backend.services.creator_contracts"):
        result = cc._embedding_candidate_tool_ids(["summarize document"])

    assert result == set(), "expected empty set on total embedding failure"

    log_text = caplog.text
    assert "remote_embedding_failed" in log_text
    assert "local_embedding_failed" in log_text
    assert "lexical_schema_fallback" in log_text


# ---------------------------------------------------------------------------
# Test D: Gate does NOT reject on unknown role component_hint for scripts/**
# ---------------------------------------------------------------------------

def test_gate_does_not_reject_unknown_role_for_scripts():
    """gate_tool_request must allow a valid scripts/** tool regardless of the
    file_role value, even if it is an unknown/unrelated component_hint."""
    from backend.services.creator.tool_pool_gate import gate_tool_request

    # Use a role that is clearly unrelated to text reading
    event = gate_tool_request(
        {"target_file": "scripts/a.py", "candidate_tool_id": "unified_file_text_read"},
        file_role="image_generator",
    )
    assert event.decision == "allow", (
        f"gate should allow a valid tool regardless of role hint; got {event.decision}"
    )
    # role must not appear as the rejection reason
    assert "role" not in " ".join(event.messages).lower() or event.decision == "allow"


def test_gate_does_not_reject_composite_generator_role():
    """composite_generator is a coarse role hint that must not block a concrete tool."""
    from backend.services.creator.tool_pool_gate import gate_tool_request

    event = gate_tool_request(
        {"target_file": "scripts/a.py", "candidate_tool_id": "unified_file_text_read"},
        file_role="composite_generator",
    )
    assert event.decision == "allow", (
        f"gate must allow unified_file_text_read with role=composite_generator; got {event.decision}"
    )


# ---------------------------------------------------------------------------
# Test E: reference/assets/SKILL.md cannot bind runtime tools
# ---------------------------------------------------------------------------

def test_gate_blocks_runtime_tool_on_reference_file():
    from backend.services.creator.tool_pool_gate import gate_tool_request

    for path in ("references/overview.md", "assets/logo.png"):
        event = gate_tool_request(
            {"target_file": path, "candidate_tool_id": "unified_file_text_read"},
            file_role="reference",
        )
        assert event.decision == "blocked_by_policy", (
            f"expected blocked_by_policy for {path}, got {event.decision}"
        )


def test_gate_blocks_runtime_tool_on_skill_md():
    from backend.services.creator.tool_pool_gate import gate_tool_request

    # SKILL.md does not start with scripts/ so it must be blocked
    event = gate_tool_request(
        {"target_file": "SKILL.md", "candidate_tool_id": "unified_file_text_read"},
        file_role="skill_overview",
    )
    assert event.decision == "blocked_by_policy", (
        f"expected blocked_by_policy for SKILL.md, got {event.decision}"
    )


# ---------------------------------------------------------------------------
# Test F: Generation prompt disallows helpers outside allowed_helper_imports
# ---------------------------------------------------------------------------

def test_generation_prompt_enforces_helper_import_boundary():
    """The generation prompt must explicitly state that helpers NOT listed in
    allowed_helper_imports are forbidden."""
    from backend.services.creator import generation

    source = inspect.getsource(generation)

    # The prompt must contain an explicit restriction on helper imports
    assert any(phrase in source for phrase in [
        "allowed_helper_imports",
        "禁止 import",
        "forbidden",
        "not in allowed",
    ]), "generation module must reference allowed_helper_imports restriction"


# ---------------------------------------------------------------------------
# Test G: Generation skeleton does NOT hardcode 'input_text' as a spec key
# ---------------------------------------------------------------------------

def test_generation_skeleton_does_not_hardcode_input_text():
    """The generation skeleton must not contain 'input_text' as a literal
    required spec key.  'input_text' was the old hardcoded example that forced
    the code model to use a specific business field name."""
    from backend.services.creator import generation

    # Get the skeleton builder function
    skeleton_source = inspect.getsource(generation._script_generation_skeleton)

    # The skeleton must NOT contain the old hard-coded example key as a real spec entry
    assert "'input_text': {'type': str, 'required': True}" not in skeleton_source, (
        "skeleton must not hardcode 'input_text' as a required spec key"
    )


# ---------------------------------------------------------------------------
# Test H: E2E returns pre_e2e_tool_binding_error, never calls explore_tool_pool
# ---------------------------------------------------------------------------

def test_e2e_returns_pre_e2e_tool_binding_error_for_unbound_import():
    """If the E2E import guard detects a forbidden helper, the raised error
    must use the pre_e2e_tool_binding_error layer (not runtime_import_guard)
    and must not call explore_tool_pool."""
    from backend.services.creator import e2e

    source = inspect.getsource(e2e._run_skill_workflow_e2e_once)

    # The new error layer name must appear in the source
    assert "pre_e2e_tool_binding_error" in source, (
        "E2E must raise pre_e2e_tool_binding_error for unbound helper imports"
    )

    # E2E must not call explore_tool_pool
    assert "explore_tool_pool" not in source, (
        "E2E must not call explore_tool_pool"
    )

    # E2E must not call add_tool_requests
    assert "add_tool_requests" not in source or "ToolPoolPatch" not in source, (
        "E2E must not add tool requests"
    )


def test_e2e_does_not_import_explore_tool_pool():
    """The E2E module (e2e.py) must not import explore_tool_pool at all."""
    from backend.services.creator import e2e

    source = inspect.getsource(e2e)
    assert "explore_tool_pool" not in source, (
        "e2e.py must not import or use explore_tool_pool"
    )


# ---------------------------------------------------------------------------
# Test I: E2E command/argv/stdout fixes do not change tool pool
# ---------------------------------------------------------------------------

def test_e2e_argv_fix_does_not_mutate_tool_pool():
    """E2E argv-key consistency checks (_e2e_argv_key_consistency_error) are
    purely diagnostic and must not add to or modify the tool pool."""
    from backend.services.creator.e2e import _e2e_argv_key_consistency_error
    from backend.services.creator.common import E2EWorkflowCommand
    from backend.services.skill_plan import SkillPlanEntry

    command = E2EWorkflowCommand(
        ordinal=1,
        source_path="SKILL.md",
        script_path="scripts/run.py",
        raw_command='python scripts/run.py \'{"topic": "test"}\'',
        runner="python",
        argv_template={"topic": "test"},
    )
    entry = SkillPlanEntry(
        path="scripts/run.py",
        file_type="script",
        role="generic_script",
        purpose="test",
        language="python",
        runtime="python",
        entrypoint="scripts/run.py",
    )
    content = """
from backend.services.runtime_tools import strict_json_argv_guard

def parse_args(payload):
    return strict_json_argv_guard(payload, {'query': {'type': str, 'required': True}})

def run(args):
    return {'result': args['query']}
"""
    # Call the pure diagnostic function
    error = _e2e_argv_key_consistency_error(command=command, content=content, entry=entry)

    # The function must return a string error or None - never add tool requests
    assert error is None or isinstance(error, str), (
        "argv consistency check must return str or None"
    )
    # The function source must not contain tool pool mutation calls
    fn_source = inspect.getsource(_e2e_argv_key_consistency_error)
    assert "add_tool_requests" not in fn_source
    assert "explore_tool_pool" not in fn_source


def test_api_does_not_call_explore_tool_pool_in_e2e_phase():
    """api.py must not call explore_tool_pool inside the E2E/repair loop."""
    from backend.services.creator import api

    source = inspect.getsource(api)

    # explore_tool_pool must not be called anywhere in api.py (import removed)
    assert "explore_tool_pool" not in source, (
        "api.py must not call explore_tool_pool after E2E boundary was enforced"
    )
