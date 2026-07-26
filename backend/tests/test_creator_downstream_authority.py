import logging

import pytest

from backend.services.creator.execution_contract import (
    ExecutionContractError,
    log_downstream_authority,
    project_per_file_execution_contract,
    validate_script_execution_contract,
)
from backend.services.creator.runtime_import_guard import guard_runtime_imports
from backend.services.creator_tool_registry import ToolCapability, ToolFunctionManifest


def _tool(tool_id, capabilities, module, function):
    return ToolCapability(
        name=tool_id, display_name=tool_id, category="abstract",
        capabilities=capabilities,
        functions=[ToolFunctionManifest(
            function_name=function, import_path=module, short_description="",
            when_to_use="", signature=f"{function}(value: str) -> dict",
        )],
    )


def _project(required=("capability_a",), forbidden=(), registry=None, target="scripts/a.py"):
    return project_per_file_execution_contract(
        target_file=target, inputs=["input_a"], outputs=["output_a"],
        required_capabilities=required, forbidden_capabilities=forbidden,
        registry=registry or [_tool("tool_a", ["capability_a"], "module.a", "function_a")],
    )


def test_per_file_tool_isolation_and_no_skill_wide_leakage():
    registry = [
        _tool("tool_a", ["capability_a"], "module.a", "function_a"),
        _tool("tool_b", ["capability_b"], "module.b", "function_b"),
        _tool("tool_c", ["capability_c"], "module.c", "function_c"),
    ]
    a = _project(registry=registry)
    b = _project(required=["capability_b"], registry=registry, target="scripts/b.py")
    assert [tool["tool_id"] for tool in a["allowed_tools"]] == ["tool_a"]
    assert [tool["tool_id"] for tool in b["allowed_tools"]] == ["tool_b"]
    assert "tool_b" not in str(a) and "tool_c" not in str(a)


def test_tool_id_and_callable_are_separate_registry_identities():
    contract = _project(registry=[_tool("tool-alpha", ["capability_a"], "module.runtime", "execute_alpha")])
    tool = contract["allowed_tools"][0]
    assert tool["tool_id"] == "tool-alpha"
    assert tool["callables"] == [{"import_path": "module.runtime", "function": "execute_alpha", "signature": "execute_alpha(value: str) -> dict"}]
    assert "tool_alpha" not in str(contract)


def test_exact_registry_authority_ignores_discovery_ranking_and_supports_many_to_many():
    registry = [
        _tool("tool_a", ["capability_a", "capability_b"], "module.a", "function_a"),
        _tool("tool_b", ["capability_a"], "module.b", "function_b"),
    ]
    contract = _project(registry=registry)
    assert [tool["tool_id"] for tool in contract["allowed_tools"]] == ["tool_a", "tool_b"]


def test_unknown_capability_fails_without_fuzzy_matching():
    with pytest.raises(ExecutionContractError) as exc:
        _project(required=["capability_unknown"])
    assert exc.value.failure["code"] == "unknown_required_capability"


def test_forbidden_capability_excludes_multi_capability_tool():
    registry = [
        _tool("tool_a", ["capability_a", "capability_b"], "module.a", "function_a"),
        _tool("tool_b", ["capability_a"], "module.b", "function_b"),
    ]
    contract = _project(forbidden=["capability_b"], registry=registry)
    assert [tool["tool_id"] for tool in contract["allowed_tools"]] == ["tool_b"]


@pytest.mark.parametrize(
    ("source", "code"),
    [
        ("def run():\n    strict_json_argv_guard({}, {'input_b': {'type': 'string'}})\n    return {'output_a': 1}\n", "script_input_contract_mismatch"),
        ("def run():\n    strict_json_argv_guard({}, {'input_a': {}, 'input_extra': {}})\n    return {'output_a': 1}\n", "unexpected_script_input"),
        ("def run():\n    strict_json_argv_guard({}, {'input_a': {}})\n    return {'result': 1}\n", "script_output_contract_mismatch"),
    ],
)
def test_static_interface_exact_mismatches(source, code):
    result = validate_script_execution_contract(source, _project())
    assert not result["success"]
    assert code in [failure["code"] for failure in result["failures"]]


def test_unauthorized_callable_is_deterministically_rejected():
    source = "from module.b import function_b\n\ndef run():\n    strict_json_argv_guard({}, {'input_a': {}})\n    function_b('x')\n    return {'output_a': 1}\n"
    # Use the platform namespace, which is the guarded callable boundary.
    source = source.replace("module.b", "backend.services.runtime_tools.module_b")
    result = validate_script_execution_contract(source, _project())
    assert "unauthorized_callable" in [failure["code"] for failure in result["failures"]]


def test_runtime_guard_consumes_execution_contract_without_reprojection():
    contract = _project(registry=[_tool("tool_a", ["capability_a"], "backend.services.runtime_tools", "strict_json_argv_guard")])
    contract["allowed_tools"][0]["callables"][0]["signature"] = "strict_json_argv_guard(payload: dict, spec: dict) -> dict"
    source = "from backend.services.runtime_tools import strict_json_argv_guard\nstrict_json_argv_guard({}, {})\n"
    result = guard_runtime_imports(source, "scripts/a.py", contract)
    assert result.success


def test_all_downstream_phase_telemetry_uses_same_digest(caplog):
    contract = _project()
    with caplog.at_level(logging.INFO):
        for phase in ("generator", "reviewer", "repair", "runtime_guard"):
            log_downstream_authority(phase, contract)
    lines = [record.message for record in caplog.records if "downstream_authority" in record.message]
    assert len(lines) == 4
    assert all(contract["contract_digest"] in line for line in lines)


def test_role_purpose_and_filename_do_not_change_authorized_domain():
    registry = [_tool("tool_a", ["capability_a"], "module.a", "function_a")]
    a = _project(registry=registry, target="scripts/a.py")
    renamed = _project(registry=registry, target="scripts/unrelated.py")
    assert a["allowed_tools"] == renamed["allowed_tools"]
