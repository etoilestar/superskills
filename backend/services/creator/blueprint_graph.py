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


def _matches(source: dict[str, Any], target: dict[str, Any]) -> bool:
    semantic = source["semantic_id"] and source["semantic_id"] == target["semantic_id"]
    named = source["name"] == target["name"]
    return bool((semantic or named) and type_compatibility(source["value_type"], target["value_type"]) != "incompatible")


def normalize_blueprint_graph_facts(
    *, function_items: list[dict[str, Any]], workflow_topology: dict[str, list[str]] | None = None,
    input_bindings: list[dict[str, Any]] | None = None,
    final_output_bindings: list[dict[str, Any]] | None = None,
    platform_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return stable compiler input and only the genuinely ambiguous choices."""
    items = normalize_structured_function_items(function_items, source="blueprint")
    by_node = {str(item["target_file"]): item for item in items}
    topology = {node: sorted(set((workflow_topology or {}).get(node) or item.get("dependencies") or []))
                for node, item in by_node.items()}
    bindings = copy.deepcopy(input_bindings or [])
    finals = copy.deepcopy(final_output_bindings or [])
    issues: list[dict[str, Any]] = []
    ambiguities: list[dict[str, Any]] = []
    bound = {(str(value.get("target_node")), str(value.get("target_input"))) for value in bindings}
    contract = platform_contract or {}
    platform_inputs = _slots(contract, "input_envelope_fields", "input_fields")

    def ambiguity(kind: str, target_node: str, target_port: str, candidates: list[dict[str, Any]]) -> None:
        ambiguity_id = f"A{len(ambiguities) + 1}"
        ambiguities.append({
            "ambiguity_id": ambiguity_id, "kind": kind,
            "target_node": target_node, "target_input": target_port,
            "candidate_sources": [{"candidate_id": f"{ambiguity_id}-C{i}", **candidate}
                                  for i, candidate in enumerate(candidates, 1)],
        })

    for target_node, item in by_node.items():
        defaults = item.get("default_values") or {}
        for raw_input in item.get("inputs") or []:
            target = _port(raw_input)
            key = (target_node, target["name"])
            if key in bound or target["name"] in defaults:
                continue
            predecessors = [node for node in topology[target_node] if node in by_node and node != target_node]
            candidates = []
            for source_node in predecessors:
                for raw_output in by_node[source_node].get("outputs") or []:
                    source = _port(raw_output)
                    if _matches(source, target):
                        candidates.append({"binding_kind": "script_output", "source_node": source_node,
                                           "source_output": source["name"], "target_node": target_node,
                                           "target_input": target["name"]})
            if len(candidates) == 1:
                bindings.append(candidates[0]); bound.add(key)
            elif len(candidates) > 1:
                ambiguity("input_source", target_node, target["name"], candidates)
            else:
                boundary = [{"binding_kind": "platform_parameter", "source_node": "platform_input_node",
                             "source_output": slot["name"], "source_root": slot["name"],
                             "source_key": slot["name"], "target_node": target_node,
                             "target_input": target["name"], "required": target["required"]}
                            for slot in platform_inputs if _matches(slot, target)]
                if len(boundary) == 1:
                    bindings.append(boundary[0]); bound.add(key)
                elif len(boundary) > 1:
                    ambiguity("platform_input", target_node, target["name"], boundary)
                elif target["required"]:
                    issues.append({"issue_type": "unbound_required_input", "target_node": target_node,
                                   "target_input": target["name"]})

    if not finals:
        nonterminals = {source for target in topology for source in topology[target]}
        terminals = sorted(set(by_node) - nonterminals)
        output_slots = _slots(contract, "final_output_fields", "output_fields")
        for slot in output_slots:
            candidates = [{"platform_slot": slot["name"], "source_node": node, "source_output": output["name"]}
                          for node in terminals for output in map(_port, by_node[node].get("outputs") or [])
                          if _matches(output, slot)]
            if len(candidates) == 1:
                finals.append(candidates[0])
            elif len(candidates) > 1:
                ambiguity("final_output", "platform_output_node", slot["name"], candidates)
            # Platform output fields are an accepted vocabulary, not a list of
            # fields every skill must emit.  A gap exists only when no terminal
            # output matches any accepted field (checked after this loop).
        if output_slots and not finals and not any(item["kind"] == "final_output" for item in ambiguities):
            issues.append({"issue_type": "missing_required_output"})
    return {"function_items": items, "workflow_topology": topology, "input_bindings": bindings,
            "final_output_bindings": finals, "ambiguities": ambiguities, "structural_issues": issues,
            "metrics": {"function_item_count": len(items), "explicit_edge_count": len(input_bindings or []),
                        "inferred_edge_count": len(bindings) - len(input_bindings or []),
                        "ambiguity_count": len(ambiguities), "model_selection_call_count": 0,
                        "input_boundary_count": sum(x.get("binding_kind") == "platform_parameter" for x in bindings),
                        "final_output_boundary_count": len(finals),
                        "constraint_count": sum(len(x.get("constraints") or []) for x in items)}}


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
