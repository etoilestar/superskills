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


def test_seed_initial_e2e_payload_populates_typed_fields_from_script_schema(tmp_path):
    dynamic_key = "dynamic_list_field"
    skill_dir = tmp_path / "typed-skill"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "scripts" / "generate_story.py").write_text(
        "from backend.services.runtime_tools import strict_json_argv_guard\n"
        "def parse(payload):\n"
        f"    return strict_json_argv_guard(payload, {{\"{dynamic_key}\": {{\"type\": list, \"required\": True}}}})\n",
        encoding="utf-8",
    )
    command = E2EWorkflowCommand(
        1,
        "SKILL.md",
        "scripts/generate_story.py",
        f"python scripts/generate_story.py '{{\"{dynamic_key}\":\"{{{{fields.{dynamic_key}}}}}\"}}'",
        "python",
        {dynamic_key: "{{fields." + dynamic_key + "}}"},
    )

    payload = e2e._seed_initial_e2e_payload([command], skill_dir=skill_dir)
    rendered = e2e._render_e2e_command_payload(command, payload=payload)

    assert isinstance(payload["fields"][dynamic_key], list)
    assert payload["fields"][dynamic_key]
    assert rendered[dynamic_key] == payload["fields"][dynamic_key]

    provided_value = ["runtime-value"]
    payload = e2e._seed_initial_e2e_payload(
        [command],
        skill_dir=skill_dir,
        external_context={"fields": {dynamic_key: provided_value}},
    )
    rendered = e2e._render_e2e_command_payload(command, payload=payload)

    assert payload["fields"][dynamic_key] == provided_value
    assert rendered[dynamic_key] == provided_value


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


def test_checkpoint_rejects_changed_script_hash(tmp_path, monkeypatch):
    skill_dir = _make_skill(tmp_path)
    _patch_fast_e2e(monkeypatch)
    monkeypatch.setattr(e2e, "_execute_e2e_python_command", lambda command, **kwargs: subprocess.CompletedProcess([], 0, stdout='{"ok": true}', stderr=""))
    monkeypatch.setattr(e2e, "_parse_e2e_stdout_json", lambda command, **kwargs: ({"one": "ok"} if command.ordinal == 1 else {"text": "done"}))

    session = e2e._create_e2e_session("demo", source_skill_dir=skill_dir)
    assert e2e._run_skill_workflow_e2e_once("demo", source_skill_dir=skill_dir, e2e_session=session) == []
    assert e2e._load_valid_checkpoint(session, 1) is not None

    (session.workspace_dir / "scripts" / "one.py").write_text("print('changed')\n", encoding="utf-8")
    assert e2e._load_valid_checkpoint(session, 1) is None

@pytest.mark.asyncio
async def test_validate_skill_e2e_repair_handoff_continues_to_next_target(monkeypatch, tmp_path):
    from backend.services.creator import api
    from backend.services.creator.common import SkillActionRequest

    skill_name = "handoff-skill"
    skill_dir = tmp_path / skill_name
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: handoff\n---\n", encoding="utf-8")
    (skill_dir / "scripts" / "step1.py").write_text("bad1", encoding="utf-8")
    (skill_dir / "scripts" / "step2.py").write_text("bad2", encoding="utf-8")

    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    monkeypatch.setattr(api, "run_action", lambda _payload: pytest.fail("validate_skill must not call quick_validate/run_action"))
    monkeypatch.setattr(api, "_create_e2e_session", lambda *_args, **_kwargs: SimpleNamespace(events=[]))

    validation_targets = iter([
        ["E2E_REPAIR_TARGET=scripts/step1.py\nE2E_LAYER=script_stdout\nstep1 failed"],
        ["E2E_REPAIR_TARGET=scripts/step2.py\nE2E_LAYER=script_stdout\nstep2 failed"],
        [],
    ])
    repaired_targets = []

    def fake_validate_workflow_e2e(*_args, **_kwargs):
        return next(validation_targets)

    async def fake_repair_existing_file_for_e2e_failure(**kwargs):
        target = kwargs["target_path"]
        repaired_targets.append(target)
        events = kwargs.get("repair_events")
        if target == "scripts/step1.py":
            if events is not None:
                events.append({
                    "target_file": target,
                    "patch_status": "partial_success_target_changed",
                    "status": "target_changed",
                    "next_target": "scripts/step2.py",
                })
            return {
                "status": "target_changed",
                "repaired_target": target,
                "next_target": "scripts/step2.py",
                "next_failure": ["E2E_REPAIR_TARGET=scripts/step2.py\nE2E_LAYER=script_stdout\nstep2 failed"],
            }
        if events is not None:
            events.append({"target_file": target, "patch_status": "e2e_fully_passed", "status": "repaired"})
        return {"status": "repaired", "repaired_target": target, "next_target": None, "next_failure": []}

    monkeypatch.setattr(api, "validate_workflow_e2e", fake_validate_workflow_e2e)
    monkeypatch.setattr(api, "_repair_existing_file_for_e2e_failure", fake_repair_existing_file_for_e2e_failure)

    response = await api.validate_skill(SkillActionRequest(skill_name=skill_name, auto_repair=True, max_e2e_repair_attempts=1))

    assert response.success is True
    assert repaired_targets == ["scripts/step1.py", "scripts/step2.py"]
    assert "E2E_REPAIR_TARGET_CHANGED" not in response.message
    assert [event["patch_status"] for event in response.repair_events] == [
        "partial_success_target_changed",
        "e2e_fully_passed",
    ]


def test_e2e_command_parser_normalizes_bare_value_placeholders():
    from backend.services.creator.common import _parse_e2e_workflow_command

    command = _parse_e2e_workflow_command(
        command="python scripts/generate_illustrations.py '{\"story_segments\": {{story_segments}}, \"style\": \"fixed-style\"}'",
        ordinal=1,
        source_path="SKILL.md",
    )

    assert command is not None
    assert command.argv_template == {"story_segments": "{{story_segments}}", "style": "fixed-style"}


def test_e2e_command_parser_normalization_renders_whole_placeholder_types():
    from backend.services.creator.common import _parse_e2e_workflow_command
    from backend.services.creator.e2e import _render_e2e_command_payload

    command = _parse_e2e_workflow_command(
        command="python scripts/build_pdf.py '{\"story_segments\": {{story_segments}}, \"image_paths\": {{image_paths}}}'",
        ordinal=2,
        source_path="SKILL.md",
    )
    payload = {
        "story_segments": [{"text": "Once"}],
        "image_paths": ["outputs/a.png"],
    }

    assert command is not None
    assert command.argv_template == {
        "story_segments": "{{story_segments}}",
        "image_paths": "{{image_paths}}",
    }
    rendered = _render_e2e_command_payload(command, payload=payload)
    assert rendered == payload
    assert isinstance(rendered["story_segments"], list)
    assert isinstance(rendered["story_segments"][0], dict)
    assert isinstance(rendered["image_paths"], list)


def test_e2e_command_parser_accepts_nested_and_index_placeholders():
    from backend.services.creator.common import _parse_e2e_workflow_command

    command = _parse_e2e_workflow_command(
        command="python scripts/main.py '{\"nested\": {{field.subkey}}, \"first\": {{field.0}}, \"label\": \"ok\"}'",
        ordinal=1,
        source_path="SKILL.md",
    )

    assert command is not None
    assert command.argv_template == {"nested": "{{field.subkey}}", "first": "{{field.0}}", "label": "ok"}


def test_e2e_command_parser_rejects_non_value_illegal_json():
    from backend.services.creator.common import _parse_e2e_workflow_command

    with pytest.raises(ValueError) as exc_info:
        _parse_e2e_workflow_command(
            command="python scripts/main.py '{{bad_key}}: \"value\"}'",
            ordinal=1,
            source_path="SKILL.md",
        )

    assert "command_json_parse" in str(exc_info.value)


def test_e2e_repair_hint_does_not_misdiagnose_bare_value_placeholder_as_quote_escape():
    hint = e2e._targeted_e2e_repair_hint([
        "E2E_LAYER=command_json_parse\nproblem=Expecting value near {{story_segments}}"
    ])

    assert "确定性规范化" in hint
    assert "误诊断为双引号转义问题" in hint


def test_e2e_typed_seed_materializes_requirement_shapes(tmp_path):
    skill_dir = tmp_path / "typed-shapes"
    skill_dir.mkdir()
    commands = [E2EWorkflowCommand(1, "SKILL.md", "scripts/a.py", "python scripts/a.py '{}'", "python", {})]
    reqs = {
        "scripts/a.py": [
            e2e.RequirementItem(target_file="scripts/a.py", inputs=[
                "items: list[string]",
                "attachments: list[file_path]",
                "source_file: file_path",
                "config: object",
            ])
        ]
    }

    payload = e2e._seed_initial_e2e_payload(commands, skill_dir=skill_dir, requirements_by_file=reqs)

    assert payload["items"] == ["sample item 1", "sample item 2"]
    assert len(payload["attachments"]) == 2
    assert all(Path(path).is_file() for path in payload["attachments"])
    assert Path(payload["source_file"]).is_file()
    assert payload["config"] == {"value": "sample value"}


def test_e2e_placeholder_bracket_and_dot_indexes_are_equivalent():
    payload = {"items": ["first", "second"], "result": {"items": ["nested-first"]}}
    missing = []

    assert e2e._resolve_e2e_payload_expr("items[0]", payload=payload, missing=missing) == "first"
    assert e2e._resolve_e2e_payload_expr("items.0", payload=payload, missing=missing) == "first"
    assert e2e._resolve_e2e_payload_expr("result.items[0]", payload=payload, missing=missing) == "nested-first"
    assert e2e._resolve_e2e_payload_expr("result.items.0", payload=payload, missing=missing) == "nested-first"
    assert missing == []


def test_e2e_missing_placeholder_reports_empty_list_index_out_of_range():
    command = E2EWorkflowCommand(
        1,
        "SKILL.md",
        "scripts/a.py",
        "python scripts/a.py '{}'",
        "python",
        {"item": "{{items[0]}}"},
    )

    with pytest.raises(ValueError) as exc:
        e2e._render_e2e_command_payload(
            command,
            payload={"items": []},
            typed_input_specs=[e2e.E2ETypedInputSpec(name="items", shape="list[string]", source="requirement_graph")],
        )

    message = str(exc.value)
    assert '"reason": "index_out_of_range"' in message
    assert '"root_shape": "list[0]"' in message
    assert '"expected_shape_from_graph": "list[string]"' in message


def test_e2e_infers_indexed_placeholder_root_item_shape_from_argv_file_path(tmp_path):
    skill_dir = tmp_path / "argv-file-path"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "scripts" / "consume.py").write_text(
        "from backend.services.runtime_tools import strict_json_argv_guard\n"
        "def parse(payload):\n"
        "    return strict_json_argv_guard(payload, {'file_path': {'type': 'file_path', 'required': True}})\n",
        encoding="utf-8",
    )
    command = E2EWorkflowCommand(1, "SKILL.md", "scripts/consume.py", "python scripts/consume.py '{}'", "python", {"file_path": "{{items[0]}}"})

    specs = e2e._collect_e2e_typed_inputs_from_graph(commands=[command], requirements_by_file={}, skill_plan_entries=None, skill_dir=skill_dir)
    by_name = {spec.name: spec for spec in specs}
    payload = e2e._seed_initial_e2e_payload([command], skill_dir=skill_dir)

    assert by_name["items"].shape == "list[file_path]"
    assert len(payload["items"]) == 2
    assert all(Path(path).is_file() for path in payload["items"])


def test_e2e_infers_indexed_placeholder_root_item_shape_from_argv_object(tmp_path):
    skill_dir = tmp_path / "argv-object"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "scripts" / "consume.py").write_text(
        "from backend.services.runtime_tools import strict_json_argv_guard\n"
        "def parse(payload):\n"
        "    return strict_json_argv_guard(payload, {'target_item': {'type': 'object', 'required': True}})\n",
        encoding="utf-8",
    )
    command = E2EWorkflowCommand(1, "SKILL.md", "scripts/consume.py", "python scripts/consume.py '{}'", "python", {"target_item": "{{records[0]}}"})

    specs = e2e._collect_e2e_typed_inputs_from_graph(commands=[command], requirements_by_file={}, skill_plan_entries=None, skill_dir=skill_dir)
    by_name = {spec.name: spec for spec in specs}

    assert by_name["records"].shape == "list[object]"


def test_e2e_infers_unindexed_placeholder_root_shape_from_list_argv(tmp_path):
    skill_dir = tmp_path / "argv-list"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "scripts" / "consume.py").write_text(
        "from backend.services.runtime_tools import strict_json_argv_guard\n"
        "def parse(payload):\n"
        "    return strict_json_argv_guard(payload, {'documents': {'type': 'list[string]', 'required': True}})\n",
        encoding="utf-8",
    )
    command = E2EWorkflowCommand(1, "SKILL.md", "scripts/consume.py", "python scripts/consume.py '{}'", "python", {"documents": "{{documents}}"})

    specs = e2e._collect_e2e_typed_inputs_from_graph(commands=[command], requirements_by_file={}, skill_plan_entries=None, skill_dir=skill_dir)
    by_name = {spec.name: spec for spec in specs}
    payload = e2e._seed_initial_e2e_payload([command], skill_dir=skill_dir)

    assert by_name["documents"].shape == "list[string]"
    assert payload["documents"] == ["sample item 1", "sample item 2"]


def test_e2e_input_files_files_alias_sync_preserves_non_empty_external_context(tmp_path):
    skill_dir = tmp_path / "alias-sync"
    skill_dir.mkdir()
    commands = [E2EWorkflowCommand(1, "SKILL.md", "scripts/a.py", "python scripts/a.py '{}'", "python", {})]

    reqs = {"scripts/a.py": [e2e.RequirementItem(target_file="scripts/a.py", inputs=["input_files: list[file_path]"])]}
    payload = e2e._seed_initial_e2e_payload(commands, skill_dir=skill_dir, requirements_by_file=reqs)
    assert payload["input_files"]
    assert payload["files"] == payload["input_files"]

    reqs = {"scripts/a.py": [e2e.RequirementItem(target_file="scripts/a.py", inputs=["files: list[file_path]"])]}
    payload = e2e._seed_initial_e2e_payload(commands, skill_dir=skill_dir, requirements_by_file=reqs)
    assert payload["files"]
    assert payload["input_files"] == payload["files"]

    payload = e2e._seed_initial_e2e_payload(
        commands,
        skill_dir=skill_dir,
        requirements_by_file={"scripts/a.py": [e2e.RequirementItem(target_file="scripts/a.py", inputs=["input_files: list[file_path]"])]},
        external_context={"input_files": ["real-input.txt"], "files": ["real-files.txt"]},
    )
    assert payload["input_files"] == ["real-input.txt"]
    assert payload["files"] == ["real-files.txt"]


def test_e2e_fields_fallback_normalizes_bracket_placeholder(tmp_path):
    dynamic_key = "attachments"
    skill_dir = tmp_path / "fields-normalized"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "scripts" / "consume.py").write_text(
        "from backend.services.runtime_tools import strict_json_argv_guard\n"
        "def parse(payload):\n"
        "    return strict_json_argv_guard(payload, {'attachments': {'type': 'list[file_path]', 'required': True}})\n",
        encoding="utf-8",
    )
    command = E2EWorkflowCommand(1, "SKILL.md", "scripts/consume.py", "python scripts/consume.py '{}'", "python", {"first": "{{fields.attachments[0]}}"})

    payload = e2e._seed_initial_e2e_payload([command], skill_dir=skill_dir)

    assert len(payload["fields"][dynamic_key]) == 2
    assert all(Path(path).is_file() for path in payload["fields"][dynamic_key])


def test_e2e_missing_placeholder_reports_index_not_integer_and_empty_expr():
    missing = []
    details = []
    value = e2e._resolve_e2e_payload_expr("items[x]", payload={"items": ["a"]}, missing=missing, missing_details=details)
    assert value == ""
    assert details[-1]["reason"] == "index_not_integer"
    assert details[-1]["root_shape"] == "list[1]<string(non_empty)>"

    missing = []
    details = []
    value = e2e._resolve_e2e_payload_expr("   ", payload={}, missing=missing, missing_details=details)
    assert value == ""
    assert details[-1]["reason"] == "empty_expr"
