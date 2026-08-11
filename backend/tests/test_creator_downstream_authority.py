import pytest

from backend.services.creator.execution_contract import (
    project_frozen_script_contract,
    project_frozen_skill_tool_contract,
    validate_script_execution_contract,
)
from backend.services.creator.runtime_import_guard import guard_runtime_imports
from backend.services.creator.tool_pool_models import ToolPoolModel, ToolPoolTool
from backend.services.creator_tool_registry import (
    ToolCapability,
    ToolFunctionManifest,
    clear_registered_tool_capabilities,
    register_tool_capability,
)


def _register(tool_id, module, function):
    register_tool_capability(ToolCapability(
        name=tool_id, display_name=tool_id, category="abstract",
        functions=[ToolFunctionManifest(
            function_name=function, import_path=module, short_description="",
            when_to_use="", signature=f"{function}(value: str) -> dict",
        )],
    ))


@pytest.fixture(autouse=True)
def registry():
    clear_registered_tool_capabilities()
    _register("tool_a", "module.a", "function_a")
    _register("tool_b", "module.b", "function_b")
    _register("tool_c", "module.c", "function_c")
    yield
    clear_registered_tool_capabilities()


def _tool_contract(*tool_ids):
    return project_frozen_skill_tool_contract(ToolPoolModel(
        tools=[ToolPoolTool(tool_id=tool_id, status="allowed") for tool_id in tool_ids]
    ))


def _script(target="scripts/a.py", inputs=("input_a",), outputs=("output_a",), responsibility="a"):
    return project_frozen_script_contract(
        target_file=target, inputs=inputs, outputs=outputs, responsibility=responsibility,
    )


def _source(input_fields=("input_a",), output_fields=("output_a",), tool=None):
    import_line, call_line = "", ""
    if tool:
        module, function = tool
        import_line = f"from {module} import {function}\n"
        call_line = f"    {function}('x')\n"
    spec = ", ".join(repr(key) + ": {}" for key in input_fields)
    result = ", ".join(repr(key) + ": 1" for key in output_fields)
    return f"{import_line}def run():\n    strict_json_argv_guard({{}}, {{{spec}}})\n{call_line}    return {{{result}}}\n"


def test_whole_skill_toolpool_is_shared_by_all_scripts():
    tools = _tool_contract("tool_a", "tool_b")
    assert tools["allowed_tool_ids"] == ["tool_a", "tool_b"]
    for script in (_script(target="scripts/a.py"), _script(target="scripts/b.py", responsibility="different")):
        for callable_pair in (("module.a", "function_a"), ("module.b", "function_b")):
            assert validate_script_execution_contract(_source(tool=callable_pair), script_contract=script, tool_contract=tools)["success"]


def test_registry_known_tool_outside_frozen_skill_pool_is_rejected():
    result = validate_script_execution_contract(
        _source(tool=("module.c", "function_c")),
        script_contract=_script(), tool_contract=_tool_contract("tool_a", "tool_b"),
    )
    assert "unauthorized_callable" in [failure["code"] for failure in result["failures"]]


def test_function_item_semantics_do_not_change_tool_authority():
    tools = _tool_contract("tool_a", "tool_b")
    first = _script(target="scripts/a.py", responsibility="capability_x")
    second = _script(target="scripts/z.py", responsibility="unknown_business_capability")
    assert first["script_contract_digest"] != second["script_contract_digest"]
    assert tools == _tool_contract("tool_a", "tool_b")


def test_script_contracts_have_independent_io_but_share_tool_domain():
    tools = _tool_contract("tool_a", "tool_b")
    a = _script(inputs=["a"], outputs=["b"])
    b = _script(target="scripts/b.py", inputs=["c"], outputs=["d"])
    assert validate_script_execution_contract(_source(["a"], ["b"]), script_contract=a, tool_contract=tools)["success"]
    assert validate_script_execution_contract(_source(["c"], ["d"]), script_contract=b, tool_contract=tools)["success"]


def test_interface_order_is_irrelevant_and_diff_is_structured():
    script = _script(inputs=["a", "b"], outputs=["x", "y"])
    tools = _tool_contract("tool_a")
    assert validate_script_execution_contract(_source(["b", "a"], ["y", "x"]), script_contract=script, tool_contract=tools)["success"]
    result = validate_script_execution_contract(_source(["b", "c"], ["x"]), script_contract=script, tool_contract=tools)
    input_failure = next(item for item in result["failures"] if item["code"] == "script_input_contract_mismatch")
    assert input_failure["missing"] == ["a"] and input_failure["unexpected"] == ["c"]


def test_interfaces_fail_closed_when_not_statically_verifiable():
    result = validate_script_execution_contract(
        "def run():\n    return build_result()\n",
        script_contract=_script(), tool_contract=_tool_contract("tool_a"),
    )
    assert {item["code"] for item in result["failures"]} == {
        "script_input_contract_unverifiable", "script_output_contract_unverifiable",
    }


def test_output_detection_is_limited_to_canonical_run():
    source = "def helper():\n    return {'output_a': 1}\n\ndef run():\n    strict_json_argv_guard({}, {'input_a': {}})\n    return build_result()\n"
    result = validate_script_execution_contract(source, script_contract=_script(), tool_contract=_tool_contract("tool_a"))
    assert "script_output_contract_unverifiable" in [item["code"] for item in result["failures"]]


def test_ordinary_library_calls_are_outside_tool_authorization():
    source = "import json\n\ndef run():\n    strict_json_argv_guard({}, {'input_a': {}})\n    json.dumps({})\n    return {'output_a': 1}\n"
    assert validate_script_execution_contract(source, script_contract=_script(), tool_contract=_tool_contract("tool_a"))["success"]


def test_runtime_guard_consumes_skill_contract_for_every_target():
    _register("tool_runtime", "backend.services.runtime_tools", "strict_json_argv_guard")
    tools = _tool_contract("tool_runtime")
    tools["allowed_callables"][0]["signature"] = "strict_json_argv_guard(payload: dict, spec: dict) -> dict"
    source = "from backend.services.runtime_tools import strict_json_argv_guard\nstrict_json_argv_guard({}, {})\n"
    assert guard_runtime_imports(source, "scripts/a.py", skill_tool_contract=tools).success
    assert guard_runtime_imports(source, "scripts/b.py", skill_tool_contract=tools).success
