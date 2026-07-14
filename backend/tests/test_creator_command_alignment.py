from backend.services.creator.contracts import build_command_alignment_snapshot


SCRIPT = 'def run(args):\n    return {"ok": args["content"]}\n'


def test_current_command_is_candidate_not_confirmed_and_marks_missing_source():
    snapshot = build_command_alignment_snapshot(
        script_path="scripts/x.py",
        script_content=SCRIPT,
        command='python scripts/x.py \'{"content":"{{missing_source}}"}\'',
        platform_input_fields=["user_request"],
        prior_stdout_fields=[],
        function_execution_context={},
    )

    assert snapshot["confirmed_bindings"] == {}
    assert snapshot["candidate_bindings"]["content"] == {
        "source": "missing_source",
        "valid_target": True,
        "source_available": False,
    }
    assert "missing_source" not in snapshot["available_sources"]


def test_graph_and_e2e_verified_bindings_confirm_when_both_sides_valid_and_renamed():
    graph_snapshot = build_command_alignment_snapshot(
        script_path="scripts/x.py",
        script_content=SCRIPT,
        command='python scripts/x.py \'{"content":"{{story_text}}"}\'',
        platform_input_fields=["user_request"],
        prior_stdout_fields=["story_text"],
        function_execution_context={
            "incoming_edges": [{"from_output": "story_text", "to_input": "content"}]
        },
    )
    assert graph_snapshot["confirmed_bindings"] == {"content": "story_text"}
    assert graph_snapshot["candidate_bindings"] == {}

    e2e_snapshot = build_command_alignment_snapshot(
        script_path="scripts/x.py",
        script_content=SCRIPT,
        command="",
        platform_input_fields=["user_request"],
        prior_stdout_fields=["story_text"],
        function_execution_context={},
        e2e_verified_bindings={"content": "story_text"},
    )
    assert e2e_snapshot["confirmed_bindings"] == {"content": "story_text"}


def test_invalid_confirmed_binding_is_demoted_to_candidate():
    snapshot = build_command_alignment_snapshot(
        script_path="scripts/x.py",
        script_content=SCRIPT,
        platform_input_fields=["user_request"],
        prior_stdout_fields=[],
        function_execution_context={},
        e2e_verified_bindings={"content": "not_available"},
    )

    assert snapshot["confirmed_bindings"] == {}
    assert snapshot["candidate_bindings"]["content"]["source"] == "not_available"
    assert snapshot["candidate_bindings"]["content"]["source_available"] is False
