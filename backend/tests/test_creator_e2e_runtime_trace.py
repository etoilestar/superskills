from backend.services.creator.e2e import (
    _artifact_runtime_state,
    _e2e_behavior_fingerprint,
    _e2e_candidate_improved,
    _runtime_binding_trace,
    _verified_bindings_from_runtime_trace,
)
from backend.services.creator.common import E2EWorkflowCommand


def _failure(filesystem_trace, *, code="artifact_not_created", layer=None, actual=""):
    filesystem_trace = dict(filesystem_trace)
    filesystem_trace.setdefault("failure_code", code)
    return (
        "E2E_REPAIR_TARGET=scripts/x.py\n"
        f"E2E_LAYER={layer or code}\n"
        "E2E_STRUCTURED_FAILURE="
        + __import__("json").dumps({
            "failed_step_index": 1,
            "target_file": "scripts/x.py",
            "layer": layer or code,
            "actual": actual,
            "details": {
                "failure_code": code,
                "filesystem_trace": filesystem_trace,
            },
        })
    )


def test_runtime_binding_trace_uses_source_root_provenance_and_verified_binding():
    command = E2EWorkflowCommand(
        ordinal=1,
        runner="python",
        script_path="scripts/x.py",
        argv_template={"content": "{{story_text}}"},
        raw_command="python scripts/x.py '{}'",
        source_path="SKILL.md",
    )
    trace = _runtime_binding_trace(
        command=command,
        payload={"story_text": "hello"},
        rendered_payload={"content": "hello"},
        value_provenance={"story_text": {"producer_step": 1, "producer_script": "scripts/a.py", "source_kind": "stdout"}},
    )

    assert trace["content"]["source_root"] == "story_text"
    assert trace["content"]["source_provenance"]["producer_step"] == 1
    assert _verified_bindings_from_runtime_trace(
        runtime_binding_trace=trace,
        script_content='def run(args):\n    return args["content"]\n',
        script_path="scripts/x.py",
    ) == {"content": "story_text"}


def test_artifact_progress_recognizes_creation_and_existing_reported_path():
    old = _failure({"created_files": [], "modified_files": [], "reported_paths": [], "resolved_reported_paths": []})
    new = _failure({
        "created_files": [{"relative_path": "out.txt"}],
        "modified_files": [],
        "reported_paths": ["missing.txt"],
        "resolved_reported_paths": [{"raw_path": "missing.txt", "exists": False}],
    }, code="artifact_return_path_missing")
    assert _e2e_candidate_improved([old], [new], target_file="scripts/x.py") is True

    old_missing = _failure({
        "created_files": [],
        "modified_files": [],
        "reported_paths": ["a.txt"],
        "resolved_reported_paths": [{"raw_path": "a.txt", "exists": False}],
    }, code="artifact_return_path_missing")
    new_missing = _failure({
        "created_files": [],
        "modified_files": [],
        "reported_paths": ["b.txt"],
        "resolved_reported_paths": [{"raw_path": "b.txt", "exists": False}],
    }, code="artifact_return_path_missing")
    assert _e2e_candidate_improved([old_missing], [new_missing], target_file="scripts/x.py") is False

    new_existing = _failure({
        "created_files": [],
        "modified_files": [],
        "reported_paths": ["ok.txt"],
        "resolved_reported_paths": [{"raw_path": "ok.txt", "exists": True}],
    }, code="artifact_validation_failed")
    assert _e2e_candidate_improved([old_missing], [new_existing], target_file="scripts/x.py") is True


def test_dynamic_baseline_uses_recent_failure_not_initial_failure():
    initial = _failure({"created_files": [], "modified_files": [], "reported_paths": [], "resolved_reported_paths": []})
    progressed = _failure({
        "created_files": [{"relative_path": "out.dat"}],
        "modified_files": [],
        "reported_paths": ["missing.dat"],
        "resolved_reported_paths": [{"raw_path": "missing.dat", "exists": False}],
    }, code="artifact_return_path_missing")

    assert _e2e_candidate_improved([initial], [progressed], target_file="scripts/x.py") is True
    assert _e2e_candidate_improved([progressed], [progressed], target_file="scripts/x.py") is False
    assert _e2e_behavior_fingerprint(progressed, target_file="scripts/x.py") == _e2e_behavior_fingerprint(progressed, target_file="scripts/x.py")
    assert _artifact_runtime_state({"created_files": [], "modified_files": [], "reported_paths": [], "resolved_reported_paths": []})["created_count"] == 0


def test_non_artifact_temp_file_does_not_count_as_progress():
    old = _failure({
        "created_files": [],
        "modified_files": [],
        "reported_paths": [],
        "resolved_reported_paths": [],
    }, code="script_exit", layer="script_exit", actual="same traceback")
    new = _failure({
        "created_files": [{"relative_path": "tmp/cache.tmp"}],
        "modified_files": [],
        "reported_paths": [],
        "resolved_reported_paths": [],
    }, code="script_exit", layer="script_exit", actual="same traceback")

    assert _e2e_candidate_improved([old], [new], target_file="scripts/x.py") is False


def test_artifact_created_file_counts_as_progress_for_artifact_failure():
    old = _failure({
        "created_files": [],
        "modified_files": [],
        "reported_paths": [],
        "resolved_reported_paths": [],
    }, code="artifact_not_created")
    new = _failure({
        "created_files": [{"relative_path": "outputs/result.dat"}],
        "modified_files": [],
        "reported_paths": ["missing/result.dat"],
        "resolved_reported_paths": [{"raw_path": "missing/result.dat", "exists": False}],
    }, code="artifact_return_path_mismatch")

    assert _e2e_candidate_improved([old], [new], target_file="scripts/x.py") is True


def test_stdout_contract_temp_file_is_not_artifact_progress():
    old = _failure({
        "created_files": [],
        "modified_files": [],
        "reported_paths": [],
        "resolved_reported_paths": [],
    }, code="stdout_contract", layer="stdout_contract", actual="stdout serialization failed")
    new = _failure({
        "created_files": [{"relative_path": "tmp/cache.tmp"}],
        "modified_files": [],
        "reported_paths": [],
        "resolved_reported_paths": [],
    }, code="stdout_contract", layer="stdout_contract", actual="stdout serialization failed")

    assert "artifact_" not in "stdout_contract"
    assert _e2e_candidate_improved([old], [new], target_file="scripts/x.py") is False


def test_synthetic_fixture_cannot_be_verified_but_external_context_can():
    command = E2EWorkflowCommand(
        ordinal=1,
        runner="python",
        script_path="scripts/x.py",
        argv_template={"topic": "{{user_request}}"},
        raw_command="python scripts/x.py '{}'",
        source_path="SKILL.md",
    )
    synthetic_trace = _runtime_binding_trace(
        command=command,
        payload={"user_request": "sample"},
        rendered_payload={"topic": "sample"},
        value_provenance={"user_request": {"producer_step": 0, "source_kind": "synthetic_fixture"}},
    )
    assert _verified_bindings_from_runtime_trace(
        runtime_binding_trace=synthetic_trace,
        script_content='def run(args):\n    return args["topic"]\n',
        script_path="scripts/x.py",
    ) == {}

    external_trace = _runtime_binding_trace(
        command=command,
        payload={"user_request": "real"},
        rendered_payload={"topic": "real"},
        value_provenance={"user_request": {"producer_step": 0, "source_kind": "external_context"}},
    )
    assert _verified_bindings_from_runtime_trace(
        runtime_binding_trace=external_trace,
        script_content='def run(args):\n    return args["topic"]\n',
        script_path="scripts/x.py",
    ) == {"topic": "user_request"}


def test_stdout_fields_final_contract_does_not_force_artifact_scope():
    from backend.services.creator.e2e import _allowed_edit_scope_for_failure, _is_artifact_validation_failure

    class Entry:
        artifact_contract = {"stdout_fields": ["text"], "final": True}

    assert _is_artifact_validation_failure(
        error="stdout JSON contains error field",
        reported_paths=[],
        entry=Entry(),
    ) is False
    assert _allowed_edit_scope_for_failure(
        target_path="scripts/text.py",
        failure_code="stdout_contract",
        failure_layer="stdout_contract",
        is_artifact_failure=False,
        created_count=1,
        missing_reported_count=0,
    ) == ["current script stdout serialization and return logic"]
