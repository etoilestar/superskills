import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest


def test_skill_plan_parses_explicit_contract_fields_and_reference_mapping():
    from backend.services.blueprint_parser import parse_blueprint

    blueprint = """
📋 Skill 架构蓝图
- **Skill 名称**: poster-skill
- scripts/: `scripts/render.py`
  scripts/render.py
  role: image_generator
  inputs: topic, prompt
  outputs: image_paths, images
  dependencies: references/image-generation.md
  required_capabilities: image_generation
  forbidden_capabilities: pdf_generation
- references/: `references/image-generation.md`
"""
    plan = parse_blueprint([{"role": "assistant", "content": blueprint}])
    entry = next(item for item in plan.skill_plan.files if item.path == "scripts/render.py")

    assert entry.role == "image_generator"
    assert entry.inputs == ["topic", "prompt"]
    assert entry.outputs == ["image_paths", "images"]
    assert entry.dependencies == ["references/image-generation.md"]
    assert entry.required_capabilities == ["image_generation"]
    assert entry.forbidden_capabilities == ["pdf_generation"]


def test_low_confidence_script_falls_back_to_generic_with_warning():
    from backend.services.blueprint_parser import parse_blueprint

    blueprint = """
📋 Skill 架构蓝图
- **Skill 名称**: vague-skill
- scripts/: `scripts/main.py` 生成图片和 PDF，但未声明 role
"""
    plan = parse_blueprint([{"role": "assistant", "content": blueprint}])
    entry = next(item for item in plan.skill_plan.files if item.path == "scripts/main.py")

    assert entry.role == "generic_script"
    assert entry.confidence < 0.7
    assert any("generic_script" in warning for warning in plan.warnings)
    assert "image_generation" not in entry.required_capabilities


def test_skill_md_command_block_must_match_skillplan_inputs():
    from backend.routers.creator import _check_skill_md_contract

    blueprint = """
📋 Skill 架构蓝图
- **Skill 名称**: text-skill
- scripts/: `scripts/write.py`
  scripts/write.py role: text_generator inputs: topic, prompt outputs: text
"""
    skill_md = """---
name: text-skill
description: text
---
# text-skill

## 执行流程
```bash
python scripts/write.py '{"topic":"{{topic}}","extra":"{{extra}}"}'
```
"""
    results = _check_skill_md_contract(skill_md, blueprint)
    failed_ids = {result.id for result in results if not result.passed}

    assert "command_block.skillplan_inputs.exact" in failed_ids


def test_skill_md_can_delegate_script_command_to_reference():
    from backend.routers.creator import _check_skill_md_contract

    blueprint = """
📋 Skill 架构蓝图
- **Skill 名称**: delegated-skill
- scripts/: `scripts/write.py`
  scripts/write.py role: text_generator inputs: topic outputs: text
- references/: `references/text-generation.md`
"""
    skill_md = """---
name: delegated-skill
description: delegated
---
# delegated-skill

## 执行流程
1. 读取 `references/text-generation.md` 中的执行步骤和命令模板。
2. 根据 reference 的 command 运行 `scripts/write.py`。

## 参考资料
- `references/text-generation.md`: 定义 text_generator 的命令、输入输出和约束。
"""
    results = _check_skill_md_contract(skill_md, blueprint)
    execution_result = next(result for result in results if result.id == "skill_md.script_command.exists")

    assert execution_result.passed


def test_reference_contract_requires_subtask_contract_sections():
    from backend.routers.creator import _check_reference_file_contract

    content = """
## 规范
按照主题生成文本。

## 示例
输入 topic=猫，输出猫故事。

## 反例
不要输出图片。

## 约束
不得生成 PDF。这里补充足够多的文字以满足最低长度要求，确保文档不是占位符，而是可以指导子任务执行的规则集合。
"""
    results = _check_reference_file_contract("references/text-generation.md", content, purpose="text_generator 子任务执行参考")
    failed_ids = {result.id for result in results if not result.passed}

    assert "reference.subtask_contract_sections" in failed_ids


def test_skill_plan_runtime_defaults_python_and_supports_node_bash():
    from backend.services.skill_plan import build_skill_plan_entry
    from backend.routers.creator import _script_command_template, _script_generation_skeleton

    py_entry = build_skill_plan_entry(file_path="scripts/main.py", purpose="inputs: topic")
    js_entry = build_skill_plan_entry(file_path="scripts/main.js", purpose="inputs: topic")
    sh_entry = build_skill_plan_entry(file_path="scripts/main.sh", purpose="inputs: topic")
    explicit_node_entry = build_skill_plan_entry(file_path="scripts/runner.txt", purpose="language: javascript runtime: node inputs: topic")

    assert py_entry.language == "python"
    assert py_entry.runtime == "python"
    assert _script_command_template("scripts/main.py", "", py_entry).startswith("python scripts/main.py")
    assert js_entry.language == "javascript"
    assert js_entry.runtime == "node"
    assert _script_command_template("scripts/main.js", "", js_entry).startswith("node scripts/main.js")
    assert "process.argv[2]" in _script_generation_skeleton("scripts/main.js", "", "", skill_plan_entry=js_entry.__dict__)
    assert explicit_node_entry.language == "javascript"
    assert explicit_node_entry.runtime == "node"
    assert explicit_node_entry.command_template.startswith("node scripts/runner.txt")
    assert sh_entry.language == "bash"
    assert sh_entry.runtime == "bash"
    assert _script_command_template("scripts/main.sh", "", sh_entry).startswith("bash scripts/main.sh")
    assert "$1" in _script_generation_skeleton("scripts/main.sh", "", "", skill_plan_entry=sh_entry.__dict__)


def test_strict_script_contract_validates_runtime_json_argv_and_inputs():
    from backend.routers.creator import _check_script_file_contract

    entry = {
        "path": "scripts/main.js",
        "role": "generic_script",
        "inputs": ["topic"],
        "outputs": ["text"],
        "language": "javascript",
        "runtime": "node",
    }
    bad = "console.log(JSON.stringify({text: 'fixed'}));"
    good = "const payload = JSON.parse(process.argv[2]);\nconsole.log(JSON.stringify({text: payload.topic}));"

    bad_failed = {result.id for result in _check_script_file_contract("scripts/main.js", bad, skill_plan_entry=entry) if not result.passed}
    good_failed = {result.id for result in _check_script_file_contract("scripts/main.js", good, skill_plan_entry=entry) if not result.passed}

    assert "script.json_argv.runtime" in bad_failed
    assert "script.skillplan_inputs.used" in bad_failed
    assert "script.json_argv.runtime" not in good_failed
    assert "script.skillplan_inputs.used" not in good_failed


def test_skill_plan_inputs_strip_types_defaults_without_concatenation():
    from backend.services.skill_plan import build_skill_plan_entry

    entry = build_skill_plan_entry(
        file_path="scripts/write.py",
        purpose="inputs: topic: string, tone=humorous, style (default: popular-science)",
    )

    assert entry.inputs == ["topic", "tone", "style"]
    assert "topicstring" not in entry.command_template
    assert "tonehumorous" not in entry.command_template
    assert "stylepopular-science" not in entry.command_template


def test_script_sanitize_strips_orphan_trailing_fences_for_python_node_bash():
    from backend.routers.creator import _sanitize_generated_file_content

    py_entry = {
        "path": "scripts/write.py",
        "role": "generic_script",
        "inputs": ["topic"],
        "outputs": ["text"],
        "language": "python",
        "runtime": "python",
    }
    py = """import json
import sys

def main():
    payload = json.loads(sys.argv[1])
    print(json.dumps({'text': payload.get('topic', '')}))

if __name__ == '__main__':
    main()
```"""
    assert _sanitize_generated_file_content("scripts/write.py", py, skill_plan_entry=py_entry).endswith("main()")

    node_entry = {**py_entry, "path": "scripts/write.js", "language": "javascript", "runtime": "node"}
    node = """const payload = JSON.parse(process.argv[2] || '{}');
console.log(JSON.stringify({ text: payload.topic || '' }));
~~~"""
    assert "~~~" not in _sanitize_generated_file_content("scripts/write.js", node, skill_plan_entry=node_entry)

    bash_entry = {**py_entry, "path": "scripts/write.sh", "language": "bash", "runtime": "bash"}
    bash = """#!/usr/bin/env bash
payload_json=${1:-'{}'}
text=$(python -c 'import json,sys; p=json.loads(sys.argv[1]); print(p.get("topic", ""))' "$payload_json")
printf '{"text":"%s"}\n' "$text"
```"""
    assert "```" not in _sanitize_generated_file_content("scripts/write.sh", bash, skill_plan_entry=bash_entry)


def test_reference_command_block_validates_runtime_and_skillplan_inputs():
    from backend.routers.creator import _check_reference_file_contract

    content = """
## 输入输出
inputs: topic
outputs: text

## 执行步骤
```bash
node scripts/write.py '{"topic":"{{topic}}"}'
```

## 角色与能力边界
role: text_generator runtime: python，禁止生成图片或 PDF。

## 规范
按照 topic 生成文本，stdout 输出 JSON。

## 示例
输入 topic=猫，输出 {"text":"猫"}。

## 反例
不要使用 extra 参数，不要输出图片。

## 约束
JSON argv keys 必须与 SkillPlan inputs 对齐，文档内容足够长，可直接指导执行。
"""
    results = _check_reference_file_contract("references/text-generation.md", content, purpose="scripts/write.py role: text_generator runtime: python inputs: topic outputs: text")
    failed_ids = {result.id for result in results if not result.passed}

    assert "command_block.runtime.matches_skillplan" in failed_ids


def test_asset_contract_validates_yaml_and_markdown_placeholders():
    import pytest

    from backend.routers.creator import ContractValidationError, _validate_asset_file_contract

    _validate_asset_file_contract("assets/config.yaml", "name: demo\nitems:\n  - one\n")
    with pytest.raises(ContractValidationError, match="不是合法 YAML"):
        _validate_asset_file_contract("assets/config.yaml", "name: [unterminated")
    with pytest.raises(ContractValidationError, match="占位短语"):
        _validate_asset_file_contract("assets/guide.md", "TODO: 待补充内容，需要以后再写。" * 3)



def test_role_skeletons_inject_platform_calls_for_python_node_bash():
    from backend.services.skill_plan import build_skill_plan_entry
    from backend.routers.creator import _script_generation_skeleton

    text_entry = build_skill_plan_entry(file_path="scripts/write.py", purpose="role: text_generator inputs: topic")
    image_entry = build_skill_plan_entry(file_path="scripts/render.js", purpose="role: image_generator inputs: topic")
    pdf_entry = build_skill_plan_entry(file_path="scripts/pdf.sh", purpose="role: pdf_builder inputs: text runtime: bash")

    assert "generate_text_with_llm" in _script_generation_skeleton("scripts/write.py", "", "", skill_plan_entry=text_entry.__dict__)
    image_skeleton = _script_generation_skeleton("scripts/render.js", "", "", skill_plan_entry=image_entry.__dict__)
    assert "process.argv[2]" in image_skeleton
    assert "generate_stable_diffusion_image" in image_skeleton
    pdf_skeleton = _script_generation_skeleton("scripts/pdf.sh", "", "", skill_plan_entry=pdf_entry.__dict__)
    assert "$1" in pdf_skeleton
    assert "pdf_path" in pdf_skeleton


def test_required_capability_contract_rejects_empty_text_generator_shell():
    from backend.routers.creator import _check_script_file_contract

    entry = {
        "path": "scripts/write.py",
        "role": "text_generator",
        "inputs": ["topic"],
        "outputs": ["text"],
        "required_capabilities": ["text_generation"],
        "language": "python",
        "runtime": "python",
    }
    fixed_template = """import json
import sys

def main():
    payload = json.loads(sys.argv[1])
    print(json.dumps({'text': payload.get('topic', '')}))

if __name__ == '__main__':
    main()
"""
    real_call = """import json
import sys
from backend.services.skill_runtime import generate_text_with_llm

def main():
    payload = json.loads(sys.argv[1])
    prompt = payload.get('topic', '')
    print(json.dumps({'text': generate_text_with_llm(prompt)}))

if __name__ == '__main__':
    main()
"""

    bad_failed = {result.id for result in _check_script_file_contract("scripts/write.py", fixed_template, skill_plan_entry=entry) if not result.passed}
    good_failed = {result.id for result in _check_script_file_contract("scripts/write.py", real_call, skill_plan_entry=entry) if not result.passed}

    assert "script.required_capabilities.called" in bad_failed
    assert "script.required_capabilities.called" not in good_failed


def test_sanitize_trims_prose_from_node_entrypoint_to_stdout():
    from backend.routers.creator import _sanitize_generated_file_content

    entry = {
        "path": "scripts/write.js",
        "role": "generic_script",
        "inputs": ["topic"],
        "outputs": ["text"],
        "language": "javascript",
        "runtime": "node",
    }
    raw = """下面是 scripts/write.js：
const payload = JSON.parse(process.argv[2] || '{}');
console.log(JSON.stringify({ text: payload.topic || '' }));
这是一段说明文字，不应保存。
```"""
    sanitized = _sanitize_generated_file_content("scripts/write.js", raw, skill_plan_entry=entry)

    assert sanitized.startswith("const payload")
    assert sanitized.endswith("));")
    assert "说明文字" not in sanitized
    assert "```" not in sanitized
