import json

import pytest

from backend.services.creator.research.contract_graph import (
    ContractGraph,
    build_contract_graph,
)
from backend.services.skill_plan import SkillPlan, SkillPlanEntry


def _plan(first_output="text", second_input="source_text", edges=True):
    paths = ("scripts/extract.py", "scripts/summarize.py")
    files = [
        SkillPlanEntry(path=paths[0], file_type="script", file_kind="script", role="extractor", purpose="Extract", inputs=["request"], outputs=[first_output], runtime="python", runtime_contract={"argv": "json"}, artifact_contract={"format": "json"}, command_arg_bindings=[{"arg": "request"}]),
        SkillPlanEntry(path=paths[1], file_type="script", file_kind="script", role="summarizer", purpose="Summarize", inputs=[second_input], outputs=["result"], runtime="python"),
    ]
    items = [
        {"target_file": paths[0], "role": "extractor", "purpose": "Extract", "inputs": ["request"], "outputs": [first_output], "default_values": {}, "required_capabilities": [], "constraints": []},
        {"target_file": paths[1], "role": "summarizer", "purpose": "Summarize", "inputs": [second_input], "outputs": ["result"], "default_values": {}, "required_capabilities": [], "constraints": []},
    ]
    graph_edges = [
        {"from_node": "platform_input_node", "from_output": "request", "to_node": paths[0], "to_input": "request", "purpose": "supply", "constraints": []},
        {"from_node": paths[0], "from_output": first_output, "to_node": paths[1], "to_input": second_input, "purpose": "handoff", "constraints": ["preserve"]},
        {"from_node": paths[1], "from_output": "result", "to_node": "platform_output_node", "to_input": "result", "purpose": "return", "constraints": []},
    ] if edges else []
    return SkillPlan(skill_name="two-stage", files=files, function_items=items, responsibility_edges=graph_edges)


SKILL_MD = """```bash
python scripts/extract.py '{}'
```
```bash
python scripts/summarize.py '{}'
```
"""


def test_minimal_two_stage_skill_has_all_artifact_nodes():
    graph = build_contract_graph(skill_plan=_plan(), skill_md=SKILL_MD)
    assert {node.id for node in graph.nodes} >= {
        "platform:input", "platform:output", "function:scripts/extract.py",
        "function:scripts/summarize.py", "script_interface:scripts/extract.py",
        "script_interface:scripts/summarize.py", "skill_command:scripts/extract.py",
        "skill_command:scripts/summarize.py",
    }


def test_responsibility_edge_preserves_ports_and_owner():
    graph = build_contract_graph(skill_plan=_plan(), skill_md=SKILL_MD)
    edge = next(edge for edge in graph.edges if edge.id == "flow:scripts/extract.py:text->scripts/summarize.py:source_text")
    assert (edge.edge_type, edge.owner, edge.source_port, edge.target_port) == ("flows_to", "ResponsibilityGraph", "text", "source_text")


def test_skill_binding_has_explicit_owner():
    graph = build_contract_graph(skill_plan=_plan(), skill_md=SKILL_MD)
    edge = next(edge for edge in graph.edges if edge.id == "binding:SKILL.md:scripts/extract.py->scripts/extract.py")
    assert (edge.edge_type, edge.owner) == ("binds_to", "SKILL.md")


def test_serialization_is_deterministic():
    first = build_contract_graph(skill_plan=_plan(), skill_md=SKILL_MD).to_dict()
    second = build_contract_graph(skill_plan=_plan(), skill_md=SKILL_MD).to_dict()
    assert first == second
    assert json.dumps(first, sort_keys=True, ensure_ascii=False) == json.dumps(second, sort_keys=True, ensure_ascii=False)


def test_equal_port_names_do_not_infer_dataflow():
    plan = _plan(first_output="text", second_input="text", edges=False)
    graph = build_contract_graph(skill_plan=plan, skill_md="")
    assert not any(edge.source == "function:scripts/extract.py" and edge.target == "function:scripts/summarize.py" for edge in graph.edges)


def _structural_signature(graph: ContractGraph):
    node_types = {node.id: node.node_type for node in graph.nodes}
    return sorted((edge.edge_type, node_types[edge.source], node_types[edge.target], edge.owner) for edge in graph.edges)


def test_identifier_randomization_preserves_structure():
    named = build_contract_graph(skill_plan=_plan("input_text", "summary"), skill_md=SKILL_MD)
    randomized = build_contract_graph(skill_plan=_plan("q17", "zeta_83"), skill_md=SKILL_MD)
    assert _structural_signature(named) == _structural_signature(randomized)


def test_missing_responsibility_endpoint_fails():
    plan = _plan()
    plan.responsibility_edges[0] = {"from_node": "scripts/not_exist.py", "from_output": "x", "to_node": "scripts/extract.py", "to_input": "request", "purpose": "bad", "constraints": []}
    with pytest.raises(ValueError, match="missing source"):
        build_contract_graph(skill_plan=plan, skill_md="")


def test_missing_skill_command_interface_fails():
    skill_md = "```bash\npython scripts/missing.py '{}'\n```\n"
    with pytest.raises(ValueError, match="missing target"):
        build_contract_graph(skill_plan=_plan(), skill_md=skill_md)
