"""Deterministic compilation of a frozen Creator ResponsibilityGraph.

This module deliberately does not plan nodes or infer topology from prose.  It
accepts the executable target domain and structural facts frozen by the
Blueprint/FilePlan owner, then either commits an immutable graph or reports
protocol issues.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence


TYPE_ALIASES = {
    "": "unknown",
    "any": "unknown",
    "none": "unknown",
    "null": "unknown",
    "int": "integer",
    "float": "number",
    "double": "number",
    "str": "string",
    "bool": "boolean",
}


def normalize_type_descriptor(raw_type: Any) -> str:
    """Normalize universal aliases while preserving open custom descriptors."""
    value = str(raw_type or "").strip().lower()
    return TYPE_ALIASES.get(value, value or "unknown")


class TypeCompatibility(str, Enum):
    COMPATIBLE = "compatible"
    UNKNOWN = "unknown"
    INCOMPATIBLE = "incompatible"


def type_compatibility(
    source_type: Any,
    target_type: Any,
    *,
    subtype_relations: Iterable[tuple[str, str]] = (),
    incompatible_relations: Iterable[tuple[str, str]] = (),
) -> TypeCompatibility:
    """Return a three-valued result without treating unfamiliar types as errors."""
    source = normalize_type_descriptor(source_type)
    target = normalize_type_descriptor(target_type)
    if "unknown" in (source, target):
        return TypeCompatibility.UNKNOWN
    if source == target or (source, target) == ("integer", "number"):
        return TypeCompatibility.COMPATIBLE
    subtypes = {
        (normalize_type_descriptor(child), normalize_type_descriptor(parent))
        for child, parent in subtype_relations
    }
    if (source, target) in subtypes:
        return TypeCompatibility.COMPATIBLE
    incompatible = {
        (normalize_type_descriptor(left), normalize_type_descriptor(right))
        for left, right in incompatible_relations
    }
    if (source, target) in incompatible or (target, source) in incompatible:
        return TypeCompatibility.INCOMPATIBLE
    return TypeCompatibility.UNKNOWN


@dataclass(frozen=True)
class Port:
    name: str
    value_type: str = "unknown"
    semantic_id: str = ""

    @classmethod
    def freeze(cls, raw: Any) -> "Port":
        if isinstance(raw, str):
            return cls(name=raw.strip())
        data = dict(raw or {})
        return cls(
            name=str(data.get("name") or data.get("id") or "").strip(),
            value_type=normalize_type_descriptor(
                data.get("value_type", data.get("type"))
            ),
            semantic_id=str(data.get("semantic_id") or "").strip(),
        )


@dataclass(frozen=True)
class Node:
    target_file: str
    inputs: tuple[Port, ...] = ()
    outputs: tuple[Port, ...] = ()


@dataclass(frozen=True)
class Edge:
    from_node: str
    from_output: str
    to_node: str
    to_input: str
    provenance: str = "explicit_binding"


@dataclass(frozen=True)
class GraphIssue:
    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)


class GraphProtocolError(ValueError):
    def __init__(self, issues: Sequence[GraphIssue]):
        self.issues = tuple(issues)
        super().__init__("; ".join(f"{issue.code}: {issue.message}" for issue in issues))


@dataclass(frozen=True)
class GraphDraft:
    nodes: tuple[Node, ...]

    @classmethod
    def freeze(
        cls,
        function_items: Sequence[Any],
        *,
        allowed_node_targets: Iterable[str],
    ) -> "GraphDraft":
        allowed = {str(value).strip() for value in allowed_node_targets if str(value).strip()}
        nodes: list[Node] = []
        seen: set[str] = set()
        issues: list[GraphIssue] = []
        for index, raw in enumerate(function_items):
            data = raw if isinstance(raw, Mapping) else vars(raw)
            target = str(data.get("target_file") or "").strip()
            if not target:
                issues.append(GraphIssue("empty_node_target", "target_file must not be empty", {"index": index}))
                continue
            if target in seen:
                issues.append(GraphIssue("duplicate_node_target", "target_file must be unique", {"target_file": target}))
                continue
            if target not in allowed:
                issues.append(GraphIssue("unauthorized_node_target", "target_file is outside the frozen executable domain", {"target_file": target}))
                continue
            seen.add(target)
            inputs = tuple(Port.freeze(port) for port in data.get("inputs", ()) or ())
            outputs = tuple(Port.freeze(port) for port in data.get("outputs", ()) or ())
            if any(not port.name for port in (*inputs, *outputs)):
                issues.append(GraphIssue("empty_port_name", "port names must not be empty", {"target_file": target}))
                continue
            nodes.append(Node(target, inputs, outputs))
        if issues:
            raise GraphProtocolError(issues)
        return cls(tuple(nodes))


@dataclass(frozen=True)
class CompiledGraph:
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]


CandidateSelector = Callable[[Sequence[Mapping[str, Any]]], Awaitable[Mapping[str, Any]]]


async def select_candidate(
    candidates: Sequence[Mapping[str, Any]], selector: CandidateSelector
) -> Mapping[str, Any] | None:
    """Select only an opaque candidate ID, with at most one protocol retry."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    by_id = {str(item.get("candidate_id") or ""): item for item in candidates}
    if "" in by_id or len(by_id) != len(candidates):
        raise GraphProtocolError((GraphIssue(
            "invalid_candidate_domain", "candidate IDs must be non-empty and unique"
        ),))
    for _attempt in range(2):
        response = await selector(tuple(
            {"candidate_id": candidate_id} for candidate_id in by_id
        ))
        if not isinstance(response, Mapping):
            continue
        decision = response.get("decision")
        if decision == "no_semantically_valid_candidate" and len(response) == 1:
            return None
        if decision == "selected" and set(response) == {
            "decision", "selected_candidate_id"
        }:
            selected = str(response.get("selected_candidate_id") or "")
            if selected in by_id:
                return by_id[selected]
    raise GraphProtocolError((GraphIssue(
        "candidate_selector_protocol", "selector failed the candidate-ID protocol twice"
    ),))


def compile_graph(draft: GraphDraft, bindings: Sequence[Mapping[str, Any]]) -> CompiledGraph:
    """Compile authoritative bindings; never mutate, add, or remove nodes."""
    node_ports = {
        node.target_file: ({port.name for port in node.inputs}, {port.name for port in node.outputs})
        for node in draft.nodes
    }
    edges: list[Edge] = []
    issues: list[GraphIssue] = []
    incoming: set[tuple[str, str]] = set()
    adjacency: dict[str, set[str]] = {node.target_file: set() for node in draft.nodes}
    boundaries = {"platform_input_node", "platform_output_node", "static_resource_node"}
    for index, raw in enumerate(bindings):
        edge = Edge(
            str(raw.get("from_node") or "").strip(),
            str(raw.get("from_output") or "").strip(),
            str(raw.get("to_node") or "").strip(),
            str(raw.get("to_input") or "").strip(),
            str(raw.get("provenance") or "explicit_binding").strip(),
        )
        source_ok = edge.from_node in boundaries or edge.from_node in node_ports
        target_ok = edge.to_node in boundaries or edge.to_node in node_ports
        if not source_ok or not target_ok:
            issues.append(GraphIssue("edge_boundary_issue", "edge endpoint is outside the frozen graph", {"index": index}))
            continue
        if edge.from_node in node_ports and edge.from_output not in node_ports[edge.from_node][1]:
            issues.append(GraphIssue("topology_contract_issue", "edge references an undeclared output", {"index": index}))
            continue
        if edge.to_node in node_ports and edge.to_input not in node_ports[edge.to_node][0]:
            issues.append(GraphIssue("topology_contract_issue", "edge references an undeclared input", {"index": index}))
            continue
        key = (edge.to_node, edge.to_input)
        if key in incoming:
            issues.append(GraphIssue("duplicate_provenance", "an input has more than one provenance", {"index": index, "target": key}))
            continue
        incoming.add(key)
        edges.append(edge)
        if edge.from_node in node_ports and edge.to_node in node_ports:
            adjacency[edge.from_node].add(edge.to_node)

    visiting: set[str] = set()
    visited: set[str] = set()
    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        cyclic = any(visit(child) for child in adjacency[node])
        visiting.remove(node)
        visited.add(node)
        return cyclic
    if any(visit(node) for node in adjacency):
        issues.append(GraphIssue("graph_cycle", "workflow topology must be acyclic"))
    if issues:
        raise GraphProtocolError(issues)
    return CompiledGraph(draft.nodes, tuple(edges))
