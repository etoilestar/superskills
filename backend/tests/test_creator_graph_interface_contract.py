import pytest

from backend.services.creator.common import (
    graph_interface_contract,
    project_script_interface_contract,
    validate_graph_interface_projection,
)


def graph():
    return {
        "requirements": [{
            "target_file": "scripts/compare_csvs.py",
            "purpose": "Compare CSV files.",
            "inputs": [{"name": "input_files", "type": "list[file_path]", "shape": "list", "required": True, "source": "platform_input"}],
            "outputs": [{"name": "text", "type": "string", "required": True}],
        }],
        "dataflow_edges": [
            {"from_node": "platform_input_node", "from_output": "payload", "to_node": "scripts/compare_csvs.py", "to_input": "input_files", "purpose": "input", "constraints": [], "mapping": {"source": "payload", "target": "input_files", "type": "list[file_path]"}},
            {"from_node": "scripts/compare_csvs.py", "from_output": "text", "to_node": "platform_output_node", "to_input": "text", "purpose": "output", "constraints": [], "mapping": {"source": "text", "target": "text", "type": "string"}},
        ],
    }


def test_graph_contract_preserves_ports_shape_and_explicit_mapping():
    contract = graph_interface_contract(graph())
    assert contract["input_ports"] == [{"name": "input_files", "direction": "input", "type": "list[file_path]", "shape": "list", "required": True, "source": "platform_input", "consumer": "scripts/compare_csvs.py"}]
    assert contract["output_ports"] == [{"name": "text", "direction": "output", "type": "string", "shape": "scalar", "required": True, "producer": "scripts/compare_csvs.py", "terminal": True}]
    assert contract["edge_mappings"][1]["mapping"] == {"source": "text", "target": "text", "type": "string", "conversion": "identity"}


def test_all_derived_contracts_are_exact_graph_projections():
    projected = project_script_interface_contract(graph(), "scripts/compare_csvs.py")
    assert projected["command_payload"] == {"input_files": "{{input_files}}"}
    assert projected["argv_schema"]["properties"]["input_files"]["type"] == "array"
    assert projected["stdout_schema"]["required"] == ["text"]
    validate_graph_interface_projection(graph(), "scripts/compare_csvs.py", projected)
    with pytest.raises(ValueError, match="does not equal"):
        validate_graph_interface_projection(graph(), "scripts/compare_csvs.py", {**projected, "stdout_schema": {}})


def test_graph_projection_preserves_and_passes_optional_default():
    value = graph()
    value["requirements"][0]["inputs"].append({
        "name": "limit", "type": "integer", "required": False,
    })
    value["requirements"][0]["default_values"] = {"limit": 3}
    value["dataflow_edges"].insert(1, {
        "from_node": "platform_input_node", "from_output": "options",
        "to_node": "scripts/compare_csvs.py", "to_input": "limit",
        "purpose": "optional input", "constraints": [],
        "mapping": {"source": "options", "target": "limit", "type": "integer"},
    })

    contract = graph_interface_contract(value)
    limit = next(port for port in contract["input_ports"] if port["name"] == "limit")
    projected = project_script_interface_contract(value, "scripts/compare_csvs.py")

    assert limit["default"] == 3
    assert projected["argv_schema"]["properties"]["limit"]["default"] == 3
    assert projected["command_payload"]["limit"] == 3


def test_member_stdout_ports_are_not_collapsed_into_platform_output_field():
    value = graph()
    value["requirements"][0]["outputs"] = [
        {"name": "report_json_path", "type": "file_path", "required": True},
        {"name": "report_csv_path", "type": "file_path", "required": True},
    ]
    value["dataflow_edges"][1:] = [
        {"from_node": "scripts/compare_csvs.py", "from_output": name, "to_node": "platform_output_node", "to_input": "file_outputs", "purpose": "output", "constraints": [], "mapping": {"source": name, "target": "file_outputs", "type": "file_path"}}
        for name in ("report_json_path", "report_csv_path")
    ]

    projected = project_script_interface_contract(value, "scripts/compare_csvs.py")

    assert projected["stdout_schema"]["properties"] == {
        "report_json_path": {"type": "file_path", "x-graph-type": "file_path", "x-shape": "scalar"},
        "report_csv_path": {"type": "file_path", "x-graph-type": "file_path", "x-shape": "scalar"},
    }
    assert projected["platform_output_mapping"] == {
        "file_outputs": ["report_json_path", "report_csv_path"]
    }
    validate_graph_interface_projection(value, "scripts/compare_csvs.py", projected)


def test_validation_rejects_platform_fan_in_used_as_member_stdout_field():
    value = graph()
    value["requirements"][0]["outputs"] = ["report_json_path", "report_csv_path"]
    value["dataflow_edges"][1:] = [
        {"from_node": "scripts/compare_csvs.py", "from_output": name, "to_node": "platform_output_node", "to_input": "file_outputs", "purpose": "output", "constraints": []}
        for name in ("report_json_path", "report_csv_path")
    ]
    projected = project_script_interface_contract(value, "scripts/compare_csvs.py")
    collapsed = {
        **projected,
        "stdout_schema": {
            "type": "object",
            "properties": {"file_outputs": {"type": "array"}},
            "required": ["file_outputs"],
            "additionalProperties": False,
        },
    }

    with pytest.raises(ValueError, match="multiple graph output ports"):
        validate_graph_interface_projection(value, "scripts/compare_csvs.py", collapsed)
