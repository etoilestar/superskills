"""Deterministic compiler for Creator executable responsibility graphs.

The compiler deliberately contains no business vocabulary.  FunctionItems are
the node contracts; models may select an opaque candidate id, but may never
author nodes, ports, or edges.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal


ResolutionKind = Literal[
    "platform_runtime", "upstream_runtime", "local_default",
    "static_resolved", "unresolved",
]

ISSUE_TYPES = frozenset({
    "protocol_shape_error", "illegal_node", "illegal_port",
    "node_contract_gap", "ambiguous_source", "wrong_source_selection",
    "duplicate_provenance", "type_mismatch", "topology_contract_issue",
    "cycle_detected", "missing_platform_input", "missing_platform_output",
    "unreachable_final_output", "constraint_conflict",
})


def port_id(node: str, direction: str, name: str) -> str:
    """Create the stable, Backend-owned identity of a frozen port."""
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
    platform_inputs: tuple[str, ...] = ()
    platform_outputs: tuple[str, ...] = ()

    @classmethod
    def freeze(
        cls, function_items: list[dict[str, Any]], platform_contract: dict[str, Any],
        allowed_predecessors: dict[str, list[str]] | None = None,
    ) -> "GraphDraft":
        items = copy.deepcopy(function_items)
        targets = [str(item["target_file"]) for item in items]
        if len(targets) != len(set(targets)):
            raise ValueError("FunctionItem target_file identities must be unique")
        if any(not node.startswith("scripts/") for node in targets):
            raise ValueError("executable FunctionItems must be scripts/** nodes")
        # Input order is the frozen workflow skeleton when no explicit skeleton
        # is available. Graph binding never gets to reverse it.
        topology = allowed_predecessors or {
            node: targets[:index] for index, node in enumerate(targets)
        }
        if set(topology) != set(targets) or any(
            predecessor not in targets
            for predecessors in topology.values() for predecessor in predecessors
        ):
            raise ValueError("allowed_predecessors contains an illegal node")
        return cls(
            function_items=items,
            allowed_predecessors=copy.deepcopy(topology),
            platform_inputs=tuple(platform_contract.get("input_envelope_fields") or ()),
            platform_outputs=tuple(platform_contract.get("final_output_fields") or ()),
        )


@dataclass
class GraphTransaction:
    """Keep the last validated graph isolated from mutable candidates."""

    committed_graph: GraphDraft | None = None

    async def compile_candidate(
        self, candidate: GraphDraft,
        selector: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    ) -> GraphDraft:
        result = await compile_responsibility_graph(candidate, selector)
        if result.status == "committed":
            self.committed_graph = copy.deepcopy(result)
        return result

    def require_committed(self) -> GraphDraft:
        if self.committed_graph is None:
            raise ValueError("no committed responsibility graph is available")
        return copy.deepcopy(self.committed_graph)


def compute_legal_source_domain(
    target_node: str, target_input: str, function_items: list[dict[str, Any]],
    platform_contract: dict[str, Any], topology_constraints: dict[str, list[str]],
) -> list[SourceCandidate]:
    """Return exact-name, direction-safe sources in deterministic order."""
    by_node = {str(item["target_file"]): item for item in function_items}
    target = by_node.get(target_node)
    if target is None or target_input not in target.get("inputs", []):
        return []
    raw: list[tuple[str, str]] = []
    platform_inputs = platform_contract.get("input_envelope_fields") or []
    if target_input in platform_inputs:
        raw.append(("platform_input_node", target_input))
    for predecessor in topology_constraints.get(target_node, []):
        source = by_node.get(predecessor)
        if source and target_input in source.get("outputs", []):
            raw.append((predecessor, target_input))
    candidates = []
    for index, (node, output) in enumerate(raw, 1):
        candidates.append(SourceCandidate(
            candidate_id=f"C{index}", source_node=node, source_output=output,
            source_port_id=port_id(node, "output", output),
            target_node=target_node, target_input=target_input,
            target_port_id=port_id(target_node, "input", target_input),
            compatibility_evidence={
                "name_match": True, "type_match": "unknown",
                "topology_allowed": True,
            },
        ))
    return candidates


def _edge(candidate: SourceCandidate) -> dict[str, Any]:
    return {
        "from_node": candidate.source_node,
        "from_output": candidate.source_output,
        "to_node": candidate.target_node,
        "to_input": candidate.target_input,
        "purpose": f"provide {candidate.target_input}",
        "constraints": [],
        "from_port_id": candidate.source_port_id,
        "to_port_id": candidate.target_port_id,
    }


def _add_edge(draft: GraphDraft, edge: dict[str, Any]) -> bool:
    """Validate one candidate edge before mutating the candidate graph."""
    if any(existing["to_port_id"] == edge["to_port_id"] for existing in draft.compiled_edges):
        draft.issues.append({"issue_type": "duplicate_provenance", "target_port_id": edge["to_port_id"]})
        return False
    if edge["from_node"] == edge["to_node"]:
        draft.issues.append({"issue_type": "cycle_detected", "target_node": edge["to_node"]})
        return False
    if (
        edge["to_node"] != "platform_output_node"
        and edge["from_node"] != "platform_input_node"
        and edge["from_node"] not in draft.allowed_predecessors.get(edge["to_node"], [])
    ):
        draft.issues.append({"issue_type": "topology_contract_issue", "target_node": edge["to_node"]})
        return False
    draft.compiled_edges.append(edge)
    return True


def classify_and_compute_domains(draft: GraphDraft) -> None:
    platform = {"input_envelope_fields": list(draft.platform_inputs)}
    for item in draft.function_items:
        node = item["target_file"]
        defaults = item.get("default_values") or {}
        static_inputs = set(item.get("static_resolved_inputs") or [])
        for name in item.get("inputs", []):
            if name in defaults:
                kind: ResolutionKind = "local_default"
                candidates: list[SourceCandidate] = []
            elif name in static_inputs:
                kind = "static_resolved"
                candidates = []
            else:
                candidates = compute_legal_source_domain(
                    node, name, draft.function_items, platform,
                    draft.allowed_predecessors,
                )
                source_kinds = {candidate.source_node == "platform_input_node" for candidate in candidates}
                if not candidates:
                    kind = "unresolved"
                elif source_kinds == {True}:
                    kind = "platform_runtime"
                else:
                    kind = "upstream_runtime"
            resolution = {"target_node": node, "target_input": name, "target_port_id": port_id(node, "input", name), "resolution_kind": kind}
            draft.input_resolutions.append(resolution)
            draft.source_domains.append({**resolution, "legal_sources": [candidate.as_dict() for candidate in candidates]})
            if kind == "unresolved":
                draft.issues.append({"issue_type": "node_contract_gap", "target_node": node, "target_input": name, "legal_sources": []})
            elif len(candidates) == 1:
                _add_edge(draft, _edge(candidates[0]))
            elif len(candidates) > 1:
                draft.ambiguous_targets.append({**resolution, "candidates": [candidate.as_dict() for candidate in candidates]})


def validate_source_selection(value: Any, candidate_ids: set[str]) -> str | None:
    if not isinstance(value, dict) or set(value) - {"decision", "selected_candidate_id"}:
        raise ValueError("source selector returned protocol_shape_error")
    decision = value.get("decision")
    if decision not in {"selected", "no_semantically_valid_candidate"}:
        raise ValueError("source selector decision is invalid")
    selected = value.get("selected_candidate_id")
    if decision == "selected" and selected not in candidate_ids:
        raise ValueError("selected_candidate_id is outside the legal candidate domain")
    if decision != "selected" and selected is not None:
        raise ValueError("no-candidate decision must not select a candidate")
    return selected


async def compile_responsibility_graph(
    draft: GraphDraft,
    selector: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
) -> GraphDraft:
    """Compile into a candidate and commit atomically only after validation."""
    classify_and_compute_domains(draft)
    for target in draft.ambiguous_targets:
        if selector is None:
            draft.issues.append({"issue_type": "ambiguous_source", "target_node": target["target_node"], "target_input": target["target_input"]})
            continue
        public_candidates = [{key: candidate[key] for key in ("candidate_id", "source_node", "source_output")} for candidate in target["candidates"]]
        payload = {"target_node": target["target_node"], "target_input": target["target_input"], "candidates": public_candidates}
        selected = None
        for attempt in range(2):
            try:
                selected = validate_source_selection(await selector(payload), {c["candidate_id"] for c in target["candidates"]})
                break
            except ValueError:
                if attempt:
                    draft.issues.append({"issue_type": "wrong_source_selection", "target_node": target["target_node"], "target_input": target["target_input"]})
        if selected:
            candidate = next(c for c in target["candidates"] if c["candidate_id"] == selected)
            _add_edge(draft, _edge(SourceCandidate(**candidate)))
        elif not any(i.get("target_node") == target["target_node"] and i.get("target_input") == target["target_input"] for i in draft.issues):
            draft.issues.append({"issue_type": "node_contract_gap", "target_node": target["target_node"], "target_input": target["target_input"]})

    # Platform output compilation is exact and never delegated to a model.
    non_terminal_nodes = {
        predecessor for predecessors in draft.allowed_predecessors.values()
        for predecessor in predecessors
    }
    output_candidates = [
        (item["target_file"], output)
        for item in draft.function_items for output in item.get("outputs", [])
        if output in draft.platform_outputs
        and item["target_file"] not in non_terminal_nodes
    ]
    for node, output in output_candidates:
        edge = {"from_node": node, "from_output": output, "to_node": "platform_output_node", "to_input": output, "purpose": f"publish {output}", "constraints": [], "from_port_id": port_id(node, "output", output), "to_port_id": port_id("platform_output_node", "input", output)}
        _add_edge(draft, edge)
    if not output_candidates:
        draft.issues.append({"issue_type": "missing_platform_output"})
    if draft.function_items and not any(e["from_node"] == "platform_input_node" for e in draft.compiled_edges):
        draft.issues.append({"issue_type": "missing_platform_input"})
    draft.status = "committed" if not draft.issues else "validation_failed"
    return draft


def public_edges(draft: GraphDraft) -> list[dict[str, Any]]:
    if draft.status != "committed":
        raise ValueError("only a committed responsibility graph may be consumed")
    return [{key: value for key, value in edge.items() if key not in {"from_port_id", "to_port_id"}} for edge in draft.compiled_edges]


def export_script_generation_contracts(draft: GraphDraft) -> list[dict[str, Any]]:
    """Project downstream contracts exclusively from a committed graph."""
    if draft.status != "committed":
        raise ValueError("only a committed responsibility graph may be exported")
    contracts = []
    for item in draft.function_items:
        node = item["target_file"]
        incoming = [edge for edge in draft.compiled_edges if edge["to_node"] == node]
        outgoing = [edge for edge in draft.compiled_edges if edge["from_node"] == node]
        contracts.append({
            "target_file": node, "purpose": item.get("purpose", ""),
            "runtime_inputs": [{"name": edge["to_input"], "source_node": edge["from_node"], "source_output": edge["from_output"]} for edge in incoming],
            "runtime_outputs": [{"name": name, "consumers": [f'{edge["to_node"]}.{edge["to_input"]}' for edge in outgoing if edge["from_output"] == name]} for name in item.get("outputs", [])],
            "required_capabilities": copy.deepcopy(item.get("required_capabilities") or []),
            "constraints": copy.deepcopy(item.get("constraints") or []),
            "default_values": copy.deepcopy(item.get("default_values") or {}),
        })
    return contracts
