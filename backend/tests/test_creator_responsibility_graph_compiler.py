import pytest

from backend.services.creator.responsibility_graph import (
    GraphDraft, GraphProtocolError, TypeCompatibility, compile_graph,
    normalize_type_descriptor, select_candidate, type_compatibility,
)


def _node(path, inputs=(), outputs=()):
    return {"target_file": path, "inputs": list(inputs), "outputs": list(outputs)}


def _port(name, value_type="unknown", semantic_id=""):
    return {"name": name, "value_type": value_type, "semantic_id": semantic_id}


def test_freeze_uses_authoritative_domain_not_file_suffix():
    targets = {"scripts/process.sh", "scripts/render.js", "scripts/query.sql"}
    draft = GraphDraft.freeze([_node(path) for path in targets], allowed_node_targets=targets)
    assert {node.target_file for node in draft.nodes} == targets
    with pytest.raises(GraphProtocolError, match="unauthorized_node_target"):
        GraphDraft.freeze([_node("assets/input.csv")], allowed_node_targets=targets)


def test_open_port_descriptors_survive_freeze_and_compare_without_hard_failure():
    types = ("image", "audio", "dataframe", "custom_record")
    draft = GraphDraft.freeze(
        [_node("scripts/x.sh", inputs=[_port(value, value) for value in types])],
        allowed_node_targets={"scripts/x.sh"},
    )
    assert [port.value_type for port in draft.nodes[0].inputs] == list(types)
    assert normalize_type_descriptor(" int ") == "integer"
    assert type_compatibility("custom_record", "custom_record") == TypeCompatibility.COMPATIBLE
    assert type_compatibility("custom_a", "custom_b") == TypeCompatibility.UNKNOWN


def test_compiles_parallel_fan_out_and_fan_in_from_structure_only():
    nodes = [
        _node("scripts/a.sql", outputs=["one", "two"]),
        _node("scripts/b.js", inputs=["left"], outputs=["b"]),
        _node("scripts/c.sh", inputs=["right"], outputs=["c"]),
        _node("scripts/d.bin", inputs=["b", "c"], outputs=["done"]),
    ]
    draft = GraphDraft.freeze(nodes, allowed_node_targets={n["target_file"] for n in nodes})
    edges = [
        {"from_node": "scripts/a.sql", "from_output": "one", "to_node": "scripts/b.js", "to_input": "left"},
        {"from_node": "scripts/a.sql", "from_output": "two", "to_node": "scripts/c.sh", "to_input": "right"},
        {"from_node": "scripts/b.js", "from_output": "b", "to_node": "scripts/d.bin", "to_input": "b"},
        {"from_node": "scripts/c.sh", "from_output": "c", "to_node": "scripts/d.bin", "to_input": "c"},
        {"from_node": "scripts/d.bin", "from_output": "done", "to_node": "platform_output_node", "to_input": "file_outputs"},
    ]
    compiled = compile_graph(draft, edges)
    assert compiled.nodes == draft.nodes
    assert len(compiled.edges) == 5


@pytest.mark.asyncio
async def test_single_candidate_never_calls_selector_and_protocol_has_one_retry():
    calls = 0
    async def selector(_domain):
        nonlocal calls
        calls += 1
        return ({"bad": True} if calls == 1 else
                {"decision": "selected", "selected_candidate_id": "C2"})
    one = {"candidate_id": "C1", "edge": 1}
    assert await select_candidate([one], selector) is one
    assert calls == 0
    selected = await select_candidate([one, {"candidate_id": "C2", "edge": 2}], selector)
    assert selected["edge"] == 2 and calls == 2
