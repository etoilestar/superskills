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


def test_runtime_stdout_validation_is_role_aware():
    from backend.routers.sandbox_chat import _validate_stdout_against_action_entry

    with pytest.raises(ValueError, match="非空 text"):
        _validate_stdout_against_action_entry(json.dumps({"file_paths": ["x.pdf"]}), {"role": "text_generator"})

    _validate_stdout_against_action_entry(json.dumps({"text": "done"}), {"role": "text_generator"})


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


def test_composite_generator_stdout_validates_required_capability_outputs():
    from backend.routers.sandbox_chat import _validate_stdout_against_action_entry

    entry = {
        "role": "composite_generator",
        "required_capabilities": ["text_generation", "image_generation"],
        "outputs": ["text", "image_paths"],
    }

    with pytest.raises(ValueError, match="image_path"):
        _validate_stdout_against_action_entry(json.dumps({"text": "story"}), entry)

    _validate_stdout_against_action_entry(json.dumps({"text": "story", "image_paths": ["assets/generated/a.png"]}), entry)


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
out = Path('assets/generated/story.png')
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
    assert json.loads(run_result["stdout"])["image_paths"] == ["assets/generated/story.png"]
    assert run_result["output_files"] == [
        {
            "path": "assets/generated/story.png",
            "url": "/api/skills/story-skill/files/assets/generated/story.png",
        }
    ]

    exec_result = {"results": [read_result, run_result], "output_files": run_result["output_files"]}
    answer = _render_success_stdout_payload(exec_result)
    answer = _finalize_answer_output_file_links(answer, exec_result["output_files"])

    assert "Story about 狐狸" in answer
    assert "/api/skills/story-skill/files/assets/generated/story.png" in answer


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
        json.dumps({"docx_path": "assets/generated/story.docx"}),
        {"role": "docx_builder", "outputs": ["docx_path"]},
    )
    _validate_stdout_against_action_entry(
        json.dumps({"pptx_path": "assets/generated/story.pptx"}),
        {"role": "pptx_builder", "outputs": ["pptx_path"]},
    )


def test_output_files_from_stdout_json_collects_all_artifact_fields(tmp_path):
    from backend.routers.sandbox_chat import _output_files_from_stdout_json

    skill_dir = tmp_path / "schema-skill"
    generated = skill_dir / "assets" / "generated"
    outputs = skill_dir / "outputs"
    generated.mkdir(parents=True)
    outputs.mkdir(parents=True)
    for rel in ("assets/generated/story.pdf", "assets/generated/story.docx", "assets/generated/story.pptx", "assets/generated/story.html", "outputs/img.png"):
        path = skill_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data")

    files = _output_files_from_stdout_json(
        json.dumps({
            "pdf_path": "assets/generated/story.pdf",
            "docx_path": "assets/generated/story.docx",
            "pptx_path": "assets/generated/story.pptx",
            "html_path": "assets/generated/story.html",
            "image_paths": ["outputs/img.png"],
        }),
        cwd=skill_dir,
        skill_name="schema-skill",
    )

    assert {item["path"] for item in files} == {
        "assets/generated/story.pdf",
        "assets/generated/story.docx",
        "assets/generated/story.pptx",
        "assets/generated/story.html",
        "outputs/img.png",
    }
