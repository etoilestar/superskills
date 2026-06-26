from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest

from backend.services.creator import e2e
from backend.services.creator.common import E2EWorkflowCommand


def _make_skill(tmp_path: Path) -> Path:
    skill_dir = tmp_path / "demo"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
    (skill_dir / "scripts" / "one.py").write_text("print('one')\n", encoding="utf-8")
    (skill_dir / "scripts" / "two.py").write_text("print('two')\n", encoding="utf-8")
    return skill_dir


def _commands():
    return [
        E2EWorkflowCommand(1, "SKILL.md", "scripts/one.py", "python scripts/one.py '{}'", "python", {}),
        E2EWorkflowCommand(2, "SKILL.md", "scripts/two.py", "python scripts/two.py '{}'", "python", {"first": "{{one}}"}),
    ]


def _patch_fast_e2e(monkeypatch):
    commands = _commands()
    monkeypatch.setattr(e2e, "_validate_skill_md_contract", lambda *args, **kwargs: None)
    monkeypatch.setattr(e2e, "_extract_e2e_workflow_commands", lambda *args, **kwargs: commands)
    monkeypatch.setattr(e2e, "_deps_signature_for_commands", lambda *args, **kwargs: "deps-v1")
    monkeypatch.setattr(e2e, "_get_skill_venv_python", lambda skill_dir: skill_dir / ".venv" / "bin" / "python")
    monkeypatch.setattr(e2e, "_install_capability_dependencies", lambda *args, **kwargs: None)
    monkeypatch.setattr(e2e, "_install_declared_dependency_packages", lambda *args, **kwargs: None)
    monkeypatch.setattr(e2e, "_contract_resolution_for_trial", lambda *args, **kwargs: (SimpleNamespace(declared_dependencies=[]), SimpleNamespace(declared_dependencies=[])))
    monkeypatch.setattr(e2e, "_validate_e2e_command_static", lambda command, **kwargs: SimpleNamespace(runtime="python", role="generic", path=command.script_path))
    monkeypatch.setattr(e2e, "_run_e2e_step_argument_effect_review", lambda **kwargs: {"passed": True})
    monkeypatch.setattr(e2e, "_validate_final_platform_output_contract", lambda **kwargs: None)
    monkeypatch.setattr(e2e, "_stdout_artifact_paths", lambda *args, **kwargs: [])

    return commands


def test_e2e_session_reuses_workspace_venv_and_dependency_signature(tmp_path, monkeypatch):
    skill_dir = _make_skill(tmp_path)
    _patch_fast_e2e(monkeypatch)
    install_calls = []
    monkeypatch.setattr(e2e, "_install_declared_dependency_packages", lambda *args, **kwargs: install_calls.append(args))
    monkeypatch.setattr(e2e, "_execute_e2e_python_command", lambda command, **kwargs: subprocess.CompletedProcess([], 0, stdout='{"one":"ok","text":"done"}', stderr=""))
    monkeypatch.setattr(e2e, "_parse_e2e_stdout_json", lambda command, **kwargs: {"one" if command.ordinal == 1 else "text": "ok"})

    session = e2e._create_e2e_session("demo", source_skill_dir=skill_dir)
    first_workspace = session.workspace_dir
    first_venv = session.venv_path

    assert e2e._run_skill_workflow_e2e_once("demo", source_skill_dir=skill_dir, e2e_session=session) == []
    assert e2e._run_skill_workflow_e2e_once("demo", source_skill_dir=skill_dir, e2e_session=session) == []

    assert session.workspace_dir == first_workspace
    assert session.venv_path == first_venv
    assert session.installed_deps_signature == "deps-v1"
    assert len(install_calls) == 2  # one install pass for two commands; second E2E run reuses deps
    assert any(event["event"] == "dependencies_reused" for event in session.events)


def test_checkpoint_saved_and_resume_from_changed_step(tmp_path, monkeypatch):
    skill_dir = _make_skill(tmp_path)
    _patch_fast_e2e(monkeypatch)
    executed = []

    def fake_execute(command, **kwargs):
        executed.append(command.ordinal)
        return subprocess.CompletedProcess([], 0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(e2e, "_execute_e2e_python_command", fake_execute)
    monkeypatch.setattr(e2e, "_parse_e2e_stdout_json", lambda command, **kwargs: ({"one": "ok"} if command.ordinal == 1 else {"text": "done"}))

    session = e2e._create_e2e_session("demo", source_skill_dir=skill_dir)
    assert e2e._run_skill_workflow_e2e_once("demo", source_skill_dir=skill_dir, e2e_session=session) == []
    assert e2e._checkpoint_path(session, 1).is_file()
    checkpoint = e2e._load_valid_checkpoint(session, 1)
    assert checkpoint["stdout_json"] == {"one": "ok"}
    assert checkpoint["context_after"]["one"] == "ok"

    invalidated = e2e._invalidate_checkpoints_from(session, 2)
    executed.clear()
    assert e2e._run_skill_workflow_e2e_once("demo", source_skill_dir=skill_dir, e2e_session=session, resume_from_step=2) == []

    assert invalidated == [2]
    assert executed == [2]
    assert any(event.get("reused_checkpoints") == [1] for event in session.events)


def test_invalidation_rules_for_script_and_skill_md_command_plan():
    commands = _commands()
    old_sig = e2e._command_plan_signature(commands)
    changed_commands = commands + [E2EWorkflowCommand(3, "SKILL.md", "scripts/three.py", "python scripts/three.py '{}'", "python", {})]
    new_sig = e2e._command_plan_signature(changed_commands)

    assert e2e._earliest_invalid_step(changed_file="scripts/two.py", commands=commands, old_command_plan_signature=old_sig, new_command_plan_signature=old_sig) == 2
    assert e2e._earliest_invalid_step(changed_file="scripts/one.py", commands=commands, old_command_plan_signature=old_sig, new_command_plan_signature=old_sig) == 1
    assert e2e._earliest_invalid_step(changed_file="SKILL.md", commands=commands, old_command_plan_signature=old_sig, new_command_plan_signature=new_sig) == 1
    assert e2e._earliest_invalid_step(changed_file="SKILL.md", commands=commands, old_command_plan_signature=old_sig, new_command_plan_signature=old_sig) is None
    assert e2e._earliest_invalid_step(changed_file="frontend/view.js", commands=commands, old_command_plan_signature=old_sig, new_command_plan_signature=old_sig) is None


def test_e2e_state_ignores_advisory_when_no_failed_checks():
    state = e2e._e2e_repair_state_from_errors([], resolved_failures=[])
    assert state["full_e2e_passed"] is True
    assert state["remaining_failed_checks"] == []
    assert state["current_target_file"] == "none"
