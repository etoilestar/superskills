from backend.services.creator.e2e import (
    _artifact_runtime_state,
    _e2e_behavior_fingerprint,
    _e2e_candidate_improved,
    _runtime_binding_trace,
    _verified_bindings_from_runtime_trace,
)
from backend.services.creator.common import E2EWorkflowCommand


def _failure(filesystem_trace, *, code="artifact_not_created"):
    filesystem_trace = dict(filesystem_trace)
    filesystem_trace.setdefault("failure_code", code)
    return (
        "E2E_REPAIR_TARGET=scripts/x.py\n"
        "E2E_LAYER=stdout_contract\n"
        "E2E_STRUCTURED_FAILURE="
        + __import__("json").dumps({
            "failed_step_index": 1,
            "target_file": "scripts/x.py",
            "layer": "stdout_contract",
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
        value_provenance={"story_text": {"producer_step": 1, "producer_script": "scripts/a.py"}},
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
