from pathlib import Path
import inspect
import json
from types import SimpleNamespace
import subprocess
import sys

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

    response = await api.validate_skill(SkillActionRequest(skill_name=skill_name, auto_repair=True, max_e2e_repair_attempts=2))

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

    assert "items" not in payload
    assert "attachments" not in payload
    assert "source_file" not in payload
    assert "config" not in payload


def test_e2e_seed_does_not_materialize_recommended_or_argv_schema_root_fields(tmp_path):
    recommended_key = "generic_arg_key"
    skill_dir = tmp_path / "recommended-root"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "scripts" / "consume.py").write_text(
        "from backend.services.runtime_tools import strict_json_argv_guard\n"
        "def parse(payload):\n"
        f"    return strict_json_argv_guard(payload, {{'{recommended_key}': {{'type': 'string', 'required': True}}}})\n",
        encoding="utf-8",
    )
    command = E2EWorkflowCommand(
        1,
        "SKILL.md",
        "scripts/consume.py",
        "python scripts/consume.py '{}'",
        "python",
        {recommended_key: "{{generic_arg_key}}"},
    )
    reqs = {
        "scripts/consume.py": [
            e2e.RequirementItem(target_file="scripts/consume.py", inputs=[f"{recommended_key}: string"])
        ]
    }

    payload = e2e._seed_initial_e2e_payload([command], skill_dir=skill_dir, requirements_by_file=reqs)

    assert recommended_key not in payload
    assert recommended_key not in payload["fields"]
    with pytest.raises(ValueError) as exc:
        e2e._render_e2e_command_payload(command, payload=payload)
    assert "external_input_missing" in str(exc.value)


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
    assert "items" not in payload


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
    assert "documents" not in payload


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

    assert payload["fields"][dynamic_key]
    assert all(isinstance(path, str) for path in payload["fields"][dynamic_key])
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

@pytest.mark.asyncio
async def test_e2e_repair_stays_localized_after_repeated_attempts(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    skill_dir = root / "demo"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Demo\n```bash\npython scripts/one.py '{}'\n```\n", encoding="utf-8")
    (skill_dir / "scripts" / "one.py").write_text("print({})\n", encoding="utf-8")
    monkeypatch.setattr(e2e.settings, "skills_path", root)
    monkeypatch.setattr(e2e, "_skill_plan_entry_for_file", lambda **kwargs: SimpleNamespace(role="generic_script", runtime="python", runtime_contract={"stdout": ["ok"]}, coverage_requirements=["emit ok"], selected_tools=[]))
    monkeypatch.setattr(e2e, "_validate_e2e_script_static_preflight", lambda **kwargs: None)
    monkeypatch.setattr(e2e, "_extract_e2e_workflow_commands", lambda *args, **kwargs: [E2EWorkflowCommand(1, "SKILL.md", "scripts/one.py", "python scripts/one.py '{}'", "python", {})])
    monkeypatch.setattr(e2e, "_command_plan_signature", lambda commands: "sig")
    monkeypatch.setattr(e2e, "_earliest_invalid_step", lambda **kwargs: 1)
    monkeypatch.setattr(e2e, "_invalidate_checkpoints_from", lambda *args, **kwargs: [])
    monkeypatch.setattr(e2e, "_load_valid_checkpoint", lambda *args, **kwargs: None)

    patch_calls = []
    async def fake_patch(**kwargs):
        patch_calls.append(kwargs)
        return None, "print({})\n", {"changed_line_count": 0, "applied": [{"fallback_type": "none"}]}
    monkeypatch.setattr(e2e, "_request_and_apply_repair_patch", fake_patch)

    full_calls = []
    async def fake_full(**kwargs):
        full_calls.append(kwargs)
        return "print('{\"ok\": true}')\n"
    monkeypatch.setattr(e2e, "_request_full_file_rewrite_for_e2e", fake_full)

    gate_calls = []
    def fake_gate(**kwargs):
        gate_calls.append(kwargs)
        if len(gate_calls) < 3:
            return {"accepted": False, "errors": ["E2E_REPAIR_TARGET=scripts/one.py\nE2E_LAYER=stdout_schema\nstill missing ok"]}
        return {"accepted": True, "errors": []}
    monkeypatch.setattr(e2e, "_run_e2e_sandbox_acceptance_gate", fake_gate)

    events = []
    result = await e2e._repair_existing_file_for_e2e_failure(
        skill_name="demo",
        target_path="scripts/one.py",
        e2e_errors=["E2E_REPAIR_TARGET=scripts/one.py\nE2E_LAYER=stdout_schema\nmissing ok"],
        repair_events=events,
    )

    assert result["status"] == "debug_hypothesis_rejected"
    assert result["sandbox_executed"] is True
    assert len(patch_calls) == 1
    assert full_calls == []
    assert len(gate_calls) == 1
    assert result["hypothesis_key"]
    assert events[-1]["writeback_status"] == "rolled_back"


def _write_trial_script(tmp_path: Path, script: str, command_payload: dict | None = None):
    skill_dir = tmp_path / "trial-skill"
    (skill_dir / "scripts").mkdir(parents=True)
    script_path = skill_dir / "scripts" / "run.py"
    script_path.write_text(script, encoding="utf-8")
    payload = command_payload if command_payload is not None else {"user_request": "hello"}
    raw_payload = json.dumps(payload, ensure_ascii=False)
    skill_md = f"---\nname: demo\ndescription: Demo skill\n---\n# Demo\n```bash\npython scripts/run.py '{raw_payload}'\n```\n"
    (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    command = E2EWorkflowCommand(1, "SKILL.md", "scripts/run.py", f"python scripts/run.py '{raw_payload}'", "python", payload)
    return skill_dir, command, skill_md, payload


def _generic_python_entry(**kwargs):
    return SimpleNamespace(runtime="python", language="python", role="generic_script", file_type="script", file_kind="script", path=kwargs.get("file_path", "scripts/run.py"), inputs=["user_request"], outputs=["text"], dependencies=[], required_capabilities=[], runtime_contract={"stdout": ["text"]}, artifact_contract={}, artifacts=[])


def _parse_trial_stdout(command, skill_dir, skill_md, script, entry, payload, proc):
    return e2e._parse_e2e_stdout_json(command=command, proc=proc, trial_skill_dir=skill_dir, trial_skill_md=skill_md, content=script, entry=entry, rendered_payload=payload)


def test_inline_dunder_main_enters_real_subprocess_and_parses_stdout(tmp_path, monkeypatch):
    script = 'import json\nimport sys\n\nif __name__ == "__main__":\n    payload = json.loads(sys.argv[1])\n    print(json.dumps({"text": payload["user_request"]}))\n'
    skill_dir, command, skill_md, payload = _write_trial_script(tmp_path, script)
    monkeypatch.setattr(e2e, "_skill_plan_entry_for_file", _generic_python_entry)
    entry = e2e._validate_e2e_command_static(command=command, trial_skill_dir=skill_dir, skill_md=skill_md)
    proc = e2e._execute_e2e_python_command(command=command, trial_skill_dir=skill_dir, rendered_payload=payload, venv_python=Path(sys.executable))
    assert proc.returncode == 0
    assert _parse_trial_stdout(command, skill_dir, skill_md, script, entry, payload, proc) == {"text": "hello"}


def test_top_level_script_without_dunder_main_runs_successfully(tmp_path, monkeypatch):
    script = 'import json\nimport sys\n\npayload = json.loads(sys.argv[1])\nprint(json.dumps({"text": payload["user_request"]}))\n'
    skill_dir, command, skill_md, payload = _write_trial_script(tmp_path, script)
    monkeypatch.setattr(e2e, "_skill_plan_entry_for_file", _generic_python_entry)
    entry = e2e._validate_e2e_command_static(command=command, trial_skill_dir=skill_dir, skill_md=skill_md)
    proc = e2e._execute_e2e_python_command(command=command, trial_skill_dir=skill_dir, rendered_payload=payload, venv_python=Path(sys.executable))
    assert proc.returncode == 0
    assert _parse_trial_stdout(command, skill_dir, skill_md, script, entry, payload, proc) == {"text": "hello"}


def test_script_with_no_stdout_fails_after_real_execution_not_static_preflight(tmp_path, monkeypatch):
    script = 'import json\nimport sys\n\npayload = json.loads(sys.argv[1])\n_ = payload["user_request"]\n'
    skill_dir, command, skill_md, payload = _write_trial_script(tmp_path, script)
    monkeypatch.setattr(e2e, "_skill_plan_entry_for_file", _generic_python_entry)
    entry = e2e._validate_e2e_command_static(command=command, trial_skill_dir=skill_dir, skill_md=skill_md)
    proc = e2e._execute_e2e_python_command(command=command, trial_skill_dir=skill_dir, rendered_payload=payload, venv_python=Path(sys.executable))
    assert proc.returncode == 0
    with pytest.raises(ValueError) as excinfo:
        _parse_trial_stdout(command, skill_dir, skill_md, script, entry, payload, proc)
    failure = str(excinfo.value)
    assert "script_static_contract" not in failure
    assert "\"return_code\": 0" in failure
    assert "stdout" in failure


def test_python_syntax_error_is_real_subprocess_failure_not_static_preflight(tmp_path, monkeypatch):
    script = "if True print('bad')\n"
    skill_dir, command, skill_md, payload = _write_trial_script(tmp_path, script)
    monkeypatch.setattr(e2e, "_skill_plan_entry_for_file", _generic_python_entry)
    entry = e2e._validate_e2e_command_static(command=command, trial_skill_dir=skill_dir, skill_md=skill_md)
    proc = e2e._execute_e2e_python_command(command=command, trial_skill_dir=skill_dir, rendered_payload=payload, venv_python=Path(sys.executable))
    assert proc.returncode != 0
    assert "SyntaxError" in proc.stderr
    with pytest.raises(ValueError) as excinfo:
        _parse_trial_stdout(command, skill_dir, skill_md, script, entry, payload, proc)
    failure = str(excinfo.value)
    assert "script_exit" in failure
    assert "script_static_contract" not in failure
    assert "\"failed_command\": \"python scripts/run.py" in failure
    assert "SyntaxError" in failure


def test_argv_guard_type_error_comes_from_real_subprocess(tmp_path, monkeypatch):
    script = 'import json\nimport sys\nfrom backend.services.runtime_tools import strict_json_argv_guard\n\npayload = json.loads(sys.argv[1])\nargs = strict_json_argv_guard(payload, {"user_request": {"type": str, "required": True}})\nprint(json.dumps({"text": args["user_request"]}))\n'
    payload = {"user_request": ["not", "a", "string"]}
    skill_dir, command, skill_md, _ = _write_trial_script(tmp_path, script, payload)
    monkeypatch.setattr(e2e, "_skill_plan_entry_for_file", _generic_python_entry)
    entry = e2e._validate_e2e_command_static(command=command, trial_skill_dir=skill_dir, skill_md=skill_md)
    proc = e2e._execute_e2e_python_command(command=command, trial_skill_dir=skill_dir, rendered_payload=payload, venv_python=Path(sys.executable))
    assert proc.returncode != 0
    assert "user_request" in proc.stderr
    with pytest.raises(ValueError) as excinfo:
        _parse_trial_stdout(command, skill_dir, skill_md, script, entry, payload, proc)
    failure = str(excinfo.value)
    assert "script_static_contract" not in failure
    assert "E2E_REPAIR_TARGET=" in failure
    assert "return_code" in failure
    assert "user_request" in failure

def test_input_text_list_guard_preflight_reaches_subprocess(tmp_path, monkeypatch):
    skill_dir = tmp_path / "input-text-list"
    (skill_dir / "scripts").mkdir(parents=True)
    script = (
        "import json, sys\n"
        "from backend.services.runtime_tools import strict_json_argv_guard\n"
        "def run(args):\n"
        "    return {'text': ','.join(args['input_text'])}\n"
        "def main():\n"
        "    payload = json.loads(sys.argv[1])\n"
        "    args = strict_json_argv_guard(payload, {'input_text': {'type': list, 'required': True}, 'num_images': {'type': int, 'required': False, 'default': 3}})\n"
        "    print(json.dumps(run(args)))\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
    )
    script_path = skill_dir / "scripts" / "run.py"
    script_path.write_text(script, encoding="utf-8")
    skill_md = "# Demo\n```bash\npython scripts/run.py '{\"input_text\":[\"a\",\"b\"]}'\n```\n"
    (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    command = E2EWorkflowCommand(1, "SKILL.md", "scripts/run.py", "python scripts/run.py '{\"input_text\":[\"a\",\"b\"]}'", "python", {"input_text": ["a", "b"]})

    monkeypatch.setattr(e2e, "_skill_plan_entry_for_file", lambda **kwargs: SimpleNamespace(runtime="python", language="python", role="generic_script", path="scripts/run.py", inputs=["input_text"], outputs=["text"], required_capabilities=[]))
    entry = e2e._validate_e2e_command_static(command=command, trial_skill_dir=skill_dir, skill_md=skill_md)
    assert entry.runtime == "python"

    result = e2e._execute_e2e_python_command(command=command, trial_skill_dir=skill_dir, rendered_payload={"input_text": ["a", "b"]}, venv_python=Path(sys.executable))
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"text": "a,b"}
    assert script_path.read_text(encoding="utf-8") == script


def test_argv_schema_failure_targets_skill_md_for_command_key_error_and_script_for_undeclared_run_key():
    command = E2EWorkflowCommand(1, "SKILL.md", "scripts/main.py", "python scripts/main.py '{}'", "python", {"title": "wrong"})
    entry = SimpleNamespace(runtime="python", inputs=["input_text"])
    self_consistent = 'ALLOWED_KEYS = {"input_text"}\nREQUIRED_KEYS = {"input_text"}\ndef run(argv):\n    return {"text": argv.get("input_text")}\n'
    details = e2e._classify_argv_schema_failure(
        command=command,
        content=self_consistent,
        entry=entry,
        rendered_payload={"title": "wrong"},
        stdout="",
        stderr="ValueError: missing required argv keys: ['input_text']",
    )
    assert details["primary_target"] == "SKILL.md"

    mismatched = 'ALLOWED_KEYS = {"input_text"}\nREQUIRED_KEYS = {"input_text"}\ndef run(argv):\n    return {"text": argv["title"]}\n'
    details = e2e._classify_argv_schema_failure(
        command=command,
        content=mismatched,
        entry=entry,
        rendered_payload={"title": "wrong"},
        stdout="",
        stderr="ValueError: missing required argv keys: ['input_text']",
    )
    assert details["primary_target"] == "scripts/main.py"
    assert details["script_guard_run_mismatch"] is True


def test_final_step_json_without_platform_terminal_fields_still_passes(tmp_path, monkeypatch):
    skill_dir = _make_skill(tmp_path)
    _patch_fast_e2e(monkeypatch)
    monkeypatch.setattr(
        e2e,
        "_validate_final_platform_output_contract",
        lambda **kwargs: pytest.fail("final platform output contract should not be called by E2E closure"),
    )
    monkeypatch.setattr(
        e2e,
        "_execute_e2e_python_command",
        lambda command, **kwargs: subprocess.CompletedProcess([], 0, stdout='{"ok": true}', stderr=""),
    )
    monkeypatch.setattr(
        e2e,
        "_parse_e2e_stdout_json",
        lambda command, **kwargs: ({"one": "ok"} if command.ordinal == 1 else {"custom_business_result": {"ok": True}}),
    )

    session = e2e._create_e2e_session("demo", source_skill_dir=skill_dir)

    assert e2e._run_skill_workflow_e2e_once("demo", source_skill_dir=skill_dir, e2e_session=session) == []



def _callable_context(function_name: str = "real_callable_name"):
    return {
        "authorization_scope": "skill",
        "read_only": True,
        "binding_digest": "digest123",
        "available_tools": [{
            "tool_id": f"fixture_tool.{function_name}",
            "capability_name": "fixture_tool",
            "function_name": function_name,
        }],
        "resolved_tools": [{
            "tool_id": f"fixture_tool.{function_name}",
            "capability_name": "fixture_tool",
            "function_name": function_name,
            "import_path": "tests.fixtures.creator_tools",
            "signature": f"{function_name}(text: str) -> dict",
            "return_contract": {"type": "object"},
        }],
    }


def test_e2e_missing_import_runs_real_subprocess_without_import_guard_failure(tmp_path, monkeypatch):
    script = "from definitely_missing_creator_package import run\nrun()\n"
    skill_dir, _command, _skill_md, _payload = _write_trial_script(tmp_path, script)
    monkeypatch.setattr(e2e, "_skill_plan_entry_for_file", _generic_python_entry)

    errors = e2e._run_skill_workflow_e2e_once("trial-skill", source_skill_dir=skill_dir)

    joined = "\n".join(errors)
    assert "ModuleNotFoundError" in joined
    assert "script_exit" in joined or "return_code" in joined
    assert "runtime_import_guard_failed" not in joined


@pytest.mark.asyncio
async def test_import_error_repair_prompt_receives_read_only_callable_context(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    skill_dir = root / "demo"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
    (skill_dir / "scripts" / "one.py").write_text("print('before')\n", encoding="utf-8")
    monkeypatch.setattr(e2e.settings, "skills_path", root)
    monkeypatch.setattr(e2e, "_skill_plan_entry_for_file", _generic_python_entry)

    captured = {}

    async def fake_patch(**kwargs):
        captured["task_context"] = kwargs["task_context"]
        return None, "print('after')\n", {"changed_line_count": 1, "applied": [{"fallback_type": "test"}]}

    monkeypatch.setattr(e2e, "_request_and_apply_repair_patch", fake_patch)
    monkeypatch.setattr(e2e, "_run_e2e_sandbox_acceptance_gate", lambda **kwargs: {"accepted": True, "errors": []})

    context = _callable_context()
    failure = {"stderr": "ImportError: cannot import name X from Y"}
    from backend.services.creator import api
    assert api._is_callable_runtime_failure(failure, context) is True

    result = await e2e._repair_existing_file_for_e2e_failure(
        skill_name="demo",
        target_path="scripts/one.py",
        e2e_errors=["E2E_REPAIR_TARGET=scripts/one.py\nE2E_LAYER=script_exit\nImportError: cannot import name X from Y"],
        read_only_callable_context=context,
    )

    assert result["status"] == "repaired"
    prompt = captured["task_context"]
    assert "read_only=true" in prompt
    assert "binding_digest" in prompt
    assert "resolved_tools" in prompt
    assert "tests.fixtures.creator_tools" in prompt
    assert "real_callable_name(text: str) -> dict" in prompt


def test_callable_runtime_failure_filters_business_type_errors_and_tool_type_errors():
    from backend.services.creator import api
    context = _callable_context()

    assert api._is_callable_runtime_failure(
        {"stderr": "TypeError: unsupported operand type(s) for +: 'int' and 'str'"},
        context,
    ) is False
    assert api._is_callable_runtime_failure(
        {"stderr": "TypeError: real_callable_name() got an unexpected keyword argument 'bad'"},
        context,
    ) is True


def test_callable_runtime_failure_filters_local_name_errors_and_tool_name_errors():
    from backend.services.creator import api
    context = _callable_context()

    assert api._is_callable_runtime_failure(
        {"stderr": "NameError: name 'local_result' is not defined"},
        context,
    ) is False
    assert api._is_callable_runtime_failure(
        {"stderr": "NameError: name 'real_callable_name' is not defined"},
        context,
    ) is True


def test_e2e_callable_context_builder_is_read_only_and_empty_context_is_not_callable_failure():
    from backend.services.creator import api

    assert api._is_callable_runtime_failure(
        {"stderr": "TypeError: whatever() got an unexpected keyword argument"},
        {"resolved_tools": []},
    ) is False


def test_e2e_repair_source_forbids_tool_exploration_and_mock_fallbacks():
    source = inspect.getsource(e2e._repair_existing_file_for_e2e_failure)
    assert "allow_tool_explore=False" in source
    assert "不得请求工具探索或 tool_pool_patch" in source
    assert "_recall_creator_tool_candidates" not in source
    assert "_plan_tool_pool_patch_from_responsibility_feedback" not in source
    assert "gate_tool_request" not in source
    assert "build_tool_pool" not in source
    assert "save_tool_pool" not in source
    assert "mock" in source
    assert "placeholder" in source
    assert "fixed text" in source
    assert "fake path" in source


def test_e2e_repair_does_not_mutate_toolpool_digest(tmp_path, monkeypatch):
    from backend.services.creator import api
    from backend.services.creator.tool_pool_models import ToolPoolModel, ToolPoolTool
    from backend.services.creator.tool_pool_store import get_skill_tool_binding, load_tool_pool, save_tool_pool

    root = tmp_path / "skills"
    skill_dir = root / "demo"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
    (skill_dir / "scripts" / "one.py").write_text("print('before')\n", encoding="utf-8")
    pool = ToolPoolModel(
        skill_name="demo",
        tools=[ToolPoolTool(tool_id="system_text_generation", status="allowed", source="system_required")],
    )
    save_tool_pool(skill_dir, pool)
    monkeypatch.setattr(api.settings, "skills_path", root)

    before_pool = load_tool_pool(skill_dir)
    before_binding = get_skill_tool_binding(before_pool, target_file="scripts/one.py", include_script_core=True).model_dump(mode="json")
    before_digest = api._tool_binding_digest(before_binding)

    context = api._build_e2e_callable_repair_context(skill_name="demo", target_file="scripts/one.py")

    after_pool = load_tool_pool(skill_dir)
    after_binding = get_skill_tool_binding(after_pool, target_file="scripts/one.py", include_script_core=True).model_dump(mode="json")
    assert [tool.tool_id for tool in after_pool.tools] == [tool.tool_id for tool in before_pool.tools]
    assert api._tool_binding_digest(after_binding) == before_digest
    assert context.get("read_only") is True or context == {}

@pytest.mark.asyncio
async def test_e2e_debug_diagnosis_can_choose_skill_md_not_symptom(monkeypatch, tmp_path):
    skill_dir = _make_skill(tmp_path)
    session = e2e._create_e2e_session("demo", source_skill_dir=skill_dir)
    monkeypatch.setattr(e2e, "_complete_chat_once_sync_for_e2e", lambda *_args: json.dumps({
        "repair_target": "SKILL.md", "root_cause_hypothesis": "The command passes an invalid upstream argument.",
        "evidence": ["step 2 payload"], "repair_instruction": "Fix the command", "confidence": "high",
    }))
    diagnosis = await e2e._diagnose_e2e_failure_for_repair(
        skill_name="demo", skill_dir=skill_dir,
        e2e_errors=["E2E_SYMPTOM_FILE=scripts/two.py\nE2E_LAYER=script_exit\nE2E_STRUCTURED_FAILURE={\"target_file\": \"scripts/two.py\"}"],
        e2e_session=session,
    )
    assert diagnosis["symptom_file"] == "scripts/two.py"
    assert diagnosis["repair_target"] == "SKILL.md"


@pytest.mark.asyncio
async def test_e2e_debug_diagnosis_rejects_repeated_failed_hypothesis(monkeypatch, tmp_path):
    skill_dir = _make_skill(tmp_path)
    session = e2e._create_e2e_session("demo", source_skill_dir=skill_dir)
    hypothesis = "Upstream script emits an invalid payload."
    key = f"scripts/one.py|{e2e._normalized_debug_hypothesis(hypothesis)}"
    session.debug_attempts.append({"hypothesis_key": key, "improved": False, "result": "no_progress"})
    monkeypatch.setattr(e2e, "_complete_chat_once_sync_for_e2e", lambda *_args: json.dumps({
        "repair_target": "scripts/one.py", "root_cause_hypothesis": hypothesis,
    }))
    diagnosis = await e2e._diagnose_e2e_failure_for_repair(
        skill_name="demo", skill_dir=skill_dir,
        e2e_errors=["E2E_SYMPTOM_FILE=scripts/two.py"], e2e_session=session,
    )
    # Hypothesis prose is no longer the deduplication key; a real candidate
    # digest is required before an experiment can be rejected.
    assert diagnosis["repair_target"] == "scripts/one.py"

@pytest.mark.asyncio
async def test_e2e_diagnosis_reads_session_workspace_and_retries_rejected_proposal(monkeypatch, tmp_path):
    skill_dir = _make_skill(tmp_path)
    (skill_dir / "scripts" / "one.py").write_text("official-old\n", encoding="utf-8")
    session = e2e._create_e2e_session("demo", source_skill_dir=skill_dir)
    (session.workspace_dir / "scripts" / "one.py").write_text("session-accepted-patch\n", encoding="utf-8")
    hypothesis_a = "already tested"
    session.debug_attempts.append({
        "hypothesis_key": f"scripts/one.py|{e2e._normalized_debug_hypothesis(hypothesis_a)}",
        "improved": False,
        "result": "no_progress",
    })
    calls, prompts = [], []
    proposals = iter([
        {"repair_target": "scripts/one.py", "root_cause_hypothesis": hypothesis_a},
        {"repair_target": "scripts/two.py", "root_cause_hypothesis": "different upstream cause"},
    ])
    def fake_complete(messages, *_args):
        calls.append(1)
        prompts.append(messages[1]["content"])
        return json.dumps(next(proposals))
    monkeypatch.setattr(e2e, "_complete_chat_once_sync_for_e2e", fake_complete)
    diagnosis = await e2e._diagnose_e2e_failure_for_repair(
        skill_name="demo", skill_dir=skill_dir,
        e2e_errors=["E2E_SYMPTOM_FILE=scripts/two.py"], e2e_session=session,
    )
    assert diagnosis["repair_target"] == "scripts/one.py"
    assert len(calls) == 1
    assert "session-accepted-patch" in prompts[0]

@pytest.mark.asyncio
async def test_patch_failed_history_is_not_a_rejected_hypothesis(monkeypatch, tmp_path):
    skill_dir = _make_skill(tmp_path)
    session = e2e._create_e2e_session("demo", source_skill_dir=skill_dir)
    hypothesis = "patch formatting failed before execution"
    session.debug_attempts.append({"hypothesis_key": f"scripts/one.py|{e2e._normalized_debug_hypothesis(hypothesis)}", "improved": None, "result": "patch_failed"})
    monkeypatch.setattr(e2e, "_complete_chat_once_sync_for_e2e", lambda *_args: json.dumps({"repair_target": "scripts/one.py", "root_cause_hypothesis": hypothesis}))
    diagnosis = await e2e._diagnose_e2e_failure_for_repair(skill_name="demo", skill_dir=skill_dir, e2e_errors=["E2E_SYMPTOM_FILE=scripts/two.py"], e2e_session=session)
    assert diagnosis["repair_target"] == "scripts/one.py"

@pytest.mark.asyncio
async def test_validate_skill_bounds_patch_proposal_exhaustion_cycles(monkeypatch, tmp_path):
    from backend.services.creator import api
    from backend.services.creator.common import SkillActionRequest
    skill_dir = tmp_path / "demo"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    monkeypatch.setattr(api, "_create_e2e_session", lambda *_a, **_k: SimpleNamespace(events=[]))
    monkeypatch.setattr(api, "validate_workflow_e2e", lambda *_a, **_k: ["E2E_SYMPTOM_FILE=scripts/a.py\nE2E_LAYER=script_exit"])
    calls = []
    async def exhausted(**_kwargs):
        calls.append(1)
        return {"status": "patch_proposal_exhausted", "repaired_target": "scripts/a.py"}
    monkeypatch.setattr(api, "_repair_existing_file_for_e2e_failure", exhausted)
    response = await api.validate_skill(SkillActionRequest(skill_name="demo", auto_repair=True, max_e2e_repair_attempts=1))
    assert response.success is False
    assert "orchestration cycle" in response.message
    assert len(calls) == 3

@pytest.mark.asyncio
@pytest.mark.parametrize("first_status", ["debug_hypothesis_rejected", "debug_progress"])
async def test_validate_skill_continues_after_sandbox_debug_outcome(monkeypatch, tmp_path, first_status):
    from backend.services.creator import api
    from backend.services.creator.common import SkillActionRequest
    skill_dir = tmp_path / "demo"; (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    monkeypatch.setattr(api, "_create_e2e_session", lambda *_a, **_k: SimpleNamespace(events=[]))
    failures = iter([["E2E_SYMPTOM_FILE=scripts/b.py\nE2E_LAYER=script_exit"], ["E2E_SYMPTOM_FILE=scripts/a.py\nE2E_LAYER=script_exit"], []])
    monkeypatch.setattr(api, "validate_workflow_e2e", lambda *_a, **_k: next(failures))
    results = iter([{"status": first_status, "repaired_target": "scripts/b.py", "sandbox_executed": True}, {"status": "repaired", "repaired_target": "scripts/a.py", "sandbox_executed": True}])
    calls = []
    async def repair(**kwargs): calls.append(kwargs["target_path"]); return next(results)
    monkeypatch.setattr(api, "_repair_existing_file_for_e2e_failure", repair)
    response = await api.validate_skill(SkillActionRequest(skill_name="demo", auto_repair=True, max_e2e_repair_attempts=2))
    assert response.success is True
    assert calls == ["scripts/b.py", "scripts/a.py"]


def _structured_runtime_error(*, workspace: str, exception: str, source: str, step: int = 2):
    failure = {
        "failed_step_index": step,
        "target_file": "scripts/generate_images.py",
        "target_region": "run",
        "failed_command": "python scripts/generate_images.py '{}'",
        "return_code": 1,
        "layer": "script_exit",
        "actual": "runtime failed",
        "stderr": (
            f'Traceback (most recent call last):\n  File "{workspace}/scripts/generate_images.py", line 12, in run\n'
            f"    {source}\n{exception}: failure\n"
        ),
        "details": {"failure_code": "script_exit"},
    }
    return "E2E_STRUCTURED_FAILURE=" + json.dumps(failure)


def test_same_step_new_runtime_breakpoint_is_debug_progress():
    before = _structured_runtime_error(
        workspace="/tmp/creator-e2e-session-a", exception="TypeError", source='response["text"]',
    )
    after = _structured_runtime_error(
        workspace="/tmp/creator-e2e-session-b", exception="NameError", source="file_outputs",
    )
    before_identity = e2e._e2e_failure_identity(before, target_file="scripts/generate_images.py")
    after_identity = e2e._e2e_failure_identity(after, target_file="scripts/generate_images.py")

    assert e2e._e2e_candidate_improved([before], [after], target_file="scripts/generate_images.py") is True
    assert e2e._e2e_breakpoint_changed(before_identity, after_identity) is True
    assert before_identity["failed_step_index"] == after_identity["failed_step_index"] == 2
    assert before_identity["layer"] == after_identity["layer"] == "script_exit"


def test_same_breakpoint_with_only_session_path_change_is_not_progress():
    before = _structured_runtime_error(
        workspace="/tmp/creator-e2e-session-a", exception="TypeError", source='response["text"]',
    )
    after = _structured_runtime_error(
        workspace="/tmp/creator-e2e-session-b", exception="TypeError", source='response["text"]',
    )
    assert e2e._e2e_candidate_improved([before], [after], target_file="scripts/generate_images.py") is False
    assert e2e._e2e_failure_identity(before)["traceback_source_line"] == e2e._e2e_failure_identity(after)["traceback_source_line"]


def test_experiment_key_deduplicates_wording_but_allows_different_patch(tmp_path):
    before = _structured_runtime_error(
        workspace="/tmp/creator-e2e-session-a", exception="TypeError", source='response["text"]',
    )
    identity = e2e._e2e_failure_identity(before, target_file="scripts/generate_images.py")
    one = e2e._e2e_experiment_key(repair_target="scripts/generate_images.py", before_failure_identity=identity, patch_digest=e2e._stable_json_hash("patch one"))
    same = e2e._e2e_experiment_key(repair_target="scripts/generate_images.py", before_failure_identity=identity, patch_digest=e2e._stable_json_hash("patch one"))
    two = e2e._e2e_experiment_key(repair_target="scripts/generate_images.py", before_failure_identity=identity, patch_digest=e2e._stable_json_hash("patch two"))
    assert one == same
    assert one != two

    skill_dir = _make_skill(tmp_path)
    session = e2e._create_e2e_session("demo", source_skill_dir=skill_dir)
    session.debug_attempts.extend([
        {"result": "no_progress", "repair_target": "scripts/generate_images.py", "before_failure_identity": identity},
        {"result": "no_progress", "repair_target": "scripts/generate_images.py", "before_failure_identity": identity},
    ])
    assert e2e._count_matching_no_progress_attempts(session, repair_target="scripts/generate_images.py", before_failure_identity=identity) == 2
