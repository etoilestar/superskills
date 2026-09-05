import pytest

from backend.services.platform_io_contract import (
    build_platform_io_contract,
    commit_platform_output_emissions,
    normalize_platform_output_sinks,
    project_and_commit_platform_outputs,
)
from backend.services.creator.responsibility_graph_expansion import (
    ResponsibilityGraphExpansionError,
    validate_responsibility_graph_candidate,
)


def _contract(declaration):
    return {"platform_skill_boundary": {
        "input_envelope_fields": [],
        "final_output_fields": [declaration],
        "required_final_output_fields": ["sink_a"],
    }}


def _items():
    return [
        {"target_file": "scripts/unit_a.py", "role": "script", "purpose": "unit_a", "inputs": [], "outputs": ["value_x"], "default_values": {}, "required_capabilities": [], "constraints": []},
        {"target_file": "scripts/unit_b.py", "role": "script", "purpose": "unit_b", "inputs": [], "outputs": ["value_y"], "default_values": {}, "required_capabilities": [], "constraints": []},
    ]


def _plan(sink="sink_a"):
    return {"interfaces": [
        {"interface_id": "I1", "kind": "member_to_platform", "source_member": "scripts/unit_a.py", "source_output": "value_x", "target_platform_output": sink, "goal": "deliver"},
        {"interface_id": "I2", "kind": "member_to_platform", "source_member": "scripts/unit_b.py", "source_output": "value_y", "target_platform_output": sink, "goal": "deliver"},
    ]}


@pytest.mark.parametrize("declaration", [
    "sink_a",
    {"name": "sink_a", "value_schema": {"type": "string"}, "cardinality": "one", "write_semantics": "single"},
])
def test_single_sink_rejects_multiple_terminal_emissions(declaration):
    with pytest.raises(ResponsibilityGraphExpansionError) as raised:
        validate_responsibility_graph_candidate(
            function_items=_items(), platform_contract=_contract(declaration), interface_plan=_plan(),
        )
    assert raised.value.code == "duplicate_terminal_provenance"


@pytest.mark.parametrize("write_semantics", ["append", "collect"])
def test_many_sink_accepts_multiple_terminal_emissions(write_semantics):
    contract = _contract({
        "name": "sink_a", "value_schema": {"type": "string"},
        "cardinality": "many", "write_semantics": write_semantics,
    })
    edges = validate_responsibility_graph_candidate(
        function_items=_items(), platform_contract=contract, interface_plan=_plan(),
    )
    assert [edge["from_node"] for edge in edges] == ["scripts/unit_a.py", "scripts/unit_b.py"]


def test_legacy_sink_normalization_is_single_and_name_independent():
    for name in ("sink_q7", "sink_m2"):
        sink = normalize_platform_output_sinks(_contract(name))[0]
        assert sink["name"] == name
        assert (sink["cardinality"], sink["write_semantics"]) == ("one", "single")


def test_commit_composes_already_ordered_emissions():
    contract = _contract({
        "name": "sink_a", "value_schema": {"type": "string"},
        "cardinality": "many", "write_semantics": "append",
    })
    committed = commit_platform_output_emissions(contract, [
        {"sink": "sink_a", "value": "value_y"},
        {"sink": "sink_a", "value": "value_x"},
    ])
    assert committed == {"sink_a": "value_yvalue_x"}


def test_runtime_projection_uses_terminal_edge_order_not_completion_order():
    contract = _contract({
        "name": "sink_a", "value_schema": {"type": "string"},
        "cardinality": "many", "write_semantics": "append",
    })
    edges = validate_responsibility_graph_candidate(
        function_items=_items(), platform_contract=contract, interface_plan=_plan(),
    )
    # Completion insertion order is deliberately the reverse of Graph order.
    completed = {
        "scripts/unit_b.py": {"value_y": "value_y"},
        "scripts/unit_a.py": {"value_x": "value_x"},
    }
    assert project_and_commit_platform_outputs(contract, edges, completed) == {
        "sink_a": "value_xvalue_y",
    }


def test_runtime_projection_collect_preserves_value_identity():
    contract = _contract({
        "name": "sink_a", "value_schema": {"type": "string"},
        "cardinality": "many", "write_semantics": "collect",
    })
    edges = validate_responsibility_graph_candidate(
        function_items=_items(), platform_contract=contract, interface_plan=_plan(),
    )
    completed = {
        "scripts/unit_b.py": {"value_y": "value_y"},
        "scripts/unit_a.py": {"value_x": "value_x"},
    }
    assert project_and_commit_platform_outputs(contract, edges, completed) == {
        "sink_a": ["value_x", "value_y"],
    }


def test_legacy_runtime_commit_rejects_multiple_emissions():
    with pytest.raises(ValueError, match="multiple emissions for single sink"):
        commit_platform_output_emissions(_contract("sink_a"), [
            {"sink": "sink_a", "value": "value_x"},
            {"sink": "sink_a", "value": "value_y"},
        ])


def test_real_platform_contract_declares_runtime_capabilities_canonically():
    sinks = normalize_platform_output_sinks(build_platform_io_contract())
    assert all({"name", "semantic_type", "accepted_source_types", "allowed_transforms",
                "value_schema", "cardinality", "write_semantics", "output_encoding"} == set(sink)
               for sink in sinks)
    by_name = {sink["name"]: sink for sink in sinks}
    assert by_name["text"]["allowed_transforms"] == ["text_render"]
    assert by_name["json"]["accepted_source_types"] == ["object", "json"]
    assert by_name["file_outputs"]["semantic_type"] == "file"
    assert (by_name["text"]["cardinality"], by_name["text"]["write_semantics"]) == ("many", "append")
    assert (by_name["file_outputs"]["cardinality"], by_name["file_outputs"]["write_semantics"]) == ("one", "single")


def test_unrelated_sinks_commit_independently():
    contract = {"final_output_fields": [
        {"name": "sink_a", "value_schema": {"type": "string"}, "cardinality": "one", "write_semantics": "single"},
        {"name": "sink_b", "value_schema": {"type": "string"}, "cardinality": "one", "write_semantics": "single"},
    ]}
    assert commit_platform_output_emissions(contract, [
        {"sink": "sink_b", "value": "value_y"},
        {"sink": "sink_a", "value": "value_x"},
    ]) == {"sink_a": "value_x", "sink_b": "value_y"}


def test_many_platform_sink_does_not_relax_internal_input_provenance():
    contract = _contract({
        "name": "sink_a", "value_schema": {"type": "string"},
        "cardinality": "many", "write_semantics": "append",
    })
    items = _items() + [{
        "target_file": "scripts/unit_c.py", "role": "script", "purpose": "unit_c",
        "inputs": ["input_x"], "outputs": ["value_z"], "default_values": {},
        "required_capabilities": [], "constraints": [],
    }]
    plan = {"interfaces": [
        {"interface_id": "I1", "kind": "member_to_member", "source_member": "scripts/unit_a.py", "source_output": "value_x", "target_member": "scripts/unit_c.py", "target_input": "input_x", "goal": "deliver"},
        {"interface_id": "I2", "kind": "member_to_member", "source_member": "scripts/unit_b.py", "source_output": "value_y", "target_member": "scripts/unit_c.py", "target_input": "input_x", "goal": "deliver"},
        {"interface_id": "I3", "kind": "member_to_platform", "source_member": "scripts/unit_c.py", "source_output": "value_z", "target_platform_output": "sink_a", "goal": "deliver"},
    ]}
    with pytest.raises(ResponsibilityGraphExpansionError) as raised:
        validate_responsibility_graph_candidate(
            function_items=items, platform_contract=contract, interface_plan=plan,
        )
    assert raised.value.code == "duplicate_input_provenance"
