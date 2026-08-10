import pytest

from backend.services.platform_io_contract import (
    commit_platform_output_emissions,
    normalize_platform_output_sinks,
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


def test_transport_commit_uses_emission_declaration_order():
    contract = _contract({
        "name": "sink_a", "value_schema": {"type": "string"},
        "cardinality": "many", "write_semantics": "append",
    })
    committed = commit_platform_output_emissions(contract, [
        {"sink": "sink_a", "value": "value_y"},
        {"sink": "sink_a", "value": "value_x"},
    ])
    assert committed == {"sink_a": "value_yvalue_x"}


def test_unrelated_sinks_commit_independently():
    contract = {"final_output_fields": [
        {"name": "sink_a", "value_schema": {"type": "string"}, "cardinality": "one", "write_semantics": "single"},
        {"name": "sink_b", "value_schema": {"type": "string"}, "cardinality": "one", "write_semantics": "single"},
    ]}
    assert commit_platform_output_emissions(contract, [
        {"sink": "sink_b", "value": "value_y"},
        {"sink": "sink_a", "value": "value_x"},
    ]) == {"sink_a": "value_x", "sink_b": "value_y"}
