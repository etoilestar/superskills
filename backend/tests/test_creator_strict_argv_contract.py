from types import SimpleNamespace

import pytest

from backend.services.creator import e2e
from backend.services.creator.common import E2EWorkflowCommand
from backend.services.creator.contracts import _validate_script_contract_static, extract_python_strict_argv_schema
from backend.services.creator_tool_registry import get_tool_capability, resolve_tools_for_skill_plan_entry
from backend.services.runtime_tools import strict_json_argv_guard
from backend.services.skill_plan import SkillPlanEntry


SKILL_MD = '''# Test Skill

```bash
python scripts/main.py '{"text":"hello","style":"plain"}'
```
'''


def _validate(code: str) -> None:
    _validate_script_contract_static(file_path="scripts/main.py", content=code, skill_md=SKILL_MD)


STRICT_OK = r'''
import json
import sys
from backend.services.runtime_tools import strict_json_argv_guard

def parse_args():
    if len(sys.argv) != 2:
        raise ValueError("missing JSON argv")
    payload = json.loads(sys.argv[1])
    return strict_json_argv_guard(payload, {
        "text": {"type": str, "required": True},
        "style": {"type": str, "required": True},
    })

def run(args):
    return {"result": args["text"] + args["style"]}

def main():
    print(json.dumps(run(parse_args()), ensure_ascii=False))

if __name__ == "__main__":
    main()
'''


def test_runtime_tools_exports_strict_json_argv_guard():
    assert strict_json_argv_guard({"text": "ok"}, {"text": {"type": str}}) == {"text": "ok"}


def test_strict_json_argv_guard_supports_string_type_aliases():
    payload = {"name": "Ada", "count": 2, "items": ["a"], "meta": {"ok": True}}
    spec = {
        "name": {"type": "string"},
        "count": {"type": "integer"},
        "items": {"type": "array"},
        "meta": {"type": "object"},
    }
    assert strict_json_argv_guard(payload, spec) == payload
    with pytest.raises(TypeError, match="unknown argv type alias"):
        strict_json_argv_guard({"name": "Ada"}, {"name": {"type": "unsupported"}})


def test_strict_json_argv_guard_failures_and_no_input():
    spec = {"text": {"type": str, "required": True}}
    with pytest.raises(ValueError, match="unknown argv keys"):
        strict_json_argv_guard({"text": "ok", "extra": 1}, spec)
    with pytest.raises(ValueError, match="missing required argv keys"):
        strict_json_argv_guard({}, spec)
    with pytest.raises(ValueError, match="empty required argv value"):
        strict_json_argv_guard({"text": ""}, spec)
    with pytest.raises(TypeError, match="invalid argv type"):
        strict_json_argv_guard({"text": 1}, spec)
    assert strict_json_argv_guard({}, {}) == {}
    with pytest.raises(ValueError, match="unknown argv keys"):
        strict_json_argv_guard({"extra": 1}, {})


def test_creator_tool_registry_has_mandatory_script_argv_guard():
    cap = get_tool_capability("script_argv_guard")
    assert cap is not None
    assert cap.usage_policy == "helper_required"
    assert "strict_json_argv_guard" in cap.helper_imports


def test_resolve_tools_injects_guard_for_python_scripts_without_affecting_business_tools():
    entry = SkillPlanEntry(path="scripts/main.py", role="generic_script", file_type="script", purpose="x", runtime="python", inputs=[], outputs=["result"])
    result = resolve_tools_for_skill_plan_entry(entry)
    assert "script_argv_guard" in result.allowed_tools
    assert "strict_json_argv_guard" in result.allowed_helper_imports
    assert any("strict_json_argv_guard" in card for card in result.tool_function_cards)
    assert any(item.get("tool") == "script_argv_guard" for item in result.tool_snippets)

    for path_attr in ("path", "file_path", "script_path"):
        data = {"role": "custom_role", "runtime": "python", path_attr: "scripts/alt.py", "outputs": ["result"]}
        result = resolve_tools_for_skill_plan_entry(data)
        assert "script_argv_guard" in result.allowed_tools
        assert "strict_json_argv_guard" in result.allowed_helper_imports


def test_strict_argv_guard_accepts_mandatory_helper_call():
    _validate(STRICT_OK)


def test_missing_guard_import_or_call_fails_first_round():
    with pytest.raises(ValueError, match="import strict_json_argv_guard"):
        _validate(STRICT_OK.replace("from backend.services.runtime_tools import strict_json_argv_guard\n", ""))
    with pytest.raises(ValueError, match="call strict_json_argv_guard"):
        _validate(STRICT_OK.replace("return strict_json_argv_guard(payload, {", "return ({"))


def test_run_reparse_or_direct_payload_use_fails_first_round():
    reparsing = STRICT_OK.replace('return {"result": args["text"] + args["style"]}', 'payload = json.loads(sys.argv[1])\n    return {"result": payload["text"]}')
    with pytest.raises(ValueError, match="re-parse"):
        _validate(reparsing)
    direct_payload = STRICT_OK.replace('def run(args):\n    return {"result": args["text"] + args["style"]}', 'def run(payload):\n    return {"result": payload["text"]}')
    with pytest.raises(ValueError, match="unvalidated payload"):
        _validate(direct_payload)


def test_internal_argv_field_names_are_not_rejected_first_round():
    for key in ["input_text", "example", "todo", "story_sections", "chapter_text", "image_paths", "content", "result"]:
        code = STRICT_OK.replace('"text": {"type": str, "required": True}', f'"{key}": {{"type": str, "required": True}}')
        _validate(code)


def test_required_get_default_is_not_a_first_round_hard_gate():
    code = STRICT_OK.replace('return {"result": args["text"] + args["style"]}', 'return {"result": args.get("text", "fallback")}')
    schema_code = 'REQUIRED_KEYS = {"text"}\n' + code
    _validate(schema_code)


def test_extract_schema_supports_arg_schema_and_defaults_for_attribution():
    schema = extract_python_strict_argv_schema('ARG_SCHEMA = {"allowed_keys": {"a", "b"}, "required_keys": {"a"}, "expected_types": {"a": str}}\nOPTIONAL_KEYS={"b"}\nDEFAULT_VALUES={"b": 1}')
    assert schema["allowed_keys"] == ["a", "b"]
    assert schema["required_keys"] == ["a"]
    assert schema["optional_keys"] == ["b"]
    assert schema["defaulted_keys"] == ["b"]
    assert schema["expected_types"] == {"a": "str"}


def _argv_details(stderr, *, inputs, rendered, allowed='ALLOWED_KEYS = {"text"}', required='REQUIRED_KEYS = {"text"}', expected='EXPECTED_TYPES = {"text": str}', content=None):
    content = content or (STRICT_OK + "\n" + allowed + "\n" + required + "\n" + expected)
    return e2e._classify_argv_schema_failure(
        command=E2EWorkflowCommand(1, "SKILL.md", "scripts/main.py", "python scripts/main.py {}", "python", rendered),
        content=content,
        entry=SimpleNamespace(runtime="python", inputs=inputs),
        rendered_payload=rendered,
        stdout="",
        stderr=stderr,
    )


def test_argv_schema_attribution_targets():
    assert _argv_details("ValueError: unknown argv keys: ['extra']", inputs=["text"], rendered={"text": "ok", "extra": "x"})["primary_target"] == "SKILL.md"
    assert _argv_details("ValueError: unknown argv keys: ['text']", inputs=["text"], rendered={"text": "ok"}, allowed='ALLOWED_KEYS = set()')["primary_target"] == "SKILL.md"
    missing = _argv_details("ValueError: missing required argv keys: ['extra']", inputs=["text"], rendered={"text": "ok"}, required='REQUIRED_KEYS = {"text", "extra"}')
    assert missing["primary_target"] == "SKILL.md"
    assert missing["candidate_targets"] == ["SKILL.md"]
    uncertain = _argv_details("ValueError: unknown argv schema error", inputs=[], rendered={"mystery": "ok"}, allowed='ALLOWED_KEYS = {"other"}')
    assert uncertain["primary_target"] == "SKILL.md"
    assert uncertain["candidate_targets"] == ["SKILL.md"]


def test_argv_schema_prefers_skill_md_when_script_interface_self_consistent():
    content = 'ALLOWED_KEYS = {"input_text"}\nREQUIRED_KEYS = {"input_text"}\ndef run(argv):\n    return {"text": argv.get("input_text")}\n'
    details = _argv_details(
        "ValueError: missing required argv keys: ['input_text']",
        inputs=["input_text"],
        rendered={"title": "wrong"},
        content=content,
    )
    assert details["primary_target"] == "SKILL.md"


def test_argv_schema_targets_script_when_guard_and_run_keys_disagree():
    content = 'ALLOWED_KEYS = {"input_text"}\nREQUIRED_KEYS = {"input_text"}\ndef run(argv):\n    return {"text": argv["title"]}\n'
    details = _argv_details(
        "ValueError: missing required argv keys: ['input_text']",
        inputs=["input_text"],
        rendered={"title": "wrong"},
        content=content,
    )
    assert details["primary_target"] == "scripts/main.py"
    assert details["script_guard_run_mismatch"] is True


def test_argv_schema_requested_regressions_for_guard_as_interface_fact():
    content = 'ALLOWED_KEYS = {"input_files"}\nREQUIRED_KEYS = {"input_files"}\ndef run(args):\n    return {"count": len(args["input_files"])}\n'
    assert _argv_details("ValueError: unknown argv keys: ['file_path']", inputs=["input_files"], rendered={"file_path": "x"}, content=content)["primary_target"] == "SKILL.md"
    assert _argv_details("ValueError: unknown argv keys: ['model']", inputs=["input_files"], rendered={"input_files": ["x"], "model": "y"}, content=content)["primary_target"] == "SKILL.md"
    assert _argv_details("ValueError: missing required argv keys: ['input_files']", inputs=["input_files"], rendered={}, content=content)["primary_target"] == "SKILL.md"


def test_argv_schema_run_args_ast_detection():
    undeclared = 'ALLOWED_KEYS = {"input_files"}\nREQUIRED_KEYS = {"input_files"}\ndef run(args):\n    return {"x": args["file_path"]}\n'
    assert _argv_details("ValueError: missing required argv keys: ['input_files']", inputs=["input_files"], rendered={}, content=undeclared)["primary_target"] == "scripts/main.py"
    parse_main_sys_argv = 'import sys\nALLOWED_KEYS = {"input_files"}\nREQUIRED_KEYS = {"input_files"}\ndef parse_args():\n    return sys.argv[1]\ndef main():\n    print(sys.argv[0])\ndef run(args):\n    return {"x": args["input_files"]}\n'
    assert _argv_details("ValueError: missing required argv keys: ['input_files']", inputs=["input_files"], rendered={}, content=parse_main_sys_argv)["primary_target"] == "SKILL.md"
    run_sys_argv = 'import sys\nALLOWED_KEYS = {"input_files"}\nREQUIRED_KEYS = {"input_files"}\ndef run(args):\n    return {"x": sys.argv[1]}\n'
    assert _argv_details("ValueError: missing required argv keys: ['input_files']", inputs=["input_files"], rendered={}, content=run_sys_argv)["primary_target"] == "scripts/main.py"
    optional_get = 'ALLOWED_KEYS = {"input_files", "style"}\nREQUIRED_KEYS = {"input_files"}\ndef run(args):\n    return {"x": args["input_files"], "style": args.get("style")}\n'
    details = _argv_details("ValueError: missing required argv keys: ['input_files']", inputs=["input_files"], rendered={}, content=optional_get)
    assert details["primary_target"] == "SKILL.md"
    assert details["script_run_optional_read_keys"] == ["style"]

from backend.services.creator.generation import (
    _build_script_generate_file_prompt_variant,
    _script_generation_skeleton,
)


def test_generation_skeleton_uses_mandatory_guard_import_call_not_inline_validate():
    skeleton = _script_generation_skeleton("scripts/main.py", "test", "", skill_plan_entry={"path": "scripts/main.py", "runtime": "python", "inputs": ["input_text"], "outputs": ["result"]})
    assert "from backend.services.runtime_tools import strict_json_argv_guard" in skeleton
    assert "strict_json_argv_guard(payload" in skeleton
    assert "def validate_payload" not in skeleton


def test_generation_skeleton_displays_current_planned_interface_names():
    skeleton = _script_generation_skeleton(
        "scripts/main.py",
        "test",
        "",
        skill_plan_entry={
            "path": "scripts/main.py",
            "runtime": "python",
            "inputs": ["input_a"],
            "outputs": ["output_a"],
        },
    )

    assert 'inputs: ["input_a"]' in skeleton
    assert 'outputs: ["output_a"]' in skeleton
    assert "Prefer these input names for strict_json_argv_guard" in skeleton
    assert "these output names for the final stdout object" in skeleton


def test_script_producer_prompt_no_longer_encourages_argv_key_renaming():
    prompt_text = "\n".join(
        value
        for value in _build_script_generate_file_prompt_variant.__code__.co_consts
        if isinstance(value, str)
    )

    assert "inputs 只提供语义输入提示" not in prompt_text
    assert "不是 argv key 白名单" not in prompt_text
    assert "脚本第一轮可以选择清晰、稳定的 argv key" not in prompt_text
    assert "应优先直接沿用这些 input 字段名" in prompt_text


def test_script_producer_prompt_preserves_internal_naming_freedom():
    prompt_text = "\n".join(
        value
        for value in _build_script_generate_file_prompt_variant.__code__.co_consts
        if isinstance(value, str)
    )

    assert "脚本内部局部变量名、helper 参数名和 Tool 调用参数名可以自由设计" in prompt_text
    assert "脚本内部局部变量、helper 参数和 Tool 参数仍由实现自由决定" in prompt_text


def test_argv_schema_repair_instruction_treats_guard_as_probe():
    instruction = e2e._argv_schema_repair_instruction(
        "scripts/main.py",
        {"primary_target": "scripts/main.py", "candidate_targets": ["SKILL.md", "scripts/main.py"], "target_reason": "x"},
    )
    assert "strict_json_argv_guard(payload, spec) 是当前脚本入口接口事实" in instruction
    assert "禁止只改 guard schema" in instruction
    assert "不要只修 guard" in instruction
    assert "只修当前脚本 mandatory argv guard import/call 或 guard spec" not in instruction


def test_e2e_script_target_rule_contains_coverage_guardrail():
    source = e2e._repair_existing_file_for_e2e_failure.__code__.co_consts
    joined = "\n".join(str(item) for item in source if isinstance(item, str))
    assert "strict_json_argv_guard 是接口不对齐探针" in joined
    assert "不能通过删除参数降低功能覆盖面" in joined
    assert "真实 workflow 执行证据" in joined
    assert "不得检查 ToolPool" in joined


def test_argv_schema_noop_guardrail_source_contains_two_noop_block():
    source = e2e._repair_existing_file_for_e2e_failure.__code__.co_consts
    joined = "\n".join(str(item) for item in source if isinstance(item, str))
    assert "two consecutive no-op patches" in joined
    assert "next_target" in joined and "SKILL.md" in joined
