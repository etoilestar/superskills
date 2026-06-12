import pytest

from backend.routers.creator import (
    ContractValidationError,
    _validate_script_contract_static,
    _validate_script_file_source_contract,
)
from backend.routers.sandbox_chat import _validate_stdout_against_action_entry
from backend.services.skill_plan import build_skill_plan_entry, capabilities_for_role


PDF_BUILDER_SOURCE = r'''
import json
import sys
from backend.services.skill_runtime import create_pdf, print_json


def run(payload: dict) -> dict:
    story_text = str(payload.get("story_text") or payload.get("text") or "export text")
    image_paths = payload.get("image_paths") or []
    previous_stdout = payload.get("previous_stdout") or ""
    template_path = payload.get("template_path") or ""
    text = "\n".join([story_text, str(image_paths), str(previous_stdout), str(template_path)])
    return create_pdf(text, filename="export.pdf")


def main():
    payload = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    print_json(run(payload))


if __name__ == "__main__":
    main()
'''

PDF_BUILDER_WITH_OPTIONAL_TEXT_SOURCE = PDF_BUILDER_SOURCE.replace(
    "from backend.services.skill_runtime import create_pdf, print_json",
    "from backend.services.skill_runtime import create_pdf, print_json, generate_text_with_llm",
).replace(
    'story_text = str(payload.get("story_text") or payload.get("text") or "export text")',
    'story_text = generate_text_with_llm(str(payload.get("story_text") or payload.get("text") or "export text"))',
)


def test_builder_defaults_do_not_require_or_forbid_model_generation():
    required, forbidden = capabilities_for_role("pdf_builder")
    assert required == ["pdf_generation", "file_output"]
    assert "text_generation" not in required
    assert "image_generation" not in forbidden
    assert "text_generation" not in forbidden

    entry = build_skill_plan_entry(file_path="scripts/build_pdf.py", purpose="role: pdf_builder")
    assert entry.required_capabilities == ["pdf_generation", "file_output"]
    assert "text_generation" not in entry.required_capabilities
    assert "text_generation" not in entry.forbidden_capabilities
    assert "image_generation" not in entry.forbidden_capabilities


def test_pdf_builder_without_model_call_passes_source_contract():
    _validate_script_file_source_contract("scripts/build_pdf.py", PDF_BUILDER_SOURCE, role="pdf_builder")


def test_pdf_builder_optional_model_call_passes_when_not_forbidden():
    entry = {
        "path": "scripts/build_pdf.py",
        "role": "pdf_builder",
        "inputs": ["story_text", "image_paths", "previous_stdout", "template_path"],
        "outputs": ["pdf_path", "file_paths"],
        "required_capabilities": ["pdf_generation", "file_output"],
        "optional_capabilities": ["text_generation"],
        "forbidden_capabilities": [],
    }
    _validate_script_file_source_contract(
        "scripts/build_pdf.py",
        PDF_BUILDER_WITH_OPTIONAL_TEXT_SOURCE,
        role="pdf_builder",
        skill_plan_entry=entry,
    )


def test_pdf_builder_forbidden_text_generation_blocks_text_model_call():
    entry = {
        "path": "scripts/build_pdf.py",
        "role": "pdf_builder",
        "inputs": ["story_text", "image_paths", "previous_stdout", "template_path"],
        "outputs": ["pdf_path", "file_paths"],
        "required_capabilities": ["pdf_generation", "file_output"],
        "forbidden_capabilities": ["text_generation"],
    }
    with pytest.raises(ContractValidationError, match="forbidden_text_generation"):
        _validate_script_file_source_contract(
            "scripts/build_pdf.py",
            PDF_BUILDER_WITH_OPTIONAL_TEXT_SOURCE,
            role="pdf_builder",
            skill_plan_entry=entry,
        )


def test_skill_md_model_step_does_not_force_export_script_to_call_model():
    skill_md = '''
# Mixed Skill

前置脚本会调用 LLM/TEXT_MODEL 生成故事；导出脚本只读取已有文本并构建 PDF。

### SkillPlan / 文件职责计划
- scripts/build_pdf.py
  role: pdf_builder
  inputs: [story_text, image_paths, previous_stdout, template_path]
  outputs: [pdf_path, file_paths]
  required_capabilities: [pdf_generation, file_output]

```bash
python scripts/build_pdf.py '{"story_text":"{{story_text}}","image_paths":"{{image_paths}}","previous_stdout":"{{previous_stdout}}","template_path":"{{template_path}}"}'
```
'''
    _validate_script_contract_static(
        file_path="scripts/build_pdf.py",
        content=PDF_BUILDER_SOURCE,
        skill_md=skill_md,
    )


def test_sandbox_html_builder_accepts_file_paths_field():
    _validate_stdout_against_action_entry(
        '{"file_paths": ["assets/generated/page.html"]}',
        {"role": "html_asset_builder", "outputs": ["file_paths"], "required_capabilities": ["html_asset_generation", "file_output"]},
    )


def test_global_skill_md_model_declaration_does_not_force_pdf_builder_capabilities():
    skill_md = '''
# Global Model Skill

本 Skill 全局说明：使用 LLM/TEXT_MODEL 生成文案，并使用 IMAGE_MODEL 生成插图。
required_capabilities: [text_generation, image_generation]

### SkillPlan / 文件职责计划
- scripts/build_pdf.py
  role: pdf_builder
  inputs: [story_text, image_paths]
  outputs: [pdf_path, file_paths]

```bash
python scripts/build_pdf.py '{"story_text":"{{story_text}}","image_paths":"{{image_paths}}"}'
```
'''
    entry = build_skill_plan_entry(file_path="scripts/build_pdf.py", blueprint_summary=skill_md)
    assert entry.role == "pdf_builder"
    assert entry.required_capabilities == ["pdf_generation", "file_output"]
    assert "text_generation" not in entry.required_capabilities
    assert "image_generation" not in entry.required_capabilities

    _validate_script_contract_static(
        file_path="scripts/build_pdf.py",
        content=PDF_BUILDER_SOURCE,
        skill_md=skill_md,
    )


def test_pdf_builder_ignores_propagated_model_capabilities_during_source_contract():
    entry = {
        "path": "scripts/build_pdf.py",
        "role": "pdf_builder",
        "inputs": ["story_text", "image_paths", "previous_stdout", "template_path"],
        "outputs": ["pdf_path", "file_paths"],
        "required_capabilities": ["text_generation", "image_generation", "pdf_generation", "file_output"],
        "forbidden_capabilities": [],
    }
    _validate_script_file_source_contract(
        "scripts/build_pdf.py",
        PDF_BUILDER_SOURCE,
        role="pdf_builder",
        skill_plan_entry=entry,
    )


def test_text_and_image_scripts_allow_self_implementation_until_e2e():
    text_source_without_llm = '''
import json
import sys


def main():
    payload = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    topic = str(payload.get("topic") or "")
    print(json.dumps({"text": topic}, ensure_ascii=False))


if __name__ == "__main__":
    main()
'''
    image_source_without_helper = '''
import json
import sys


def main():
    payload = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    topic = str(payload.get("topic") or "")
    print(json.dumps({"image_paths": [topic]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
'''
    _validate_script_file_source_contract(
        "scripts/generate_story.py",
        text_source_without_llm,
        skill_plan_entry={
            "path": "scripts/generate_story.py",
            "role": "text_generator",
            "inputs": ["topic"],
            "outputs": ["text"],
            "required_capabilities": ["text_generation"],
        },
    )
    _validate_script_file_source_contract(
        "scripts/generate_images.py",
        image_source_without_helper,
        skill_plan_entry={
            "path": "scripts/generate_images.py",
            "role": "image_generator",
            "inputs": ["topic"],
            "outputs": ["image_paths"],
            "required_capabilities": ["image_generation"],
        },
    )


def test_combine_to_pdf_path_is_pdf_builder_without_explicit_role_or_model_call():
    skill_md = '''
# Combine PDF Skill

全局说明：前置步骤可使用 LLM/TEXT_MODEL 和 IMAGE_MODEL 生成内容。
required_capabilities: [text_generation, image_generation]

### 使用方式
先由其他脚本生成文本与图片，然后执行 PDF 合并脚本：
```bash
python scripts/combine_to_pdf.py '{"story_text":"{{story_text}}","image_paths":"{{image_paths}}"}'
```
'''
    entry = build_skill_plan_entry(file_path="scripts/combine_to_pdf.py", blueprint_summary=skill_md)
    assert entry.role == "pdf_builder"
    assert entry.required_capabilities == ["pdf_generation", "file_output"]

    _validate_script_contract_static(
        file_path="scripts/combine_to_pdf.py",
        content=PDF_BUILDER_SOURCE,
        skill_md=skill_md,
    )


def test_global_required_capabilities_after_command_block_do_not_leak_to_pdf_builder():
    skill_md = '''
# PDF Export Skill

### SkillPlan / 文件职责计划
- scripts/build_pdf.py
  role: pdf_builder
  inputs: [story_text, image_paths]
  outputs: [pdf_path, file_paths]

```bash
python scripts/build_pdf.py '{"story_text":"{{story_text}}","image_paths":"{{image_paths}}"}'
```

## 全局模型说明
required_capabilities: [text_generation, image_generation]
需要由前置生成脚本调用 LLM/TEXT_MODEL 与 IMAGE_MODEL。
'''
    entry = build_skill_plan_entry(file_path="scripts/build_pdf.py", blueprint_summary=skill_md)
    assert entry.role == "pdf_builder"
    assert entry.required_capabilities == ["pdf_generation", "file_output"]
    assert "text_generation" not in entry.required_capabilities
    assert "image_generation" not in entry.required_capabilities


def test_registered_helper_capabilities_are_validated_from_registry():
    from backend.routers.creator import _script_satisfies_required_capability

    content = "from backend.services.skill_runtime import web_search\nweb_search('topic')"

    assert _script_satisfies_required_capability(content, "web_search") is True
    assert _script_satisfies_required_capability("print('no search')", "web_search") is False


def test_pdf_builder_direct_reportlab_allowed_until_e2e_artifact_validation():
    direct_reportlab = '''
import json
import sys
from reportlab.pdfgen import canvas


def main():
    payload = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    path = "outputs/direct.pdf"
    c = canvas.Canvas(path)
    c.drawString(10, 10, str(payload.get("text") or "hello"))
    c.save()
    print(json.dumps({"pdf_path": path, "file_paths": [path]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
'''
    _validate_script_file_source_contract(
        "scripts/build_pdf.py",
        direct_reportlab,
        role="pdf_builder",
        skill_plan_entry={
            "path": "scripts/build_pdf.py",
            "role": "pdf_builder",
            "inputs": ["text"],
            "outputs": ["pdf_path", "file_paths"],
            "required_capabilities": ["pdf_generation", "file_output"],
            "forbidden_capabilities": [],
        },
    )
