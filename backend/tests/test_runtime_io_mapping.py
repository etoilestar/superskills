import json

import pytest

from backend.services.creator.runtime_io_mapping_plan import (
    RuntimeIOMappingError,
    execute_runtime_io_mapping,
    plan_runtime_io_mappings,
    runtime_capability_summary,
)
from backend.services.platform_io_contract import project_and_commit_platform_outputs


def item():
    return {"target_file": "scripts/asserter.py", "outputs": [{"name": "report_json", "contract": {"type": "object"}}]}


def interface(target):
    return {"interfaces": [{"interface_id": "I1", "kind": "member_to_platform", "source_member": "scripts/asserter.py", "source_output": "report_json", "target_platform_output": target}]}


def platform(name, schema):
    return {"platform_skill_boundary": {"final_output_fields": [{"name": name, "value_schema": schema, "cardinality": "one", "write_semantics": "single"}]}}


@pytest.mark.asyncio
async def test_object_to_file_outputs_materializes_json(tmp_path):
    contract = platform("file_outputs", {"type": "array", "items": {"type": "string"}})

    async def model(messages, model):
        return json.dumps({"mappings": [{
            "source": {"member": "scripts/asserter.py", "output": "report_json", "schema": {"type": "object"}},
            "target": {"platform_output": "file_outputs", "schema": {"type": "array", "items": {"type": "string"}}},
            "mode": "serialize_to_file", "operation": "serialize_json_file", "artifact_name": "report_json.json", "reason": "materialize object",
        }]})

    plan = await plan_runtime_io_mappings(canonical_interface_contract=interface("file_outputs"), function_items=[item()], platform_contract=contract, planner_model="test", model_call=model)
    value = {"matched_records": [], "diff": []}
    paths = execute_runtime_io_mapping(member="scripts/asserter.py", output="report_json", platform_output="file_outputs", value=value, mapping_plan=plan, output_dir=tmp_path)
    assert json.loads((tmp_path / "report_json.json").read_text()) == value
    assert paths == [str(tmp_path / "report_json.json")]


@pytest.mark.asyncio
async def test_object_to_text_json_stringifies():
    contract = platform("text", {"type": "string"})

    async def model(messages, model):
        return json.dumps({"mappings": [{
            "source": {"member": "scripts/asserter.py", "output": "report_json", "schema": {"type": "object"}},
            "target": {"platform_output": "text", "schema": {"type": "string"}},
            "mode": "json_stringify", "operation": "json_stringify", "reason": "text representation",
        }]})

    plan = await plan_runtime_io_mappings(canonical_interface_contract=interface("text"), function_items=[item()], platform_contract=contract, planner_model="test", model_call=model)
    result = execute_runtime_io_mapping(member="scripts/asserter.py", output="report_json", platform_output="text", value={"ok": True}, mapping_plan=plan, output_dir="unused")
    assert json.loads(result) == {"ok": True}


@pytest.mark.asyncio
async def test_planning_fails_when_capability_is_not_supported():
    contract = platform("file_outputs", {"type": "array", "items": {"type": "string"}})

    async def model(messages, model):
        return json.dumps({"mappings": [{
            "source": {"member": "scripts/asserter.py", "output": "report_json", "schema": {"type": "object"}},
            "target": {"platform_output": "file_outputs", "schema": {"type": "array", "items": {"type": "string"}}},
            "mode": "serialize_to_file", "operation": "serialize_json_file", "artifact_name": "report_json.json", "reason": "requested",
        }]})

    with pytest.raises(RuntimeIOMappingError, match="not supported") as exc:
        await plan_runtime_io_mappings(canonical_interface_contract=interface("file_outputs"), function_items=[item()], platform_contract=contract, planner_model="test", model_call=model, capabilities={"supported_mappings": []})
    assert exc.value.code == "runtime_io_mapping_planning_failure"


def test_runtime_never_guesses_missing_mapping(tmp_path):
    with pytest.raises(RuntimeIOMappingError) as exc:
        execute_runtime_io_mapping(member="scripts/asserter.py", output="report_json", platform_output="file_outputs", value={}, mapping_plan=None, output_dir=tmp_path)
    assert exc.value.code == "runtime_io_mapping_missing"


@pytest.mark.asyncio
async def test_terminal_commit_maps_before_sink_validation(tmp_path):
    contract = platform("file_outputs", {"type": "array", "items": {"type": "string"}})

    async def model(messages, model):
        return json.dumps({"mappings": [{
            "source": {"member": "scripts/asserter.py", "output": "report_json", "schema": {"type": "object"}},
            "target": {"platform_output": "file_outputs", "schema": {"type": "array", "items": {"type": "string"}}},
            "mode": "serialize_to_file", "operation": "serialize_json_file", "artifact_name": "report_json.json", "reason": "materialize",
        }]})

    plan = await plan_runtime_io_mappings(canonical_interface_contract=interface("file_outputs"), function_items=[item()], platform_contract=contract, planner_model="test", model_call=model)
    committed = project_and_commit_platform_outputs(
        contract,
        [{"from_node": "scripts/asserter.py", "from_output": "report_json", "to_node": "platform_output_node", "to_input": "file_outputs"}],
        {"scripts/asserter.py": {"report_json": {"ok": True}}},
        runtime_io_mapping_plan=plan,
        output_dir=str(tmp_path),
    )
    assert committed == {"file_outputs": [str(tmp_path / "report_json.json")]}
