"""Tests for pure helper/parser functions in backend/routers/chat_utils.py.

These functions do not require a running LLM or file system access.
Also includes tests for _is_within_sandbox, a security-critical guard that
rejects symlinks escaping the skill execution sandbox.
"""

import json
import pytest

# ---------------------------------------------------------------------------
# _strip_markdown_json_fence
# ---------------------------------------------------------------------------

def test_strip_plain_json():
    from backend.routers.chat_utils import _strip_markdown_json_fence

    assert _strip_markdown_json_fence('{"a": 1}') == '{"a": 1}'


def test_strip_json_fence():
    from backend.routers.chat_utils import _strip_markdown_json_fence

    text = '```json\n{"a": 1}\n```'
    assert _strip_markdown_json_fence(text) == '{"a": 1}'


def test_strip_bare_fence_with_json():
    from backend.routers.chat_utils import _strip_markdown_json_fence

    text = '```\n{"a": 1}\n```'
    assert _strip_markdown_json_fence(text).startswith("{")


def test_strip_embedded_json_block():
    from backend.routers.chat_utils import _strip_markdown_json_fence

    text = "Here is the result:\n```json\n{\"ok\": true}\n```\nDone."
    result = _strip_markdown_json_fence(text)
    assert result.startswith("{")
    assert '"ok"' in result


def test_strip_fallback_finds_json_in_prose():
    from backend.routers.chat_utils import _strip_markdown_json_fence

    text = 'Sure! Here is the JSON: {"result": 42} — done.'
    result = _strip_markdown_json_fence(text)
    assert '{"result": 42}' in result


# ---------------------------------------------------------------------------
# _parse_need_body_decision
# ---------------------------------------------------------------------------

def test_parse_need_body_true():
    from backend.routers.sandbox_chat import _parse_need_body_decision

    assert _parse_need_body_decision('{"need_body": true}') is True


def test_parse_need_body_false():
    from backend.routers.sandbox_chat import _parse_need_body_decision

    assert _parse_need_body_decision('{"need_body": false}') is False


def test_parse_need_body_string_true():
    from backend.routers.sandbox_chat import _parse_need_body_decision

    assert _parse_need_body_decision('{"need_body": "true"}') is True


def test_parse_need_body_invalid_json_defaults_true():
    """Invalid JSON should default to True (safe: load body anyway)."""
    from backend.routers.sandbox_chat import _parse_need_body_decision

    assert _parse_need_body_decision("not json at all") is True


def test_parse_need_body_missing_key_defaults_true():
    from backend.routers.sandbox_chat import _parse_need_body_decision

    assert _parse_need_body_decision('{"reason": "irrelevant"}') is True


# ---------------------------------------------------------------------------
# _parse_child_skill_decision
# ---------------------------------------------------------------------------

def test_parse_child_skill_need_child_true():
    from backend.routers.sandbox_chat import _parse_child_skill_decision

    text = '{"need_child": true, "child_ref": "skills/child-a", "reason": "matched"}'
    result = _parse_child_skill_decision(
        text, valid_child_refs={"skills/child-a", "skills/child-b"}
    )
    assert result["need_child"] is True
    assert result["child_ref"] == "skills/child-a"


def test_parse_child_skill_invalid_ref_rejected():
    from backend.routers.sandbox_chat import _parse_child_skill_decision

    text = '{"need_child": true, "child_ref": "skills/hacked", "reason": ""}'
    result = _parse_child_skill_decision(
        text, valid_child_refs={"skills/real-child"}
    )
    assert result["need_child"] is False


def test_parse_child_skill_no_need_child():
    from backend.routers.sandbox_chat import _parse_child_skill_decision

    text = '{"need_child": false, "reason": "not matched"}'
    result = _parse_child_skill_decision(text, valid_child_refs={"skills/x"})
    assert result["need_child"] is False
    assert result["child_ref"] == ""


def test_parse_child_skill_invalid_json():
    from backend.routers.sandbox_chat import _parse_child_skill_decision

    result = _parse_child_skill_decision("NOT JSON", valid_child_refs=set())
    assert result["need_child"] is False


def test_parse_child_skill_missing_child_ref():
    from backend.routers.sandbox_chat import _parse_child_skill_decision

    text = '{"need_child": true, "reason": "match"}'
    result = _parse_child_skill_decision(text, valid_child_refs={"skills/x"})
    assert result["need_child"] is False
    assert "child_ref" in result["reason"]


# ---------------------------------------------------------------------------
# _parse_resource_selection_decision
# ---------------------------------------------------------------------------

def _make_catalog(n: int = 3) -> list[dict]:
    return [
        {
            "resource_handle": f"resource:{i}",
            "path": f"references/ref{i}.md",
            "kind": "reference",
            "title": f"Ref {i}",
            "allowed_actions": ["read_resource"],
        }
        for i in range(n)
    ]


def test_parse_resource_selection_picks_valid_handles():
    from backend.routers.sandbox_chat import _parse_resource_selection_decision

    catalog = _make_catalog(3)
    text = '{"need_resources": true, "resource_handles": ["resource:0", "resource:2"], "reason": "need"}'
    result = _parse_resource_selection_decision(text, resource_catalog=catalog)

    assert result["need_resources"] is True
    assert "resource:0" in result["resource_handles"]
    assert "resource:2" in result["resource_handles"]


def test_parse_resource_selection_ignores_invalid_handles():
    from backend.routers.sandbox_chat import _parse_resource_selection_decision

    catalog = _make_catalog(2)
    text = '{"need_resources": true, "resource_handles": ["resource:99"], "reason": "oops"}'
    result = _parse_resource_selection_decision(text, resource_catalog=catalog)

    # resource:99 is not in catalog — selection should be empty, need_resources → False
    assert result["need_resources"] is False
    assert result["resource_handles"] == []


def test_parse_resource_selection_caps_at_five():
    from backend.routers.sandbox_chat import _parse_resource_selection_decision

    catalog = _make_catalog(10)
    handles = [f"resource:{i}" for i in range(10)]
    text = json.dumps({"need_resources": True, "resource_handles": handles, "reason": "all"})
    result = _parse_resource_selection_decision(text, resource_catalog=catalog)

    assert len(result["resource_handles"]) <= 5


def test_parse_resource_selection_invalid_json():
    from backend.routers.sandbox_chat import _parse_resource_selection_decision

    result = _parse_resource_selection_decision("bad json", resource_catalog=[])
    assert result["need_resources"] is False


# ---------------------------------------------------------------------------
# creator_chat resource preload parser
# ---------------------------------------------------------------------------

def test_extract_creator_resource_catalog_from_prompt():
    from backend.routers.creator_chat import _extract_creator_resource_catalog

    text = (
        "参考 `references/spec.md`：规范\n"
        "模板 `assets/template.json`\n"
        "脚本 `scripts/build.py`\n"
    )
    result = _extract_creator_resource_catalog(text)

    assert len(result) == 3
    assert [item["resource_handle"] for item in result] == ["resource:0", "resource:1", "resource:2"]
    assert result[0]["path"] == "references/spec.md"
    assert result[1]["path"] == "assets/template.json"
    assert result[2]["path"] == "scripts/build.py"


def test_parse_creator_resource_selection_accepts_plain_text():
    from backend.routers.creator_chat import _parse_creator_resource_selection_decision

    catalog = _make_catalog(3)
    text = "建议先读取 resource:1 和 resource:2，再继续。"
    result = _parse_creator_resource_selection_decision(text, resource_catalog=catalog)

    assert result["need_resources"] is True
    assert result["resource_handles"] == ["resource:1", "resource:2"]


def test_parse_creator_resource_selection_accepts_json():
    from backend.routers.creator_chat import _parse_creator_resource_selection_decision

    catalog = _make_catalog(3)
    text = '{"need_resources": true, "resource_handles": ["resource:0"], "reason": "需要"}'
    result = _parse_creator_resource_selection_decision(text, resource_catalog=catalog)

    assert result["need_resources"] is True
    assert result["resource_handles"] == ["resource:0"]


# ---------------------------------------------------------------------------
# _planner_model_name
# ---------------------------------------------------------------------------

def test_planner_model_name_falls_back_to_default():
    from backend.routers.chat_utils import _planner_model_name
    from backend.config import settings
    from unittest.mock import patch

    with patch.object(settings, "planner_model", None):
        assert _planner_model_name("my-default") == "my-default"


def test_planner_model_name_uses_configured():
    from backend.routers.chat_utils import _planner_model_name
    from backend.config import settings
    from unittest.mock import patch

    with patch.object(settings, "planner_model", "fast-model"):
        assert _planner_model_name("other") == "fast-model"


# ---------------------------------------------------------------------------
# _normalize_skill_runtime_plan
# ---------------------------------------------------------------------------

def test_normalize_plan_raises_on_non_dict():
    from backend.routers.sandbox_chat import _normalize_skill_runtime_plan

    with pytest.raises(ValueError):
        _normalize_skill_runtime_plan("not a dict")


def test_normalize_plan_direct_answer_mode():
    from backend.routers.sandbox_chat import _normalize_skill_runtime_plan

    plan = {"mode": "direct_answer", "actions": [], "errors": [], "missing": []}
    result = _normalize_skill_runtime_plan(plan)
    assert result["mode"] == "direct_answer"
    assert result["tasks"] == []


def test_normalize_plan_unknown_mode_becomes_ask_user():
    from backend.routers.sandbox_chat import _normalize_skill_runtime_plan

    plan = {"mode": "invalid_mode", "actions": [], "errors": [], "missing": []}
    result = _normalize_skill_runtime_plan(plan)
    assert result["mode"] == "ask_user"


def test_normalize_plan_read_resource_without_handle():
    from backend.routers.sandbox_chat import _normalize_skill_runtime_plan

    plan = {
        "mode": "execute",
        "actions": [{"action": "read_resource"}],
        "errors": [],
        "missing": [],
    }
    result = _normalize_skill_runtime_plan(plan, resource_catalog=[])
    # The action should be rejected and moved to errors
    assert any("read_resource" in str(e) for e in result["errors"])
    assert result["tasks"] == []


def test_normalize_plan_execute_with_all_actions_rejected_becomes_ask_user():
    from backend.routers.sandbox_chat import _normalize_skill_runtime_plan

    plan = {
        "mode": "execute",
        "actions": [
            {"action": "run_command", "command": "nonexistent_binary_xyz arg1"},
        ],
        "errors": [],
        "missing": [],
    }
    result = _normalize_skill_runtime_plan(plan, resource_catalog=[])
    # run_command precheck will fail (binary doesn't exist), errors populated
    assert result["mode"] == "ask_user"


# ---------------------------------------------------------------------------
# _validate_skill_md
# ---------------------------------------------------------------------------

def test_validate_skill_md_valid(tmp_path):
    from backend.routers.chat_utils import _validate_skill_md

    md = tmp_path / "SKILL.md"
    md.write_text("---\nname: my-skill\ndescription: test\n---\n# Body\n")
    _validate_skill_md(md)  # should not raise


def test_validate_skill_md_missing_file(tmp_path):
    from backend.routers.chat_utils import _validate_skill_md

    with pytest.raises(ValueError, match="SKILL.md"):
        _validate_skill_md(tmp_path / "SKILL.md")


def test_validate_skill_md_no_frontmatter(tmp_path):
    from backend.routers.chat_utils import _validate_skill_md

    md = tmp_path / "SKILL.md"
    md.write_text("# No frontmatter here\n")
    with pytest.raises(ValueError, match="frontmatter"):
        _validate_skill_md(md)


def test_validate_skill_md_missing_name(tmp_path):
    from backend.routers.chat_utils import _validate_skill_md

    md = tmp_path / "SKILL.md"
    md.write_text("---\ndescription: no name field\n---\n# Body\n")
    with pytest.raises(ValueError, match="name"):
        _validate_skill_md(md)


def test_validate_skill_md_name_too_long(tmp_path):
    from backend.routers.chat_utils import _validate_skill_md

    md = tmp_path / "SKILL.md"
    long_name = "a" * 65
    md.write_text(f"---\nname: {long_name}\ndescription: ok\n---\n# Body\n")
    with pytest.raises(ValueError, match="64"):
        _validate_skill_md(md)


# ---------------------------------------------------------------------------
# _extract_all_fenced_blocks
# ---------------------------------------------------------------------------

def test_extract_fenced_blocks_basic():
    from backend.routers.chat_utils import _extract_all_fenced_blocks

    text = "Before\n```python\nprint('hello')\n```\nAfter"
    blocks = _extract_all_fenced_blocks(text)

    assert len(blocks) == 1
    assert blocks[0].lang == "python"
    assert "print" in blocks[0].code


def test_extract_multiple_blocks():
    from backend.routers.chat_utils import _extract_all_fenced_blocks

    text = "```bash\necho hi\n```\n\n```python\nprint('world')\n```"
    blocks = _extract_all_fenced_blocks(text)

    assert len(blocks) == 2
    assert blocks[0].lang == "bash"
    assert blocks[1].lang == "python"


def test_extract_unclosed_block_ignored():
    from backend.routers.chat_utils import _extract_all_fenced_blocks

    text = "```python\nprint('oops')"
    blocks = _extract_all_fenced_blocks(text)

    assert blocks == []


def test_extract_blocks_no_blocks():
    from backend.routers.chat_utils import _extract_all_fenced_blocks

    blocks = _extract_all_fenced_blocks("Just plain text.")
    assert blocks == []


# ---------------------------------------------------------------------------
# _is_within_sandbox — symlink escape guard
# ---------------------------------------------------------------------------

def test_is_within_sandbox_regular_file(tmp_path):
    from backend.routers.sandbox_chat import _is_within_sandbox

    file = tmp_path / "scripts" / "run.py"
    file.parent.mkdir()
    file.write_text("pass")

    assert _is_within_sandbox(file, tmp_path.resolve()) is True


def test_is_within_sandbox_escape_rejected(tmp_path):
    from backend.routers.sandbox_chat import _is_within_sandbox

    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    scripts = tmp_path / "skill" / "scripts"
    scripts.mkdir(parents=True)
    link = scripts / "evil.py"
    link.symlink_to(outside)

    sandbox = (tmp_path / "skill").resolve()
    assert _is_within_sandbox(link, sandbox) is False


def test_is_within_sandbox_nested_path_ok(tmp_path):
    from backend.routers.sandbox_chat import _is_within_sandbox

    nested = tmp_path / "skill" / "scripts" / "sub" / "run.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("pass")

    sandbox = (tmp_path / "skill").resolve()
    assert _is_within_sandbox(nested, sandbox) is True


def test_run_command_injects_configured_model_environment(tmp_path, monkeypatch):
    from backend.config import settings
    from backend.routers.chat_models import ChatRequest
    from backend.routers.sandbox_chat import _execute_single_task

    monkeypatch.setattr(settings, "text_model", "text-env-model")
    monkeypatch.setattr(settings, "image_model", "image-env-model")
    monkeypatch.setattr(settings, "vision_model", "vision-env-model")

    request = ChatRequest(messages=[])
    result, _ = _execute_single_task(
        {
            "action": "run_command",
            "command": "python -c \"import os; print(os.environ['TEXT_MODEL'] + '|' + os.environ['IMAGE_MODEL'] + '|' + os.environ['VISION_MODEL'])\"",
        },
        [],
        request,
        execution_root=tmp_path,
    )

    assert result["success"] is True
    assert "text-env-model|image-env-model|vision-env-model" in result["stdout"]


def test_run_command_injects_default_model_api_keys(tmp_path, monkeypatch):
    from backend.config import settings
    from backend.routers.chat_models import ChatRequest
    from backend.routers.sandbox_chat import _execute_single_task

    monkeypatch.setattr(settings, "llm_api_key", None)
    monkeypatch.setattr(settings, "openai_api_key", None)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    request = ChatRequest(messages=[])
    result, _ = _execute_single_task(
        {
            "action": "run_command",
            "command": "python -c \"import os; print(os.environ['LLM_API_KEY'] + '|' + os.environ['OPENAI_API_KEY'])\"",
        },
        [],
        request,
        execution_root=tmp_path,
    )

    assert result["success"] is True
    assert "ollama|ollama" in result["stdout"]


def test_creator_phase2_prompt_requires_blueprint_before_confirmation():
    from backend.services.kernel_loader import load_kernel_creator_for_phase

    prompt = load_kernel_creator_for_phase("phase2")

    assert "必须先输出完整蓝图正文" in prompt
    assert "不要只输出确认问题" in prompt
    assert "Phase 2 期间禁止输出 phase3_start" in prompt
    assert "\"对，开始做吧\"" in prompt


def test_creator_phase_refinement_revision_hint_overrides_confirmation():
    import asyncio
    from backend.routers.creator_chat import _refine_creator_phase_with_model

    messages = [
        {"role": "assistant", "content": "## 📋 Skill 架构蓝图\n### 资源清单\n- 图片API密钥\n- 关键词数据库"},
        {"role": "user", "content": "确认，继续构建"},
        {"role": "user", "content": "我的模型不需要api密钥，直接使用内置的多模态模型就行，关键词也不需要数据库"},
    ]

    result = asyncio.run(_refine_creator_phase_with_model(messages, "phase3+", "general-model"))

    assert result["phase"] == "phase2"
    assert result["used_model"] is False


def test_creator_phase_refinement_uses_model_for_ambiguous_revision(monkeypatch):
    import asyncio
    from backend.routers.creator_chat import _refine_creator_phase_with_model

    async def fake_complete_chat_once(messages, model):
        return '{"phase":"phase2","reason":"用户在调整蓝图约束"}'

    monkeypatch.setattr("backend.routers.creator_chat.complete_chat_once", fake_complete_chat_once)

    messages = [
        {"role": "assistant", "content": "## 📋 Skill 架构蓝图\n### 资源清单\n- 图片API密钥"},
        {"role": "user", "content": "确认，继续构建"},
        {"role": "user", "content": "资源部分按我刚才说的方案处理"},
    ]

    result = asyncio.run(_refine_creator_phase_with_model(messages, "phase3+", "general-model"))

    assert result["phase"] == "phase2"
    assert result["used_model"] is True


def test_creator_phase_guess_accepts_continue_build_confirmation():
    from backend.routers.creator_chat import _guess_current_phase

    messages = [
        {"role": "assistant", "content": "## 📋 Skill 架构蓝图\n### 基本信息\n- **Skill 名称**: demo"},
        {"role": "user", "content": "确认，继续构建"},
    ]

    assert _guess_current_phase(messages) == "phase3+"


def test_strip_phase3_marker_from_visible_creator_text():
    from backend.routers.creator_chat import _strip_phase3_marker_from_visible_text

    text = "蓝图内容\n{\"creator_phase\":\"phase3_start\"}\n后续文字"

    assert _strip_phase3_marker_from_visible_text(text) == "蓝图内容\n后续文字"


def test_request_messages_with_inline_images_builds_data_url(tmp_path):
    from backend.routers.chat_models import ChatRequest, Message
    from backend.routers.sandbox_chat import _request_messages_with_inline_images

    image = tmp_path / "inputs" / "s1" / "photo.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"fakepng")
    request = ChatRequest(
        messages=[Message(role="user", content="分析图片")],
        input_files=[{"path": "inputs/s1/photo.png", "filename": "photo.png"}],
    )

    messages = _request_messages_with_inline_images(request, tmp_path)

    assert isinstance(messages[-1]["content"], list)
    assert messages[-1]["content"][0]["type"] == "text"
    assert messages[-1]["content"][1]["type"] == "image_url"
    assert messages[-1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_render_success_stdout_payload_extracts_story_json():
    from backend.routers.sandbox_chat import _render_success_stdout_payload

    rendered = _render_success_stdout_payload({
        "results": [{
            "success": True,
            "stdout": json.dumps({"text": "# 小猪冒险\n\n故事正文", "image": "generated_image.png"}, ensure_ascii=False),
        }]
    })

    assert "# 小猪冒险" in rendered
    assert "![插图](generated_image.png)" in rendered


def test_finalize_answer_rewrites_generated_image_to_download_url():
    from backend.routers.sandbox_chat import _finalize_answer_output_file_links

    answer = "# 科普故事\n\n正文\n\n![插图](generated-image.png)"
    rendered = _finalize_answer_output_file_links(
        answer,
        [{"path": "outputs/generated-image.png", "url": "/api/skills/story/files/outputs/generated-image.png"}],
    )

    assert "![插图](/api/skills/story/files/outputs/generated-image.png)" in rendered
    assert "![插图](generated-image.png)" not in rendered


def test_finalize_answer_does_not_append_omitted_generated_image_url():
    from backend.routers.sandbox_chat import _finalize_answer_output_file_links

    answer = "# 科普故事\n\n正文"
    rendered = _finalize_answer_output_file_links(
        answer,
        [{"path": "generated-image.png", "url": "/api/skills/story/files/generated-image.png"}],
    )

    assert rendered == answer
    assert "generated-image.png" not in rendered


def test_runtime_planner_prompt_requires_fenced_block_trigger():
    from backend.routers.sandbox_chat import _compose_skill_runtime_planner_prompt

    prompt = _compose_skill_runtime_planner_prompt()

    assert "显式可执行 fenced code block 触发" in prompt
    assert "不要因为磁盘上存在脚本就直接规划 run_command" in prompt
    assert "禁止的 action：run_command、write_file、create_directory" in prompt



def test_normalize_plan_rejects_direct_run_command_trigger():
    from backend.routers.sandbox_chat import _normalize_skill_runtime_plan

    plan = {
        "mode": "execute",
        "actions": [{"action": "run_command", "command": "python scripts/build.py"}],
        "errors": [],
        "missing": [],
    }

    result = _normalize_skill_runtime_plan(plan)

    assert result["mode"] == "ask_user"
    assert result["tasks"] == []
    assert any("显式 fenced code block" in str(error) for error in result["errors"])


def test_extract_skill_command_contract_requires_shell_fenced_template():
    from backend.routers.sandbox_chat import _extract_skill_command_contract

    implicit_skill = "立即调用 `scripts/generate_chord.py` 生成结果。"
    explicit_skill = """执行命令：
```bash
python scripts/generate_chord.py '{"style":"{{style}}","key":"{{key}}"}'
```
"""

    assert not _extract_skill_command_contract(implicit_skill)["has_executable_command_block"]

    explicit_contract = _extract_skill_command_contract(explicit_skill)
    assert explicit_contract["has_executable_command_block"]
    assert "scripts/generate_chord.py" in explicit_contract["command_blocks"][0]["code"]


def test_normalize_plan_rejects_generated_command_without_skill_template():
    from backend.routers.sandbox_chat import (
        _extract_skill_command_contract,
        _normalize_skill_runtime_plan,
    )

    command_contract = _extract_skill_command_contract(
        "当用户提供风格时，调用 `scripts/generate_chord.py` 生成结果。"
    )
    plan = {
        "mode": "direct_answer",
        "actions": [],
        "errors": [],
        "missing": [],
        "final_instruction": "输出 fenced code block 调用 scripts/generate_chord.py。",
    }

    result = _normalize_skill_runtime_plan(plan, command_contract=command_contract)

    assert result["mode"] == "ask_user"
    assert any("缺少可执行命令 fenced block 示例" in str(error) for error in result["errors"])


def test_normalize_plan_allows_generated_command_with_skill_template():
    from backend.routers.sandbox_chat import (
        _extract_skill_command_contract,
        _normalize_skill_runtime_plan,
    )

    command_contract = _extract_skill_command_contract(
        """执行命令：
```bash
python scripts/generate_chord.py '{"style":"{{style}}","key":"{{key}}"}'
```
"""
    )
    plan = {
        "mode": "direct_answer",
        "actions": [],
        "errors": [],
        "missing": [],
        "final_instruction": "按 SKILL.md 中已有命令模板替换参数后输出 fenced code block。",
    }

    result = _normalize_skill_runtime_plan(plan, command_contract=command_contract)

    assert result["mode"] == "direct_answer"
    assert result["errors"] == []


def test_creator_sanitizes_script_from_multifile_bundle():
    from backend.routers.creator import _sanitize_generated_file_content

    bundle = """# fairy-tale-generator

## 📜 SKILL.md

```markdown
# 童话故事生成器
```

## 📁 scripts/generate_story.py

```python
import argparse

def main():
    print("ok")

if __name__ == "__main__":
    main()
```

## 📁 references/config.md

```markdown
# config
```
"""

    result = _sanitize_generated_file_content("scripts/generate_story.py", bundle)

    assert result.startswith("import argparse")
    assert "## 📜 SKILL.md" not in result
    assert "```" not in result


def test_creator_rejects_markdown_bundle_for_script_without_target_section():
    from backend.routers.creator import _sanitize_generated_file_content

    bundle = """# fairy-tale-generator

## 📜 SKILL.md

```markdown
# 童话故事生成器
```

## 📁 scripts/other.py

```python
print("wrong target")
```
"""

    with pytest.raises(ValueError, match="不是单个脚本源码"):
        _sanitize_generated_file_content("scripts/generate_story.py", bundle)


def test_creator_rejects_script_that_ignores_json_argv_contract():
    from backend.routers.creator import _validate_script_contract_static

    skill_md = """执行命令：
```bash
python scripts/process_params.py '{"theme":"{{theme}}","character":"{{character}}"}'
```
"""
    script = """import json

def main():
    print(json.dumps({"text": "固定输出"}))
"""

    with pytest.raises(ValueError, match="json.loads"):
        _validate_script_contract_static(
            file_path="scripts/process_params.py",
            content=script,
            skill_md=skill_md,
        )


def test_creator_accepts_script_that_reads_contract_placeholders():
    from backend.routers.creator import _validate_script_contract_static

    skill_md = """执行命令：
```bash
python scripts/process_params.py '{"theme":"{{theme}}","character":"{{character}}"}'
```
"""
    script = """import json
import sys

def main():
    payload = json.loads(sys.argv[1])
    theme = payload.get("theme")
    character = payload.get("character")
    print(json.dumps({"text": f"{character}:{theme}"}, ensure_ascii=False))
"""

    _validate_script_contract_static(
        file_path="scripts/process_params.py",
        content=script,
        skill_md=skill_md,
    )


def test_creator_generate_skill_md_prompt_uses_standard_markdown_execution_guidance():
    from backend.routers.creator import _build_generate_file_prompt

    messages = _build_generate_file_prompt(
        file_path="SKILL.md",
        skill_name="demo-skill",
        purpose="创建主 Skill 文档",
        blueprint_text="## 📋 Skill 架构蓝图\n### 宿主执行方式\n- 需要脚本/命令",
        conversation_history=[],
    )
    prompt = messages[0]["content"]

    assert "宿主 Markdown 执行说明" in prompt
    assert "普通 Markdown 说明书" in prompt
    assert "只有 assistant 在 Sandbox 当轮回复中输出的 fenced code block" in prompt
    assert "禁止在 SKILL.md 中只写“立即调用 `scripts/...`”" in prompt
    assert "不要引入自定义协议章节" in prompt
    assert "宿主已配置的模型能力" in prompt
    assert "可按需读取" in prompt
    assert "不需要额外校验" in prompt


def test_creator_rejects_custom_runtime_contract_section():
    from backend.routers.creator import _sanitize_generated_file_content

    skill_md = """---
name: demo
description: demo
---

### Runtime Contract
```json
{}
```
"""

    with pytest.raises(ValueError, match="Runtime Contract"):
        _sanitize_generated_file_content("SKILL.md", skill_md)


def test_creator_rejects_placeholder_image_script():
    from backend.routers.creator import _sanitize_generated_file_content

    script = """import os

def main():
    os.makedirs('generated_images', exist_ok=True)
    with open('generated_images/demo.png', 'w') as f:
        f.write('placeholder for image')

if __name__ == '__main__':
    main()
"""

    with pytest.raises(ValueError, match="占位|placeholder"):
        _sanitize_generated_file_content("scripts/generate_image.py", script)


def test_creator_rejects_model_declared_script_without_model_call():
    from backend.routers.creator import _validate_script_contract_static

    skill_md = """---
name: model-backed-generator
description: 使用宿主内置文本模型生成结果
---

本 Skill 需要使用宿主配置的文本模型完成开放式生成。

执行命令：
```bash
python scripts/run_model_task.py '{"topic":"{{topic}}","detail":"{{detail}}"}'
```
"""
    script = """import json
import sys

def main():
    data = json.loads(sys.argv[1])
    topic = data.get('topic', '')
    detail = data.get('detail', '')
    text = f'{topic}: {detail}'
    print(json.dumps({'text': text, 'image': ''}, ensure_ascii=False))

if __name__ == '__main__':
    main()
"""

    with pytest.raises(ValueError, match="声明需要使用宿主/内置/配置模型"):
        _validate_script_contract_static(
            file_path="scripts/run_model_task.py",
            content=script,
            skill_md=skill_md,
        )


def test_creator_accepts_model_declared_script_that_calls_configured_text_model():
    from backend.routers.creator import _validate_script_contract_static

    skill_md = """---
name: model-backed-generator
description: 使用宿主内置文本模型生成结果
---

本 Skill 需要使用宿主配置的文本模型完成开放式生成。

执行命令：
```bash
python scripts/run_model_task.py '{"topic":"{{topic}}","detail":"{{detail}}"}'
```
"""
    script = """import json
import os
import sys
import httpx

def main():
    payload = json.loads(sys.argv[1])
    topic = payload.get('topic')
    detail = payload.get('detail')
    response = httpx.post(
        os.environ['LLM_BASE_URL'].rstrip('/') + '/v1/chat/completions',
        json={
            'model': os.environ.get('TEXT_MODEL'),
            'messages': [{'role': 'user', 'content': f'{topic}: {detail}'}],
            'stream': False,
        },
        headers={'Authorization': 'Bearer ' + os.environ.get('LLM_API_KEY', 'ollama')},
        timeout=120,
    )
    response.raise_for_status()
    text = response.json()['choices'][0]['message']['content']
    print(json.dumps({'text': text, 'image': ''}, ensure_ascii=False))

if __name__ == '__main__':
    main()
"""

    _validate_script_contract_static(
        file_path="scripts/run_model_task.py",
        content=script,
        skill_md=skill_md,
    )


def test_creator_accepts_deterministic_script_when_skill_does_not_declare_model():
    from backend.routers.creator import _validate_script_contract_static

    skill_md = """---
name: deterministic-tool
description: 格式化输入数据
---

执行命令：
```bash
python scripts/format_data.py '{"value":"{{value}}"}'
```
"""
    script = """import json
import sys

def main():
    payload = json.loads(sys.argv[1])
    value = payload.get('value', '')
    print(json.dumps({'text': value.strip().upper()}, ensure_ascii=False))

if __name__ == '__main__':
    main()
"""

    _validate_script_contract_static(
        file_path="scripts/format_data.py",
        content=script,
        skill_md=skill_md,
    )

def test_kernel_creator_phase_prompts_include_block_runtime_requirements():
    from backend.services.kernel_loader import load_kernel_creator_for_phase

    phase2_prompt = load_kernel_creator_for_phase("phase2")
    phase3_prompt = load_kernel_creator_for_phase("phase3+")

    assert "宿主执行方式" in phase2_prompt
    assert "标准 Markdown fenced block" in phase2_prompt
    assert "生成的 Skill.md Markdown 运行说明" in phase3_prompt
    assert "不会触发宿主执行" in phase3_prompt
    assert "不要加入自定义 Runtime Contract JSON" in phase3_prompt
    assert "LLM_BASE_URL + TEXT_MODEL" in phase3_prompt
    assert "IMAGE_BASE_URL + IMAGE_MODEL" in phase3_prompt
    assert "generate_stable_diffusion_image" in phase3_prompt


def test_creator_script_prompt_requires_platform_image_runtime_helper():
    from backend.routers.creator import _build_generate_file_prompt

    messages = _build_generate_file_prompt(
        file_path="scripts/generate_image.py",
        skill_name="image-skill",
        purpose="调用平台 diffusion 生成图片",
        blueprint_text="需要根据用户输入生成图片",
        conversation_history=[],
    )
    prompt = messages[0]["content"]

    assert "LLM_BASE_URL" in prompt
    assert "IMAGE_BASE_URL" in prompt
    assert "IMAGE_MODEL" in prompt
    assert "VISION_MODEL" in prompt
    assert "generate_stable_diffusion_image" in prompt
    assert "不要在脚本里写中文 prompt 翻译逻辑" in prompt
    assert "禁止输出 base64 data URI" in prompt
    assert "可按需读取平台注入的 IMAGE_MODEL" in prompt
    assert "不需要额外校验它们是否存在" in prompt


def test_creator_rejects_direct_image_api_without_platform_helper():
    from backend.routers.creator import _validate_script_contract_static

    skill_md = """---
name: image-skill
description: 使用图像模型生成图片
---

执行命令：
```bash
python scripts/generate.py '{"prompt":"{{prompt}}"}'
```
"""
    script = """import json
import os
import sys

payload = json.loads(sys.argv[1])
prompt = payload.get('prompt')
print(os.environ['IMAGE_BASE_URL'], os.environ['IMAGE_MODEL'], prompt)
"""

    with pytest.raises(ValueError, match="直接调用图片生成接口"):
        _validate_script_contract_static(
            file_path="scripts/generate.py",
            content=script,
            skill_md=skill_md,
        )




def test_creator_accepts_platform_image_helper_for_image_generation():
    from backend.routers.creator import _validate_script_contract_static

    skill_md = """---
name: image-skill
description: 使用图像模型生成图片
---

执行命令：
```bash
python scripts/generate.py '{"prompt":"{{prompt}}"}'
```
"""
    script = """import json
import sys

from backend.services.skill_runtime import generate_stable_diffusion_image, print_json

payload = json.loads(sys.argv[1])
prompt = payload.get('prompt')
result = generate_stable_diffusion_image(prompt, filename_prefix='generated')
print_json({"image_path": result["image_path"], "prompt": result["prompt"]})
"""

    _validate_script_contract_static(
        file_path="scripts/generate.py",
        content=script,
        skill_md=skill_md,
    )


def test_creator_rejects_vision_model_for_image_generation_endpoint():
    from backend.routers.creator import _validate_script_contract_static

    skill_md = """---
name: image-skill
description: 使用图像模型生成图片
---

执行命令：
```bash
python scripts/generate.py '{"prompt":"{{prompt}}"}'
```
"""
    script = """import json
import os
import sys

payload = json.loads(sys.argv[1])
prompt = payload.get('prompt')
print(os.environ['IMAGE_BASE_URL'], os.environ['VISION_MODEL'], prompt)
"""

    with pytest.raises(ValueError, match="VISION_MODEL"):
        _validate_script_contract_static(
            file_path="scripts/generate.py",
            content=script,
            skill_md=skill_md,
        )


def test_creator_trial_run_prepares_python_deps_before_execution(monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    from backend.routers import creator

    prepared = {}

    def fake_get_venv_python(skill_dir):
        prepared["skill_dir"] = skill_dir
        return Path("/tmp/fake-skill-venv/bin/python")

    def fake_scan_and_install(script_path, venv_python):
        prepared["script_path"] = script_path
        prepared["venv_python"] = venv_python

    def fake_run(argv, **kwargs):
        prepared["argv"] = argv
        prepared["cwd"] = kwargs.get("cwd")
        prepared["env"] = kwargs.get("env")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(creator, "_get_skill_venv_python", fake_get_venv_python)
    monkeypatch.setattr(creator, "_scan_and_install_python_deps", fake_scan_and_install)
    monkeypatch.setattr(creator.subprocess, "run", fake_run)

    creator._trial_run_generated_script(
        "dep-skill",
        "scripts/use_dep.py",
        "import requests\nprint('{}')\n",
    )

    assert prepared["script_path"].name == "use_dep.py"
    assert prepared["venv_python"] == Path("/tmp/fake-skill-venv/bin/python")
    assert prepared["argv"][0] == "/tmp/fake-skill-venv/bin/python"
    assert prepared["argv"][1].endswith("use_dep.py")
    assert prepared["env"]["SKILL_TRIAL_RUN"] == "1"
    assert "PYTHONPATH" in prepared["env"]

def test_creator_trial_args_render_skill_md_template():
    from backend.routers.creator import _trial_args_for_script

    skill_md = """执行命令：
```bash
python scripts/generate.py '{"prompt":"{{prompt}}","topic":"{{topic}}"}'
```
"""
    args = _trial_args_for_script(
        skill_md,
        "scripts/generate.py",
        "import json, sys\npayload = json.loads(sys.argv[1])\n",
    )

    assert len(args) == 1
    payload = json.loads(args[0][0])
    assert payload["prompt"] == "a cinematic watercolor cat under a warm sunset"
    assert payload["topic"] == "system time"


def test_run_command_falls_back_when_inferred_skill_root_missing(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from backend.routers.chat_models import ChatRequest
    from backend.routers import sandbox_chat

    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    missing_skill_root = skills_root / "my-skill"
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["cwd"] = kwargs.get("cwd")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(sandbox_chat.subprocess, "run", fake_run)

    result, _ = sandbox_chat._execute_single_task(
        {"action": "run_command", "command": "python -c 'print(1)'"},
        [],
        ChatRequest(messages=[]),
        inferred_skill_root=missing_skill_root,
    )

    assert result["success"] is True
    assert captured["cwd"] == str(skills_root)


def test_block_planner_retries_invalid_json(monkeypatch):
    import asyncio
    import json

    from backend.routers.chat_models import ChatRequest, MarkdownBlock
    from backend.routers import sandbox_chat

    calls = []

    async def fake_complete_chat_once(messages, model):
        calls.append(messages)
        if len(calls) == 1:
            return ""
        return json.dumps({"tasks": [], "errors": []})

    monkeypatch.setattr(sandbox_chat, "complete_chat_once", fake_complete_chat_once)

    plan = asyncio.run(sandbox_chat._run_block_planner_round(
        assistant_text="执行命令：\n```bash\necho ok\n```",
        blocks=[MarkdownBlock(index=0, lang="bash", code="echo ok", before_context="执行命令：", after_context="")],
        request=ChatRequest(messages=[]),
        model="planner-model",
    ))

    assert plan == {"tasks": [], "errors": []}
    assert len(calls) == 2
    assert "只输出 JSON" in calls[1][-1]["content"]
