import inspect
import json

import pytest

from backend.services.creator import repair
from backend.services.creator.api import _uses_python_script_io_contract, generate_file
from backend.services.creator.contracts import validate_script_io_contract
from backend.services.creator.runtime_import_guard import guard_runtime_imports
from backend.services.creator_tool_registry import (
    ToolCapability,
    ToolFunctionManifest,
    clear_registered_tool_capabilities,
    register_tool_capability,
)
from backend.services.skill_plan import SkillPlanEntry


def _script(inputs: list[str], outputs: list[str]) -> str:
    schema = ",\n".join(f'        "{name}": {{"type": str, "required": True}}' for name in inputs)
    result = ", ".join(f'"{name}": "value"' for name in outputs)
    return f'''import json
import sys
from backend.services.runtime_tools import strict_json_argv_guard

def parse_args():
    payload = json.loads(sys.argv[1])
    return strict_json_argv_guard(payload, {{
{schema}
    }})

def run(args):
    return {{{result}}}

def main():
    print(json.dumps(run(parse_args())))

if __name__ == "__main__":
    main()
'''


def test_input_contract_rejects_exact_name_mismatch():
    results = validate_script_io_contract(
        file_path="scripts/main.py",
        content=_script(["input_b"], ["out"]),
        skill_plan_entry={"inputs": ["input_a"], "outputs": ["out"]},
    )
    failure = next(result for result in results if not result.passed)
    assert failure.id == "script_input_contract_mismatch"
    assert failure.details == {"missing": ["input_a"], "unexpected": ["input_b"], "actual": ["input_b"]}


def test_input_contract_is_order_independent():
    results = validate_script_io_contract(
        file_path="scripts/main.py",
        content=_script(["b", "a"], ["out"]),
        skill_plan_entry={"inputs": ["a", "b"], "outputs": ["out"]},
    )
    assert all(result.passed for result in results)


def test_output_contract_requires_every_frozen_output():
    results = validate_script_io_contract(
        file_path="scripts/main.py",
        content=_script(["input_a"], ["out_a"]),
        skill_plan_entry={"inputs": ["input_a"], "outputs": ["out_a", "out_b"]},
    )
    failure = next(result for result in results if not result.passed)
    assert failure.id == "script_output_contract_mismatch"
    assert failure.details["missing"] == ["out_b"]


def test_runtime_import_guard_uses_registry_callable_not_tool_id():
    clear_registered_tool_capabilities()
    register_tool_capability(ToolCapability(
        name="tool_alpha",
        display_name="Tool Alpha",
        category="test",
        functions=[ToolFunctionManifest(
            function_name="fn_alpha",
            import_path="module_x",
            short_description="test callable",
            when_to_use="test",
            signature="fn_alpha() -> dict",
            input_schema={},
            output_schema={"type": "object"},
            required_capabilities=["tool_alpha"],
        )],
    ))
    binding = {"available_tools": [{"tool_id": "tool_alpha", "function_name": "fn_alpha"}]}
    assert guard_runtime_imports("from module_x import fn_alpha\n", "scripts/main.py", binding).success
    assert not guard_runtime_imports("from module_x import tool_alpha\n", "scripts/main.py", binding).success


def test_contract_and_import_gates_precede_reviewer_and_repairs_reenter_gates():
    source = inspect.getsource(generate_file)
    loop = source.index("for attempt in range")
    contract_gate = source.index("validate_script_io_contract", loop)
    import_gate = source.index("guard_runtime_imports", contract_gate)
    reviewer = source.index("_run_script_responsibility_review", import_gate)
    repair = source.index("_repair_generated_file_with_feedback", reviewer)
    reenter = source.index("continue", repair)
    assert loop < contract_gate < import_gate < reviewer < repair < reenter


@pytest.mark.parametrize("runtime", ["node", "bash", "shell"])
def test_non_python_structured_runtime_skips_python_io_gate_but_keeps_later_gates(runtime):
    assert not _uses_python_script_io_contract({"runtime": runtime, "file_type": "script"})
    source = inspect.getsource(generate_file)
    runtime_gate = source.index("_uses_python_script_io_contract")
    python_io_gate = source.index("validate_script_io_contract", runtime_gate)
    import_gate = source.index('if request.file_path.startswith("scripts/"):', python_io_gate)
    assert runtime_gate < python_io_gate < import_gate < source.index("guard_runtime_imports", import_gate)


def test_output_contract_proves_run_local_variable_return():
    content = _script(["input_a"], ["output_a"]).replace(
        'return {"output_a": "value"}',
        'result = {"output_a": "value"}\n    return result',
    )
    results = validate_script_io_contract(
        file_path="scripts/main.py",
        content=content,
        skill_plan_entry={"inputs": ["input_a"], "outputs": ["output_a"]},
    )
    assert all(result.passed for result in results)


def test_unused_print_cannot_prove_run_output():
    content = _script(["input_a"], ["other"]).replace(
        "def run(args):",
        'def unused():\n    print(json.dumps({"output_a": "fake"}))\n\ndef run(args):',
    )
    results = validate_script_io_contract(
        file_path="scripts/main.py",
        content=content,
        skill_plan_entry={"inputs": ["input_a"], "outputs": ["output_a"]},
    )
    failure = next(result for result in results if not result.passed)
    assert failure.id == "script_output_contract_mismatch"


@pytest.mark.parametrize(
    "authority_issue_type",
    ["deterministic_authority_conflict", "tool_contract_mismatch"],
)
@pytest.mark.asyncio
async def test_reviewer_authority_overreach_cannot_reject_legal_callable(
    monkeypatch, caplog, authority_issue_type
):
    clear_registered_tool_capabilities()
    register_tool_capability(ToolCapability(
        name="tool_alpha",
        display_name="Tool Alpha",
        category="test",
        functions=[ToolFunctionManifest(
            function_name="fn_alpha",
            import_path="module_x",
            short_description="test callable",
            when_to_use="test",
            signature="fn_alpha() -> dict",
            input_schema={},
            output_schema={"type": "object"},
            required_capabilities=["tool_alpha"],
        )],
    ))
    binding = {"available_tools": [{"tool_id": "tool_alpha", "function_name": "fn_alpha"}]}
    assert guard_runtime_imports("from module_x import fn_alpha\n", "scripts/main.py", binding).success

    class Route:
        model = "unit-test-model"

    async def reviewer_response(messages, role, fallback_model=None):
        return json.dumps({
            "passed": False,
            "blocking_issues": [{
                "issue_type": authority_issue_type,
                "scope": "current_file_only",
                "failure_layer": "responsibility",
                "severity": "error",
                "failed_file": "scripts/main.py",
                "semantic_failure": "The legal Registry callable should be replaced with its Tool ID.",
            }],
            "repair_instructions": "Replace fn_alpha with tool_alpha.",
        })

    monkeypatch.setattr(repair, "route_model", lambda *args, **kwargs: Route())
    monkeypatch.setattr(repair, "complete_creator_role_once", reviewer_response)
    result = await repair._run_script_responsibility_review(
        file_path="scripts/main.py",
        script_content="from module_x import fn_alpha\n\ndef run(args):\n    return {'output_a': fn_alpha()}\n",
        skill_plan_entry=SkillPlanEntry(
            path="scripts/main.py",
            role="generic_script",
            file_type="script",
            purpose="Return the callable result.",
            runtime="python",
            inputs=["input_a"],
            outputs=["output_a"],
        ),
        review_context={"current_file_tool_binding": binding},
    )
    assert result["passed"] is True
    assert result["issues"] == []
    assert "authority_overreach_ignored" in caplog.text


@pytest.mark.asyncio
async def test_authority_overreach_filter_preserves_semantic_blocker(monkeypatch):
    class Route:
        model = "unit-test-model"

    async def reviewer_response(messages, role, fallback_model=None):
        return json.dumps({
            "passed": False,
            "blocking_issues": [
                {
                    "issue_type": "tool_contract_mismatch",
                    "scope": "current_file_only",
                    "failure_layer": "responsibility",
                    "severity": "error",
                    "failed_file": "scripts/main.py",
                    "semantic_failure": "Re-evaluate a deterministic tool authorization.",
                },
                {
                    "issue_type": "semantic_responsibility_incomplete",
                    "scope": "current_file_only",
                    "failure_layer": "responsibility",
                    "severity": "error",
                    "failed_file": "scripts/main.py",
                    "semantic_failure": "The implementation does not consume its input.",
                },
            ],
            "repair_instructions": "Make the input participate in the semantic result.",
        })

    monkeypatch.setattr(repair, "route_model", lambda *args, **kwargs: Route())
    monkeypatch.setattr(repair, "complete_creator_role_once", reviewer_response)
    result = await repair._run_script_responsibility_review(
        file_path="scripts/main.py",
        script_content="def run(args):\n    return {'output_a': 'constant'}\n",
        skill_plan_entry=SkillPlanEntry(
            path="scripts/main.py",
            role="generic_script",
            file_type="script",
            purpose="Transform the input.",
            runtime="python",
            inputs=["input_a"],
            outputs=["output_a"],
        ),
    )
    assert result["passed"] is False
    assert len(result["issues"]) == 1
    assert result["issues"][0]["details"]["issue_type"] == "semantic_responsibility_incomplete"
