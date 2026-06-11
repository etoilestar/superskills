import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _write_skill(root: Path, skill_md: str, *, reference: str | None = None, asset: tuple[str, bytes] | None = None) -> Path:
    skill_dir = root / "schema-skill"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "scripts" / "run.py").write_text("print('{}')", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    if reference is not None:
        (skill_dir / "references").mkdir(exist_ok=True)
        (skill_dir / "references" / "exec.md").write_text(reference, encoding="utf-8")
    if asset is not None:
        rel, data = asset
        path = skill_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return skill_dir


def test_runtime_action_schema_reads_reference_command_entry(tmp_path):
    from backend.routers.sandbox_chat import _extract_skill_command_contract

    skill_md = """---
name: schema-skill
description: demo
---
# 使用
读取 `references/exec.md` 中的执行步骤和命令模板。
"""
    reference = """# 执行参考
role: text_generator
inputs: topic
outputs: text
required_capabilities: text_generation
forbidden_capabilities: image_generation, pdf_generation

```bash
python scripts/run.py '{"topic":"{{topic}}"}'
```
"""
    skill_dir = _write_skill(tmp_path, skill_md, reference=reference)

    contract = _extract_skill_command_contract(skill_md, execution_root=skill_dir)

    assert contract["has_executable_command_block"]
    assert contract["command_blocks"][0]["source_path"] == "references/exec.md"
    assert contract["action_schema"]["errors"] == []
    assert contract["action_schema"]["entries"][0]["role"] == "text_generator"


def test_runtime_action_schema_rejects_conflicting_skill_and_reference_entries(tmp_path):
    from backend.routers.sandbox_chat import _build_runtime_action_schema

    skill_md = """---
name: schema-skill
description: demo
---
role: text_generator
inputs: topic
outputs: text
```bash
python scripts/run.py '{"topic":"{{topic}}"}'
```
"""
    reference = """# 执行参考
role: text_generator
inputs: prompt
outputs: text
```bash
python scripts/run.py '{"prompt":"{{prompt}}"}'
```
"""
    skill_dir = _write_skill(tmp_path, skill_md, reference=reference)

    schema = _build_runtime_action_schema(skill_md, execution_root=skill_dir)

    assert any("多个不一致执行入口" in item["error"] for item in schema["errors"])


def test_runtime_command_validation_requires_declared_json_keys(tmp_path):
    from backend.routers.sandbox_chat import _validate_runtime_command_against_action_schema

    skill_md = """---
name: schema-skill
description: demo
---
role: text_generator
inputs: topic
outputs: text
```bash
python scripts/run.py '{"topic":"{{topic}}"}'
```
"""
    skill_dir = _write_skill(tmp_path, skill_md)

    with pytest.raises(ValueError, match="JSON keys"):
        _validate_runtime_command_against_action_schema(
            "python scripts/run.py '{\"topic\":\"cats\",\"extra\":\"no\"}'",
            execution_root=skill_dir,
        )

    entry = _validate_runtime_command_against_action_schema(
        "python scripts/run.py '{\"topic\":\"cats\"}'",
        execution_root=skill_dir,
    )
    assert entry["role"] == "text_generator"


def test_runtime_stdout_validation_is_field_name_agnostic():
    from backend.routers.sandbox_chat import _validate_stdout_against_action_entry

    _validate_stdout_against_action_entry(json.dumps({"draft_copy": "done"}), {"role": "text_generator"})

    with pytest.raises(ValueError, match="至少需要一个非空字段"):
        _validate_stdout_against_action_entry(json.dumps({"draft_copy": ""}), {"role": "text_generator"})


def test_runtime_action_schema_validates_referenced_assets(tmp_path):
    from backend.routers.sandbox_chat import _build_runtime_action_schema

    skill_md = """---
name: schema-skill
description: demo
---
# 使用
读取 `assets/config.json` 后执行。
role: generic_script
inputs: topic
outputs: text
```bash
python scripts/run.py '{"topic":"{{topic}}"}'
```
"""
    skill_dir = _write_skill(tmp_path, skill_md, asset=("assets/config.json", b"not-json"))

    schema = _build_runtime_action_schema(skill_md, execution_root=skill_dir)

    assert any("assets/config.json" in item.get("asset_path", "") for item in schema["errors"])


def test_composite_generator_stdout_uses_field_name_agnostic_runtime_validation():
    from backend.routers.sandbox_chat import _validate_stdout_against_action_entry

    entry = {
        "role": "composite_generator",
        "required_capabilities": ["text_generation", "image_generation"],
        "outputs": ["text", "image_paths"],
    }

    _validate_stdout_against_action_entry(json.dumps({"draft": "story"}), entry)

    with pytest.raises(ValueError, match="至少需要一个非空字段"):
        _validate_stdout_against_action_entry(json.dumps({"draft": ""}), entry)


def test_generic_script_high_impact_capability_error_mentions_explicit_roles():
    from backend.routers.sandbox_chat import _build_runtime_action_schema

    skill_md = """---
name: schema-skill
description: demo
---
role: generic_script
inputs: topic
outputs: html_path
required_capabilities: html_generation
```bash
python scripts/build.py '{"topic":"{{topic}}"}'
```
"""
    schema = _build_runtime_action_schema(skill_md)

    assert any("html_asset_builder" in item.get("error", "") for item in schema["errors"])


def test_execute_plan_read_resource_then_final_instruction_run_command_uses_stdout(tmp_path):
    from backend.routers.sandbox_chat import (
        _execute_single_task,
        _extract_executable_command_blocks_from_text,
        _finalize_answer_output_file_links,
        _render_success_stdout_payload,
    )
    from backend.routers.chat_models import ChatRequest, Message

    skill_dir = tmp_path / "story-skill"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "references").mkdir()
    (skill_dir / "assets" / "generated").mkdir(parents=True)
    (skill_dir / "references" / "story_templates.md").write_text(
        "Use a short fable style.", encoding="utf-8"
    )
    (skill_dir / "scripts" / "generate.py").write_text(
        """
import json
import sys
from pathlib import Path

payload = json.loads(sys.argv[1])
out = Path('outputs/story.png')
out.parent.mkdir(parents=True, exist_ok=True)
out.write_bytes(b'fake-png')
print(json.dumps({
    'text': f"Story about {payload['topic']}",
    'image_paths': [str(out)],
}))
""".strip(),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text(
        """---
name: story-skill
description: demo
---
Read `references/story_templates.md`, then run the declared command.
role: composite_generator
inputs: topic
outputs: text, image_paths
required_capabilities: text_generation, image_generation
forbidden_capabilities: pdf_generation
```bash
python scripts/generate.py '{"topic":"{{topic}}"}'
```
""",
        encoding="utf-8",
    )

    request = ChatRequest(messages=[Message(role="user", content="写一个狐狸故事")])
    read_result, _ = _execute_single_task(
        {"action": "read_resource", "path": "references/story_templates.md", "reason": "style"},
        [],
        request,
        execution_root=skill_dir,
        skill_name="story-skill",
    )
    assert read_result["success"] is True
    assert "short fable" in read_result["content"]

    final_instruction = """Use the loaded reference and execute:
```bash
python scripts/generate.py '{"topic":"狐狸"}'
```
"""
    commands = _extract_executable_command_blocks_from_text(final_instruction)
    assert commands == ["python scripts/generate.py '{\"topic\":\"狐狸\"}'"]

    run_result, _ = _execute_single_task(
        {"action": "run_command", "command": commands[0], "reason": "final_instruction"},
        [],
        request,
        execution_root=skill_dir,
        skill_name="story-skill",
    )
    assert run_result["success"] is True
    assert run_result["action"] == "run_command"
    assert json.loads(run_result["stdout"])["image_paths"] == ["outputs/story.png"]
    assert run_result["output_files"] == [
        {
            "path": "outputs/story.png",
            "url": "/api/skills/story-skill/files/outputs/story.png",
        }
    ]

    exec_result = {"results": [read_result, run_result], "output_files": run_result["output_files"]}
    answer = _render_success_stdout_payload(exec_result)
    answer = _finalize_answer_output_file_links(answer, exec_result["output_files"])

    assert "Story about 狐狸" in answer
    assert "/api/skills/story-skill/files/outputs/story.png" in answer


def test_final_instruction_accepts_plain_single_line_command():
    from backend.routers.sandbox_chat import _extract_executable_command_blocks_from_text

    final_instruction = "请执行下面命令：\npython scripts/generate.py '{\"topic\":\"狐狸\"}'\n完成后使用 stdout JSON。"

    assert _extract_executable_command_blocks_from_text(final_instruction) == [
        "python scripts/generate.py '{\"topic\":\"狐狸\"}'"
    ]


def test_runtime_optional_inputs_may_be_omitted_or_empty(tmp_path):
    from backend.routers.sandbox_chat import _validate_runtime_command_against_action_schema

    skill_md = """---
name: optional-skill
description: demo
---
role: composite_generator
inputs: topic, style, custom_character?
optional_inputs: custom_character
outputs: text, image_paths
required_capabilities: text_generation, image_generation
```bash
python scripts/run.py '{"topic":"{{topic}}","style":"{{style}}"}'
```
"""
    skill_dir = _write_skill(tmp_path, skill_md)

    entry = _validate_runtime_command_against_action_schema(
        "python scripts/run.py '{\"topic\":\"cats\",\"style\":\"ink\"}'",
        execution_root=skill_dir,
    )
    assert entry["optional_inputs"] == ["custom_character"]

    _validate_runtime_command_against_action_schema(
        "python scripts/run.py '{\"topic\":\"cats\",\"style\":\"ink\",\"custom_character\":\"\"}'",
        execution_root=skill_dir,
    )

    with pytest.raises(ValueError, match="JSON keys"):
        _validate_runtime_command_against_action_schema(
            "python scripts/run.py '{\"topic\":\"cats\",\"style\":\"ink\",\"extra\":\"no\"}'",
            execution_root=skill_dir,
        )


def test_planner_missing_available_script_is_corrected(tmp_path):
    from backend.routers.sandbox_chat import _normalize_skill_runtime_plan

    skill_dir = tmp_path / "available-skill"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "scripts" / "run.py").write_text("print('{}')", encoding="utf-8")

    plan = _normalize_skill_runtime_plan(
        {
            "mode": "ask_user",
            "actions": [],
            "missing": [{"path": "scripts/run.py", "reason": "planner says missing"}],
            "errors": [],
        },
        resource_catalog=[],
        execution_root=skill_dir,
        command_contract={"has_executable_command_block": True, "action_schema": {"entries": []}},
        available_scripts=["scripts/run.py"],
    )

    assert plan["missing"] == []
    assert plan["mode"] == "direct_answer"
    assert plan["planner_inconsistent"][0]["missing_type"] == "planner_inconsistent"


def test_structured_stdout_accepts_story_text_alias():
    from backend.routers.sandbox_chat import _render_success_stdout_payload, _validate_stdout_against_action_entry

    entry = {
        "role": "composite_generator",
        "required_capabilities": ["text_generation", "image_generation"],
        "outputs": ["story_text", "image_paths"],
    }
    stdout = json.dumps({"story_text": "A real story", "image_paths": ["outputs/a.png"]})

    _validate_stdout_against_action_entry(stdout, entry)
    assert "A real story" in _render_success_stdout_payload({"results": [{"success": True, "stdout": stdout}]})


def test_runtime_stdout_validation_accepts_multifunction_export_fields():
    from backend.routers.sandbox_chat import _validate_stdout_against_action_entry

    _validate_stdout_against_action_entry(
        json.dumps({"story_text": "once", "image_paths": ["outputs/img.png"]}),
        {"role": "composite_generator", "outputs": ["story_text", "image_paths"]},
    )
    _validate_stdout_against_action_entry(
        json.dumps({"docx_path": "outputs/story.docx"}),
        {"role": "docx_builder", "outputs": ["docx_path"]},
    )
    _validate_stdout_against_action_entry(
        json.dumps({"pptx_path": "outputs/story.pptx"}),
        {"role": "pptx_builder", "outputs": ["pptx_path"]},
    )


def test_output_files_from_stdout_json_collects_all_artifact_fields(tmp_path):
    from backend.routers.sandbox_chat import _output_files_from_stdout_json

    skill_dir = tmp_path / "schema-skill"
    outputs = skill_dir / "outputs"
    outputs.mkdir(parents=True)
    from zipfile import ZipFile, ZIP_DEFLATED
    (skill_dir / "outputs/story.pdf").write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
    with ZipFile(skill_dir / "outputs/story.docx", "w", ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", "<w:document/>")
    with ZipFile(skill_dir / "outputs/story.pptx", "w", ZIP_DEFLATED) as zf:
        zf.writestr("ppt/presentation.xml", "<p:presentation/>")
    (skill_dir / "outputs/story.html").write_text("<!doctype html><html></html>", encoding="utf-8")
    (skill_dir / "outputs/img.png").write_bytes(b"data")

    files = _output_files_from_stdout_json(
        json.dumps({
            "pdf_path": "outputs/story.pdf",
            "docx_path": "outputs/story.docx",
            "pptx_path": "outputs/story.pptx",
            "html_path": "outputs/story.html",
            "image_paths": ["outputs/img.png"],
        }),
        cwd=skill_dir,
        skill_name="schema-skill",
    )

    assert {item["path"] for item in files} == {
        "outputs/story.pdf",
        "outputs/story.docx",
        "outputs/story.pptx",
        "outputs/story.html",
        "outputs/img.png",
    }

def test_file_output_validation_accepts_arbitrary_field_names(tmp_path):
    from backend.routers.sandbox_chat import _output_files_from_stdout_json
    from backend.services.artifact_validator import validate_stdout_file_outputs

    skill_dir = tmp_path / "schema-skill"
    outputs = skill_dir / "outputs"
    outputs.mkdir(parents=True)
    report = outputs / "custom-report.pdf"
    report.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")

    stdout = json.dumps({"business_report": "outputs/custom-report.pdf"})

    assert validate_stdout_file_outputs(stdout, skill_dir=skill_dir, cwd=skill_dir / "scripts") == [
        {"path": "outputs/custom-report.pdf"}
    ]
    assert _output_files_from_stdout_json(stdout, cwd=skill_dir, skill_name="schema-skill") == [
        {"path": "outputs/custom-report.pdf", "url": "/api/skills/schema-skill/files/outputs/custom-report.pdf"}
    ]


def test_skill_dataflow_validates_stdout_fields_against_skill_placeholders():
    from backend.services.skill_dataflow import extract_skill_commands, validate_dataflow_closed

    skill_md = """---
name: flow-skill
description: demo
---
```bash
python scripts/write.py '{"topic":"{{topic}}"}'
```
```bash
python scripts/render.py '{"draft":"{{draft}}"}'
```
"""

    commands = extract_skill_commands(skill_md)
    final_context = validate_dataflow_closed(
        commands,
        initial_context={"topic": "cats"},
        stdout_by_script={"scripts/write.py": json.dumps({"draft": "a cat tale"})},
    )

    assert final_context["draft"] == "a cat tale"



def _write_workflow_skill(root: Path) -> tuple[Path, dict]:
    from backend.routers.sandbox_chat import _extract_skill_command_contract

    skill_dir = root / "workflow-skill"
    scripts = skill_dir / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "produce_batch.py").write_text(
        """
import json, sys
payload = json.loads(sys.argv[1])
items = [{"unit_text": f"{payload['seed']}-{i + 1}-{payload['label']}"} for i in range(int(payload["item_count"]))]
print(json.dumps({"batch": items, "document_text": payload["seed"]}, ensure_ascii=False))
""".strip(),
        encoding="utf-8",
    )
    (scripts / "render_unit.py").write_text(
        """
import hashlib, json, pathlib, sys
payload = json.loads(sys.argv[1])
name = hashlib.sha1(payload["unit_text"].encode()).hexdigest()[:8] + ".dat"
path = pathlib.Path("outputs") / name
path.parent.mkdir(exist_ok=True)
path.write_text(payload["unit_text"], encoding="utf-8")
print(json.dumps({"artifact_path": path.as_posix()}, ensure_ascii=False))
""".strip(),
        encoding="utf-8",
    )
    (scripts / "assemble_file.py").write_text(
        """
import json, pathlib, sys
payload = json.loads(sys.argv[1])
path = pathlib.Path("outputs/bundle.pdf")
path.parent.mkdir(exist_ok=True)
path.write_bytes(b"%PDF-1.4\\n%%EOF")
print(json.dumps({"final_file": path.as_posix(), "received_count": len(payload["artifact_paths"])}, ensure_ascii=False))
""".strip(),
        encoding="utf-8",
    )
    skill_md = """---
name: workflow-skill
description: generic dataflow demo
---
role: text_generator
inputs: seed=default-seed, item_count=2, label=default-label
outputs: document_text, batch
```bash
python scripts/produce_batch.py '{"seed":"{{seed}}","item_count":"{{item_count}}","label":"{{label}}"}'
```

role: text_generator
inputs: unit_text
outputs: artifact_path
```bash
python scripts/render_unit.py '{"unit_text":"{{unit_text}}"}'
```

role: pdf_builder
inputs: document_text, artifact_paths
outputs: final_file
required_capabilities: pdf_generation
```bash
python scripts/assemble_file.py '{"document_text":"{{document_text}}","artifact_paths":"{{artifact_paths}}"}'
```
"""
    (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    return skill_dir, _extract_skill_command_contract(skill_md, execution_root=skill_dir)


def test_multi_script_skill_forces_execute_workflow(tmp_path):
    from backend.routers.sandbox_chat import _normalize_skill_runtime_plan

    skill_dir, contract = _write_workflow_skill(tmp_path)

    result = _normalize_skill_runtime_plan(
        {"mode": "direct_answer", "actions": [], "errors": [], "missing": []},
        execution_root=skill_dir,
        available_scripts=["scripts/produce_batch.py", "scripts/render_unit.py", "scripts/assemble_file.py"],
        command_contract=contract,
        user_text="生成一个文件",
    )

    assert result["mode"] == "execute_workflow"
    assert [item["script_path"] for item in result["workflow_actions"]] == [
        "scripts/produce_batch.py",
        "scripts/render_unit.py",
        "scripts/assemble_file.py",
    ]
    assert result["final_instruction"] == ""


def test_render_command_template_uses_previous_stdout_context():
    from backend.routers.sandbox_chat import merge_step_output, render_command_template

    context = {"seed": "alpha"}
    merge_step_output(context, "scripts/produce_batch.py", {"document_text": "merged text"})

    rendered = render_command_template(
        "python scripts/assemble_file.py '{\"document_text\":\"{{document_text}}\"}'",
        context,
    )

    import shlex
    assert json.loads(shlex.split(rendered)[2])["document_text"] == "merged text"


def test_execute_skill_workflow_generic_foreach_collects_outputs_and_final_file(tmp_path):
    import asyncio
    import shlex
    from backend.routers.chat_models import ChatRequest, Message
    from backend.routers.sandbox_chat import _execute_skill_workflow

    skill_dir, contract = _write_workflow_skill(tmp_path)
    request_text = "seed: user-seed"
    result = asyncio.run(_execute_skill_workflow(
        execution_root=skill_dir,
        action_schema=contract["action_schema"],
        user_context={"user_request": request_text},
        request=ChatRequest(messages=[Message(role="user", content=request_text)]),
        skill_name="workflow-skill",
    ))

    first_payload = json.loads(shlex.split(result["results"][0]["command"])[2])
    assert first_payload == {"seed": "user-seed", "item_count": 2, "label": "default-label"}
    assert result["executed"] is True
    assert len([r for r in result["results"] if r["action"] == "run_command"]) == 4
    assert len(result["context"]["batch"]) == 2
    assert len(result["context"]["artifact_paths"]) == 2
    final_payload = json.loads(shlex.split(result["results"][-1]["command"])[2])
    assert final_payload["artifact_paths"] == result["context"]["artifact_paths"]
    assert any(item["path"] == "outputs/bundle.pdf" for item in result["output_files"])
    assert (skill_dir / "outputs" / "bundle.pdf").is_file()


def test_render_command_template_supports_dotted_context_in_plain_commands():
    from backend.routers.sandbox_chat import render_command_template

    rendered = render_command_template(
        "python scripts/plain.py --value={{loop_item.value}} --items={{collection}}",
        {"loop_item": {"value": "alpha"}, "collection": ["one", "two"]},
    )

    assert "--value=alpha" in rendered
    assert '--items=["one", "two"]' in rendered


def test_render_command_template_reports_dataflow_mismatch():
    from backend.routers.sandbox_chat import render_command_template

    with pytest.raises(ValueError, match="dataflow_mismatch"):
        render_command_template(
            "python scripts/assemble_file.py '{\"artifact_paths\":\"{{artifact_paths}}\"}'",
            {},
        )


def _write_initial_context_workflow_skill(root: Path) -> tuple[Path, dict]:
    from backend.routers.sandbox_chat import _extract_skill_command_contract

    skill_dir = root / "initial-context-skill"
    scripts = skill_dir / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "first.py").write_text(
        """
import json, sys
payload = json.loads(sys.argv[1])
print(json.dumps({"first_output": payload["required_value"], "default_seen": payload["optional_value"]}, ensure_ascii=False))
""".strip(),
        encoding="utf-8",
    )
    (scripts / "second.py").write_text(
        """
import json, pathlib, sys
payload = json.loads(sys.argv[1])
path = pathlib.Path("outputs/initial.pdf")
path.parent.mkdir(exist_ok=True)
path.write_bytes(b"%PDF-1.4\\n%%EOF")
print(json.dumps({"result_file": path.as_posix(), "used": payload["first_output"]}, ensure_ascii=False))
""".strip(),
        encoding="utf-8",
    )
    skill_md = """---
name: initial-context-skill
description: generic defaults demo
---
role: text_generator
inputs: required_value, optional_value=from-default
outputs: first_output, default_seen
```bash
python scripts/first.py '{"required_value":"{{required_value}}","optional_value":"{{optional_value}}"}'
```

role: pdf_builder
inputs: first_output
outputs: result_file
required_capabilities: pdf_generation
```bash
python scripts/second.py '{"first_output":"{{first_output}}"}'
```
"""
    (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    return skill_dir, _extract_skill_command_contract(skill_md, execution_root=skill_dir)


def test_execute_skill_workflow_injects_defaults_and_user_overrides(tmp_path):
    import asyncio
    import shlex
    from backend.routers.chat_models import ChatRequest, Message
    from backend.routers.sandbox_chat import _execute_skill_workflow

    skill_dir, contract = _write_initial_context_workflow_skill(tmp_path)
    request_text = "required_value: from-user, optional_value: from-user-too"

    result = asyncio.run(_execute_skill_workflow(
        execution_root=skill_dir,
        action_schema=contract["action_schema"],
        user_context={"user_request": request_text},
        request=ChatRequest(messages=[Message(role="user", content=request_text)]),
        skill_name="initial-context-skill",
    ))

    first_payload = json.loads(shlex.split(result["results"][0]["command"])[2])
    assert first_payload == {"required_value": "from-user", "optional_value": "from-user-too"}
    assert result["context"]["first_output"] == "from-user"
    assert any(item["path"] == "outputs/initial.pdf" for item in result["output_files"])


def test_execute_skill_workflow_reports_initial_input_parse_failure(tmp_path):
    import asyncio
    from backend.routers.chat_models import ChatRequest, Message
    from backend.routers.sandbox_chat import _execute_skill_workflow

    skill_dir, contract = _write_initial_context_workflow_skill(tmp_path)
    request_text = "请执行。"

    with pytest.raises(ValueError, match="初始输入解析失败"):
        asyncio.run(_execute_skill_workflow(
            execution_root=skill_dir,
            action_schema=contract["action_schema"],
            user_context={"user_request": request_text},
            request=ChatRequest(messages=[Message(role="user", content=request_text)]),
            skill_name="initial-context-skill",
        ))


def test_model_dataflow_plan_initial_context_overrides_defaults_without_business_fields(tmp_path):
    import asyncio
    import shlex
    from backend.routers.chat_models import ChatRequest, Message
    from backend.routers.sandbox_chat import _execute_skill_workflow, _validate_workflow_dataflow_plan

    skill_dir, contract = _write_initial_context_workflow_skill(tmp_path)
    dataflow_plan = {
        "initial_context": {"required_value": "planned-user", "optional_value": "planned-override"},
        "steps": [
            {
                "script_path": "scripts/first.py",
                "input_mapping": {
                    "required_value": "{{required_value}}",
                    "optional_value": "{{optional_value}}",
                },
                "loop": None,
                "output_policy": "merge_stdout",
            },
            {
                "script_path": "scripts/second.py",
                "input_mapping": {"first_output": "{{first_output}}"},
                "loop": None,
                "output_policy": "merge_stdout",
            },
        ],
        "collections": [],
        "missing": [],
        "errors": [],
    }

    assert _validate_workflow_dataflow_plan(dataflow_plan, contract["action_schema"])["initial_context"]["required_value"] == "planned-user"
    result = asyncio.run(_execute_skill_workflow(
        execution_root=skill_dir,
        action_schema=contract["action_schema"],
        user_context={"user_request": "required_value: ignored-by-plan"},
        request=ChatRequest(messages=[Message(role="user", content="required_value: ignored-by-plan")]),
        skill_name="initial-context-skill",
        dataflow_plan=dataflow_plan,
    ))

    first_payload = json.loads(shlex.split(result["results"][0]["command"])[2])
    assert first_payload == {"required_value": "planned-user", "optional_value": "planned-override"}
    assert result["context"]["used"] == "planned-user"
    assert (skill_dir / "outputs" / "initial.pdf").is_file()


def test_model_dataflow_plan_drives_loop_and_aggregates_outputs(tmp_path):
    import asyncio
    import shlex
    from backend.routers.chat_models import ChatRequest, Message
    from backend.routers.sandbox_chat import _execute_skill_workflow

    skill_dir, contract = _write_workflow_skill(tmp_path)
    dataflow_plan = {
        "initial_context": {"seed": "planned", "item_count": 2, "label": "loop"},
        "steps": [
            {
                "script_path": "scripts/produce_batch.py",
                "input_mapping": {"seed": "{{seed}}", "item_count": "{{item_count}}", "label": "{{label}}"},
                "loop": None,
                "output_policy": "merge_stdout",
            },
            {
                "script_path": "scripts/render_unit.py",
                "input_mapping": {"unit_text": "{{loop_item.unit_text}}"},
                "loop": {"collection": "batch", "item_name": "loop_item"},
                "output_policy": "merge_stdout",
            },
            {
                "script_path": "scripts/assemble_file.py",
                "input_mapping": {"document_text": "{{document_text}}", "artifact_paths": "{{artifact_paths}}"},
                "loop": None,
                "output_policy": "merge_stdout",
            },
        ],
        "collections": [{"target": "artifact_paths", "source": "artifact_path"}],
        "missing": [],
        "errors": [],
    }

    result = asyncio.run(_execute_skill_workflow(
        execution_root=skill_dir,
        action_schema=contract["action_schema"],
        user_context={"user_request": "do it"},
        request=ChatRequest(messages=[Message(role="user", content="do it")]),
        skill_name="workflow-skill",
        dataflow_plan=dataflow_plan,
    ))

    assert len([r for r in result["results"] if r["action"] == "run_command"]) == 4
    final_payload = json.loads(shlex.split(result["results"][-1]["command"])[2])
    assert len(final_payload["artifact_paths"]) == 2
    assert result["context"]["received_count"] == 2
    assert any(item["path"] == "outputs/bundle.pdf" for item in result["output_files"])


def test_model_dataflow_plan_missing_mapping_is_rejected_before_execution(tmp_path):
    from backend.routers.sandbox_chat import _validate_workflow_dataflow_plan

    skill_dir, contract = _write_initial_context_workflow_skill(tmp_path)
    assert skill_dir.is_dir()
    bad_plan = {
        "initial_context": {"required_value": "x"},
        "steps": [
            {
                "script_path": "scripts/first.py",
                "input_mapping": {"required_value": "{{required_value}}"},
                "loop": None,
                "output_policy": "merge_stdout",
            },
            {
                "script_path": "scripts/second.py",
                "input_mapping": {"first_output": "{{first_output}}"},
                "loop": None,
                "output_policy": "merge_stdout",
            },
        ],
        "collections": [],
        "missing": [],
        "errors": [],
    }

    with pytest.raises(ValueError, match="input_mapping"):
        _validate_workflow_dataflow_plan(bad_plan, contract["action_schema"])


def test_skill_dataflow_aligns_generic_stdout_output_aliases():
    from backend.services.skill_dataflow import validate_and_align_step_stdout

    aligned = validate_and_align_step_stdout(
        {"outputs": ["artifact_paths"]},
        {"script_path": "scripts/render.py", "input_mapping": {}, "loop": None},
        {"artifact_path": "outputs/one.dat"},
    )

    assert aligned["artifact_paths"] == ["outputs/one.dat"]


def test_skill_dataflow_reports_missing_expected_stdout_outputs():
    from backend.services.skill_dataflow import DataflowError, validate_and_align_step_stdout

    with pytest.raises(DataflowError, match="missing_outputs"):
        validate_and_align_step_stdout(
            {"outputs": ["final_file"]},
            {"script_path": "scripts/build.py", "input_mapping": {}, "loop": None},
            {"unexpected": "outputs/final.pdf"},
        )


def test_skill_dataflow_loop_collection_accepts_source_object_and_dotted_paths():
    from backend.services.skill_dataflow import materialize_step_contexts_from_plan

    contexts = materialize_step_contexts_from_plan(
        {
            "script_path": "scripts/render.py",
            "input_mapping": {"value": "{{loop_item.value}}"},
            "loop": {"collection": {"path": "groups.0.items"}, "item_name": "loop_item"},
        },
        {"placeholder_keys": ["value"]},
        {"groups": [{"items": [{"value": "alpha"}, {"value": "beta"}]}]},
    )

    assert [context["value"] for context in contexts] == ["alpha", "beta"]


def test_skill_dataflow_collections_support_dotted_sources_and_step_scope():
    from backend.services.skill_dataflow import apply_dataflow_collections

    context = {}
    updates = apply_dataflow_collections(
        [
            {"target": "ignored", "source": "meta.path", "script_path": "scripts/other.py"},
            {"target": "paths", "source": "meta.path", "script_path": "scripts/render.py"},
        ],
        context,
        [{"meta": {"path": "outputs/one.dat"}}, {"meta": {"path": "outputs/two.dat"}}],
        script_path="scripts/render.py",
        step_index=1,
    )

    assert updates == {"paths": ["outputs/one.dat", "outputs/two.dat"]}
    assert context == updates
