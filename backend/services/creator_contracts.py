"""Canonical Creator script contracts and evidence validation.

The code here is intentionally generic: it compiles normalized SkillPlan data
into a per-file canonical contract, resolves whether a callable registered tool
or Creator-owned local implementation should be used, and validates generated
Python source with AST evidence rather than business field names.
"""

from __future__ import annotations

import ast
import logging
import math
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Literal
import httpx

from backend.config import settings
from .creator_tool_registry import ToolCapability, list_tool_capabilities
from .skill_plan import SkillPlanEntry

ImplementationMode = Literal["script_composition", "unresolved"]
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CallableToolManifest:
    tool_id: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    artifact_outputs: list[dict[str, Any]] = field(default_factory=list)
    side_effects: list[str] = field(default_factory=list)
    import_path: str = ""
    function_name: str = ""
    dependencies: list[str] = field(default_factory=list)
    example_call: str = ""
    signature: str = ""
    return_contract: str = ""
    example_return: str = ""
    example_stdout: str = ""
    common_mistakes: list[str] = field(default_factory=list)
    snippets: list[dict[str, Any]] = field(default_factory=list)
    usage_policy: str = ""
    required_env: list[str] = field(default_factory=list)
    required_secrets: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CapabilityRequirement:
    capability_id: str
    source: str = "skill_plan"
    required: bool = True


@dataclass(frozen=True)
class CanonicalFileContract:
    file_path: str
    file_kind: str
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    stdout_schema: dict[str, Any] = field(default_factory=dict)
    artifact_contract: dict[str, Any] = field(default_factory=dict)
    functional_requirements: list[str] = field(default_factory=list)
    capability_requirements: list[CapabilityRequirement] = field(default_factory=list)
    side_effects: list[str] = field(default_factory=list)
    resource_refs: list[str] = field(default_factory=list)
    declared_dependencies: list[str] = field(default_factory=list)
    upstream_dependencies: list[str] = field(default_factory=list)
    downstream_consumers: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ImplementationResolution:
    mode: ImplementationMode
    selected_tools: list[CallableToolManifest] = field(default_factory=list)  # compatibility; not populated for candidate-only available_tools
    available_tools: list[CallableToolManifest] = field(default_factory=list)
    selected_adapters: list[str] = field(default_factory=list)
    output_mappings: list[dict[str, str]] = field(default_factory=list)
    tool_slots: list[dict[str, Any]] = field(default_factory=list)
    local_fallback_allowed: bool = True
    allowed_imports: list[str] = field(default_factory=list)
    declared_dependencies: list[str] = field(default_factory=list)
    required_evidence: list[str] = field(default_factory=list)
    reason: str = ""


def _schema_required(schema: dict[str, Any]) -> list[str]:
    raw = schema.get("required") if isinstance(schema, dict) else []
    return [str(x) for x in raw] if isinstance(raw, list) else []


def _dependency_import_roots(dependencies: list[Any]) -> list[str]:
    roots: list[str] = []
    for dep in dependencies or []:
        if isinstance(dep, dict):
            imports = dep.get("imports") or dep.get("import_names") or []
            if isinstance(imports, str):
                imports = [imports]
            for item in imports:
                root = str(item).split(".")[0].replace("-", "_").strip()
                if root and root not in roots:
                    roots.append(root)
            package = str(dep.get("package") or dep.get("name") or "").strip()
            if package:
                root = package.split(".")[0].replace("-", "_")
                if root and root not in roots:
                    roots.append(root)
        elif isinstance(dep, str) and dep.strip():
            root = dep.split(".")[0].replace("-", "_").strip()
            if root and root not in roots:
                roots.append(root)
    return roots


def callable_manifest_from_capability(cap: ToolCapability) -> list[CallableToolManifest]:
    manifests: list[CallableToolManifest] = []
    for fn in cap.functions or []:
        if not fn.import_path or not fn.function_name:
            continue
        deps = _dependency_import_roots(list(cap.dependencies or []))
        manifests.append(CallableToolManifest(
            tool_id=f"{cap.name}.{fn.function_name}",
            description=fn.when_to_use or fn.short_description or cap.display_name,
            input_schema=fn.input_schema or cap.input_schema or {},
            output_schema=fn.output_schema or cap.output_schema or {},
            artifact_outputs=getattr(fn, "artifact_outputs", []) or getattr(cap, "artifact_outputs", []) or [],
            side_effects=list(getattr(fn, "side_effects", []) or getattr(cap, "side_effects", []) or []),
            import_path=fn.import_path,
            function_name=fn.function_name,
            dependencies=deps,
            example_call=fn.example_call,
            signature=fn.signature,
            return_contract=fn.return_contract,
            example_return=fn.example_return,
            example_stdout=fn.example_stdout,
            common_mistakes=list(fn.common_mistakes or []),
            snippets=[asdict(snippet) for snippet in (cap.snippets or []) if fn.function_name in str(snippet.code or "") or fn.function_name in str(snippet.id or "")],
            usage_policy=fn.usage_policy or cap.usage_policy,
            required_env=list(fn.required_env or cap.required_env or []),
            required_secrets=list(fn.required_secrets or cap.required_secrets or []),
        ))
    return manifests


def compile_canonical_file_contract(entry: SkillPlanEntry, stdout_schema: dict[str, Any]) -> CanonicalFileContract:
    requirements = _capability_requirements_from_entry(entry)
    artifact_contract = getattr(entry, "artifact_contract", {}) or {"stdout_fields": list(entry.outputs or [])}
    inputs = [key for key in (entry.inputs or []) if _is_script_io_key(key)]
    outputs = [key for key in (entry.outputs or []) if _is_script_io_key(key)]
    return CanonicalFileContract(
        file_path=entry.path,
        file_kind=getattr(entry, "file_kind", entry.file_type),
        inputs=inputs,
        outputs=outputs,
        stdout_schema=stdout_schema,
        artifact_contract=artifact_contract,
        functional_requirements=_functional_requirements_from_entry(entry),
        capability_requirements=requirements,
        side_effects=list(getattr(entry, "side_effects", []) or []),
        resource_refs=[getattr(r, "path", str(r)) for r in (getattr(entry, "resources", []) or [])],
        declared_dependencies=[str(dep) for dep in (entry.dependencies or []) if _is_declared_dependency(dep)],
        upstream_dependencies=list(getattr(entry, "upstream_dependencies", []) or []),
        downstream_consumers=list(getattr(entry, "downstream_consumers", []) or []),
    )


def _functional_requirements_from_entry(entry: SkillPlanEntry) -> list[str]:
    out: list[str] = []
    for slot in getattr(entry, "required_tool_slots", []) or []:
        if isinstance(slot, dict):
            text = slot.get("functional_requirement") or slot.get("purpose") or slot.get("slot_id")
        else:
            text = getattr(slot, "functional_requirement", "") or getattr(slot, "slot_id", "")
        if str(text or "").strip():
            out.append(str(text).strip())
    for strategy in getattr(entry, "implementation_strategy", []) or []:
        reason = strategy.get("reason") if isinstance(strategy, dict) else getattr(strategy, "reason", "")
        if str(reason or "").strip():
            out.append(str(reason).strip())
    if not out and str(getattr(entry, "purpose", "") or "").strip():
        out.append(str(entry.purpose).strip())
    return list(dict.fromkeys(out))


def _capability_requirements_from_entry(entry: SkillPlanEntry) -> list[CapabilityRequirement]:
    out: list[CapabilityRequirement] = []
    seen: set[tuple[str, str]] = set()

    def add(values: Any, *, source: str, required: bool) -> None:
        for raw in _iter_capability_values(values):
            key = (raw, source)
            if key in seen:
                continue
            seen.add(key)
            out.append(CapabilityRequirement(raw, source=source, required=required))

    add(getattr(entry, "required_capabilities", []) or [], source="skill_plan", required=True)
    add(getattr(entry, "raw_capability_hints", []) or [], source="raw_capability_hints", required=False)
    add(getattr(entry, "optional_capabilities", []) or [], source="optional_capabilities", required=False)
    add(getattr(entry, "allowed_capabilities", []) or [], source="allowed_capabilities", required=False)
    for slot in getattr(entry, "required_tool_slots", []) or []:
        add(getattr(slot, "slot_id", "") if not isinstance(slot, dict) else slot.get("slot_id"), source="required_tool_slots", required=False)
        runtime_requirements = getattr(slot, "runtime_requirements", {}) if not isinstance(slot, dict) else slot.get("runtime_requirements", {})
        if isinstance(runtime_requirements, dict):
            add(runtime_requirements.get("capabilities") or runtime_requirements.get("required_capabilities") or [], source="required_tool_slots.runtime_requirements", required=False)
    for strategy in getattr(entry, "implementation_strategy", []) or []:
        add(getattr(strategy, "tool_id", "") if not isinstance(strategy, dict) else strategy.get("tool_id"), source="implementation_strategy", required=False)
    return out


def _iter_capability_values(values: Any) -> list[str]:
    if values in (None, "", False):
        return []
    if isinstance(values, str):
        return [values.strip()] if values.strip() else []
    if isinstance(values, dict):
        candidates: list[str] = []
        for key in ("capability", "capability_id", "tool_id", "name", "slot_id"):
            value = values.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())
        return candidates
    if isinstance(values, (list, tuple, set)):
        out: list[str] = []
        for item in values:
            out.extend(_iter_capability_values(item))
        return out
    return []


def _is_script_io_key(value: Any) -> bool:
    text = str(value or "").replace("\\", "/").strip()
    return bool(text) and not text.startswith(("references/", "assets/")) and text not in {"references", "assets", "assets/"}


def _is_declared_dependency(value: Any) -> bool:
    text = str(value or "").replace("\\", "/").strip()
    return bool(text) and not text.startswith(("references/", "assets/")) and text not in {"references", "assets", "assets/"}


def _filter_available_tools(manifests: list[CallableToolManifest], contract: CanonicalFileContract, *, limit: int = 5) -> list[CallableToolManifest]:
    """Apply a small deterministic filter after structural/embedding recall."""
    del contract  # reserved for future schema-aware filters; keep filtering generic.
    out: list[CallableToolManifest] = []
    seen: set[str] = set()
    for manifest in manifests:
        if not manifest.tool_id or not manifest.import_path or not manifest.function_name:
            continue
        if manifest.tool_id in seen:
            continue
        seen.add(manifest.tool_id)
        out.append(manifest)
        if len(out) >= max(1, limit):
            break
    return out


def call_template_for_tool(tool: CallableToolManifest) -> str:
    """Return a local import/call template without stdout/return policy."""
    if tool.example_call:
        lines = []
        for line in tool.example_call.strip().splitlines():
            stripped = line.strip()
            if stripped.startswith(("return ", "print(", "sys.stdout", "raise ")):
                continue
            lines.append(line.rstrip())
        if lines:
            return "\n".join(lines)
    return f"from {tool.import_path} import {tool.function_name}\nresult = {tool.function_name}(...)"


def resolve_implementation(entry: SkillPlanEntry, contract: CanonicalFileContract) -> ImplementationResolution:
    available_manifests: list[CallableToolManifest] = []
    output_mappings: list[dict[str, str]] = []
    capability_ids = {req.capability_id for req in contract.capability_requirements if req.capability_id}
    embedded_candidates = _embedding_candidate_tool_ids(contract.functional_requirements)
    for cap in list_tool_capabilities():
        if not cap.enabled_by_default or not cap.allow_creator_use:
            continue
        cap_manifests = callable_manifest_from_capability(cap)
        if not cap_manifests:
            continue
        matched, mappings = _capability_matches_contract(cap, capability_ids, contract)
        capability_hint_matched = bool(capability_ids & ({cap.name, *cap.required_capabilities, *cap.optional_capabilities} | {item for fn in cap.functions for item in (fn.required_capabilities or [])}))
        if matched or capability_hint_matched or any(m.tool_id in embedded_candidates or cap.name in embedded_candidates for m in cap_manifests):
            available_manifests.extend(cap_manifests)
            output_mappings.extend(mappings)
    local_ok, local_reason = _creator_can_implement(contract)
    if local_ok or available_manifests:
        available_manifests = _filter_available_tools(available_manifests, contract)
        deps = sorted({d for m in available_manifests for d in m.dependencies if _is_declared_dependency(d)})
        import_paths = sorted({
            path
            for m in available_manifests
            for path in (m.import_path, f"{m.import_path}.{m.function_name}" if m.import_path and m.function_name else "")
            if path
        })
        required_evidence = ["input_dependency", "nontrivial_transform", "stdout_contract", "declared_dependency_only", "no_shell_template"]
        if _contract_declares_artifact(contract) or any(m.artifact_outputs for m in available_manifests):
            required_evidence.append("artifact_created")
        return ImplementationResolution(
            mode="script_composition",
            selected_tools=[],
            available_tools=available_manifests,
            output_mappings=output_mappings,
            tool_slots=_tool_slots_for_requirements(contract, available_manifests, output_mappings),
            local_fallback_allowed=True,
            allowed_imports=import_paths,
            declared_dependencies=sorted(set(contract.declared_dependencies + deps)),
            required_evidence=required_evidence,
            reason="Generate the script by composing argv inputs, local logic, and filtered available_tools candidates; embeddings only recall candidates.",
        )
    return ImplementationResolution(mode="unresolved", local_fallback_allowed=False, required_evidence=[], reason=local_reason or "Canonical contract is incomplete.")


def refine_contract_with_resolution(contract: CanonicalFileContract, resolution: ImplementationResolution) -> CanonicalFileContract:
    """Keep the canonical stdout contract stable for script_composition.

    Tool schemas are exposed as lightweight available_tools context and must not
    replace the script stdout contract before generation. The generated script is
    validated after the fact against stdout/artifact/evidence rules.
    """
    return contract


def _capability_matches_contract(cap: ToolCapability, capability_ids: set[str], contract: CanonicalFileContract) -> tuple[bool, list[dict[str, str]]]:
    """Structurally match tool manifests to the canonical contract.

    Capability ids are structured hints from SkillPlan; schema compatibility is
    checked against output requirements. This intentionally avoids file names,
    business keywords, and role-specific special cases.
    """
    tool_ids = {cap.name, *cap.required_capabilities, *cap.optional_capabilities}
    fn_required_caps = {item for fn in cap.functions for item in (fn.required_capabilities or [])}
    if capability_ids and not (capability_ids & (tool_ids | fn_required_caps)):
        return False, []
    required_stdout = set(_schema_required(contract.stdout_schema) or contract.outputs)
    if not required_stdout:
        return True, []
    tool_output_fields: set[str] = set()
    has_generic_capability_manifest = False
    artifact_fields: set[str] = set()
    mappings: list[dict[str, str]] = []
    has_capability_hint = bool(capability_ids & (tool_ids | fn_required_caps))
    for fn in cap.functions or []:
        schema = fn.output_schema or cap.output_schema or {}
        props = schema.get("properties") if isinstance(schema, dict) else {}
        if isinstance(props, dict):
            tool_output_fields.update(str(key) for key in props.keys())
        tool_output_fields.update(_schema_required(schema))
        if isinstance(schema, dict) and schema.get("type") == "object" and not props and schema.get("additionalProperties") is True:
            has_generic_capability_manifest = True
        mapping = _single_scalar_output_mapping(
            tool_schema=schema if isinstance(schema, dict) else {},
            stdout_schema=contract.stdout_schema,
            capability_matched=has_capability_hint,
        )
        if mapping:
            mappings.append(mapping)
        for artifact in (fn.artifact_outputs or cap.artifact_outputs or []):
            if isinstance(artifact, dict) and artifact.get("field"):
                artifact_fields.add(str(artifact.get("field")))
    if required_stdout.issubset(tool_output_fields):
        return True, []
    if required_stdout and required_stdout.issubset(artifact_fields):
        return True, []
    if mappings:
        return True, mappings
    return bool(has_capability_hint and has_generic_capability_manifest), []


def _tool_slots_for_requirements(contract: CanonicalFileContract, manifests: list[CallableToolManifest], mappings: list[dict[str, str]]) -> list[dict[str, Any]]:
    slots: list[dict[str, Any]] = []
    requirements = contract.functional_requirements or ["final stdout contract"]
    for idx, tool in enumerate(manifests):
        req = requirements[min(idx, len(requirements) - 1)]
        slots.append({
            "slot_id": f"tool_slot_{idx + 1}",
            "functional_requirement": req,
            "tool_id": tool.tool_id,
            "call_template": call_template_for_tool(tool),
            "input_construction": {"source": "argv/payload", "schema": tool.input_schema},
            "output_consumption": {"schema": tool.output_schema, "mappings": mappings, "rule": "Use the tool result as evidence for this requirement; map or transform it locally into the canonical stdout fields."},
        })
    return slots


_EMBEDDING_INDEX_CACHE: tuple[tuple[str, ...], list[tuple[str, list[float]]]] | None = None

# Module-level cache for the local BGE model to avoid repeated disk loads.
_LOCAL_EMBEDDING_MODEL: "Any | None" = None
_LOCAL_EMBEDDING_MODEL_TRIED: bool = False

_LOCAL_MODEL_PATH = str(__import__("pathlib").Path(__file__).resolve().parents[1] / "bge-large-zh-v1.5")


def _tool_card_text(cap: ToolCapability, manifest: CallableToolManifest) -> str:
    return "\n".join([
        cap.name,
        cap.display_name,
        cap.category,
        manifest.tool_id,
        manifest.description,
        str(manifest.input_schema),
        str(manifest.output_schema),
    ])


def _embed_texts_remote(texts: list[str]) -> list[list[float]]:
    """Call remote embedding endpoint.  Raises on any failure."""
    model = settings.embedding_model or ""
    if not model:
        raise RuntimeError("EMBEDDING_MODEL is not configured")
    url = f"{settings.llm_base_url.rstrip('/')}/v1/embeddings"
    headers = {"Content-Type": "application/json"}
    key = settings.openai_api_key or settings.llm_api_key
    if key:
        headers["Authorization"] = f"******"
    with httpx.Client(timeout=10.0) as client:
        response = client.post(url, headers=headers, json={"model": model, "input": texts})
        response.raise_for_status()
        data = response.json().get("data") or []
    return [list(map(float, item.get("embedding") or [])) for item in data]


def _load_local_embedding_model() -> "Any | None":
    """Try to load the local BGE model from disk.  Returns None on any failure."""
    global _LOCAL_EMBEDDING_MODEL, _LOCAL_EMBEDDING_MODEL_TRIED
    if _LOCAL_EMBEDDING_MODEL_TRIED:
        return _LOCAL_EMBEDDING_MODEL
    _LOCAL_EMBEDDING_MODEL_TRIED = True
    import os
    if not os.path.isdir(_LOCAL_MODEL_PATH):
        logger.debug("local_embedding_model_not_found path=%s", _LOCAL_MODEL_PATH)
        return None
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
        _LOCAL_EMBEDDING_MODEL = SentenceTransformer(_LOCAL_MODEL_PATH)
        logger.info("local_embedding_model_loaded path=%s", _LOCAL_MODEL_PATH)
    except Exception as exc:
        logger.warning("local_embedding_model_load_failed path=%s error=%s", _LOCAL_MODEL_PATH, exc)
        _LOCAL_EMBEDDING_MODEL = None
    return _LOCAL_EMBEDDING_MODEL


def _embed_texts_local(texts: list[str]) -> list[list[float]]:
    """Embed texts using the local model.  Raises if unavailable."""
    model = _load_local_embedding_model()
    if model is None:
        raise RuntimeError("local embedding model not available")
    vecs = model.encode(texts, normalize_embeddings=True)
    return [list(map(float, v)) for v in vecs]


def _embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed texts with remote -> local -> error fallback chain.

    Logs structured events so callers can observe which path was taken:
    - remote_embedding_success
    - remote_embedding_failed  + local_embedding_success
    - remote_embedding_failed  + local_embedding_failed  -> raises
    """
    if not texts:
        return []
    try:
        result = _embed_texts_remote(texts)
        logger.info("remote_embedding_success count=%d", len(texts))
        return result
    except Exception as remote_exc:
        logger.warning("remote_embedding_failed error=%s", remote_exc)
        try:
            result = _embed_texts_local(texts)
            logger.info("local_embedding_success count=%d", len(texts))
            return result
        except Exception as local_exc:
            logger.warning("local_embedding_failed error=%s", local_exc)
            raise


def _embedding_candidate_tool_ids(requirements: list[str], *, top_k: int = 5) -> set[str]:
    """Return semantic-recall candidates; logs embedding path; falls back to empty on all failures."""
    global _EMBEDDING_INDEX_CACHE
    reqs = [r for r in requirements if r]
    if not reqs:
        return set()
    try:
        cards: list[tuple[str, str]] = []
        for cap in list_tool_capabilities():
            if not cap.enabled_by_default or not cap.allow_creator_use:
                continue
            for manifest in callable_manifest_from_capability(cap):
                cards.append((manifest.tool_id, _tool_card_text(cap, manifest)))
        sig = tuple(text for _, text in cards)
        if _EMBEDDING_INDEX_CACHE is None or _EMBEDDING_INDEX_CACHE[0] != sig:
            embeddings = _embed_texts([text for _, text in cards])
            _EMBEDDING_INDEX_CACHE = (sig, [(cards[i][0], emb) for i, emb in enumerate(embeddings) if emb])
        query_embeddings = _embed_texts(reqs)
        scored: list[tuple[float, str]] = []
        for q in query_embeddings:
            for tool_id, emb in _EMBEDDING_INDEX_CACHE[1]:
                scored.append((_cosine(q, emb), tool_id))
        return {tool_id for _, tool_id in sorted(scored, reverse=True)[:max(1, top_k)]}
    except Exception as exc:
        logger.warning("lexical_schema_fallback reason=embedding_unavailable error=%s", exc)
        return set()


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    denom = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / denom if denom else -1.0


def _single_scalar_output_mapping(*, tool_schema: dict[str, Any], stdout_schema: dict[str, Any], capability_matched: bool) -> dict[str, str] | None:
    if not capability_matched:
        return None
    tool_required = _schema_required(tool_schema)
    stdout_required = _schema_required(stdout_schema)
    if len(tool_required) != 1 or len(stdout_required) != 1:
        return None
    tool_props = tool_schema.get("properties") if isinstance(tool_schema.get("properties"), dict) else {}
    stdout_props = stdout_schema.get("properties") if isinstance(stdout_schema.get("properties"), dict) else {}
    source = tool_required[0]
    target = stdout_required[0]
    tool_type = (tool_props.get(source) or {}).get("type") if isinstance(tool_props.get(source), dict) else None
    stdout_type = (stdout_props.get(target) or {}).get("type") if isinstance(stdout_props.get(target), dict) else None
    if tool_type == "string" and stdout_type in {"string", None, "any"}:
        return {"source_tool_field": source, "target_stdout_field": target}
    return None


def _creator_can_implement(contract: CanonicalFileContract) -> tuple[bool, str]:
    if not contract.file_path.startswith("scripts/"):
        return False, "Only scripts/ executable files can be creator_implemented."
    required_stdout = _schema_required(contract.stdout_schema) or contract.outputs
    if not required_stdout:
        return False, "Canonical stdout schema has no required outputs."
    if not isinstance(contract.artifact_contract, dict):
        return False, "Artifact contract is not structurally verifiable."
    allowed_side_effects = {"", "read_file", "write_output_file", "create_artifact", "local_compute"}
    side_effects = {str(item) for item in contract.side_effects or []}
    if not side_effects.issubset(allowed_side_effects):
        return False, "Canonical side effects are outside Creator local implementation boundary."
    for dep in contract.declared_dependencies:
        if not isinstance(dep, str) or not dep.strip():
            return False, "Declared dependencies must be explicit package/import roots."
    return True, ""


def _contract_declares_artifact(contract: CanonicalFileContract) -> bool:
    artifact = contract.artifact_contract or {}
    if not isinstance(artifact, dict):
        return False
    return bool(artifact.get("tool_artifact_outputs") or artifact.get("artifact_outputs") or artifact.get("file_outputs"))


def contract_payload(contract: CanonicalFileContract, resolution: ImplementationResolution) -> dict[str, Any]:
    return {"canonical_contract": asdict(contract), "implementation_resolution": asdict(resolution)}


class _EvidenceVisitor(ast.NodeVisitor):
    def __init__(self, resolution: ImplementationResolution):
        self.resolution = resolution
        self.import_roots: set[str] = set()
        self.import_modules: set[str] = set()
        self.import_paths: set[str] = set()
        self.calls: set[str] = set()
        self.has_file_write = False
        self.has_transform_call = False
        self.input_refs = 0
        self.constants_in_returns = 0
        self.nonconstants_in_returns = 0
        self.tool_result_names: set[str] = set()
        self.return_name_roots: set[str] = set()

    def visit_Assign(self, node: ast.Assign) -> Any:
        call = node.value if isinstance(node.value, ast.Call) else None
        call_name = _call_name(call.func) if call is not None else ""
        selected = {tool.function_name for tool in self.resolution.available_tools if tool.function_name}
        if call_name and call_name.split(".")[-1] in selected:
            for target in node.targets:
                root = _name_root(target)
                if root:
                    self.tool_result_names.add(root)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> Any:
        for alias in node.names:
            if alias.name:
                self.import_roots.add(alias.name.split(".")[0])
                self.import_modules.add(alias.name)
                self.import_paths.add(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:
        if node.module:
            self.import_roots.add(node.module.split(".")[0])
            self.import_modules.add(node.module)
            for alias in node.names:
                if alias.name and alias.name != "*":
                    self.import_paths.add(f"{node.module}.{alias.name}")

    def visit_Call(self, node: ast.Call) -> Any:
        name = _call_name(node.func)
        if name:
            self.calls.add(name)
            if name.split(".")[-1] in {"write", "write_text", "write_bytes", "open", "dump", "dumps", "loads", "join", "format", "append", "extend", "update", "read", "read_text", "read_bytes", "str", "int", "float", "len", "sorted"}:
                self.has_transform_call = True
            if name.split(".")[-1] in {"write", "write_text", "write_bytes"}:
                self.has_file_write = True
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> Any:
        if _name_root(node.value) in {"payload", "data", "argv", "args"}:
            self.input_refs += 1
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        if _name_root(node.value) in {"payload", "data", "argv", "args"}:
            self.input_refs += 1
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> Any:
        for child in ast.walk(node.value) if node.value is not None else []:
            if isinstance(child, (ast.Constant, ast.JoinedStr)):
                self.constants_in_returns += 1
            elif isinstance(child, (ast.Name, ast.Call, ast.Subscript, ast.Attribute, ast.BinOp, ast.DictComp, ast.ListComp)):
                self.nonconstants_in_returns += 1
                root = _name_root(child)
                if root:
                    self.return_name_roots.add(root)
        self.generic_visit(node)


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _name_root(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _name_root(node.value)
    if isinstance(node, ast.Subscript):
        return _name_root(node.value)
    return ""


def validate_python_evidence(content: str, contract: CanonicalFileContract, resolution: ImplementationResolution) -> list[str]:
    if resolution.mode == "unresolved":
        return ["implementation_resolution_unresolved: unresolved scripts must not be generated"]
    try:
        tree = ast.parse(content or "")
    except SyntaxError as exc:
        return [f"python_ast_invalid: {exc}"]
    visitor = _EvidenceVisitor(resolution)
    visitor.visit(tree)
    issues: list[str] = []
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    allowed_exact = set(resolution.allowed_imports)
    allowed_dependency_roots = {d.split(".")[0].replace("-", "_") for d in resolution.declared_dependencies if _is_declared_dependency(d)}
    undeclared = []
    for module in visitor.import_modules:
        root = module.split(".")[0]
        if root in stdlib or root in allowed_dependency_roots or module in allowed_exact:
            continue
        if any(path.startswith(module + ".") for path in visitor.import_paths & allowed_exact):
            continue
        undeclared.append(module)
    undeclared = sorted(set(undeclared))
    if undeclared:
        issues.append("declared_dependency_only: undeclared imports " + ", ".join(undeclared))
    required = set(_schema_required(contract.stdout_schema) or contract.outputs)
    if required:
        source = content or ""
        missing = [key for key in required if key not in source]
        if missing:
            issues.append("stdout_contract: source does not mention required stdout fields " + ", ".join(missing))
    available_functions = {m.function_name for m in resolution.available_tools if m.function_name}
    available_imports = {m.import_path for m in resolution.available_tools if m.import_path}
    called_tools = available_functions & {c.split(".")[-1] for c in visitor.calls}
    imported_tool_modules = available_imports & visitor.import_modules
    if imported_tool_modules and not called_tools:
        issues.append("tool_call: imported an available tool module but did not call an available tool")
    if called_tools:
        if visitor.tool_result_names and not (visitor.tool_result_names & visitor.return_name_roots):
            issues.append("tool_result_used: called tool result does not appear to feed stdout/local transform")
        elif visitor.nonconstants_in_returns == 0:
            issues.append("tool_result_used: stdout does not appear to depend on computed/tool values")
    if visitor.input_refs == 0:
        issues.append("input_dependency: outputs do not appear to depend on argv/payload input")
    if not (visitor.has_transform_call or visitor.has_file_write):
        issues.append("nontrivial_transform: no parsing/transformation/file-processing evidence found")
    if visitor.nonconstants_in_returns == 0 and visitor.constants_in_returns > 0:
        issues.append("no_shell_template: required outputs appear to be literal-only")
    return issues


def validate_script_functional_evidence(
    *,
    content: str,
    stdout_payload: dict[str, Any],
    argv_payload: dict[str, Any],
    contract: CanonicalFileContract,
    resolution: ImplementationResolution,
) -> list[dict[str, Any]]:
    """Validate first-round single-script functional closure.

    This check is intentionally local to one generated script.  It combines
    static AST evidence with the actual trial-run stdout payload to ensure the
    script can stand alone against its own SkillPlan purpose/role/outputs and
    artifact contract, without judging cross-script workflow dataflow.
    """
    issues: list[dict[str, Any]] = []
    for issue in validate_python_evidence(content, contract, resolution):
        issues.append({
            "id": "script_functional." + issue.split(":", 1)[0],
            "failed_file": contract.file_path,
            "failed_function": "main/run/stdout construction",
            "code_region": "script body and final stdout construction",
            "reason": issue,
            "minimal_edit": "只修改当前脚本中读取输入、调用工具/本地处理、组织 stdout 或写入产物的失败区域；保留入口、JSON argv 解析、stdout 字段和文件输出协议。",
            "allowed_scope": contract.file_path,
            "forbidden_scope": "不得修改其它脚本、SKILL.md、SkillPlan、workflow；不得全量重写或通过 try/except 输出假成功。",
        })

    required = _schema_required(contract.stdout_schema) or contract.outputs
    missing = [key for key in required if key not in stdout_payload or not _json_value_non_empty(stdout_payload.get(key))]
    if missing:
        issues.append({
            "id": "script_functional.required_outputs",
            "failed_file": contract.file_path,
            "failed_function": "stdout JSON construction",
            "code_region": "return/print(json.dumps(...)) near final output",
            "reason": "stdout 缺少非空 required outputs: " + ", ".join(missing),
            "minimal_edit": "只补齐当前脚本 stdout JSON 中缺失的 required output 字段，并让字段值来自真实输入/处理结果。",
            "allowed_scope": contract.file_path,
            "forbidden_scope": "不得改 SKILL.md、其它脚本或 SkillPlan；不得输出空模板/假数据。",
        })

    for argv_key, argv_value in argv_payload.items():
        if not isinstance(argv_value, list) or not argv_value:
            continue
        matching_outputs = [
            (out_key, out_value)
            for out_key, out_value in stdout_payload.items()
            if isinstance(out_value, list)
        ]
        if not matching_outputs:
            continue
        if not any(len(out_value) == len(argv_value) for _out_key, out_value in matching_outputs):
            issues.append({
                "id": "script_functional.list_cardinality",
                "failed_file": contract.file_path,
                "failed_function": "batch/map processing",
                "code_region": "for/foreach loop and result collection",
                "reason": f"argv 字段 {argv_key!r} 是长度 {len(argv_value)} 的 list，但 stdout 中 list 输出没有保持相同长度，脚本可能只处理了部分元素或返回固定假列表。",
                "minimal_edit": "只修改当前脚本的循环/收集逻辑，确保对输入 list 逐项处理，并输出等长结果列表。",
                "allowed_scope": contract.file_path,
                "forbidden_scope": "不得硬编码固定输出长度；不得改 SKILL.md、其它脚本或 SkillPlan。",
            })

    if _contract_declares_artifact(contract):
        artifact_fields = _artifact_field_names(contract.artifact_contract)
        produced = [field for field in artifact_fields if _json_value_non_empty(stdout_payload.get(field))]
        if not produced:
            issues.append({
                "id": "script_functional.artifact_declared",
                "failed_file": contract.file_path,
                "failed_function": "artifact/file output generation",
                "code_region": "file creation and stdout file field assignment",
                "reason": "artifact_contract 声明文件产物，但 stdout 未返回对应非空产物字段。",
                "minimal_edit": "只修当前脚本的文件生成和 stdout 产物字段赋值，确保试运行真实创建文件。",
                "allowed_scope": contract.file_path,
                "forbidden_scope": "不得删除产物声明或改 workflow；不得返回不存在路径。",
            })

    return issues


def _json_value_non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set)):
        return any(_json_value_non_empty(item) for item in value)
    if isinstance(value, dict):
        return any(_json_value_non_empty(item) for item in value.values())
    return True


def _artifact_field_names(contract: dict[str, Any]) -> list[str]:
    fields: list[str] = []
    for key in ("artifact_fields", "file_fields", "file_outputs", "output_fields"):
        raw = contract.get(key) if isinstance(contract, dict) else None
        if isinstance(raw, str) and raw.strip():
            fields.append(raw.strip())
        elif isinstance(raw, list):
            fields.extend(str(item).strip() for item in raw if str(item).strip())
    for item in (contract.get("artifact_outputs") if isinstance(contract, dict) else []) or []:
        if isinstance(item, dict):
            field_name = str(item.get("field") or item.get("name") or "").strip()
            if field_name:
                fields.append(field_name)
    return list(dict.fromkeys(fields))
