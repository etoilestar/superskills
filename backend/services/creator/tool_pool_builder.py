from __future__ import annotations
import logging
from typing import Any
from backend.services.creator_tool_registry import get_tool_capability
from .tool_pool_models import ToolPoolModel, ToolPoolTool, ToolPoolFileBinding, ToolPoolDeniedRequest, ToolPoolMissingRequest
from .tool_pool_explorer import explore_tool_pool
from .tool_pool_gate import gate_tool_request

logger = logging.getLogger(__name__)

CORE_HELPERS = ['strict_json_argv_guard']

def _uniq(values: list[Any]) -> list[Any]:
    out=[]
    for v in values:
        if v not in out: out.append(v)
    return out

def build_tool_pool(*, skill_name: str = '', user_request: str = '', blueprint_text: str = '', file_specs: list[dict[str, Any]] | None = None, uploaded_files: list[dict[str, Any]] | None = None) -> ToolPoolModel:
    pool=ToolPoolModel(skill_name=skill_name)
    bindings: dict[str, ToolPoolFileBinding] = {}

    # Log tool_intent_extracted for each scripts/** file spec so the audit trail
    # shows that intents were derived from the SkillPlan, not hard-coded routes.
    for spec in file_specs or []:
        target=str(spec.get('path') or spec.get('target_file') or '')
        if target.startswith('scripts/'):
            slots = spec.get('required_tool_slots') or []
            intents = [
                s.get('functional_requirement') or s.get('slot_id') or ''
                if isinstance(s, dict) else str(s)
                for s in slots
            ]
            logger.info(
                "tool_intent_extracted target_file=%s slot_count=%d intents=%s",
                target, len(slots), intents,
            )
            bindings[target]=ToolPoolFileBinding(target_file=target, allowed_tool_ids=['script_argv_guard'], primary_tool_ids=['script_argv_guard'], allowed_helper_imports=list(CORE_HELPERS), input_schema=spec.get('inputs') if isinstance(spec.get('inputs'), dict) else {}, output_schema=spec.get('outputs') if isinstance(spec.get('outputs'), dict) else {})

    exploration=explore_tool_pool(user_request=user_request, blueprint_text=blueprint_text, file_specs=file_specs, uploaded_files=uploaded_files)
    pool.exploration_candidates = list(getattr(exploration, 'scored_candidates', []) or [])
    pool.scored_candidates = list(getattr(exploration, 'scored_candidates', []) or [])
    pool.uploaded_file_triggers = list(getattr(exploration, 'uploaded_file_triggers', []) or [])

    # Log registry_semantic_recall summary
    logger.info(
        "registry_semantic_recall candidate_count=%d reason=%s",
        len(exploration.candidate_tool_requests), exploration.reason or '',
    )

    # Log candidate_scored for each scored candidate
    for row in (exploration.scored_candidates or []):
        logger.info(
            "candidate_scored tool_id=%s target_file=%s score=%.1f features=%s",
            row.get('tool_id'), row.get('target_file'), row.get('score', 0.0),
            (row.get('matched_features') or [])[:4],
        )

    grouped: dict[tuple[str,str], list[Any]] = {}
    for req in exploration.candidate_tool_requests:
        grouped.setdefault((req.target_file, req.requested_capability or req.candidate_tool_id), []).append(req)
    for (_target, _capability), reqs in grouped.items():
        reqs=sorted(reqs, key=lambda r: r.score, reverse=True)
        primary_set=False
        for req in reqs:
            spec = next((s for s in (file_specs or []) if str(s.get('path') or s.get('target_file') or '') == req.target_file), {})
            event=gate_tool_request(req, file_role=str(spec.get('role') or 'generic_script'), file_spec=spec)
            pool.gate_events.append(event)
            row={'tool_id': event.tool_id, 'score': req.score, 'rank': req.rank, 'matched_features': req.matched_features, 'matched_terms': req.matched_terms, 'decision': event.decision, 'reason': '; '.join(event.messages), 'target_file': req.target_file}
            b=bindings.setdefault(req.target_file, ToolPoolFileBinding(target_file=req.target_file, allowed_helper_imports=list(CORE_HELPERS)))
            b.scored_tools.append(row)
            b.matched_features_by_tool[event.tool_id]=req.matched_features
            if event.decision == 'allow':
                cap=get_tool_capability(event.tool_id)
                is_primary=not primary_set
                primary_set = True or primary_set
                pool.tools.append(ToolPoolTool(tool_id=event.tool_id, status='allowed', source=req.source, source_phase='blueprint', target_files=[req.target_file], allowed_helper_imports=event.allowed_helper_imports, allowed_import_paths=event.allowed_import_paths, allowed_function_imports=event.allowed_function_imports, primary_for_capabilities=[req.requested_capability] if is_primary else [], secondary_for_capabilities=[] if is_primary else [req.requested_capability], score=req.score, matched_features=req.matched_features, matched_terms=req.matched_terms, allowed_roles=list((cap.roles if cap else []) or []), input_schema=(cap.input_schema if cap else {}) or {}, output_schema=(cap.output_schema if cap else {}) or {}, required_env=event.required_env, dependencies=event.dependencies, reason=req.reason, gate_result=event.decision, gate_messages=event.messages))
                b.allowed_tool_ids = _uniq(b.allowed_tool_ids + [event.tool_id])
                if is_primary: b.primary_tool_ids = _uniq(b.primary_tool_ids + [event.tool_id])
                else: b.secondary_tool_ids = _uniq(b.secondary_tool_ids + [event.tool_id])
                b.allowed_helper_imports = _uniq(b.allowed_helper_imports + event.allowed_helper_imports)
                b.allowed_import_paths = _uniq(b.allowed_import_paths + event.allowed_import_paths)
                b.allowed_function_imports = _uniq(b.allowed_function_imports + event.allowed_function_imports)
                b.required_env = _uniq(b.required_env + event.required_env)
                b.dependencies = b.dependencies + [d for d in event.dependencies if d not in b.dependencies]
                if cap: b.snippets.extend([s.__dict__ for s in (cap.snippets or [])])
            elif event.decision in {'require_config','require_dependency'}:
                pool.missing_requests.append(ToolPoolMissingRequest(target_file=req.target_file, tool_id=event.tool_id, missing_env=event.missing_env, missing_dependencies=event.missing_dependencies, reason='; '.join(event.messages)))
            else:
                pool.denied_requests.append(ToolPoolDeniedRequest(target_file=req.target_file, tool_id=event.tool_id, helper_imports=event.denied_helper_imports, reason=event.decision, messages=event.messages, suggested_replacements=event.suggested_replacements))

    pool.file_bindings=list(bindings.values())

    # Log file_binding_created for each scripts/** binding
    for fb in pool.file_bindings:
        if fb.target_file.startswith('scripts/'):
            logger.info(
                "file_binding_created target_file=%s allowed_tools=%s allowed_helpers=%s",
                fb.target_file, fb.allowed_tool_ids, fb.allowed_helper_imports,
            )

    return pool
