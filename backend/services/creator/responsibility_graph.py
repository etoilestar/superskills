"""Backend-owned compiler for Creator executable responsibility graphs.

FunctionItems are immutable node contracts.  Structural facts build candidate
source domains; a model may only choose an opaque candidate id when the facts
leave more than one legal source.
"""
from __future__ import annotations

import copy
import inspect
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Literal

from ..platform_io_contract import build_platform_io_contract

VALUE_TYPES = frozenset({"unknown", "string", "number", "integer", "boolean", "object", "array", "path", "array[path]", "array[string]"})
ResolutionKind = Literal["platform_runtime", "upstream_runtime", "ambiguous_runtime", "local_default", "static_resolved", "unresolved"]
BindingKind = Literal["platform_parameter", "script_output", "local_default", "static_value", "unbound"]
class _MissingDefault(Enum):
    TOKEN = "__creator_graph_missing_default__"


_UNSET = _MissingDefault.TOKEN
ISSUE_TYPES = frozenset({"protocol_shape_error", "illegal_node", "illegal_port", "node_contract_gap", "node_set_gap", "ambiguous_source", "wrong_source_selection", "duplicate_provenance", "type_mismatch", "topology_contract_issue", "cycle_detected", "missing_platform_input", "missing_platform_output", "final_output_contract_gap", "unreachable_final_output", "constraint_conflict", "unauthorized_static_resource"})


def port_id(node: str, direction: Literal["input", "output"], name: str) -> str:
    return f"{node}::{direction}::{name}"


def _port(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        return {"name": raw, "value_type": "unknown", "description": "", "semantic_id": ""}
    if not isinstance(raw, dict) or not str(raw.get("name") or "").strip():
        raise ValueError("ports must be strings or objects with a name")
    value_type = str(raw.get("value_type") or raw.get("type") or "unknown")
    if value_type not in VALUE_TYPES:
        raise ValueError(f"unsupported port value_type: {value_type}")
    return {"name": str(raw["name"]), "value_type": value_type,
            "description": str(raw.get("description") or ""),
            "semantic_id": str(raw.get("semantic_id") or "")}


def type_compatibility(source_type: str, target_type: str) -> Literal["compatible", "unknown", "incompatible"]:
    if source_type == target_type:
        return "compatible"
    if "unknown" in {source_type, target_type}:
        return "unknown"
    if source_type == "integer" and target_type == "number":
        return "compatible"
    return "incompatible"


@dataclass(frozen=True)
class InputBinding:
    target_node: str
    target_input: str
    binding_kind: BindingKind
    source_node: str | None = None
    source_output: str | None = None
    source_root: str | None = None
    source_key: str | None = None
    required: bool = True
    default: Any = _UNSET
    resolver: dict[str, Any] | None = None


@dataclass(frozen=True)
class FinalOutputBinding:
    platform_slot: str
    source_node: str
    source_output: str


@dataclass(frozen=True)
class SourceCandidate:
    candidate_id: str
    source_node: str
    source_output: str
    source_port_id: str
    target_node: str
    target_input: str
    target_port_id: str
    binding_kind: str
    source_type: str = "unknown"
    target_type: str = "unknown"
    constraints: tuple[dict[str, Any], ...] = ()
    compatibility_evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        value = copy.deepcopy(self.__dict__)
        value["constraints"] = [copy.deepcopy(item) for item in self.constraints]
        return value


@dataclass
class InputResolverRegistry:
    STATIC_KINDS = frozenset({"static_reference", "uploaded_asset", "runtime_constant", "environment_value"})

    authorized_references: set[str] = field(default_factory=set)
    authorized_assets: set[str] = field(default_factory=set)

    def resolve(self, binding: InputBinding) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        resolver = copy.deepcopy(binding.resolver or {})
        kind = str(resolver.get("kind") or "")
        if binding.binding_kind != "static_value" or kind not in self.STATIC_KINDS:
            return None, None
        path = str(resolver.get("path") or "")
        if kind == "static_reference" and path not in self.authorized_references:
            return None, {"issue_type": "unauthorized_static_resource", "resolver_kind": kind, "path": path,
                          "target_node": binding.target_node, "target_input": binding.target_input}
        if kind == "uploaded_asset" and path not in self.authorized_assets:
            return None, {"issue_type": "unauthorized_static_resource", "resolver_kind": kind, "path": path,
                          "target_node": binding.target_node, "target_input": binding.target_input}
        return resolver, None


@dataclass
class GraphDraft:
    version: int = 2
    status: str = "draft"
    function_items: list[dict[str, Any]] = field(default_factory=list)
    input_bindings: list[InputBinding] = field(default_factory=list)
    final_output_bindings: list[FinalOutputBinding] = field(default_factory=list)
    input_resolutions: list[dict[str, Any]] = field(default_factory=list)
    source_domains: list[dict[str, Any]] = field(default_factory=list)
    compiled_edges: list[dict[str, Any]] = field(default_factory=list)
    ambiguous_targets: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    allowed_predecessors: dict[str, list[str]] = field(default_factory=dict)
    topology_evidence: dict[str, str] = field(default_factory=dict)
    ports: list[dict[str, str]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    resolver_registry: InputResolverRegistry = field(default_factory=InputResolverRegistry)

    @classmethod
    def freeze(cls, function_items: list[dict[str, Any]], *,
               workflow_topology: dict[str, list[str]] | None = None,
               allowed_predecessors: dict[str, list[str]] | None = None,
               input_bindings: list[InputBinding | dict[str, Any]] | None = None,
               final_output_bindings: list[FinalOutputBinding | dict[str, Any]] | None = None,
               frozen_edges: list[dict[str, Any]] | None = None,
               authorized_references: set[str] | None = None,
               authorized_assets: set[str] | None = None) -> "GraphDraft":
        items = copy.deepcopy(function_items)
        targets = [str(item.get("target_file") or "") for item in items]
        if len(set(targets)) != len(targets) or any(not target.startswith("scripts/") or not target.endswith(".py") for target in targets):
            raise ValueError("FunctionItem node domain must contain unique scripts/**/*.py targets")
        bindings = [_coerce_input_binding(value) for value in (input_bindings or [])]
        finals = [_coerce_final_binding(value) for value in (final_output_bindings or [])]
        for edge in frozen_edges or []:
            if edge.get("to_node") == "platform_output_node":
                finals.append(FinalOutputBinding(str(edge.get("to_input") or ""), str(edge.get("from_node") or ""), str(edge.get("from_output") or "")))
            elif edge.get("to_node") in targets:
                bindings.append(_binding_from_edge(edge))
        topology, evidence, topology_issues = _compile_topology(items, workflow_topology or allowed_predecessors, bindings)
        binding_targets: set[tuple[str, str]] = set()
        for binding in bindings:
            key = (binding.target_node, binding.target_input)
            if binding.binding_kind in {"platform_parameter", "script_output"} and key in binding_targets:
                topology_issues.append({"issue_type": "duplicate_provenance",
                                        "target_node": key[0], "target_input": key[1]})
            binding_targets.add(key)
        ports = []
        for item in items:
            for direction, raw_ports in (("input", item.get("inputs") or []), ("output", item.get("outputs") or [])):
                for raw in raw_ports:
                    parsed = _port(raw)
                    ports.append({"port_id": port_id(item["target_file"], direction, parsed["name"]), "name": parsed["name"],
                                  "direction": direction, "value_type": parsed["value_type"], "description": parsed["description"],
                                  "semantic_id": parsed["semantic_id"]})
        return cls(function_items=items, input_bindings=bindings, final_output_bindings=finals,
                   allowed_predecessors=topology, topology_evidence=evidence, ports=ports,
                   issues=topology_issues, resolver_registry=InputResolverRegistry(
                       authorized_references=authorized_references or set(), authorized_assets=authorized_assets or set()))

    def refreeze(
        self,
        function_items: list[dict[str, Any]], *,
        topology_updates: dict[str, list[str]] | None = None,
    ) -> "GraphDraft":
        """Refreeze after bounded node planning without dropping authority facts."""
        topology = copy.deepcopy(self.allowed_predecessors)
        for item in function_items:
            topology.setdefault(str(item["target_file"]), [])
        for target, predecessors in (topology_updates or {}).items():
            topology[target] = sorted(set(topology.get(target, [])) | set(predecessors))
        return GraphDraft.freeze(
            function_items,
            workflow_topology=topology,
            input_bindings=copy.deepcopy(self.input_bindings),
            final_output_bindings=copy.deepcopy(self.final_output_bindings),
            authorized_references=set(self.resolver_registry.authorized_references),
            authorized_assets=set(self.resolver_registry.authorized_assets),
        )


@dataclass
class GraphTransaction:
    committed_graph: GraphDraft | None = None

    def candidate(self, function_items: list[dict[str, Any]], **kwargs: Any) -> GraphDraft:
        return GraphDraft.freeze(function_items, **kwargs)

    def commit(self, candidate: GraphDraft) -> GraphDraft:
        if candidate.status != "committed" or candidate.issues:
            raise ValueError("validation_failed candidate cannot replace committed graph")
        self.committed_graph = copy.deepcopy(candidate)
        return copy.deepcopy(self.committed_graph)


def _coerce_input_binding(value: InputBinding | dict[str, Any]) -> InputBinding:
    return value if isinstance(value, InputBinding) else InputBinding(**value)


def _coerce_final_binding(value: FinalOutputBinding | dict[str, Any]) -> FinalOutputBinding:
    return value if isinstance(value, FinalOutputBinding) else FinalOutputBinding(**value)


def _binding_from_edge(edge: dict[str, Any]) -> InputBinding:
    constraints = edge.get("constraints") or []
    parameter = next((item for item in constraints if (item.get("type") or item.get("kind")) == "platform_parameter_binding"), None)
    if edge.get("from_node") == "platform_input_node":
        return InputBinding(str(edge["to_node"]), str(edge["to_input"]), "platform_parameter",
                            source_node="platform_input_node", source_output=str(edge["from_output"]),
                            source_root=str(edge["from_output"]), source_key=str((parameter or {}).get("source_key") or edge["to_input"]),
                            required=bool((parameter or {}).get("required", True)), default=(parameter or {}).get("default", _UNSET))
    return InputBinding(str(edge["to_node"]), str(edge["to_input"]), "script_output",
                        source_node=str(edge["from_node"]), source_output=str(edge["from_output"]))


def _compile_topology(items: list[dict[str, Any]], explicit: dict[str, list[str]] | None,
                      bindings: list[InputBinding]) -> tuple[dict[str, list[str]], dict[str, str], list[dict[str, Any]]]:
    targets = {str(item["target_file"]) for item in items}
    issues: list[dict[str, Any]] = []
    topology: dict[str, set[str]] = {target: set() for target in targets}
    evidence: dict[str, str] = {}
    if explicit is not None:
        for target, predecessors in explicit.items():
            if target not in targets:
                issues.append({"issue_type": "topology_contract_issue", "target_node": target, "reason": "unknown target"})
                continue
            for predecessor in predecessors:
                if predecessor not in targets or predecessor == target:
                    issues.append({"issue_type": "topology_contract_issue", "target_node": target,
                                   "source_node": predecessor, "reason": "illegal predecessor"})
                else:
                    topology[target].add(predecessor); evidence[f"{predecessor}->{target}"] = "explicit topology"
    else:
        dependency_found = False
        for item in items:
            target = str(item["target_file"])
            for dependency in item.get("depends_on") or item.get("dependencies") or []:
                if dependency in targets and dependency != target:
                    topology[target].add(dependency); evidence[f"{dependency}->{target}"] = "declared dependency"; dependency_found = True
                elif str(dependency).startswith("scripts/"):
                    issues.append({"issue_type": "topology_contract_issue", "target_node": target,
                                   "source_node": dependency, "reason": "illegal dependency"})
        bound = False
        for binding in bindings:
            if binding.binding_kind == "script_output" and binding.source_node:
                if binding.target_node in targets and binding.source_node in targets and binding.source_node != binding.target_node:
                    topology[binding.target_node].add(binding.source_node); evidence[f"{binding.source_node}->{binding.target_node}"] = "frozen source binding"; bound = True
        # Absence of topology is not evidence that every executable node may
        # precede every other node.  Such inputs remain unresolved unless an
        # explicit binding or unique semantic port identity supplies authority.
    return ({target: sorted(values) for target, values in topology.items()}, evidence, issues)


def _platform_fields(contract: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    boundary = (contract or build_platform_io_contract()).get("platform_skill_boundary") or {}
    return list(boundary.get("input_envelope_fields") or []), list(boundary.get("final_output_fields") or [])


def _find_port(draft: GraphDraft, node: str, direction: str, name: str) -> dict[str, str] | None:
    return next((item for item in draft.ports if item["port_id"] == port_id(node, direction, name)), None)


def compute_legal_source_domain(target_node: str, target_input: str, function_items: list[dict[str, Any]],
                                platform_contract: dict[str, Any] | None, topology_constraints: dict[str, list[str]],
                                *, input_bindings: list[InputBinding | dict[str, Any]] | None = None,
                                ports: list[dict[str, str]] | None = None,
                                topology_evidence: dict[str, str] | None = None) -> list[SourceCandidate]:
    bindings = [_coerce_input_binding(value) for value in (input_bindings or [])]
    target_binding = next((item for item in bindings if item.target_node == target_node and item.target_input == target_input), None)
    if target_binding and target_binding.binding_kind in {"local_default", "static_value"}:
        return []
    all_ports = ports or [
        {"port_id": port_id(item["target_file"], direction, parsed["name"]), "name": parsed["name"], "direction": direction,
         "value_type": parsed["value_type"], "description": parsed["description"], "semantic_id": parsed["semantic_id"]}
        for item in function_items for direction, values in (("input", item.get("inputs") or []), ("output", item.get("outputs") or []))
        for parsed in [_port(raw) for raw in values]
    ]
    target_port = next(item for item in all_ports if item["port_id"] == port_id(target_node, "input", target_input))
    candidates: list[SourceCandidate] = []
    input_fields, _ = _platform_fields(platform_contract)
    if target_binding and target_binding.binding_kind == "platform_parameter":
        root = target_binding.source_root or target_binding.source_output or "fields"
        if root in input_fields and (target_binding.required or target_binding.default is not _UNSET):
            constraint = {"type": "platform_parameter_binding", "source_key": target_binding.source_key or target_input,
                          "required": target_binding.required}
            if not target_binding.required: constraint["default"] = target_binding.default
            candidates.append(SourceCandidate("", "platform_input_node", root, port_id("platform_input_node", "output", root),
                target_node, target_input, target_port["port_id"], "platform_parameter", "unknown", target_port["value_type"],
                (constraint,), {"name_match": "exact" if (target_binding.source_key or target_input) == target_input else "different",
                                "type_match": "unknown", "topology_allowed": True, "explicit_binding": True,
                                "topology_evidence": "explicit platform binding"}))
        return _candidate_ids(candidates)
    if target_binding and target_binding.binding_kind == "script_output":
        sources = [(target_binding.source_node, target_binding.source_output, True)]
    else:
        sources = [(node, output_port["name"], False) for node in topology_constraints.get(target_node, [])
                   for output_port in all_ports if output_port["direction"] == "output" and output_port["port_id"].startswith(f"{node}::output::")]
        if target_input in input_fields:
            candidates.append(SourceCandidate("", "platform_input_node", target_input, port_id("platform_input_node", "output", target_input),
                target_node, target_input, target_port["port_id"], "platform_parameter", "unknown", target_port["value_type"], (),
                {"name_match": "exact", "type_match": "unknown", "topology_allowed": True, "explicit_binding": False,
                 "topology_evidence": "platform slot exact match"}))
        if not topology_constraints.get(target_node) and target_port.get("semantic_id"):
            semantic_sources = [
                (item["port_id"].split("::", 1)[0], item["name"], False)
                for item in all_ports
                if item["direction"] == "output"
                and item.get("semantic_id") == target_port.get("semantic_id")
                and not item["port_id"].startswith(f"{target_node}::")
                and type_compatibility(item["value_type"], target_port["value_type"]) != "incompatible"
            ]
            if len(semantic_sources) == 1:
                sources = semantic_sources
    for node, output, explicit_binding in sources:
        source_port = next((item for item in all_ports if item["port_id"] == port_id(str(node), "output", str(output))), None)
        semantic_authority = bool(source_port and source_port.get("semantic_id")
                                  and source_port.get("semantic_id") == target_port.get("semantic_id")
                                  and len(sources) == 1)
        if not source_port or (node not in topology_constraints.get(target_node, []) and not semantic_authority):
            continue
        compatibility = type_compatibility(source_port["value_type"], target_port["value_type"])
        semantic_match = bool(source_port.get("semantic_id") and source_port.get("semantic_id") == target_port.get("semantic_id"))
        if compatibility == "incompatible":
            continue
        candidates.append(SourceCandidate("", str(node), str(output), source_port["port_id"], target_node, target_input,
            target_port["port_id"], "script_output", source_port["value_type"], target_port["value_type"], (),
            {"name_match": "exact" if output == target_input else "different", "semantic_port_match": semantic_match,
             "type_match": compatibility, "topology_allowed": True, "explicit_binding": explicit_binding,
             "topology_evidence": (topology_evidence or {}).get(f"{node}->{target_node}", "allowed predecessor")}))
    return _candidate_ids(candidates)


def _candidate_ids(candidates: list[SourceCandidate]) -> list[SourceCandidate]:
    ordered = sorted(candidates, key=lambda item: (item.source_node, item.source_output, item.binding_kind))
    return [SourceCandidate(f"C{index}", **{key: value for key, value in item.__dict__.items() if key != "candidate_id"})
            for index, item in enumerate(ordered, 1)]


def classify_graph_inputs(draft: GraphDraft, platform_contract: dict[str, Any] | None = None) -> None:
    draft.input_resolutions.clear(); draft.source_domains.clear()
    binding_map = {(item.target_node, item.target_input): item for item in draft.input_bindings}
    for item in draft.function_items:
        target = str(item["target_file"]); defaults = item.get("default_values") or {}
        for raw in item.get("inputs") or []:
            name = _port(raw)["name"]; binding = binding_map.get((target, name))
            if binding and binding.binding_kind == "platform_parameter":
                candidates = compute_legal_source_domain(target, name, draft.function_items, platform_contract, draft.allowed_predecessors,
                    input_bindings=draft.input_bindings, ports=draft.ports, topology_evidence=draft.topology_evidence)
                kind: ResolutionKind = "platform_runtime" if candidates else "unresolved"; resolver = None
            elif name in defaults or (binding and binding.binding_kind == "local_default"):
                candidates = []; kind = "local_default"; resolver = {"kind": "local_default", "default": defaults.get(name, binding.default if binding else None)}
            elif binding and binding.binding_kind == "static_value":
                candidates = []; resolver, issue = draft.resolver_registry.resolve(binding)
                kind = "static_resolved" if resolver else "unresolved"
                if issue: draft.issues.append(issue)
            else:
                candidates = compute_legal_source_domain(target, name, draft.function_items, platform_contract, draft.allowed_predecessors,
                    input_bindings=draft.input_bindings, ports=draft.ports, topology_evidence=draft.topology_evidence)
                source_kinds = {candidate.binding_kind for candidate in candidates}
                kind = ("unresolved" if not candidates else "ambiguous_runtime" if len(candidates) > 1 or len(source_kinds) > 1
                        else "platform_runtime" if source_kinds == {"platform_parameter"} else "upstream_runtime")
                resolver = None
                if not candidates:
                    target_port = _find_port(draft, target, "input", name) or {}
                    for predecessor in draft.allowed_predecessors.get(target, []):
                        for source_port in draft.ports:
                            if source_port["direction"] != "output" or not source_port["port_id"].startswith(f"{predecessor}::output::"):
                                continue
                            if type_compatibility(source_port["value_type"], target_port.get("value_type", "unknown")) == "incompatible":
                                draft.issues.append({"issue_type": "type_mismatch", "source_type": source_port["value_type"],
                                    "target_type": target_port.get("value_type", "unknown"), "source_port_id": source_port["port_id"],
                                    "target_port_id": target_port.get("port_id"), "target_node": target, "target_input": name})
            resolution = {"target_node": target, "target_input": name, "target_port_id": port_id(target, "input", name), "resolution_kind": kind}
            if resolver: resolution["resolver"] = resolver
            draft.input_resolutions.append(resolution)
            draft.source_domains.append({"target_node": target, "target_input": name, "candidates": [candidate.as_dict() for candidate in candidates]})


def _edge(candidate: SourceCandidate) -> dict[str, Any]:
    return {"from_node": candidate.source_node, "from_output": candidate.source_output, "to_node": candidate.target_node,
            "to_input": candidate.target_input, "purpose": f"Provide {candidate.target_input} runtime provenance",
            "constraints": [copy.deepcopy(item) for item in candidate.constraints], "from_port_id": candidate.source_port_id,
            "to_port_id": candidate.target_port_id, "binding_kind": candidate.binding_kind,
            "source_key": next((item.get("source_key") for item in candidate.constraints if item.get("type") == "platform_parameter_binding"), None)}


def _would_cycle(edges: list[dict[str, Any]], edge: dict[str, Any]) -> bool:
    source, target = edge["from_node"], edge["to_node"]
    if source.startswith("platform_") or target.startswith("platform_"): return False
    links: dict[str, set[str]] = {}
    for current in [*edges, edge]: links.setdefault(current["from_node"], set()).add(current["to_node"])
    pending = [target]; seen: set[str] = set()
    while pending:
        node = pending.pop()
        if node == source: return True
        if node not in seen: seen.add(node); pending.extend(links.get(node, ()))
    return False


def _append_checked(draft: GraphDraft, edge: dict[str, Any]) -> bool:
    provenance = (edge["to_node"], edge["to_input"])
    if any((item["to_node"], item["to_input"]) == provenance for item in draft.compiled_edges):
        draft.issues.append({"issue_type": "duplicate_provenance", "target_node": provenance[0], "target_input": provenance[1]}); return False
    if edge["from_node"] == edge["to_node"] or _would_cycle(draft.compiled_edges, edge):
        draft.issues.append({"issue_type": "cycle_detected", "target_node": edge["to_node"], "target_input": edge["to_input"]}); return False
    draft.compiled_edges.append(edge); return True


Selector = Callable[[dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]]


async def _choose(candidates: list[SourceCandidate], payload: dict[str, Any], selector: Selector | None,
                  issues: list[dict[str, Any]], issue_context: dict[str, Any]) -> tuple[SourceCandidate | None, int]:
    if len(candidates) == 1: return candidates[0], 0
    if selector is None:
        issues.append({"issue_type": "ambiguous_source", **issue_context}); return None, 0
    calls = 0
    last_issue = "protocol_shape_error"
    candidate_snapshot = tuple(
        (item.candidate_id, item.source_port_id, item.target_port_id)
        for item in candidates
    )
    target_snapshot = (payload.get("target_node"), payload.get("target_input"))
    for _attempt in range(2):
        response = selector(payload); response = await response if inspect.isawaitable(response) else response; calls += 1
        if candidate_snapshot != tuple(
            (item.candidate_id, item.source_port_id, item.target_port_id)
            for item in candidates
        ) or target_snapshot != (payload.get("target_node"), payload.get("target_input")):
            issues.append({"issue_type": "protocol_shape_error", **issue_context})
            return None, calls
        allowed = {item.candidate_id: item for item in candidates}
        expected_fields = (
            {"decision", "selected_candidate_id"}
            if isinstance(response, dict) and response.get("decision") == "selected"
            else {"decision"}
        )
        if isinstance(response, dict) and set(response) == expected_fields and response.get("decision") in {"selected", "no_semantically_valid_candidate"}:
            selected = response.get("selected_candidate_id")
            if response["decision"] == "selected" and selected in allowed: return allowed[selected], calls
            if response["decision"] == "no_semantically_valid_candidate":
                issues.append({"issue_type": "wrong_source_selection", **issue_context}); return None, calls
            last_issue = "wrong_source_selection"
    issues.append({"issue_type": last_issue, **issue_context}); return None, calls


def _selector_candidate(candidate: SourceCandidate, draft: GraphDraft) -> dict[str, Any]:
    source_item = next((item for item in draft.function_items if item["target_file"] == candidate.source_node), {})
    source_port = _find_port(draft, candidate.source_node, "output", candidate.source_output) or {}
    target_port = _find_port(draft, candidate.target_node, "input", candidate.target_input) or {}
    return {"candidate_id": candidate.candidate_id, "source_node": candidate.source_node, "source_output": candidate.source_output,
            "source_purpose": source_item.get("purpose", ""), "source_output_description": source_port.get("description", ""),
            "source_type": candidate.source_type, "target_input": candidate.target_input,
            "target_input_description": target_port.get("description", ""), "target_type": candidate.target_type,
            "explicit_binding": candidate.compatibility_evidence.get("explicit_binding", False),
            "topology_evidence": candidate.compatibility_evidence.get("topology_evidence", ""),
            "requirement_ids": source_item.get("requirement_ids") or []}


async def _compile_final_outputs(draft: GraphDraft, selector: Selector | None, platform_contract: dict[str, Any] | None) -> tuple[int, int]:
    _, platform_slots = _platform_fields(platform_contract); selected_count = model_calls = 0
    explicit = {binding.platform_slot: binding for binding in draft.final_output_bindings}
    desired_slots = list(dict.fromkeys([*explicit, *[slot for slot in platform_slots if any(_port(raw)["name"] == slot for item in draft.function_items for raw in item.get("outputs") or [])]]))
    if not desired_slots and draft.function_items:
        draft.issues.append({"issue_type": "final_output_contract_gap", "platform_slot": None}); return 0, 0
    for slot in desired_slots:
        if slot not in platform_slots:
            draft.issues.append({"issue_type": "final_output_contract_gap", "platform_slot": slot, "reason": "unknown platform slot"})
            continue
        binding = explicit.get(slot); candidates: list[SourceCandidate] = []
        for item in draft.function_items:
            for raw in item.get("outputs") or []:
                output = _port(raw)
                if binding and (item["target_file"], output["name"]) != (binding.source_node, binding.source_output): continue
                if not binding and output["name"] != slot: continue
                candidates.append(SourceCandidate("", item["target_file"], output["name"], port_id(item["target_file"], "output", output["name"]),
                    "platform_output_node", slot, port_id("platform_output_node", "input", slot), "final_output", output["value_type"], "unknown", (),
                    {"name_match": "exact" if output["name"] == slot else "different", "type_match": "unknown", "topology_allowed": True,
                     "explicit_binding": bool(binding), "topology_evidence": "explicit final output binding" if binding else "unique terminal output"}))
        candidates = _candidate_ids(candidates)
        if not candidates:
            draft.issues.append({"issue_type": "final_output_contract_gap", "platform_slot": slot}); continue
        payload = {"target_node": "platform_output_node", "target_input": slot,
                   "candidates": [_selector_candidate(candidate, draft) for candidate in candidates], "immutable_constraints": ["candidate ids only"]}
        chosen, calls = await _choose(candidates, payload, selector, draft.issues, {"platform_slot": slot}); model_calls += calls
        if chosen:
            if len(candidates) > 1: selected_count += 1
            draft.compiled_edges.append({"from_node": chosen.source_node, "from_output": chosen.source_output,
                "to_node": "platform_output_node", "to_input": slot, "purpose": f"Deliver final {slot}", "constraints": [],
                "from_port_id": chosen.source_port_id, "to_port_id": chosen.target_port_id, "binding_kind": "final_output"})
    return selected_count, model_calls


async def compile_responsibility_graph(draft: GraphDraft, *, platform_contract: dict[str, Any] | None = None,
                                       source_selector: Selector | None = None) -> GraphDraft:
    frozen_issues = list(draft.issues); draft.compiled_edges.clear(); draft.issues = frozen_issues; draft.ambiguous_targets.clear()
    classify_graph_inputs(draft, platform_contract)
    domains = {(item["target_node"], item["target_input"]): item for item in draft.source_domains}
    forced = selected = platform_inputs = selector_calls = 0
    for resolution in draft.input_resolutions:
        if resolution["resolution_kind"] in {"local_default", "static_resolved"}: continue
        key = (resolution["target_node"], resolution["target_input"])
        candidates = [SourceCandidate(**{**item, "constraints": tuple(item.get("constraints") or [])}) for item in domains[key]["candidates"]]
        if not candidates:
            if not any(issue.get("target_node") == key[0] and issue.get("target_input") == key[1] for issue in draft.issues):
                has_executable_output = any(
                    port["direction"] == "output"
                    and port["port_id"].split("::", 1)[0] in draft.allowed_predecessors.get(key[0], [])
                    for port in draft.ports
                )
                draft.issues.append({"issue_type": "node_contract_gap", "target_node": key[0],
                    "target_input": key[1], "legal_sources": [],
                    "node_set_candidate": not has_executable_output})
            continue
        if len(candidates) > 1: draft.ambiguous_targets.append({"target_node": key[0], "target_input": key[1], "candidates": [item.as_dict() for item in candidates]})
        target_item = next(item for item in draft.function_items if item["target_file"] == key[0])
        payload = {"target_node": key[0], "target_input": key[1], "target_purpose": target_item.get("purpose", ""),
                   "candidates": [_selector_candidate(candidate, draft) for candidate in candidates],
                   "one_hop_predecessors": draft.allowed_predecessors.get(key[0], []),
                   "requirement_ids": target_item.get("requirement_ids") or [], "immutable_constraints": target_item.get("constraints") or []}
        chosen, calls = await _choose(candidates, payload, source_selector, draft.issues, {"target_node": key[0], "target_input": key[1]}); selector_calls += calls
        if chosen and _append_checked(draft, _edge(chosen)):
            if len(candidates) == 1: forced += 1
            else: selected += 1
            if chosen.source_node == "platform_input_node": platform_inputs += 1
    final_selected, final_calls = await _compile_final_outputs(draft, source_selector, platform_contract); selector_calls += final_calls
    platform_outputs = sum(edge["to_node"] == "platform_output_node" for edge in draft.compiled_edges)
    draft.metrics = {"function_item_count": len(draft.function_items),
        "runtime_input_count": sum(item["resolution_kind"] in {"platform_runtime", "upstream_runtime", "ambiguous_runtime"} for item in draft.input_resolutions),
        "forced_input_edge_count": forced, "selected_input_edge_count": selected,
        "platform_input_edge_count": platform_inputs, "platform_output_edge_count": platform_outputs,
        "selected_final_output_edge_count": final_selected,
        "static_resolution_count": sum(item["resolution_kind"] == "static_resolved" for item in draft.input_resolutions),
        "default_resolution_count": sum(item["resolution_kind"] == "local_default" for item in draft.input_resolutions),
        "ambiguous_input_count": len(draft.ambiguous_targets),
        "unresolved_input_count": sum(item["resolution_kind"] == "unresolved" for item in draft.input_resolutions),
        "model_source_selection_count": selector_calls, "node_contract_patch_count": 0, "topology_patch_count": 0,
        "graph_validation_issues": copy.deepcopy(draft.issues), "graph_commit_status": "rejected" if draft.issues else "committed"}
    draft.status = "validation_failed" if draft.issues else "committed"; return draft


def patch_graph_node_contract(draft: GraphDraft, operations: list[dict[str, Any]], *,
                              allowed_patch_paths: set[str],
                              allowed_replacement_values_by_path: dict[str, set[str]] | None = None) -> GraphDraft:
    """Apply a bounded local contract patch and return a fresh, uncompiled draft.

    Only replacement of an explicitly allowed scalar/list member is supported;
    graph nodes, resources and platform contracts therefore cannot be created by
    this interface.
    """
    items = copy.deepcopy(draft.function_items)
    for operation in operations:
        path = str(operation.get("path") or "")
        if set(operation) != {"op", "path", "value"} or operation.get("op") != "replace" or path not in allowed_patch_paths:
            raise ValueError("graph contract patch is outside allowed paths")
        allowed_values = (allowed_replacement_values_by_path or {}).get(path)
        if allowed_values is not None and operation.get("value") not in allowed_values:
            raise ValueError("graph contract patch value has no legal structural producer")
        parts = path.strip("/").split("/")
        if len(parts) not in {3, 4} or parts[0] != "function_items":
            raise ValueError("graph contract patch path must address one FunctionItem field")
        item = items[int(parts[1])]; field_name = parts[2]
        if field_name not in {"inputs", "outputs"}:
            raise ValueError("graph contract patch cannot modify identity, resources, or constraints")
        if len(parts) == 3: item[field_name] = copy.deepcopy(operation["value"])
        else: item[field_name][int(parts[3])] = copy.deepcopy(operation["value"])
    return GraphDraft.freeze(items, allowed_predecessors=draft.allowed_predecessors,
        input_bindings=draft.input_bindings, final_output_bindings=draft.final_output_bindings,
        authorized_references=draft.resolver_registry.authorized_references,
        authorized_assets=draft.resolver_registry.authorized_assets)


_patch_graph_node_contract = patch_graph_node_contract


def public_edges(draft: GraphDraft) -> list[dict[str, Any]]:
    if draft.status != "committed": raise ValueError("only a committed graph may be exported")
    keys = {"from_node", "from_output", "to_node", "to_input", "purpose", "constraints"}
    return [{key: copy.deepcopy(value) for key, value in edge.items() if key in keys} for edge in draft.compiled_edges]


def export_script_generation_contracts(draft: GraphDraft) -> list[dict[str, Any]]:
    if draft.status != "committed": raise ValueError("script contracts require a committed graph")
    resolutions = {(item["target_node"], item["target_input"]): item for item in draft.input_resolutions}
    contracts = []
    for item in draft.function_items:
        target = item["target_file"]; incoming = {(edge["to_node"], edge["to_input"]): edge for edge in draft.compiled_edges if edge["to_node"] == target}
        outgoing = [edge for edge in draft.compiled_edges if edge["from_node"] == target]; inputs = []
        for raw in item.get("inputs") or []:
            name = _port(raw)["name"]; resolution = resolutions[(target, name)]; exported = {"name": name, "resolution_kind": resolution["resolution_kind"]}
            edge = incoming.get((target, name))
            if edge:
                exported.update({"source_node": edge["from_node"], "source_output": edge["from_output"]})
                parameter = next((value for value in edge.get("constraints") or [] if value.get("type") == "platform_parameter_binding"), None)
                if parameter: exported["source_key"] = parameter.get("source_key")
            if resolution.get("resolver"):
                resolver = resolution["resolver"]; exported["resolver_kind"] = resolver.get("kind")
                for key in ("path", "value", "name", "default"):
                    if key in resolver: exported[key] = copy.deepcopy(resolver[key])
            inputs.append(exported)
        contracts.append({"target_file": target, "purpose": item.get("purpose", ""), "runtime_inputs": inputs,
            "runtime_outputs": [{"name": _port(raw)["name"], "consumers": [f'{edge["to_node"]}.{edge["to_input"]}' for edge in outgoing if edge["from_output"] == _port(raw)["name"]]} for raw in item.get("outputs") or []],
            "required_capabilities": copy.deepcopy(item.get("required_capabilities") or []), "constraints": copy.deepcopy(item.get("constraints") or []),
            "default_values": copy.deepcopy(item.get("default_values") or {})})
    return contracts
