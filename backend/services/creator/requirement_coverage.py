"""Pure, deterministic requirement-coverage transformations for Creator.

This module deliberately has no model, database, or Blueprint mutation access.
It turns frozen user requirement identities and frozen structural facts into an
opaque candidate domain, and validates selections at the stage that owns them.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from typing import Any, Iterable

COVERAGE_KINDS = ("node", "dataflow", "boundary", "resource", "constraint")
VALID_STATUSES = frozenset({"satisfied", "deferred", "missing_structure", "contradicted"})


def _stable(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_stable(value).encode("utf-8")).hexdigest()


def extract_frozen_requirements(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assign backend-owned IDs while preserving extraction order and evidence."""
    frozen: list[dict[str, Any]] = []
    for index, raw in enumerate(items, 1):
        if not isinstance(raw, dict) or not str(raw.get("requirement") or "").strip():
            raise ValueError("each extracted requirement must contain non-empty requirement text")
        evidence = raw.get("source_evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("each extracted requirement must contain source_evidence")
        normalized_evidence = []
        for item in evidence:
            if not isinstance(item, dict) or not str(item.get("source") or "").strip() or not str(item.get("quote") or "").strip():
                raise ValueError("source evidence requires source and quote")
            normalized_evidence.append({"source": str(item["source"]).strip(), "quote": str(item["quote"]).strip()})
        frozen.append({
            "requirement_id": f"R{index}",
            "requirement": str(raw["requirement"]).strip(),
            "source_evidence": normalized_evidence,
        })
    return frozen


def requirement_fingerprint(requirements: Iterable[dict[str, Any]]) -> str:
    identity = [{
        "requirement_id": item.get("requirement_id"),
        "requirement": item.get("requirement"),
        "source_evidence": item.get("source_evidence"),
    } for item in requirements]
    return _fingerprint(identity)


def candidate_fingerprint(registry: dict[str, dict[str, Any]]) -> str:
    return _fingerprint([{**registry[key]} for key in sorted(registry, key=_candidate_sort_key)])


def _candidate_sort_key(candidate_id: str) -> tuple[int, int, str]:
    prefixes = {"N": 0, "D": 1, "B": 2, "RSC": 3, "C": 4}
    prefix = next((p for p in ("RSC", "N", "D", "B", "C") if candidate_id.startswith(p)), "")
    suffix = candidate_id[len(prefix):]
    return prefixes.get(prefix, 99), int(suffix) if suffix.isdigit() else 0, candidate_id


def _ports(raw: Any) -> list[dict[str, Any]]:
    result = []
    for value in raw or []:
        if isinstance(value, str):
            result.append({"name": value, "value_type": "unknown", "semantic_id": ""})
        elif isinstance(value, dict) and str(value.get("name") or "").strip():
            result.append({
                "name": str(value["name"]),
                "value_type": str(value.get("value_type") or value.get("type") or "unknown").lower(),
                "semantic_id": str(value.get("semantic_id") or ""),
            })
    return result


def _compatible(left: str, right: str) -> bool:
    return left == right or "unknown" in {left, right} or (left == "integer" and right == "number")


def _contract_slots(platform_contract: dict[str, Any], key: str) -> list[str]:
    boundary = platform_contract.get("platform_skill_boundary") if isinstance(platform_contract, dict) else {}
    values = (boundary or {}).get(key) or platform_contract.get(key) or []
    return sorted({str(value) for value in values if str(value).strip()})


def build_requirement_coverage_candidates(
    *, frozen_requirements: list[dict[str, Any]], frozen_blueprint: Any,
    function_items: list[dict[str, Any]], allowed_function_item_targets: Iterable[str],
    platform_contract: dict[str, Any], authorized_references: Iterable[str] = (),
    authorized_assets: Iterable[str] = (), uploaded_files: Iterable[Any] = (),
) -> dict[str, dict[str, Any]]:
    """Build a finite candidate registry solely from frozen structural facts."""
    del frozen_requirements  # identity is deliberately independent from structure generation
    blueprint = frozen_blueprint if isinstance(frozen_blueprint, dict) else {}
    allowed = {str(value) for value in allowed_function_item_targets}
    items = [copy.deepcopy(item) for item in function_items if str(item.get("target_file") or "") in allowed]
    items.sort(key=lambda item: _stable(item))
    raw: dict[str, list[dict[str, Any]]] = {kind: [] for kind in COVERAGE_KINDS}
    by_target = {str(item["target_file"]): item for item in items}
    for item in items:
        raw["node"].append({"kind": "node", **{key: copy.deepcopy(item.get(key) or ([] if key != "purpose" else "")) for key in ("target_file", "purpose", "inputs", "outputs", "required_capabilities", "constraints")}})

    flow_evidence: dict[tuple[str, str], set[str]] = {}
    def add_flow(source: Any, target: Any, evidence: str) -> None:
        source, target = str(source or ""), str(target or "")
        if source in by_target and target in by_target and source != target:
            flow_evidence.setdefault((source, target), set()).add(evidence)
    topology = blueprint.get("workflow_topology") or blueprint.get("allowed_predecessors") or {}
    if isinstance(topology, dict):
        for target, sources in topology.items():
            for source in sources or []:
                add_flow(source, target, "workflow_topology")
    for target, item in by_target.items():
        for source in item.get("dependencies") or []:
            add_flow(source, target, "explicit_dependency")
    bindings = list(blueprint.get("input_bindings") or [])
    for binding in bindings:
        if isinstance(binding, dict) and binding.get("source_node"):
            add_flow(binding.get("source_node"), binding.get("target_node"), "input_binding")
    # Port evidence is bounded to pairs already admitted by explicit topology/bindings.
    for source, target in list(flow_evidence):
        source_ports, target_ports = _ports(by_target[source].get("outputs")), _ports(by_target[target].get("inputs"))
        if any((a["semantic_id"] and a["semantic_id"] == b["semantic_id"]) or
               (a["name"] == b["name"] and _compatible(a["value_type"], b["value_type"]))
               for a in source_ports for b in target_ports):
            flow_evidence[(source, target)].add("compatible_ports")
    for (source, target), evidence in flow_evidence.items():
        raw["dataflow"].append({"kind": "dataflow", "source_target": source, "target_target": target, "evidence": sorted(evidence)})

    input_slots = _contract_slots(platform_contract, "input_envelope_fields")
    for binding in bindings:
        if not isinstance(binding, dict) or str(binding.get("binding_kind") or "") != "platform_parameter":
            continue
        target = str(binding.get("target_node") or "")
        slot = str(binding.get("source_root") or binding.get("source_output") or "")
        if target in by_target and slot in input_slots:
            raw["boundary"].append({"kind": "boundary", "direction": "input", "platform_slot": slot, "node_target": target, "node_port": str(binding.get("target_input") or "")})
    finals = list(blueprint.get("final_output_bindings") or [])
    final_slots = _contract_slots(platform_contract, "final_output_fields")
    for binding in finals:
        if isinstance(binding, dict) and str(binding.get("source_node") or "") in by_target and str(binding.get("platform_slot") or "") in final_slots:
            raw["boundary"].append({"kind": "boundary", "direction": "output", "platform_slot": str(binding["platform_slot"]), "node_target": str(binding["source_node"]), "node_port": str(binding.get("source_output") or "")})
    if not finals:
        consumed = {source for source, _ in flow_evidence}
        terminals = sorted(set(by_target) - consumed)
        for target in terminals:
            for output in _ports(by_target[target].get("outputs")):
                for slot in final_slots:
                    # Finite terminal-only domain; semantic selection resolves differently named slots.
                    raw["boundary"].append({"kind": "boundary", "direction": "output", "platform_slot": slot, "node_target": target, "node_port": output["name"]})

    resources = []
    resources.extend((path, "reference", "authorized") for path in authorized_references)
    resources.extend((path, "asset", "authorized") for path in authorized_assets)
    for value in uploaded_files:
        path = value.get("path") if isinstance(value, dict) else value
        resources.append((path, "uploaded_file", "user_uploaded"))
    for value in blueprint.get("resources") or []:
        if isinstance(value, dict):
            resources.append((value.get("path"), value.get("resource_kind") or value.get("kind") or "bundled_resource", value.get("source") or "blueprint"))
    for path, resource_kind, source in resources:
        if str(path or "").strip():
            raw["resource"].append({"kind": "resource", "path": str(path), "resource_kind": str(resource_kind), "source": str(source)})

    for item in items:
        facts = list(item.get("constraints") or []) + [{"forbidden_capability": value} for value in item.get("forbidden_capabilities") or []]
        if facts:
            raw["constraint"].append({"kind": "constraint", "scope": "targets", "target_files": [item["target_file"]], "constraint_facts": facts})
    global_facts = list(blueprint.get("constraints") or []) + [{"forbidden_capability": value} for value in blueprint.get("forbidden_capabilities") or []]
    if global_facts:
        raw["constraint"].append({"kind": "constraint", "scope": "skill", "target_files": [], "constraint_facts": global_facts})

    prefixes = {"node": "N", "dataflow": "D", "boundary": "B", "resource": "RSC", "constraint": "C"}
    registry: dict[str, dict[str, Any]] = {}
    for kind in COVERAGE_KINDS:
        unique = {_stable(value): value for value in raw[kind]}
        for index, key in enumerate(sorted(unique), 1):
            candidate_id = f"{prefixes[kind]}{index}"
            registry[candidate_id] = {"candidate_id": candidate_id, **unique[key]}
    return registry


def coverage_claims_from_selections(
    frozen_requirements: list[dict[str, Any]], selections: Iterable[dict[str, Any]],
    candidate_registry: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate opaque selections and group them into stable per-kind claims."""
    requirement_ids = [str(item["requirement_id"]) for item in frozen_requirements]
    selection_by_id: dict[str, list[str]] = {}
    for selection in selections:
        requirement_id = str(selection.get("requirement_id") or "")
        if requirement_id not in requirement_ids or requirement_id in selection_by_id:
            raise ValueError("selection requirement IDs must be unique members of the frozen set")
        candidate_ids = selection.get("selected_candidate_ids") or []
        if not isinstance(candidate_ids, list) or any(value not in candidate_registry for value in candidate_ids):
            raise ValueError("selection contains an unknown candidate ID")
        selection_by_id[requirement_id] = sorted(set(candidate_ids), key=_candidate_sort_key)
    if set(selection_by_id) != set(requirement_ids):
        raise ValueError("selections must cover the complete frozen requirement set")
    result = []
    for requirement_id in requirement_ids:
        grouped: dict[str, list[str]] = {}
        for candidate_id in selection_by_id[requirement_id]:
            grouped.setdefault(candidate_registry[candidate_id]["kind"], []).append(candidate_id)
        claims = [{"claim_id": f"{requirement_id}-C{index}", "kind": kind, "selected_candidate_ids": grouped[kind]}
                  for index, kind in enumerate((kind for kind in COVERAGE_KINDS if kind in grouped), 1)]
        result.append({"requirement_id": requirement_id, "coverage_claims": claims})
    return result


def materialize_legacy_requirement_view(
    frozen_requirements: list[dict[str, Any]], coverage_claims: list[dict[str, Any]],
    candidate_registry: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    claims_by_id = {item["requirement_id"]: item.get("coverage_claims") or [] for item in coverage_claims}
    allocations, channels = [], {}
    for requirement in frozen_requirements:
        requirement_id = requirement["requirement_id"]
        claims = claims_by_id.get(requirement_id, [])
        selected = [candidate_registry[candidate_id] for claim in claims for candidate_id in claim.get("selected_candidate_ids") or []]
        owners = sorted({candidate["target_file"] for candidate in selected if candidate["kind"] == "node"})
        kinds = {candidate["kind"] for candidate in selected}
        channel = "resource" if kinds == {"resource"} else "direct" if kinds == {"boundary"} else "executable"
        channels[requirement_id] = channel
        allocations.append({"requirement_id": requirement_id, "requirement": requirement["requirement"], "owners": owners,
                            "coverage_claims": copy.deepcopy(claims), "evidence": {"candidate_ids": [candidate["candidate_id"] for candidate in selected]}})
    return {"requirement_allocations": allocations, "requirement_channels": channels}


def validate_requirement_coverage_preflight(
    coverage_claims: list[dict[str, Any]], candidate_registry: dict[str, dict[str, Any]],
    function_items: list[dict[str, Any]], allowed_function_item_targets: Iterable[str],
    authorized_references: Iterable[str] = (), authorized_assets: Iterable[str] = (),
) -> list[dict[str, Any]]:
    targets = {str(item.get("target_file") or "") for item in function_items} & {str(value) for value in allowed_function_item_targets}
    authorized = {str(value) for value in authorized_references} | {str(value) for value in authorized_assets}
    results = []
    for entry in coverage_claims:
        for claim in entry.get("coverage_claims") or []:
            ids = claim.get("selected_candidate_ids") or []
            candidates = [candidate_registry.get(value) for value in ids]
            kind = claim.get("kind")
            status, stage = "satisfied", "blueprint"
            if not ids:
                status = "missing_structure"
            elif kind not in COVERAGE_KINDS or any(candidate is None or candidate.get("kind") != kind for candidate in candidates):
                status = "contradicted"
            elif kind == "node" and any(candidate["target_file"] not in targets for candidate in candidates):
                status = "contradicted"
            elif kind == "dataflow":
                stage = "responsibility_graph"
                status = "deferred" if all(candidate["source_target"] in targets and candidate["target_target"] in targets for candidate in candidates) else "contradicted"
            elif kind == "boundary":
                stage, status = "responsibility_graph", "deferred"
                if any(candidate["node_target"] not in targets for candidate in candidates):
                    status = "contradicted"
            elif kind == "resource" and any(candidate["source"] == "authorized" and candidate["path"] not in authorized for candidate in candidates):
                status = "contradicted"
            results.append({"requirement_id": entry["requirement_id"], "claim_id": claim.get("claim_id"), "kind": kind,
                            "status": status, "owner_stage": stage, "evidence": {"selected_candidate_ids": list(ids)}})
    return results


def should_replan_blueprint(results: Iterable[dict[str, Any]], *, replan_count: int) -> bool:
    return replan_count == 0 and any(item.get("status") == "missing_structure" and item.get("owner_stage") == "blueprint" for item in results)


def validate_compiled_coverage_claims(
    coverage_claims: list[dict[str, Any]], candidate_registry: dict[str, dict[str, Any]],
    committed_function_items: list[dict[str, Any]], committed_edges: list[dict[str, Any]],
    platform_contract: dict[str, Any],
) -> list[dict[str, Any]]:
    targets = {str(item.get("target_file") or "") for item in committed_function_items}
    edges = {(str(edge.get("from_node") or ""), str(edge.get("to_node") or ""), str(edge.get("to_input") or "")) for edge in committed_edges}
    input_slots = set(_contract_slots(platform_contract, "input_envelope_fields"))
    output_slots = set(_contract_slots(platform_contract, "final_output_fields"))
    results = []
    for entry in coverage_claims:
        for claim in entry.get("coverage_claims") or []:
            candidates = [candidate_registry.get(value) for value in claim.get("selected_candidate_ids") or []]
            kind, status, issue = claim.get("kind"), "satisfied", ""
            if any(candidate is None for candidate in candidates):
                status, issue = "contradicted", "candidate_domain_gap"
            elif kind == "dataflow" and any((candidate["source_target"], candidate["target_target"]) not in {(a, b) for a, b, _ in edges} for candidate in candidates):
                status, issue = "contradicted", "dataflow_contract_gap"
            elif kind == "boundary":
                for candidate in candidates:
                    if candidate["direction"] == "output" and (candidate["node_target"], "platform_output_node", candidate["platform_slot"]) not in edges:
                        status, issue = "contradicted", "final_output_contract_gap"
                    elif candidate["direction"] == "input" and not any(a == "platform_input_node" and b == candidate["node_target"] for a, b, _ in edges):
                        status, issue = "contradicted", "input_boundary_contract_gap"
                    elif candidate["platform_slot"] not in (output_slots if candidate["direction"] == "output" else input_slots):
                        status, issue = "contradicted", "platform_slot_gap"
            elif kind == "node" and any(candidate["target_file"] not in targets for candidate in candidates):
                status, issue = "contradicted", "node_contract_gap"
            results.append({"requirement_id": entry["requirement_id"], "claim_id": claim.get("claim_id"), "kind": kind,
                            "status": status, "owner_stage": "responsibility_graph", "issue_type": issue})
    return results


def coverage_metrics(results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    values = list(results)
    statuses, stages = Counter(item.get("status") for item in values), Counter(item.get("owner_stage") for item in values)
    return {**{f"{status}_count": statuses[status] for status in VALID_STATUSES}, "owner_stage_counts": dict(sorted(stages.items()))}
