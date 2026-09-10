import json
from pathlib import Path

import pytest

from backend.services.creator import e2e


def _failure(*, step, layer, code, source, exception="FileNotFoundError"):
    return "E2E_STRUCTURED_FAILURE=" + json.dumps({
        "failed_step_index": step,
        "target_file": "scripts/x.py",
        "layer": layer,
        "failed_command": "python scripts/x.py '{}'",
        "stderr": f'Traceback (most recent call last):\n  File "scripts/x.py", line 8, in run\n    {source}\n{exception}: failure',
        "details": {"failure_code": code},
    })


def test_reviewer_prompt_uses_executed_capability_and_semantic_dependency():
    source = Path("backend/services/creator/repair.py").read_text(encoding="utf-8")
    assert "Producing text for a downstream capability remains text generation" in source
    assert "returning an empty placeholder" in source
    assert "Reading or loading a declared dependency alone does not prove" in source
    assert "assigning it to an unused variable" in source


def test_repair_prompt_requires_provenance_and_allows_optional_omission():
    source = Path("backend/services/creator/e2e.py").read_text(encoding="utf-8")
    assert "script-local optional default should preferably be omitted" in source
    assert "A literal appearing only in the failing SKILL.md command is not provenance" in source
    assert "Do not invent an undeclared nested path or wrapper" in source
    assert "The previous patch was rejected because it did not materially change" in source


def test_repair_prompt_classifies_contract_failures_and_carries_contract_evidence():
    source = Path("backend/services/creator/e2e.py").read_text(encoding="utf-8")
    assert "compare the implementation with the declared input/output contracts" in source
    assert "contract mismatch, implementation logic error" in source
    assert "do not add temporary conversions or defensive patches" in source
    assert '"generated_script_source": generated_script_source' in source
    assert '"input_schema": (' in source
    assert '"output_schema": (' in source
    assert '"tool_function_output_schema": read_only_callable_context or {}' in source
    assert '"actual_runtime_payload": (' in source
    assert "Do not redesign the Skill workflow, introduce new inputs" in source


def test_step_advance_within_same_interface_boundary_is_not_progress():
    before = _failure(step=0, layer="external_input_missing", code="external_input_missing", source="resolve_placeholder()", exception="KeyError")
    after = _failure(step=1, layer="argv_schema_error", code="argv_schema_error", source="strict_json_argv_guard()", exception="ValueError")
    assert e2e._e2e_candidate_improved([before], [after], target_file="scripts/x.py") is False


def test_same_file_error_and_function_with_only_expression_change_is_not_progress():
    before = _failure(step=3, layer="script_exit", code="script_exit", source='open("references/a.md")')
    after = _failure(step=3, layer="script_exit", code="script_exit", source='open("../references/a.md")')
    assert e2e._e2e_candidate_improved([before], [after], target_file="scripts/x.py") is False


@pytest.mark.asyncio
async def test_skill_failure_uses_failed_command_script_argv_schema(tmp_path, monkeypatch):
    skill_dir = tmp_path / "demo"
    (skill_dir / "scripts").mkdir(parents=True)
    command = "python scripts/x.py '{\"max_images\":{{max_images}}}'"
    (skill_dir / "SKILL.md").write_text(f"```bash\n{command}\n```\n", encoding="utf-8")
    (skill_dir / "scripts/x.py").write_text(
        "def parse(payload):\n"
        "    return strict_json_argv_guard(payload, {'max_images': {'type': int, 'required': False, 'default': 5}})\n",
        encoding="utf-8",
    )
    session = e2e._create_e2e_session("demo", source_skill_dir=skill_dir)
    captured = {}

    def complete(messages, *_args):
        captured.update(json.loads(messages[1]["content"].split("\nReturn ", 1)[0]))
        return json.dumps({"repair_target": "SKILL.md", "root_cause_hypothesis": "argv mismatch"})

    monkeypatch.setattr(e2e, "_complete_chat_once_sync_for_e2e", complete)
    failure = "E2E_STRUCTURED_FAILURE=" + json.dumps({
        "target_file": "SKILL.md", "failed_command": command, "details": {},
    })
    await e2e._diagnose_e2e_failure_for_repair(
        skill_name="demo", skill_dir=skill_dir, e2e_errors=[failure], e2e_session=session,
    )
    schema = captured["argv_interface_provenance_facts"]["script_actual_argv_contract"]
    assert schema["optional_keys"] == ["max_images"]
    assert schema["expected_types"] == {"max_images": "int"}


@pytest.mark.asyncio
async def test_diagnosis_uses_recorded_subprocess_cwd(tmp_path, monkeypatch):
    skill_dir = tmp_path / "demo"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("```bash\npython scripts/x.py '{}'\n```\n", encoding="utf-8")
    (skill_dir / "scripts/x.py").write_text("print({})\n", encoding="utf-8")
    session = e2e._create_e2e_session("demo", source_skill_dir=skill_dir)
    captured = {}

    def complete(messages, *_args):
        captured.update(json.loads(messages[1]["content"].split("\nReturn ", 1)[0]))
        return json.dumps({"repair_target": "scripts/x.py", "root_cause_hypothesis": "path base"})

    monkeypatch.setattr(e2e, "_complete_chat_once_sync_for_e2e", complete)
    subprocess_cwd = str(session.workspace_dir / "scripts")
    failure = "E2E_STRUCTURED_FAILURE=" + json.dumps({
        "target_file": "scripts/x.py",
        "details": {"filesystem_trace": {"current_working_directory": subprocess_cwd}},
    })
    await e2e._diagnose_e2e_failure_for_repair(
        skill_name="demo", skill_dir=skill_dir, e2e_errors=[failure], e2e_session=session,
    )
    assert captured["runtime_filesystem_facts"]["current_working_directory"] == subprocess_cwd
