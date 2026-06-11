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


def test_first_round_skill_md_command_block_allows_non_exact_skillplan_inputs():
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

    assert "skill_md.command_block.json_argv_object" not in failed_ids
    assert "command_block.skillplan_inputs.exact" not in failed_ids


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
    execution_result = next(result for result in results if result.id == "skill_md.command_block.fenced_exists")

    assert not execution_result.passed
    assert execution_result.target == "scripts/write.py"


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

    assert "reference.role_sections" in failed_ids


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

    assert "reference.no_executable_script_blocks" in failed_ids
    assert "command_block.runtime.matches_skillplan" not in failed_ids


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


def test_image_and_text_named_script_is_promoted_to_composite_generator_contract():
    from backend.services.blueprint_parser import parse_blueprint

    blueprint = """
📋 Skill 架构蓝图
- **Skill 名称**: fairy-images
- scripts/: `scripts/generate_fairy_tale_with_images.py` 负责根据 topic 和 custom_character 生成童话配图，调用平台图片能力。
"""
    plan = parse_blueprint([{"role": "assistant", "content": blueprint}])
    entry = next(item for item in plan.skill_plan.files if item.path == "scripts/generate_fairy_tale_with_images.py")

    assert entry.role == "composite_generator"
    assert entry.required_capabilities == ["text_generation", "image_generation"]
    assert "text_generation" not in entry.forbidden_capabilities
    assert "pdf_generation" in entry.forbidden_capabilities
    assert "custom_character" in entry.inputs
    assert entry.outputs == []
    assert entry.command_template.startswith("python scripts/generate_fairy_tale_with_images.py")


def test_main_script_with_ambiguous_image_pdf_wording_stays_generic():
    from backend.services.blueprint_parser import parse_blueprint

    blueprint = """
📋 Skill 架构蓝图
- **Skill 名称**: vague-skill
- scripts/: `scripts/main.py` 生成图片和 PDF，但未声明 role
"""
    plan = parse_blueprint([{"role": "assistant", "content": blueprint}])
    entry = next(item for item in plan.skill_plan.files if item.path == "scripts/main.py")

    assert entry.role == "generic_script"
    assert "image_generation" not in entry.required_capabilities


def test_image_generator_contract_requires_helper_without_strict_skillplan_entry():
    from backend.routers.creator import _check_script_file_contract

    no_helper = """import json
import sys

def main():
    payload = json.loads(sys.argv[1])
    print(json.dumps({'image_paths': [payload.get('topic', '')]}))

if __name__ == '__main__':
    main()
"""
    failed = {
        result.id
        for result in _check_script_file_contract("scripts/render_images.py", no_helper, role="image_generator")
        if not result.passed
    }

    assert "script.required_capabilities.called" in failed


def test_generic_script_forbids_image_helper_and_repair_guidance_keeps_role_context():
    from backend.routers.creator import _check_script_file_contract, _targeted_generated_file_repair_instructions

    entry = {
        "path": "scripts/main.py",
        "role": "generic_script",
        "inputs": ["topic"],
        "outputs": ["text"],
        "language": "python",
        "runtime": "python",
    }
    content = """import json
import sys
from backend.services.skill_runtime import generate_stable_diffusion_image

def main():
    payload = json.loads(sys.argv[1])
    result = generate_stable_diffusion_image(payload.get('topic', 'cat'))
    print(json.dumps({'text': result.get('image_path')}))

if __name__ == '__main__':
    main()
"""
    failed = {
        result.id
        for result in _check_script_file_contract("scripts/main.py", content, skill_plan_entry=entry)
        if not result.passed
    }
    guidance = _targeted_generated_file_repair_instructions(
        file_path="scripts/main.py",
        deterministic_error="script.capability.forbidden_image_generation: forbidden_capabilities 禁止但脚本调用了图片生成 helper",
    )

    assert "script.capability.forbidden_image_generation" in failed
    assert "禁止修改蓝图或 SKILL.md" in guidance or "蓝图和 SKILL.md 确定后不能" in guidance
    assert "只能修当前脚本" in guidance


def test_composite_generator_can_call_text_and_image_helpers():
    from backend.routers.creator import _check_script_file_contract, _script_generation_skeleton

    entry = {
        "path": "scripts/generate_fairy_tale_with_images.py",
        "role": "composite_generator",
        "inputs": ["topic", "custom_character"],
        "outputs": ["text", "image_paths", "images", "text_with_image_prompts"],
        "required_capabilities": ["text_generation", "image_generation"],
        "forbidden_capabilities": ["pdf_generation"],
        "language": "python",
        "runtime": "python",
    }
    source = """import json
import sys
from backend.services.skill_runtime import generate_text_with_llm, generate_stable_diffusion_image

def main():
    payload = json.loads(sys.argv[1])
    prompt = payload.get('topic', '') + payload.get('custom_character', '')
    text = generate_text_with_llm(prompt)
    result = generate_stable_diffusion_image(text, filename_prefix='generated')
    print(json.dumps({'text': text, 'text_with_image_prompts': [{'text': text, 'image_prompt': text}], 'image_paths': [result.get('image_path')], 'images': [result]}))

if __name__ == '__main__':
    main()
"""
    failed = {result.id for result in _check_script_file_contract("scripts/generate_fairy_tale_with_images.py", source, skill_plan_entry=entry) if not result.passed}
    skeleton = _script_generation_skeleton("scripts/generate_fairy_tale_with_images.py", "", "", skill_plan_entry=entry)

    assert "script.required_capabilities.called" not in failed
    assert "script.capability.forbidden_image_generation" not in failed
    assert "generate_text_with_llm" in skeleton
    assert "generate_stable_diffusion_image" in skeleton


def test_explicit_composite_role_overrides_blueprint_and_command_contract():
    from backend.services.blueprint_parser import parse_blueprint

    blueprint = """
📋 Skill 架构蓝图
- **Skill 名称**: composite-skill
- scripts/: `scripts/main.py`
  scripts/main.py
  role: composite_generator
  inputs: topic, custom_character
  outputs: text, image_paths, images, text_with_image_prompts
  required_capabilities: text_generation, image_generation
  forbidden_capabilities: pdf_generation
"""
    plan = parse_blueprint([{"role": "assistant", "content": blueprint}])
    entry = next(item for item in plan.skill_plan.files if item.path == "scripts/main.py")

    assert entry.role == "composite_generator"
    assert entry.inputs == ["topic", "custom_character"]
    assert entry.required_capabilities == ["text_generation", "image_generation"]
    assert "image_generation" not in entry.forbidden_capabilities
    assert "text_generation" not in entry.forbidden_capabilities
    assert entry.command_template == 'python scripts/main.py \'{"topic":"{{topic}}","custom_character":"{{custom_character}}"}\''


def test_creator_prompt_injects_kernel_references_for_scripts():
    from backend.routers.creator import _build_generate_file_prompt

    entry = {
        "path": "scripts/write.py",
        "role": "text_generator",
        "inputs": ["topic"],
        "outputs": ["text"],
        "required_capabilities": ["text_generation"],
        "language": "python",
        "runtime": "python",
    }
    messages = _build_generate_file_prompt(
        "scripts/write.py",
        "demo-skill",
        "生成文本",
        "scripts/write.py role: text_generator inputs: topic",
        [],
        skill_plan_entry=entry,
    )
    prompt = messages[-1]["content"]

    assert "Creator internal-only kernel guidance" in prompt
    assert "INTERNAL-ONLY kernel/references/best-practices.md" in prompt
    assert "kernel/references/output-patterns.md" in prompt


def test_reference_contract_requires_capability_mentions_for_composite_commands():
    from backend.routers.creator import _check_reference_file_contract

    content = """
## 输入输出
inputs: topic
outputs: text, image_paths

## 执行步骤
```bash
python scripts/composite_images.py '{"topic":"{{topic}}"}'
```

## 角色与能力边界
role: composite_generator，required_capabilities: text_generation, image_generation，禁止 PDF。

## 规范
先生成文本，再将文本传递给图片生成 helper，保持 JSON stdout。

## 示例
输入 topic=猫，输出 text 和 image_paths。

## 反例
不要输出固定模板，不要省略 image_generation。

## 约束
JSON argv keys 必须与 SkillPlan inputs 对齐，text 输出可作为 image prompt 跨能力传递。
"""
    results = _check_reference_file_contract(
        "references/composite.md",
        content,
        purpose="scripts/composite_images.py role: composite_generator inputs: topic outputs: text, image_paths required_capabilities: text_generation, image_generation",
    )
    failed = {result.id for result in results if not result.passed}

    assert "reference.required_capabilities.mentioned" not in failed


def test_skill_md_rejects_kernel_reference_leak_not_declared_in_blueprint():
    from backend.routers.creator import _check_skill_md_contract

    blueprint = """
📋 Skill 架构蓝图
- **Skill 名称**: no-kernel-leak
- scripts/: `scripts/run.py`
  scripts/run.py role: text_generator inputs: topic outputs: text
"""
    skill_md = """---
name: no-kernel-leak
description: demo
---
# Demo

## 执行
```bash
python scripts/run.py '{"topic":"{{topic}}"}'
```

## 参考资料
- `references/workflows.md`: 内部 workflow 参考。
- `references/output-patterns.md`: 内部输出模式。
"""
    failed = {result.id for result in _check_skill_md_contract(skill_md, blueprint) if not result.passed}

    assert "skill_md.resource.local_declared" not in failed


def test_skill_md_allows_declared_local_reference_and_script():
    from backend.routers.creator import _check_skill_md_contract

    blueprint = """
📋 Skill 架构蓝图
- **Skill 名称**: local-ok
- scripts/: `scripts/run.py`
  scripts/run.py role: text_generator inputs: topic outputs: text
- references/: `references/guide.md`
"""
    skill_md = """---
name: local-ok
description: demo
---
# Demo

读取 `references/guide.md` 中的执行步骤。

```bash
python scripts/run.py '{"topic":"{{topic}}"}'
```
"""
    failed = {result.id for result in _check_skill_md_contract(skill_md, blueprint) if not result.passed}

    assert "skill_md.resource.local_declared" not in failed
    assert "skill_md.reference.mentioned" not in failed


def test_reference_command_payload_conflict_is_rejected_against_skillplan():
    from backend.routers.creator import _check_reference_file_contract

    purpose = "scripts/generate_fable.py role: text_generator inputs: topic outputs: text required_capabilities: text_generation"
    content = """
## 规范
围绕 topic 写寓言，语言清晰。

## 示例
输入 topic=狐狸，输出有寓意的短文。

## 反例
不要忽略主题，不要输出空文本。

## 约束
必须使用 SkillPlan 的命令模板。

## 执行命令
```bash
python scripts/generate_fable.py '{"payload":{"topic":"{{topic}}"}}'
```
"""
    failed = {r.id for r in _check_reference_file_contract("references/best-practices.md", content, purpose=purpose) if not r.passed}

    assert "reference.no_executable_script_blocks" in failed
    assert "command_block.command_template.equivalent" not in failed
    assert "command_block.skillplan_inputs.exact" not in failed


def test_reference_redefined_role_is_rejected_against_skillplan():
    from backend.routers.creator import _check_reference_file_contract

    purpose = "scripts/generate_fable.py role: text_generator inputs: topic outputs: text required_capabilities: text_generation"
    content = """
## 规范
按照 topic 写完整寓言，保持温和风格。

## 示例
输入 topic=诚实，输出一个儿童能理解的故事。

## 反例
不要写成说明文，不要忽略寓意。

## 约束
role: generic_script
inputs: topic
outputs: text
required_capabilities: text_generation
"""
    failed = {r.id for r in _check_reference_file_contract("references/best-practices.md", content, purpose=purpose) if not r.passed}

    assert "reference.role_sections" in failed
    assert "reference.role.matches_skillplan" not in failed


def test_reference_cannot_put_skillplan_command_in_anti_example():
    from backend.routers.creator import _check_reference_file_contract

    purpose = "scripts/generate_fable.py role: text_generator inputs: topic outputs: text required_capabilities: text_generation"
    content = """
## 规范
按照 topic 生成寓言，正文需要自然、有结尾寓意。

## 示例
正确做法是读取用户主题并组织成完整故事。

## 反例
以下写法不要使用：
```bash
python scripts/generate_fable.py '{"topic":"{{topic}}"}'
```

## 约束
反例只能展示缺失参数或多余参数，不能否定正确命令。
"""
    failed = {r.id for r in _check_reference_file_contract("references/best-practices.md", content, purpose=purpose) if not r.passed}

    assert "reference.no_executable_script_blocks" in failed
    assert "reference.anti_example.not_skillplan_command" not in failed


def test_required_text_and_image_capabilities_normalize_to_composite_generator():
    from backend.services.skill_plan import build_skill_plan_entry

    entry = build_skill_plan_entry(
        file_path="scripts/main.py",
        purpose="role: generic_script inputs: topic outputs: text, image_paths required_capabilities: text_generation, image_generation",
    )

    assert entry.role == "composite_generator"
    assert entry.required_capabilities == ["text_generation", "image_generation"]
    assert "image_generation" not in entry.forbidden_capabilities
    assert entry.command_template == 'python scripts/main.py \'{"topic":"{{topic}}"}\''


def test_command_template_can_be_parsed_for_sandbox_trial_args():
    from backend.routers.creator import _render_trial_command_args
    from backend.services.skill_plan import build_skill_plan_entry

    entry = build_skill_plan_entry(file_path="scripts/generate_fable.py", purpose="inputs: topic")
    args = _render_trial_command_args(entry.command_template, "scripts/generate_fable.py")

    assert args is not None
    assert len(args) == 1
    assert set(__import__("json").loads(args[0]).keys()) == {"topic"}


def test_composite_trial_stdout_accepts_text_and_image_paths(tmp_path):
    from backend.routers.creator import _validate_trial_stdout_json

    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"fake")
    payload = {"text": "story", "image_paths": [str(image_path)], "images": [{"image_path": str(image_path)}]}

    _validate_trial_stdout_json(
        stdout=__import__("json").dumps(payload),
        content="generate_text_with_llm(); generate_stable_diffusion_image()",
        args=['{"topic":"cat"}'],
        role="composite_generator",
        skill_dir=tmp_path,
    )



def test_command_template_normalized_equivalence_allows_json_key_order_and_spaces():
    from backend.routers.creator import _check_command_block_contract
    from backend.services.skill_plan import build_skill_plan_entry

    entry = build_skill_plan_entry(file_path="scripts/write.py", purpose="inputs: topic, tone")
    command = "python scripts/write.py '{\"tone\": \"{{ tone }}\", \"topic\": \"{{topic}}\"}'"
    failed = {r.id for r in _check_command_block_contract("scripts/write.py", [command], entry) if not r.passed}

    assert "command_block.command_template.equivalent" not in failed
    assert "command_block.skillplan_inputs.exact" not in failed


def test_reference_command_is_rejected_when_skill_md_already_has_command():
    from backend.routers.creator import _check_reference_file_contract

    skill_md = """---
name: fable
description: demo
---
```bash
python scripts/generate_fable.py '{"topic":"{{topic}}"}'
```
"""
    content = """
## 规范
保持寓言结构清晰，结尾给出寓意。

## 示例
输入诚实，输出角色行动和寓意。

## 反例
不要输出空泛口号。

## 约束
如果 SKILL.md 已有命令，本 reference 不重复命令。
```bash
python scripts/generate_fable.py '{"topic":"{{topic}}"}'
```
"""
    failed = {r.id for r in _check_reference_file_contract("references/best-practices.md", content, purpose=skill_md) if not r.passed}

    assert "reference.no_executable_script_blocks" in failed
    assert "reference.command_block.not_duplicate_skill_md" not in failed


def test_trial_stdout_outputs_allow_limited_legacy_aliases():
    from backend.routers.creator import _validate_trial_stdout_json

    entry = {
        "path": "scripts/write.py",
        "role": "text_generator",
        "inputs": ["topic"],
        "outputs": ["text"],
        "required_capabilities": ["text_generation"],
        "language": "python",
        "runtime": "python",
    }

    _validate_trial_stdout_json(
        stdout=__import__("json").dumps({"story_text": "legacy story"}),
        content="generate_text_with_llm('x')",
        args=['{"topic":"cat"}'],
        role="text_generator",
        skill_plan_entry=entry,
    )


def test_composite_generator_skeleton_uses_required_capabilities_not_role_name():
    from backend.routers.creator import _script_generation_skeleton

    entry = {
        "path": "scripts/composite_pdf.py",
        "role": "composite_generator",
        "inputs": ["text"],
        "outputs": ["pdf_path", "file_paths"],
        "required_capabilities": ["pdf_generation", "file_output"],
        "forbidden_capabilities": ["image_generation"],
        "language": "python",
        "runtime": "python",
    }

    skeleton = _script_generation_skeleton("scripts/composite_pdf.py", "", "", skill_plan_entry=entry)

    assert "generate_stable_diffusion_image" not in skeleton
    assert "generate_text_with_llm" not in skeleton
    assert "PDF" in skeleton or "pdf_path" in skeleton


def test_skill_md_rejects_explicit_kernel_reference_leak_even_if_self_declared():
    from backend.routers.creator import _check_skill_md_contract

    skill_md = """---
name: leak
description: demo
---
请读取 kernel/references/workflows.md 和 references/output-patterns.md 作为运行规范。
"""

    failed = {result.id for result in _check_skill_md_contract(skill_md, skill_md) if not result.passed}

    assert "skill_md.resource.no_kernel_leak" in failed


def test_html_asset_builder_plan_skeleton_and_trial_validation(tmp_path):
    from backend.routers.creator import _script_generation_skeleton, _validate_trial_stdout_json
    from backend.services.skill_plan import build_skill_plan_entry

    entry = build_skill_plan_entry(
        file_path="scripts/build_html.py",
        purpose="role: html_asset_builder inputs: topic outputs: html_path, asset_paths required_capabilities: html_generation, file_output",
    )
    skeleton = _script_generation_skeleton("scripts/build_html.py", "", "", skill_plan_entry=entry.__dict__)

    assert entry.role == "html_asset_builder"
    assert entry.outputs == ["html_path", "asset_paths"]
    assert "assets/generated" in skeleton
    assert "html_path" in skeleton

    skill_dir = tmp_path / "html-skill"
    html_file = skill_dir / "assets" / "generated" / "demo.html"
    html_file.parent.mkdir(parents=True)
    html_file.write_text("<!doctype html><html><body>demo</body></html>", encoding="utf-8")
    _validate_trial_stdout_json(
        stdout=__import__("json").dumps({"html_path": "assets/generated/demo.html", "asset_paths": ["assets/generated/demo.html"]}),
        content="html_path = 'assets/generated/demo.html'\nPath('assets/generated/demo.html').write_text('<html></html>')",
        args=['{"topic":"demo"}'],
        role="html_asset_builder",
        skill_plan_entry=entry.__dict__,
        skill_dir=skill_dir,
    )


def test_multifunction_roles_have_unified_optional_output_contracts():
    from backend.services.skill_plan import build_skill_plan_entry

    composite = build_skill_plan_entry(
        file_path="scripts/create_story_and_images.py",
        purpose="role: composite_generator inputs: topic outputs: story_text, image_paths required_capabilities: text_generation, image_generation",
    )
    text = build_skill_plan_entry(file_path="scripts/write_story.py", purpose="role: text_generator inputs: topic")
    image = build_skill_plan_entry(file_path="scripts/render_images.py", purpose="role: image_generator inputs: story_text")
    docx = build_skill_plan_entry(file_path="scripts/export_docx.py", purpose="role: docx_builder inputs: previous_stdout outputs: docx_path")
    pptx = build_skill_plan_entry(file_path="scripts/export_pptx.py", purpose="role: pptx_builder inputs: previous_stdout outputs: pptx_path")

    assert composite.outputs == ["story_text", "image_paths"]
    assert {"text_generation", "image_generation"} <= set(composite.required_capabilities)
    assert text.outputs == []
    assert image.outputs == []
    assert docx.outputs == ["docx_path"]
    assert pptx.outputs == ["pptx_path"]
    assert docx.command_template == 'python scripts/export_docx.py \'{"previous_stdout":"{{previous_stdout}}"}\''
    assert pptx.command_template == 'python scripts/export_pptx.py \'{"previous_stdout":"{{previous_stdout}}"}\''


def test_export_builder_skeletons_consume_previous_stdout_without_generation_helpers():
    from backend.routers.creator import _script_generation_skeleton
    from backend.services.skill_plan import build_skill_plan_entry

    docx_entry = build_skill_plan_entry(file_path="scripts/export_docx.py", purpose="role: docx_builder inputs: previous_stdout outputs: docx_path")
    pptx_entry = build_skill_plan_entry(file_path="scripts/export_pptx.py", purpose="role: pptx_builder inputs: story_text outputs: pptx_path")

    docx_skeleton = _script_generation_skeleton("scripts/export_docx.py", "", "", skill_plan_entry=docx_entry.__dict__)
    pptx_skeleton = _script_generation_skeleton("scripts/export_pptx.py", "", "", skill_plan_entry=pptx_entry.__dict__)

    assert "previous_stdout" in docx_skeleton
    assert "from backend.services.skill_runtime import create_docx, print_json" in docx_skeleton
    assert "return create_docx(text, filename='output.docx')" in docx_skeleton
    assert "from backend.services.skill_runtime import create_pptx, print_json" in pptx_skeleton
    assert "return create_pptx(text, filename='output.pptx')" in pptx_skeleton
    assert "generate_text_with_llm" not in docx_skeleton
    assert "generate_stable_diffusion_image" not in docx_skeleton
    assert "generate_text_with_llm" not in pptx_skeleton
    assert "generate_stable_diffusion_image" not in pptx_skeleton


def test_skill_plan_separates_creator_internal_from_skill_local_references():
    from backend.services.skill_plan import build_skill_plan_entry

    entry = build_skill_plan_entry(
        file_path="SKILL.md",
        reference_files=["references/workflows.md", "kernel/references/workflows.md"],
    )

    assert entry.reference_files == ["references/workflows.md"]
    assert entry.skill_local_references == ["references/workflows.md"]
    assert "kernel/references/workflows.md" in entry.creator_internal_references
    assert "kernel/references/workflows.md" not in entry.dependencies


def test_social_card_blueprint_capabilities_are_normalized_to_runtime_needs():
    from backend.services.blueprint_parser import parse_blueprint

    blueprint = """
📋 Skill 架构蓝图
- **Skill 名称**: topic-social-card
- path: `SKILL.md`
  role: skill_overview
  required_capabilities: [workflow_overview]
- path: `scripts/analyze_topic.py`
  role: text_generator
  inputs: [topic_name]
  outputs: [structured_topic_data]
  dependencies: [references/topic_sources.md]
  required_capabilities: [text_generation, web_search, database_read]
- path: `scripts/generate_illustrated_story.py`
  role: image_generator
  inputs: [topic_data]
  outputs: [story_text, image_paths]
  required_capabilities: [text_generation, image_generation, vision_understanding]
- references/: `references/topic_sources.md`
  references/topic_sources.md
  role: reference
  required_capabilities: [reference_guidance]
- assets/: `assets/placeholder.jpg`
  assets/placeholder.jpg
  role: asset
  required_capabilities: [static_resource]
"""
    plan = parse_blueprint([{"role": "assistant", "content": blueprint}])
    entries = {entry.path: entry for entry in plan.skill_plan.files}

    assert entries["SKILL.md"].required_capabilities == []
    assert entries["references/topic_sources.md"].required_capabilities == []
    assert entries["assets/placeholder.jpg"].required_capabilities == []
    assert entries["scripts/analyze_topic.py"].required_capabilities == ["text_generation"]
    assert entries["scripts/generate_illustrated_story.py"].role == "composite_generator"
    assert entries["scripts/generate_illustrated_story.py"].required_capabilities == ["text_generation", "image_generation"]


def test_role_capability_allowlist_keeps_high_risk_tools_on_dedicated_roles():
    from backend.services.skill_plan import build_skill_plan_entry

    text = build_skill_plan_entry(
        file_path="scripts/write.py",
        purpose="role: text_generator required_capabilities: [text_generation, web_search, database_read]",
    )
    image = build_skill_plan_entry(
        file_path="scripts/render.py",
        purpose="role: image_generator required_capabilities: [image_generation, vision_understanding]",
    )
    search = build_skill_plan_entry(
        file_path="scripts/search.py",
        purpose="role: search_reader required_capabilities: [web_search, text_generation]",
    )
    database = build_skill_plan_entry(
        file_path="scripts/db.py",
        purpose="role: database_reader required_capabilities: [database_read, text_generation]",
    )
    vision = build_skill_plan_entry(
        file_path="scripts/vision.py",
        purpose="role: vision_analyzer required_capabilities: [vision_understanding]",
    )

    assert text.required_capabilities == ["text_generation"]
    assert image.required_capabilities == ["image_generation"]
    assert "web_search" in search.required_capabilities
    assert "database_read" in database.required_capabilities
    assert "vision_understanding" in vision.required_capabilities


def test_runtime_artifact_asset_is_removed_from_file_plan_generically():
    from backend.services.blueprint_parser import parse_blueprint

    blueprint = """
📋 Skill 架构蓝图
- **Skill 名称**: artifact-cleanup
- scripts/: `scripts/build_document.py`
  scripts/build_document.py role: pdf_builder inputs: text outputs: pdf_path, file_paths required_capabilities: pdf_generation, file_output
- assets/: `assets/final-report.pdf`
  assets/final-report.pdf role: asset outputs: pdf_path dependencies: scripts/build_document.py 最终输出产物，由脚本运行时生成。
"""
    plan = parse_blueprint([{"role": "assistant", "content": blueprint}])
    paths = {item.path for item in plan.skill_plan.files}
    script = next(item for item in plan.skill_plan.files if item.path == "scripts/build_document.py")

    assert "assets/final-report.pdf" not in paths
    assert script.outputs == ["pdf_path", "file_paths"]
    assert any("asset" in warning and "运行时产物" in warning for warning in plan.warnings)


def test_asset_role_is_upload_only_and_has_no_runtime_contract_fields():
    from backend.services.skill_plan import SkillPlan, build_skill_plan_entry, normalize_skill_plan

    static_asset = build_skill_plan_entry(
        file_path="assets/logo.png",
        purpose="用户上传的静态素材 upload-only static logo",
    )
    generated_asset = build_skill_plan_entry(
        file_path="assets/generated-image.png",
        purpose="role: asset outputs: image_path dependencies: scripts/render.py 运行时生成的最终图片产物",
    )
    normalized = normalize_skill_plan(SkillPlan(skill_name="asset-check", files=[static_asset, generated_asset]))
    paths = {entry.path for entry in normalized.files}
    kept = next(entry for entry in normalized.files if entry.path == "assets/logo.png")

    assert "assets/logo.png" in paths
    assert "assets/generated-image.png" not in paths
    assert kept.inputs == []
    assert kept.outputs == []
    assert kept.dependencies == []
    assert kept.required_capabilities == []


def test_creator_phase_phrases_are_rejected_from_runtime_skill_md():
    from backend.routers.creator import _check_skill_md_contract

    blueprint = """
📋 Skill 架构蓝图
- **Skill 名称**: runtime-only
- scripts/: 无需
"""
    skill_md = """---
name: runtime-only
description: demo
---
# 使用方式
先输出架构蓝图，等待用户确认后开始创建文件。
"""
    failed = {result.id for result in _check_skill_md_contract(skill_md, blueprint) if not result.passed}

    assert "skill_md.forbidden_creator_flow" in failed


def test_script_io_chain_command_must_reference_prior_output_not_raw_user_request():
    from backend.routers.creator import _check_skill_md_contract

    blueprint = """
📋 Skill 架构蓝图
- **Skill 名称**: chained-skill
- scripts/: `scripts/plan.py`, `scripts/export.py`
  scripts/plan.py role: text_generator inputs: topic outputs: outline required_capabilities: text_generation
  scripts/export.py role: docx_builder inputs: outline outputs: docx_path, file_paths required_capabilities: docx_generation, file_output
"""
    skill_md = """---
name: chained-skill
description: demo
---
# Workflow
```bash
python scripts/plan.py '{"topic":"{{topic}}"}'
```
```bash
python scripts/export.py '{"outline":"{{user_request}}"}'
```
"""
    failed = {result.id for result in _check_skill_md_contract(skill_md, blueprint) if not result.passed}

    assert "skill_md.dataflow.input_available" not in failed


def test_dependency_output_directory_is_cleaned_from_skillplan():
    from backend.services.blueprint_parser import parse_blueprint

    blueprint = """
📋 Skill 架构蓝图
- **Skill 名称**: dep-cleanup
- scripts/: `scripts/export.py`
  scripts/export.py role: docx_builder inputs: text outputs: docx_path dependencies: outputs/, references/style.md required_capabilities: docx_generation, file_output
- references/: `references/style.md`
"""
    plan = parse_blueprint([{"role": "assistant", "content": blueprint}])
    entry = next(item for item in plan.skill_plan.files if item.path == "scripts/export.py")

    assert "outputs/" not in entry.dependencies
    assert "references/style.md" in entry.dependencies
    assert any("dependencies" in warning and "输出" in warning for warning in plan.warnings)


def test_resource_capabilities_are_empty_and_role_allowlist_cleans_spread():
    from backend.services.skill_plan import build_skill_plan_entry

    reference = build_skill_plan_entry(
        file_path="references/guide.md",
        purpose="required_capabilities: text_generation, web_search, database_read",
    )
    text = build_skill_plan_entry(
        file_path="scripts/write.py",
        purpose="role: text_generator inputs: topic required_capabilities: text_generation, web_search, database_read, vision_understanding, wechat_publish, file_output",
    )
    pdf = build_skill_plan_entry(
        file_path="scripts/export_pdf.py",
        purpose="role: pdf_builder inputs: text outputs: pdf_path required_capabilities: text_generation, image_generation, pdf_generation, web_search, database_read, vision_understanding, file_output",
    )

    assert reference.required_capabilities == []
    assert text.required_capabilities == ["text_generation", "file_output"]
    assert pdf.required_capabilities == ["pdf_generation", "file_output"]


def test_skillplan_dataflow_allows_arbitrary_business_field_names():
    from backend.services.blueprint_parser import parse_blueprint

    blueprint = """
📋 Skill 架构蓝图
- **Skill 名称**: arbitrary-fields
- scripts/: `scripts/derive.py`, `scripts/render.py`
  scripts/derive.py role: generic_script inputs: customer_brief, locale_hint outputs: structured_payload required_capabilities: deterministic_execution, file_output
  scripts/render.py role: generic_script inputs: structured_payload, brand_palette outputs: artifact_path, file_paths required_capabilities: deterministic_execution, file_output
"""
    plan = parse_blueprint([{"role": "assistant", "content": blueprint}])
    warnings = "\n".join(plan.warnings)
    first = next(item for item in plan.skill_plan.files if item.path == "scripts/derive.py")
    second = next(item for item in plan.skill_plan.files if item.path == "scripts/render.py")

    assert "customer_brief" not in warnings
    assert "brand_palette" not in warnings
    assert '"customer_brief":"{{customer_brief}}"' in first.command_template
    assert '"structured_payload":"{{structured_payload}}"' in second.command_template


def test_second_round_e2e_command_key_mismatch_exposes_structured_diff(tmp_path):
    from backend.routers.creator import _parse_e2e_workflow_command, _validate_e2e_command_static

    skill_md = """---
name: generic-skill
description: generic
---
# generic-skill

- scripts/: `scripts/step.py`
  scripts/step.py role: generic_script inputs: source_value, desired_value outputs: result_value

```bash
python scripts/step.py '{"source_value":"{{source_value}}","unexpected_value":"{{unexpected_value}}"}'
```
"""
    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    (script_dir / "step.py").write_text("print('{}')", encoding="utf-8")
    command = _parse_e2e_workflow_command(
        command="python scripts/step.py '{\"source_value\":\"{{source_value}}\",\"unexpected_value\":\"{{unexpected_value}}\"}'",
        ordinal=1,
        source_path="SKILL.md",
    )

    with pytest.raises(ValueError) as excinfo:
        _validate_e2e_command_static(
            command=command,
            trial_skill_dir=tmp_path,
            skill_md=skill_md,
            available_payload_keys={"source_value"},
        )

    message = str(excinfo.value)
    assert "E2E_LAYER=e2e_dataflow" in message
    assert "command_block.skillplan_inputs.exact" in message
    assert '"target_script": "scripts/step.py"' in message
    assert '"expected_inputs": ["payload"]' in message
    assert '"actual_payload_keys": ["source_value", "unexpected_value"]' in message
    assert '"missing_keys": ["payload"]' in message
    assert '"extra_keys": ["source_value", "unexpected_value"]' in message
    assert '"available_upstream_outputs": ["source_value"]' in message


def test_deterministic_patch_replaces_command_json_keys_and_preserves_invocation():
    from backend.routers.creator import _patch_skill_md_command_payloads_from_skill_plan

    blueprint = """
📋 Skill 架构蓝图
- **Skill 名称**: generic-skill
- scripts/: `scripts/step.py`
  scripts/step.py role: generic_script inputs: alpha, beta outputs: gamma
"""
    skill_md = """---
name: generic-skill
description: generic
---
# generic-skill

```bash
python scripts/step.py '{"alpha":"{{alpha}}","legacy":"{{legacy}}"}'
```
"""

    patched, details = _patch_skill_md_command_payloads_from_skill_plan(skill_md, blueprint)

    assert details
    assert "python scripts/step.py" in patched
    assert "legacy" not in patched
    assert "'" in patched
    assert '{"alpha":"{{alpha}}","beta":"{{beta}}"}' in patched


def test_deterministic_patch_uses_upstream_output_placeholder_for_downstream_input():
    from backend.routers.creator import _patch_skill_md_command_payloads_from_skill_plan
    from backend.services.skill_plan import command_payload_placeholders

    blueprint = """
📋 Skill 架构蓝图
- **Skill 名称**: chain-skill
- scripts/: `scripts/first.py`
  scripts/first.py role: generic_script inputs: seed_value outputs: shared_value
- scripts/: `scripts/second.py`
  scripts/second.py role: generic_script inputs: shared_value outputs: final_value
"""
    skill_md = """---
name: chain-skill
description: chain
---
# chain-skill

```bash
python scripts/first.py '{"seed_value":"{{seed_value}}"}'
```

```bash
python scripts/second.py '{"wrong_key":"{{seed_value}}"}'
```
"""

    patched, _ = _patch_skill_md_command_payloads_from_skill_plan(skill_md, blueprint)
    commands = [line.strip() for line in patched.splitlines() if line.strip().startswith("python scripts/second.py")]

    assert commands
    assert command_payload_placeholders(commands[0], "scripts/second.py") == {"shared_value": "shared_value"}


def test_skill_plan_dataflow_unresolved_reports_plan_not_skill_md_loop():
    from backend.routers.creator import _check_skill_md_contract

    blueprint = """
📋 Skill 架构蓝图
- **Skill 名称**: broken-chain
- scripts/: `scripts/first.py`
  scripts/first.py role: generic_script inputs: seed_value outputs: produced_value
- scripts/: `scripts/second.py`
  scripts/second.py role: generic_script inputs: missing_value outputs: final_value
"""
    skill_md = """---
name: broken-chain
description: broken
---
# broken-chain

```bash
python scripts/first.py '{"seed_value":"{{seed_value}}"}'
```

```bash
python scripts/second.py '{"missing_value":"{{missing_value}}"}'
```
"""

    failed = {r.id for r in _check_skill_md_contract(skill_md, blueprint) if not r.passed}

    assert "skill_plan.dataflow_unresolved" not in failed
    assert "command_block.skillplan_inputs.exact" not in failed


def test_render_script_command_from_skill_plan_uses_only_entry_inputs():
    from backend.services.skill_plan import SkillPlanEntry, render_script_command_from_skill_plan, command_payload_placeholders

    entry = SkillPlanEntry(
        path="scripts/run.py",
        file_type="script",
        role="generic_script",
        purpose="generic",
        runtime="python",
        inputs=["free_name", "another_name"],
        outputs=["result_name"],
    )

    command = render_script_command_from_skill_plan(entry, {"free_name": "{{free_name}}", "ignored": "{{ignored}}"})

    assert command.startswith("python scripts/run.py")
    assert command_payload_placeholders(command, "scripts/run.py") == {
        "free_name": "free_name",
        "another_name": "another_name",
    }
