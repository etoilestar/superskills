"""Deterministic, read-only contract graph extraction from canonical artifacts."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ...skill_plan import SkillPlan
from ..command_normalizer import parse_skill_md_bash_command_blocks


NodeType = Literal[
    "platform_input",
    "platform_output",
    "function_item",
    "skill_command",
    "script_interface",
]
EdgeType = Literal["flows_to", "binds_to"]
OwnerType = Literal["ResponsibilityGraph", "SKILL.md", "Script", "Platform"]


@dataclass(frozen=True)
class ContractNode:
    id: str
    node_type: NodeType
    artifact_path: str = ""
    symbol: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ContractEdge:
    id: str
    edge_type: EdgeType
    source: str
    target: str
    owner: OwnerType
    source_port: str = ""
    target_port: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class ContractGraph:
    nodes: list[ContractNode] = field(default_factory=list)
    edges: list[ContractEdge] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a stable wire representation independent of insertion order."""
        return {
            "nodes": [asdict(node) for node in sorted(self.nodes, key=lambda item: item.id)],
            "edges": [asdict(edge) for edge in sorted(self.edges, key=lambda item: item.id)],
        }


def add_platform_nodes(graph: ContractGraph) -> None:
    graph.nodes.extend([
        ContractNode(id="platform:input", node_type="platform_input"),
        ContractNode(id="platform:output", node_type="platform_output"),
    ])


def add_function_item_nodes(graph: ContractGraph, skill_plan: SkillPlan) -> None:
    for item in skill_plan.function_items:
        path = str(item.get("target_file", ""))
        graph.nodes.append(ContractNode(
            id=f"function:{path}",
            node_type="function_item",
            artifact_path=path,
            symbol=path,
            attributes=deepcopy({
                key: item.get(key, {} if key == "default_values" else [])
                for key in (
                    "role", "purpose", "inputs", "outputs", "default_values",
                    "required_capabilities", "constraints",
                )
            }),
        ))


def add_script_interface_nodes(graph: ContractGraph, skill_plan: SkillPlan) -> None:
    for entry in skill_plan.files:
        if entry.file_kind != "script":
            continue
        graph.nodes.append(ContractNode(
            id=f"script_interface:{entry.path}",
            node_type="script_interface",
            artifact_path=entry.path,
            symbol=entry.entrypoint,
            attributes=deepcopy({
                "inputs": entry.inputs,
                "outputs": entry.outputs,
                "runtime": entry.runtime,
                "runtime_contract": entry.runtime_contract,
                "artifact_contract": entry.artifact_contract,
                "command_arg_bindings": entry.command_arg_bindings,
            }),
        ))


def _endpoint_id(endpoint: object) -> str:
    value = str(endpoint or "")
    if value == "platform_input_node":
        return "platform:input"
    if value == "platform_output_node":
        return "platform:output"
    return f"function:{value}"


def add_responsibility_edges(graph: ContractGraph, skill_plan: SkillPlan) -> None:
    for edge in skill_plan.responsibility_edges:
        from_node = str(edge.get("from_node", ""))
        from_output = str(edge.get("from_output", ""))
        to_node = str(edge.get("to_node", ""))
        to_input = str(edge.get("to_input", ""))
        graph.edges.append(ContractEdge(
            id=f"flow:{from_node}:{from_output}->{to_node}:{to_input}",
            edge_type="flows_to",
            source=_endpoint_id(from_node),
            target=_endpoint_id(to_node),
            owner="ResponsibilityGraph",
            source_port=from_output,
            target_port=to_input,
            attributes=deepcopy({
                "purpose": edge.get("purpose", ""),
                "constraints": edge.get("constraints", []),
            }),
        ))


def add_skill_command_nodes(graph: ContractGraph, skill_md: str) -> None:
    for command in parse_skill_md_bash_command_blocks(skill_md):
        if not command.script_path:
            continue
        path = command.script_path
        graph.nodes.append(ContractNode(
            id=f"skill_command:{path}",
            node_type="skill_command",
            artifact_path="SKILL.md",
            symbol=path,
            attributes={
                "command": command.content,
                "start": command.start,
                "end": command.end,
                "script_path": path,
            },
        ))
        graph.edges.append(ContractEdge(
            id=f"binding:SKILL.md:{path}->{path}",
            edge_type="binds_to",
            source=f"skill_command:{path}",
            target=f"script_interface:{path}",
            owner="SKILL.md",
        ))


def validate_contract_graph(graph: ContractGraph) -> None:
    """Reject malformed or dangling facts without changing the graph."""
    node_ids = [node.id for node in graph.nodes]
    edge_ids = [edge.id for edge in graph.edges]
    if any(not node_id for node_id in node_ids):
        raise ValueError("contract graph node id must not be empty")
    if len(node_ids) != len(set(node_ids)):
        raise ValueError("contract graph node ids must be unique")
    if any(not edge_id for edge_id in edge_ids):
        raise ValueError("contract graph edge id must not be empty")
    if len(edge_ids) != len(set(edge_ids)):
        raise ValueError("contract graph edge ids must be unique")

    known_nodes = set(node_ids)
    for edge in graph.edges:
        if not edge.source or not edge.target:
            raise ValueError(f"contract graph edge {edge.id!r} endpoints must not be empty")
        if not edge.owner:
            raise ValueError(f"contract graph edge {edge.id!r} must have an owner")
        if edge.source not in known_nodes:
            raise ValueError(f"contract graph edge {edge.id!r} has missing source {edge.source!r}")
        if edge.target not in known_nodes:
            raise ValueError(f"contract graph edge {edge.id!r} has missing target {edge.target!r}")


def build_contract_graph(*, skill_plan: SkillPlan, skill_md: str) -> ContractGraph:
    graph = ContractGraph()
    add_platform_nodes(graph)
    add_function_item_nodes(graph, skill_plan)
    add_script_interface_nodes(graph, skill_plan)
    add_responsibility_edges(graph, skill_plan)
    add_skill_command_nodes(graph, skill_md)
    validate_contract_graph(graph)
    return graph
