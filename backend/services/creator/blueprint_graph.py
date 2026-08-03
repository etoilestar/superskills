"""Deterministic preparation of Blueprint facts for ``compiled_v2``.

This module intentionally has no model, persistence, or planning dependency.
It admits only explicit Blueprint structure and compatible ports.  A caller may
resolve the small ambiguity registry separately; it must never invent graph
facts while doing so.
"""
from __future__ import annotations

import copy
import json
from typing import Any, Iterable

from ..skill_plan import normalize_structured_function_items
from .responsibility_graph import type_compatibility


def _port(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return {"name": value, "value_type": "unknown", "semantic_id": "", "required": True}
    return {
        **copy.deepcopy(value),
        "name": str(value.get("name") or ""),
        "value_type": str(value.get("value_type") or value.get("type") or "unknown"),
        "semantic_id": str(value.get("semantic_id") or ""),
        "required": bool(value.get("required", True)),
    }


def _slots(contract: dict[str, Any], *names: str) -> list[dict[str, Any]]:
    boundary = contract.get("platform_skill_boundary") or {}
    raw: Any = []
    for name in names:
        raw = boundary.get(name) or contract.get(name) or raw
    values = raw if isinstance(raw, list) else list(raw or [])
    return [_port(value) for value in values]


def match_port_evidence(
    source: dict[str, Any], target: dict[str, Any], *, relation: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Classify port compatibility without assigning semantic meaning."""
    compatibility = type_compatibility(source["value_type"], target["value_type"])
    if compatibility == "incompatible":
        return {"matched": False, "confidence": "none", "reason": "incompatible value_type"}
    relation = relation or {}
    if relation.get("has_explicit_binding"):
        return {"matched": True, "confidence": "explicit_binding", "reason": "complete explicit port binding"}
    if source["semantic_id"] and target["semantic_id"] and source["semantic_id"] == target["semantic_id"]:
        confidence = "dependency" if relation.get("has_dependency") else "semantic"
        return {"matched": True, "confidence": confidence, "reason": "equal semantic_id with compatible value_type"}
    if source["name"] != target["name"]:
        reason = ("dependency does not establish a compatible port binding"
                  if relation.get("has_dependency") else "port names and semantic_id do not match")
        return {"matched": False, "confidence": "none", "reason": reason}
    if "unknown" not in {source["value_type"], target["value_type"]}:
        confidence = "dependency" if relation.get("has_dependency") else "typed_name"
        return {"matched": True, "confidence": confidence, "reason": "equal port name with declared compatible value_type"}
    return {"matched": True, "confidence": "weak_name", "reason": "equal port name with unknown value_type"}


_CONFIDENCE = {"none": 0, "weak_name": 1, "typed_name": 2, "semantic": 3,
               "dependency": 4, "explicit_binding": 5}
_BINDING_KEYS = frozenset({"binding_kind", "source_node", "source_output", "source_root", "source_key",
                           "target_node", "target_input", "required", "default", "resolver", "platform_slot"})
_ISSUE_KIND = {
    "unknown_node": "unknown_reference", "unknown_port": "unknown_reference",
    "blueprint_unknown_dependency": "unknown_reference", "blueprint_binding_unknown_node": "unknown_reference",
    "blueprint_binding_unknown_port": "unknown_reference", "invalid_platform_boundary": "unknown_reference",
    "type_mismatch": "incompatible_type", "blueprint_binding_type_mismatch": "incompatible_type",
    "blueprint_binding_duplicate_source": "duplicate_binding", "unbound_required_input": "unresolved_input",
    "missing_required_output": "missing_output", "cycle_detected": "cycle",
    "blueprint_binding_cycle": "cycle", "blueprint_explicit_cycle": "cycle",
    "resource_authority_gap": "unauthorized_resource",
}


def _general_issue(issue: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(issue)
    value["issue_type"] = _ISSUE_KIND.get(str(value.get("issue_type") or ""), str(value.get("issue_type") or "unknown_reference"))
    return value


def _cycles(topology: dict[str, list[str]]) -> list[set[str]]:
    """Return cyclic strongly-connected node sets in stable order."""
    index = 0; stack: list[str] = []; on_stack: set[str] = set()
    indexes: dict[str, int] = {}; low: dict[str, int] = {}; result: list[set[str]] = []
    def visit(node: str) -> None:
        nonlocal index
        indexes[node] = low[node] = index; index += 1; stack.append(node); on_stack.add(node)
        for predecessor in sorted(topology.get(node) or []):
            if predecessor not in indexes:
                visit(predecessor); low[node] = min(low[node], low[predecessor])
            elif predecessor in on_stack:
                low[node] = min(low[node], indexes[predecessor])
        if low[node] == indexes[node]:
            component: set[str] = set()
            while stack:
                value = stack.pop(); on_stack.remove(value); component.add(value)
                if value == node: break
            if len(component) > 1 or node in (topology.get(node) or []): result.append(component)
    for node in sorted(topology):
        if node not in indexes: visit(node)
    return result


def normalize_blueprint_graph_facts(
    *, function_items: list[dict[str, Any]], workflow_topology: dict[str, list[str]] | None = None,
    input_bindings: list[dict[str, Any]] | None = None,
    final_output_bindings: list[dict[str, Any]] | None = None,
    platform_contract: dict[str, Any] | None = None,
    resources: list[Any] | None = None,
    authorized_resources: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build candidates from immutable facts, then resolve and apply them in batches."""
    items = sorted(normalize_structured_function_items(function_items, source="blueprint"),
                   key=lambda item: str(item["target_file"]))
    by_node = {str(item["target_file"]): item for item in items}
    original_topology = {node: sorted(set((workflow_topology or {}).get(node) or item.get("dependencies") or []))
                         for node, item in by_node.items()}
    issues: list[dict[str, Any]] = []
    resource_values = copy.deepcopy(resources or [])
    bindings = sorted(copy.deepcopy(input_bindings or []), key=lambda value: (
        str(value.get("target_node")), str(value.get("target_input")), str(value.get("source_node")), str(value.get("source_output"))))
    finals = sorted(copy.deepcopy(final_output_bindings or []), key=lambda value: (
        str(value.get("platform_slot")), str(value.get("source_node")), str(value.get("source_output"))))
    for value in bindings:
        if value.get("binding_kind") == "platform_parameter":
            value.setdefault("source_node", "platform_input_node")
            value.setdefault("source_output", value.get("source_root"))
    binding_topology = copy.deepcopy(original_topology)
    for value in bindings:
        if value.get("binding_kind") == "script_output" and value.get("source_node") in by_node and value.get("target_node") in by_node:
            target = str(value["target_node"]); source = str(value["source_node"])
            binding_topology[target] = sorted(set(binding_topology[target]) | {source})
    bound = {(str(value.get("target_node")), str(value.get("target_input"))) for value in bindings}
    platform_inputs = _slots(platform_contract or {}, "input_envelope_fields", "input_fields")
    registry: dict[tuple[str, str], list[dict[str, Any]]] = {}

    # Phase one: immutable candidate generation.
    for target_node in sorted(by_node):
        item = by_node[target_node]; defaults = item.get("default_values") or {}
        for target in sorted(map(_port, item.get("inputs") or []), key=lambda value: value["name"]):
            key = (target_node, target["name"])
            if key in bound or target["name"] in defaults: continue
            dependency_nodes = {node for node in original_topology[target_node] if node in by_node and node != target_node}
            source_nodes = [node for node in sorted(by_node) if node != target_node]
            candidates: list[dict[str, Any]] = []
            for source_node in source_nodes:
                for source in sorted(map(_port, by_node[source_node].get("outputs") or []), key=lambda value: value["name"]):
                    evidence = match_port_evidence(
                        source, target, relation={"has_dependency": source_node in dependency_nodes,
                                                  "has_explicit_binding": False},
                    )
                    if evidence["matched"]:
                        candidate_topology = copy.deepcopy(binding_topology)
                        candidate_topology[target_node] = sorted(set(candidate_topology[target_node]) | {source_node})
                        if _cycles(candidate_topology):
                            continue
                        candidates.append({"binding_kind": "script_output", "source_node": source_node,
                            "source_output": source["name"], "target_node": target_node, "target_input": target["name"],
                            "match_confidence": evidence["confidence"], "match_reason": evidence["reason"],
                            "source_port": source, "target_port": target})
            for slot in sorted(platform_inputs, key=lambda value: value["name"]):
                evidence = match_port_evidence(slot, target)
                if evidence["matched"]:
                    candidates.append({"binding_kind": "platform_parameter", "source_node": "platform_input_node",
                        "source_output": slot["name"], "source_root": slot["name"], "source_key": slot["name"],
                        "target_node": target_node, "target_input": target["name"], "required": target["required"],
                        "match_confidence": evidence["confidence"], "match_reason": evidence["reason"],
                        "source_port": slot, "target_port": target})
            registry[key] = sorted(candidates, key=lambda value: (
                -_CONFIDENCE[value["match_confidence"]], str(value["source_node"]), str(value["source_output"])))

    # Phase two: retain only the strongest evidence per target.
    deterministic: list[dict[str, Any]] = []; unresolved: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for key in sorted(registry):
        candidates = registry[key]
        if not candidates:
            issues.append({"issue_type": "unbound_required_input", "target_node": key[0], "target_input": key[1]}); continue
        highest = _CONFIDENCE[candidates[0]["match_confidence"]]
        strongest = [value for value in candidates if _CONFIDENCE[value["match_confidence"]] == highest]
        if len(strongest) == 1 and strongest[0]["match_confidence"] != "weak_name": deterministic.append(strongest[0])
        elif len(strongest) > 1: unresolved[key] = strongest
        else: issues.append({"issue_type": "unbound_required_input", "target_node": key[0], "target_input": key[1],
                             "reason": "weak_name_is_not_deterministic"})

    # Batch cycle validation uses all provisional deterministic edges at once.
    provisional = copy.deepcopy(binding_topology)
    for value in deterministic:
        if value["binding_kind"] == "script_output":
            provisional[value["target_node"]] = sorted(set(provisional[value["target_node"]]) | {value["source_node"]})
    cyclic = _cycles(provisional)
    cycle_minimum: dict[frozenset[str], int] = {}
    for component in cyclic:
        internal = [value for value in deterministic if value["binding_kind"] == "script_output"
                    and value["source_node"] in component and value["target_node"] in component]
        if internal:
            cycle_minimum[frozenset(component)] = min(_CONFIDENCE[value["match_confidence"]] for value in internal)
        else:
            issues.append({"issue_type": "blueprint_explicit_cycle", "nodes": sorted(component),
                           "candidate_targets": sorted(component),
                           "reason": "explicit_topology_cycle"})
    safe_deterministic = []
    for value in deterministic:
        component = next((nodes for nodes in cycle_minimum
                          if value["source_node"] in nodes and value["target_node"] in nodes), None)
        should_downgrade = (component is not None and
                            _CONFIDENCE[value["match_confidence"]] == cycle_minimum[component])
        if value["binding_kind"] == "script_output" and should_downgrade:
            key = (value["target_node"], value["target_input"])
            alternatives = [candidate for candidate in registry[key] if candidate is not value]
            if alternatives: unresolved[key] = registry[key]
            else: issues.append({"issue_type": "cycle_detected", "target_node": key[0], "target_input": key[1],
                                 "source_node": value["source_node"]})
        else: safe_deterministic.append(value)

    # Phase three: apply safe deterministic results and assign stable ambiguity ids.
    topology = copy.deepcopy(binding_topology)
    for value in safe_deterministic:
        bindings.append({key: copy.deepcopy(item) for key, item in value.items() if key in _BINDING_KEYS})
        if value["binding_kind"] == "script_output":
            topology[value["target_node"]] = sorted(set(topology[value["target_node"]]) | {value["source_node"]})
    ambiguities: list[dict[str, Any]] = []
    for key, candidates in sorted(unresolved.items()):
        ambiguity_id = f"A{len(ambiguities) + 1}"; target_item = by_node[key[0]]
        target_port = next(_port(value) for value in target_item.get("inputs") or [] if _port(value)["name"] == key[1])
        enriched = []
        for index, value in enumerate(sorted(candidates, key=lambda item: (str(item["source_node"]), str(item["source_output"]))), 1):
            source_item = by_node.get(str(value["source_node"]), {})
            enriched.append({**copy.deepcopy(value), "candidate_id": f"{ambiguity_id}-C{index}",
                             "source_purpose": str(source_item.get("purpose") or "")})
        ambiguities.append({"ambiguity_id": ambiguity_id, "kind": "input_source", "target_node": key[0],
                            "target_input": key[1], "target": {"node": key[0], "purpose": target_item.get("purpose", ""),
                            "input": target_port}, "candidate_sources": enriched,
                            "explicit_dependencies": copy.deepcopy(original_topology[key[0]])})

    if not finals:
        nonterminals = {source for target in topology for source in topology[target]}
        terminals = sorted(set(by_node) - nonterminals)
        output_slots = sorted(_slots(platform_contract or {}, "final_output_fields", "output_fields"), key=lambda value: value["name"])
        for slot in output_slots:
            candidates = []
            for node in terminals:
                for output in sorted(map(_port, by_node[node].get("outputs") or []), key=lambda value: value["name"]):
                    evidence = match_port_evidence(output, slot)
                    if evidence["matched"]:
                        candidates.append({"platform_slot": slot["name"], "source_node": node, "source_output": output["name"],
                            "match_confidence": evidence["confidence"], "match_reason": evidence["reason"],
                            "source_port": output, "source_purpose": by_node[node].get("purpose", "")})
            if candidates:
                highest = max(_CONFIDENCE[value["match_confidence"]] for value in candidates)
                candidates = [value for value in candidates if _CONFIDENCE[value["match_confidence"]] == highest]
            if len(candidates) == 1 and candidates[0]["match_confidence"] != "weak_name":
                finals.append({key: copy.deepcopy(item) for key, item in candidates[0].items() if key in _BINDING_KEYS})
            elif len(candidates) > 1:
                ambiguity_id = f"A{len(ambiguities) + 1}"
                enriched = [{**value, "candidate_id": f"{ambiguity_id}-C{index}"}
                            for index, value in enumerate(sorted(candidates, key=lambda item: (item["source_node"], item["source_output"])), 1)]
                ambiguities.append({"ambiguity_id": ambiguity_id, "kind": "final_output", "target_node": "platform_output_node",
                                    "target_input": slot["name"], "target": {"node": "platform_output_node", "input": slot},
                                    "candidate_sources": enriched})
        if output_slots and not finals and not any(item["kind"] == "final_output" for item in ambiguities):
            issues.append({"issue_type": "missing_required_output", "candidate_targets": terminals})
    bindings.sort(key=lambda value: (str(value.get("target_node")), str(value.get("target_input")), str(value.get("source_node")), str(value.get("source_output"))))
    result = {"function_items": items, "workflow_topology": topology, "input_bindings": bindings,
            "final_output_bindings": finals, "input_candidate_registry": registry,
            "allowed_node_targets": sorted(by_node), "platform_contract": copy.deepcopy(platform_contract or {}),
            "resources": resource_values,
            "authorized_resources": sorted(str(value) for value in (authorized_resources or [])),
            "resource_authority_enforced": authorized_resources is not None,
            "ambiguities": ambiguities, "structural_issues": sorted(issues, key=lambda value: json.dumps(value, sort_keys=True)),
            "metrics": {"function_item_count": len(items), "explicit_edge_count": len(input_bindings or []),
                        "inferred_edge_count": len(bindings) - len(input_bindings or []), "ambiguity_count": len(ambiguities),
                        "model_selection_call_count": 0, "input_boundary_count": sum(x.get("binding_kind") == "platform_parameter" for x in bindings),
                        "final_output_boundary_count": len(finals), "constraint_count": sum(len(x.get("constraints") or []) for x in items)}}
    validation = validate_blueprint_graph_facts(result, allowed_node_targets=by_node, platform_contract=platform_contract or {})
    combined = {json.dumps(value, ensure_ascii=False, sort_keys=True): value
                for value in map(_general_issue, [*result["structural_issues"], *validation["issues"]])}
    result["structural_issues"] = [combined[key] for key in sorted(combined)]
    result["validation"] = {"valid": not result["structural_issues"], "issues": copy.deepcopy(result["structural_issues"])}
    return result


def blueprint_graph_fingerprint(facts: dict[str, Any]) -> str:
    import hashlib
    payload = {key: copy.deepcopy(facts.get(key) or ([] if key != "workflow_topology" else {}))
               for key in ("function_items", "workflow_topology", "input_bindings", "final_output_bindings", "constraints", "resources")}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def validate_blueprint_graph_facts(
    facts: dict[str, Any], *, allowed_node_targets: Iterable[str], platform_contract: dict[str, Any],
) -> dict[str, Any]:
    """Validate one normalized or resolved graph using a single issue vocabulary."""
    items = facts.get("function_items") or []
    allowed = {str(value) for value in allowed_node_targets}
    by_node = {str(item.get("target_file") or ""): item for item in items}
    issues: list[dict[str, Any]] = []
    if set(by_node) != allowed:
        issues.append({"issue_type": "unknown_node", "nodes": sorted(set(by_node) ^ allowed)})
    inputs = {node: {_port(value)["name"]: _port(value) for value in item.get("inputs") or []}
              for node, item in by_node.items()}
    outputs = {node: {_port(value)["name"]: _port(value) for value in item.get("outputs") or []}
               for node, item in by_node.items()}
    for node, item in by_node.items():
        for direction in ("inputs", "outputs"):
            if any(not _port(value)["name"] for value in item.get(direction) or []):
                issues.append({"issue_type": "unknown_reference", "target_node": node, "direction": direction})
    topology = {node: list((facts.get("workflow_topology") or {}).get(node) or []) for node in by_node}
    for node, predecessors in topology.items():
        unknown = sorted(set(predecessors) - allowed)
        if unknown: issues.append({"issue_type": "unknown_node", "target_node": node, "nodes": unknown})
    target_counts: dict[tuple[str, str], int] = {}
    for binding in facts.get("input_bindings") or []:
        kind = str(binding.get("binding_kind") or "")
        target, target_input = str(binding.get("target_node") or ""), str(binding.get("target_input") or "")
        source, source_output = str(binding.get("source_node") or ""), str(binding.get("source_output") or "")
        key = (target, target_input); target_counts[key] = target_counts.get(key, 0) + 1
        if kind not in {"script_output", "platform_parameter", "local_default", "static_value"}:
            issues.append({"issue_type": "invalid_platform_boundary", "target_node": target,
                           "target_input": target_input, "reason": "missing_or_unknown_binding_kind"}); continue
        if target not in by_node:
            issues.append({"issue_type": "unknown_node", "target_node": target}); continue
        if target_input not in inputs[target]:
            issues.append({"issue_type": "unknown_port", "target_node": target, "target_input": target_input}); continue
        if kind == "script_output":
            if source not in by_node:
                issues.append({"issue_type": "unknown_node", "source_node": source}); continue
            if source == target:
                issues.append({"issue_type": "cycle_detected", "source_node": source, "target_node": target}); continue
            if source_output not in outputs[source]:
                issues.append({"issue_type": "unknown_port", "source_node": source, "source_output": source_output}); continue
            if type_compatibility(outputs[source][source_output]["value_type"], inputs[target][target_input]["value_type"]) == "incompatible":
                issues.append({"issue_type": "type_mismatch", "source_node": source, "source_output": source_output,
                               "target_node": target, "target_input": target_input})
            topology[target] = sorted(set(topology[target]) | {source})
        elif kind == "platform_parameter":
            slots = {value["name"] for value in _slots(platform_contract, "input_envelope_fields", "input_fields")}
            if source_output not in slots:
                issues.append({"issue_type": "invalid_platform_boundary", "source_output": source_output})
    for key, count in target_counts.items():
        if count > 1: issues.append({"issue_type": "duplicate_binding", "target_node": key[0], "target_input": key[1]})
    ambiguous_targets = {(str(value.get("target_node")), str(value.get("target_input")))
                         for value in facts.get("ambiguities") or [] if value.get("kind") != "final_output"}
    for node, ports in inputs.items():
        defaults = by_node[node].get("default_values") or {}
        for name, port in ports.items():
            if port.get("required", True) and target_counts.get((node, name), 0) == 0 and name not in defaults and (node, name) not in ambiguous_targets:
                issues.append({"issue_type": "unbound_required_input", "target_node": node, "target_input": name})
    for component in _cycles(topology):
        issues.append({"issue_type": "cycle_detected", "nodes": sorted(component)})
    final_slots = {value["name"] for value in _slots(platform_contract, "final_output_fields", "output_fields")}
    for binding in facts.get("final_output_bindings") or []:
        source, output = str(binding.get("source_node") or ""), str(binding.get("source_output") or "")
        slot = str(binding.get("platform_slot") or "")
        if source not in by_node: issues.append({"issue_type": "unknown_node", "source_node": source}); continue
        if output not in outputs[source]: issues.append({"issue_type": "unknown_port", "source_node": source, "source_output": output})
        if slot not in final_slots: issues.append({"issue_type": "invalid_platform_boundary", "platform_slot": slot})
    authorized = set(facts.get("authorized_resources") or [])
    if facts.get("resource_authority_enforced"):
        for value in facts.get("resources") or []:
            path = str(value.get("path") if isinstance(value, dict) else value)
            if path not in authorized: issues.append({"issue_type": "unauthorized_resource", "path": path})
    generalized = [_general_issue(value) for value in issues]
    return {"valid": not generalized, "issues": sorted(generalized, key=lambda value: json.dumps(value, sort_keys=True))}


def validate_resolved_blueprint_graph_facts(facts: dict[str, Any]) -> dict[str, Any]:
    result = validate_blueprint_graph_facts(
        facts, allowed_node_targets=facts.get("allowed_node_targets") or [],
        platform_contract=facts.get("platform_contract") or {},
    )
    if facts.get("ambiguities"):
        result["issues"].append({"issue_type": "ambiguity_unresolved"}); result["valid"] = False
    return result

def parse_ambiguity_selections(text: str, ambiguities: Iterable[dict[str, Any]]) -> dict[str, str]:
    """Strictly validate the bounded ambiguity selection protocol."""
    raw = text.strip()
    if raw.startswith("```") and raw.endswith("```"):
        lines = raw.splitlines(); raw = "\n".join(lines[1:-1]).strip()
    decoder = json.JSONDecoder(); documents = []
    while raw:
        value, end = decoder.raw_decode(raw); documents.append(value); raw = raw[end:].strip()
    if not documents or any(value != documents[0] for value in documents[1:]):
        raise ValueError("ambiguity_selection_protocol_gap: multiple different JSON objects")
    payload = documents[0]
    if not isinstance(payload, dict) or set(payload) != {"selections"} or not isinstance(payload["selections"], list):
        raise ValueError("ambiguity_selection_protocol_gap")
    registry = {item["ambiguity_id"]: {candidate["candidate_id"] for candidate in item["candidate_sources"]}
                for item in ambiguities}
    selected: dict[str, str] = {}
    for value in payload["selections"]:
        if not isinstance(value, dict) or set(value) != {"ambiguity_id", "selected_candidate_id"}:
            raise ValueError("ambiguity_selection_protocol_gap")
        ambiguity_id = str(value["ambiguity_id"]); candidate_id = str(value["selected_candidate_id"])
        if ambiguity_id in selected:
            raise ValueError("ambiguity_selection_protocol_gap: duplicate ambiguity")
        if ambiguity_id not in registry or candidate_id not in registry[ambiguity_id]:
            raise ValueError("ambiguity_selection_invalid_candidate")
        selected[ambiguity_id] = candidate_id
    if set(selected) != set(registry):
        raise ValueError("ambiguity_selection_protocol_gap: missing ambiguity")
    return selected


def apply_ambiguity_selections(
    normalized_facts: dict[str, Any], selections: dict[str, str],
) -> dict[str, Any]:
    """Apply validated opaque choices without creating or rewriting facts."""
    result = copy.deepcopy(normalized_facts)
    ambiguities = result.get("ambiguities") or []
    expected = {str(item.get("ambiguity_id")) for item in ambiguities}
    if set(selections) != expected:
        raise ValueError("ambiguity_selection_protocol_gap: selections must cover every ambiguity")
    for ambiguity in ambiguities:
        ambiguity_id = str(ambiguity["ambiguity_id"])
        candidates = {str(value["candidate_id"]): value for value in ambiguity.get("candidate_sources") or []}
        candidate_id = selections[ambiguity_id]
        if candidate_id not in candidates:
            raise ValueError("ambiguity_selection_invalid_candidate")
        selected = {key: copy.deepcopy(value) for key, value in candidates[candidate_id].items()
                    if key in _BINDING_KEYS}
        if ambiguity.get("kind") == "final_output":
            result["final_output_bindings"].append(selected)
        else:
            result["input_bindings"].append(selected)
            if selected.get("binding_kind") == "script_output":
                target = str(selected["target_node"]); source = str(selected["source_node"])
                result["workflow_topology"][target] = sorted(
                    set(result["workflow_topology"].get(target) or []) | {source}
                )
    result["ambiguities"] = []
    result["metrics"]["model_selection_call_count"] = 1 if ambiguities else 0
    result["metrics"]["inferred_edge_count"] = (
        len(result["input_bindings"]) - int(result["metrics"].get("explicit_edge_count") or 0)
    )
    result["metrics"]["final_output_boundary_count"] = len(result["final_output_bindings"])
    validation = validate_resolved_blueprint_graph_facts(result)
    if not validation["valid"]:
        raise ValueError(f"ambiguity_selection_structural_conflict: {validation['issues']}")
    return result
