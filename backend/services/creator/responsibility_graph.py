"""Deterministic compiler for Creator executable responsibility graphs.

The compiler deliberately treats FunctionItems as the graph's immutable node
contracts.  Models may resolve a choice between an already computed set of
source candidates, but they never author endpoints or complete edge sets.
"""

from __future__ import annotations

import copy
import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from ..platform_io_contract import build_platform_io_contract


ResolutionKind = Literal[
    "platform_runtime", "upstream_runtime", "local_default",
    "static_resolved", "unresolved",
]

ISSUE_TYPES = frozenset({
    "protocol_shape_error", "illegal_node", "illegal_port", "node_contract_gap",
    "ambiguous_source", "wrong_source_selection", "duplicate_provenance",
    "type_mismatch", "topology_contract_issue", "cycle_detected",
    "missing_platform_input", "missing_platform_output",
    "unreachable_final_output", "constraint_conflict", "final_output_contract_gap",
})


def port_id(node: str, direction: Literal["input", "output"], name: str) -> str:
    """Return the stable backend-owned identity for a frozen port."""
    return f"{node}::{direction}::{name}"


@dataclass(frozen=True)
class SourceCandidate:
    candidate_id: str
    source_node: str
    source_output: str
    source_port_id: str
    target_node: str
    target_input: str
    target_port_id: str
    compatibility_evidence: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.__dict__)


@dataclass
class GraphDraft:
    version: int = 1
    status: str = "draft"
    function_items: list[dict[str, Any]] = field(default_factory=list)
    input_resolutions: list[dict[str, Any]] = field(default_factory=list)
    source_domains: list[dict[str, Any]] = field(default_factory=list)
    compiled_edges: list[dict[str, Any]] = field(default_factory=list)
    ambiguous_targets: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    allowed_predecessors: dict[str, list[str]] = field(default_factory=dict)
    ports: list[dict[str, str]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def freeze(
        cls, function_items: list[dict[str, Any]], *,
        allowed_predecessors: dict[str, list[str]] | None = None,
    ) -> "GraphDraft":
        items = copy.deepcopy(function_items)
        targets = [str(item.get("target_file") or "") for item in items]
        if len(set(targets)) != len(targets) or any(not target.startswith("scripts/") for target in targets):
            raise ValueError("FunctionItem node domain must contain unique scripts/** targets")
        topology = allowed_predecessors or {
            target: targets[:index] for index, target in enumerate(targets)
        }
        ports = [
            {"port_id": port_id(target, direction, str(name)), "name": str(name),
             "direction": direction, "value_type": "unknown"}
            for item, target in zip(items, targets)
            for direction, names in (("input", item.get("inputs") or []),
                                     ("output", item.get("outputs") or []))
            for name in names
        ]
        return cls(function_items=items, allowed_predecessors=copy.deepcopy(topology), ports=ports)


@dataclass
class GraphTransaction:
    """Keep the last validated graph isolated from mutable candidates."""

    committed_graph: GraphDraft | None = None

    def candidate(self, function_items: list[dict[str, Any]], **kwargs: Any) -> GraphDraft:
        return GraphDraft.freeze(function_items, **kwargs)

    def commit(self, candidate: GraphDraft) -> GraphDraft:
        if candidate.status != "committed" or candidate.issues:
            raise ValueError("validation_failed candidate cannot replace committed graph")
        self.committed_graph = copy.deepcopy(candidate)
        return copy.deepcopy(self.committed_graph)


def _platform_fields(platform_contract: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    contract = platform_contract or build_platform_io_contract()
    boundary = contract.get("platform_skill_boundary") or {}
    return (list(boundary.get("input_envelope_fields") or []),
            list(boundary.get("final_output_fields") or []))


def compute_legal_source_domain(
    target_node: str, target_input: str, function_items: list[dict[str, Any]],
    platform_contract: dict[str, Any] | None,
    topology_constraints: dict[str, list[str]],
) -> list[SourceCandidate]:
    """Compute sources using only frozen topology and exact port compatibility."""
    input_fields, _ = _platform_fields(platform_contract)
    raw: list[tuple[str, str, str]] = []
    if target_input in input_fields:
        raw.append(("platform_input_node", target_input, "platform"))
    by_target = {str(item["target_file"]): item for item in function_items}
    for predecessor in topology_constraints.get(target_node, []):
        item = by_target.get(predecessor)
        if item and target_input in (item.get("outputs") or []):
            raw.append((predecessor, target_input, "upstream"))
    raw.sort(key=lambda value: (value[0], value[1]))
    return [SourceCandidate(
        candidate_id=f"C{index}", source_node=node, source_output=output,
        source_port_id=port_id(node, "output", output), target_node=target_node,
        target_input=target_input, target_port_id=port_id(target_node, "input", target_input),
        compatibility_evidence={"name_match": True, "type_match": "unknown",
                                "topology_allowed": True, "source_kind": kind},
    ) for index, (node, output, kind) in enumerate(raw, 1)]


def classify_graph_inputs(draft: GraphDraft, platform_contract: dict[str, Any] | None = None) -> None:
    """Classify every frozen input before any edge is compiled."""
    draft.input_resolutions.clear()
    draft.source_domains.clear()
    for item in draft.function_items:
        target = str(item["target_file"])
        defaults = item.get("default_values") or {}
        for name in item.get("inputs") or []:
            if name in defaults:
                kind: ResolutionKind = "local_default"
                candidates: list[SourceCandidate] = []
            else:
                candidates = compute_legal_source_domain(
                    target, name, draft.function_items, platform_contract,
                    draft.allowed_predecessors,
                )
                kinds = {candidate.compatibility_evidence["source_kind"] for candidate in candidates}
                kind = ("platform_runtime" if kinds == {"platform"} else
                        "upstream_runtime" if candidates else "unresolved")
            draft.input_resolutions.append({
                "target_node": target, "target_input": name,
                "target_port_id": port_id(target, "input", name), "resolution_kind": kind,
            })
            draft.source_domains.append({
                "target_node": target, "target_input": name,
                "candidates": [candidate.as_dict() for candidate in candidates],
            })


def _edge(candidate: SourceCandidate) -> dict[str, Any]:
    return {
        "from_node": candidate.source_node, "from_output": candidate.source_output,
        "to_node": candidate.target_node, "to_input": candidate.target_input,
        "purpose": f"Provide {candidate.target_input} runtime provenance",
        "constraints": [], "from_port_id": candidate.source_port_id,
        "to_port_id": candidate.target_port_id,
    }


def _would_cycle(edges: list[dict[str, Any]], edge: dict[str, Any]) -> bool:
    source, target = edge["from_node"], edge["to_node"]
    if source.startswith("platform_") or target.startswith("platform_"):
        return False
    links: dict[str, set[str]] = {}
    for current in [*edges, edge]:
        links.setdefault(current["from_node"], set()).add(current["to_node"])
    pending = [target]
    seen: set[str] = set()
    while pending:
        node = pending.pop()
        if node == source:
            return True
        if node not in seen:
            seen.add(node)
            pending.extend(links.get(node, ()))
    return False


def _append_checked(draft: GraphDraft, edge: dict[str, Any]) -> bool:
    provenance = (edge["to_node"], edge["to_input"])
    if any((item["to_node"], item["to_input"]) == provenance for item in draft.compiled_edges):
        draft.issues.append({"issue_type": "duplicate_provenance", "target_node": provenance[0], "target_input": provenance[1]})
        return False
    if _would_cycle(draft.compiled_edges, edge):
        draft.issues.append({"issue_type": "cycle_detected", "target_node": edge["to_node"], "target_input": edge["to_input"]})
        return False
    draft.compiled_edges.append(edge)
    return True


Selector = Callable[[dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]]


async def compile_responsibility_graph(
    draft: GraphDraft, *, platform_contract: dict[str, Any] | None = None,
    source_selector: Selector | None = None,
) -> GraphDraft:
    """Incrementally compile a candidate graph and commit it only if valid."""
    classify_graph_inputs(draft, platform_contract)
    draft.compiled_edges.clear()
    draft.issues.clear()
    draft.ambiguous_targets.clear()
    selections = 0
    domains = {(domain["target_node"], domain["target_input"]): domain for domain in draft.source_domains}
    for resolution in draft.input_resolutions:
        if resolution["resolution_kind"] in {"local_default", "static_resolved"}:
            continue
        key = (resolution["target_node"], resolution["target_input"])
        candidates = [SourceCandidate(**item) for item in domains[key]["candidates"]]
        if not candidates:
            draft.issues.append({"issue_type": "node_contract_gap", "target_node": key[0],
                                 "target_input": key[1], "legal_sources": []})
            continue
        selected = candidates[0]
        if len(candidates) > 1:
            draft.ambiguous_targets.append({"target_node": key[0], "target_input": key[1],
                                            "candidates": [item.as_dict() for item in candidates]})
            if source_selector is None:
                draft.issues.append({"issue_type": "ambiguous_source", "target_node": key[0], "target_input": key[1]})
                continue
            payload = {"target_node": key[0], "target_input": key[1],
                       "candidates": [{"candidate_id": item.candidate_id, "source_node": item.source_node,
                                       "source_output": item.source_output} for item in candidates],
                       "target_purpose": next(item.get("purpose", "") for item in draft.function_items if item["target_file"] == key[0])}
            response = source_selector(payload)
            response = await response if inspect.isawaitable(response) else response
            selections += 1
            allowed = {item.candidate_id: item for item in candidates}
            if set(response) - {"decision", "selected_candidate_id"} or response.get("decision") not in {"selected", "no_semantically_valid_candidate"}:
                draft.issues.append({"issue_type": "protocol_shape_error", "target_node": key[0], "target_input": key[1]})
                continue
            candidate_id = response.get("selected_candidate_id")
            if response["decision"] != "selected" or candidate_id not in allowed:
                draft.issues.append({"issue_type": "wrong_source_selection", "target_node": key[0], "target_input": key[1],
                                     "selected_candidate_id": candidate_id})
                continue
            selected = allowed[candidate_id]
        _append_checked(draft, _edge(selected))

    _, output_fields = _platform_fields(platform_contract)
    output_candidates = [(str(item["target_file"]), str(output)) for item in draft.function_items
                         for output in item.get("outputs") or [] if output in output_fields]
    for slot in output_fields:
        producers = [(node, output) for node, output in output_candidates if output == slot]
        if len(producers) == 1:
            node, output = producers[0]
            draft.compiled_edges.append({"from_node": node, "from_output": output,
                "to_node": "platform_output_node", "to_input": slot,
                "purpose": f"Deliver final {slot}", "constraints": [],
                "from_port_id": port_id(node, "output", output),
                "to_port_id": port_id("platform_output_node", "input", slot)})
    if draft.function_items and not any(edge["to_node"] == "platform_output_node" for edge in draft.compiled_edges):
        draft.issues.append({"issue_type": "missing_platform_output"})
    draft.metrics = {"function_item_count": len(draft.function_items),
        "runtime_input_count": sum(r["resolution_kind"] in {"platform_runtime", "upstream_runtime"} for r in draft.input_resolutions),
        "forced_edge_count": len(draft.compiled_edges) - selections,
        "ambiguous_input_count": len(draft.ambiguous_targets),
        "unresolved_input_count": sum(r["resolution_kind"] == "unresolved" for r in draft.input_resolutions),
        "model_source_selection_count": selections, "node_contract_patch_count": 0,
        "topology_patch_count": 0, "graph_validation_issues": copy.deepcopy(draft.issues),
        "graph_commit_status": "rejected" if draft.issues else "committed"}
    draft.status = "validation_failed" if draft.issues else "committed"
    return draft


def public_edges(draft: GraphDraft) -> list[dict[str, Any]]:
    """Project internal port identities to the existing ResponsibilityEdge wire shape."""
    if draft.status != "committed":
        raise ValueError("only a committed graph may be exported")
    return [{key: copy.deepcopy(value) for key, value in edge.items()
             if key not in {"from_port_id", "to_port_id"}} for edge in draft.compiled_edges]


def export_script_generation_contracts(draft: GraphDraft) -> list[dict[str, Any]]:
    """Deterministically project committed graph facts for later script generation."""
    if draft.status != "committed":
        raise ValueError("script contracts require a committed graph")
    contracts = []
    for item in draft.function_items:
        target = item["target_file"]
        incoming = [edge for edge in draft.compiled_edges if edge["to_node"] == target]
        outgoing = [edge for edge in draft.compiled_edges if edge["from_node"] == target]
        contracts.append({"target_file": target, "purpose": item.get("purpose", ""),
            "runtime_inputs": [{"name": edge["to_input"], "source_node": edge["from_node"],
                                "source_output": edge["from_output"]} for edge in incoming],
            "runtime_outputs": [{"name": name, "consumers": [f'{edge["to_node"]}.{edge["to_input"]}'
                for edge in outgoing if edge["from_output"] == name]} for name in item.get("outputs") or []],
            "required_capabilities": copy.deepcopy(item.get("required_capabilities") or []),
            "constraints": copy.deepcopy(item.get("constraints") or []),
            "default_values": copy.deepcopy(item.get("default_values") or {})})
    return contracts
