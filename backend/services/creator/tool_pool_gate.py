from __future__ import annotations
import importlib, importlib.util, logging, os
from pathlib import Path
from typing import Any
from backend.services.creator_tool_registry import get_tool_capability
from backend.services.runtime_tools import __all__ as RUNTIME_TOOLS_ALL
from .tool_pool_models import ToolPoolAddToolRequest, ToolPoolGateEvent

logger = logging.getLogger(__name__)

RESOURCE_ROLES = {'reference','asset','skill_overview'}
SUGGESTED = {'read_pdf_text':'extract_pdf_text','read_xlsx_text':'read_spreadsheet','read_txt_text':'read_file_text','read_excel_text':'read_spreadsheet'}

def _missing_deps(deps: list[Any]) -> list[str]:
    missing=[]
    for dep in deps or []:
        imports = dep.get('imports') if isinstance(dep, dict) else [str(dep)] if isinstance(dep, str) else []
        for name in imports or []:
            try:
                if importlib.util.find_spec(str(name)) is None:
                    missing.append(str(name))
            except Exception:
                missing.append(str(name))
    return missing

def _check_function_imports(cap: Any) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    allowed_paths=[]; allowed_functions=[]; checked_paths=[]; checked_functions=[]; messages=[]
    for fn in cap.functions or []:
        import_path=str(getattr(fn,'import_path','') or '').strip()
        function_name=str(getattr(fn,'function_name','') or '').strip()
        if not import_path or import_path == 'backend.services.runtime_tools':
            continue
        checked_paths.append(import_path); checked_functions.append(function_name)
        adapter_path=str(getattr(cap,'adapter_path','') or '')
        if '.bak' in adapter_path or 'custom_tools.bak' in adapter_path:
            messages.append('custom tool adapter is only present in a .bak path')
            continue
        try:
            module=importlib.import_module(import_path)
        except Exception as exc:
            messages.append(f'import_path not importable: {import_path}: {type(exc).__name__}: {exc}')
            continue
        if not function_name or not hasattr(module, function_name):
            messages.append(f'function not found in import_path: {import_path}.{function_name}')
            continue
        allowed_paths.append(import_path); allowed_functions.append(function_name)
    return allowed_paths, allowed_functions, checked_paths, checked_functions, messages

def _declared_tool_ids(file_spec: dict[str, Any] | None) -> set[str]:
    """Return concrete tool IDs explicitly requested by the file spec.

    Tool selection should be driven by tool IDs such as:
    - required_tools: ["unified_file_text_read", "text_generation"]
    - selected_tools: ["create_pdf_document"]
    - required_tool_slots: [{"tool_id": "..."}]

    File role remains only a coarse file/resource category and must not be used
    to reject a concrete tool candidate for scripts/**.
    """
    spec = file_spec if isinstance(file_spec, dict) else {}
    ids: set[str] = set()

    for key in (
        "required_tools",
        "selected_tools",
        "tool_ids",
        "selected_tool_ids",
        "allowed_tools",
        "required_capabilities",
    ):
        value = spec.get(key)
        if isinstance(value, list):
            for item in value:
                text = str(item or "").strip()
                if text:
                    ids.add(text)
        elif isinstance(value, str) and value.strip():
            ids.add(value.strip())

    slots = spec.get("required_tool_slots")
    if isinstance(slots, list):
        for item in slots:
            if isinstance(item, dict):
                tool_id = (
                    item.get("tool_id")
                    or item.get("candidate_tool_id")
                    or item.get("capability")
                    or item.get("capability_id")
                    or item.get("name")
                )
                if tool_id:
                    ids.add(str(tool_id).strip())
            else:
                text = str(item or "").strip()
                if text:
                    ids.add(text)

    return {item for item in ids if item}

def gate_tool_request(
    request: ToolPoolAddToolRequest | dict[str, Any],
    *,
    file_role: str = "generic_script",
    file_spec: dict[str, Any] | None = None,
    intent_id: str = "",
    slot_id: str = "",
) -> ToolPoolGateEvent:
    """Gate a candidate runtime tool for a Creator script.

    Important policy:
    - Runtime tools may bind only to scripts/**.
    - references/assets/SKILL.md-like resources must never receive runtime tools.
    - Concrete tool identity is candidate_tool_id / required_tools / selected_tools.
    - File role is NOT used as a hard allow/deny rule for scripts/**; it is
      recorded only as a log hint.  Roles such as composite_generator must not
      block a concrete tool that passes all other checks.
    - intent_id / slot_id link this gate event back to the originating ToolSlot
      so that the audit trail is complete.
    """
    req = request if isinstance(request, ToolPoolAddToolRequest) else ToolPoolAddToolRequest(**request)
    target_file = str(req.target_file or "").replace("\\", "/")
    role = str(file_role or "").strip()

    # role is logged as a hint; it does NOT drive the allow/deny decision for scripts/**
    _role_hint = role

    if not target_file.startswith("scripts/") or role in RESOURCE_ROLES:
        evt = ToolPoolGateEvent(
            decision="blocked_by_policy",
            tool_id=req.candidate_tool_id,
            target_file=req.target_file,
            intent_id=intent_id,
            slot_id=slot_id,
            candidate_tool_id=req.candidate_tool_id,
            messages=["runtime tools may only bind to scripts/**, never references/assets"],
            score=req.score,
            matched_features=req.matched_features,
        )
        logger.info(
            "gate_decision tool_id=%s target_file=%s decision=%s role_hint=%s intent_id=%s slot_id=%s",
            evt.tool_id, evt.target_file, evt.decision, _role_hint, intent_id, slot_id,
        )
        return evt

    cap = get_tool_capability(req.candidate_tool_id)
    if cap is None:
        evt = ToolPoolGateEvent(
            decision="not_found",
            tool_id=req.candidate_tool_id,
            target_file=req.target_file,
            intent_id=intent_id,
            slot_id=slot_id,
            candidate_tool_id=req.candidate_tool_id,
            messages=["tool_id is not registered"],
            suggested_replacements=list(SUGGESTED.values()),
            score=req.score,
            matched_features=req.matched_features,
        )
        logger.info(
            "gate_decision tool_id=%s target_file=%s decision=%s role_hint=%s intent_id=%s slot_id=%s",
            evt.tool_id, evt.target_file, evt.decision, _role_hint, intent_id, slot_id,
        )
        return evt

    if (
        not cap.enabled_by_default
        or not cap.allow_creator_use
        or str(cap.approval_status or "").lower() in {"disabled", "denied", "blocked"}
    ):
        evt = ToolPoolGateEvent(
            decision="blocked_by_policy",
            tool_id=cap.name,
            target_file=req.target_file,
            intent_id=intent_id,
            slot_id=slot_id,
            candidate_tool_id=req.candidate_tool_id,
            messages=["tool is disabled or not allowed for Creator use"],
            score=req.score,
            matched_features=req.matched_features,
        )
        logger.info(
            "gate_decision tool_id=%s target_file=%s decision=%s role_hint=%s intent_id=%s slot_id=%s",
            evt.tool_id, evt.target_file, evt.decision, _role_hint, intent_id, slot_id,
        )
        return evt

    declared_ids = _declared_tool_ids(file_spec)
    messages: list[str] = []
    if declared_ids:
        if cap.name in declared_ids or req.candidate_tool_id in declared_ids:
            messages.append(f"tool explicitly declared by file spec: {cap.name}")
        else:
            messages.append(
                "tool accepted by semantic exploration; explicit tool declarations are "
                f"{sorted(declared_ids)}"
            )

    helper_imports = list(cap.helper_imports or [])
    denied = [h for h in helper_imports if h not in set(RUNTIME_TOOLS_ALL)]
    if denied:
        evt = ToolPoolGateEvent(
            decision="deny",
            tool_id=cap.name,
            target_file=req.target_file,
            intent_id=intent_id,
            slot_id=slot_id,
            candidate_tool_id=req.candidate_tool_id,
            denied_helper_imports=denied,
            messages=[
                "helper_imports are not exported by backend.services.runtime_tools.__all__",
                *messages,
            ],
            suggested_replacements=[SUGGESTED[h] for h in denied if h in SUGGESTED],
            score=req.score,
            matched_features=req.matched_features,
        )
        logger.info(
            "gate_decision tool_id=%s target_file=%s decision=%s role_hint=%s intent_id=%s slot_id=%s denied_helpers=%s",
            evt.tool_id, evt.target_file, evt.decision, _role_hint, intent_id, slot_id, denied,
        )
        return evt

    import_paths, function_imports, checked_paths, checked_functions, import_messages = _check_function_imports(cap)
    has_custom_functions = bool(checked_paths)
    if has_custom_functions and not import_paths:
        evt = ToolPoolGateEvent(
            decision="deny",
            tool_id=cap.name,
            target_file=req.target_file,
            intent_id=intent_id,
            slot_id=slot_id,
            candidate_tool_id=req.candidate_tool_id,
            checked_import_paths=checked_paths,
            checked_functions=checked_functions,
            messages=(import_messages or ["custom tool import_path/function unavailable"]) + messages,
            score=req.score,
            matched_features=req.matched_features,
        )
        logger.info(
            "gate_decision tool_id=%s target_file=%s decision=%s role_hint=%s intent_id=%s slot_id=%s",
            evt.tool_id, evt.target_file, evt.decision, _role_hint, intent_id, slot_id,
        )
        return evt

    missing_env = [
        name
        for name in list(cap.required_env or []) + list(cap.required_secrets or [])
        if not os.environ.get(str(name))
    ]
    missing_deps = _missing_deps(list(cap.dependencies or []))

    decision = "allow"
    if missing_env:
        decision = "require_config"
    elif missing_deps:
        decision = "require_dependency"

    final_messages = import_messages or []
    if not final_messages:
        final_messages = ["allowed" if decision == "allow" else decision]
    final_messages.extend(messages)

    evt = ToolPoolGateEvent(
        decision=decision,
        tool_id=cap.name,
        target_file=req.target_file,
        intent_id=intent_id,
        slot_id=slot_id,
        candidate_tool_id=req.candidate_tool_id,
        allowed_helper_imports=helper_imports,
        allowed_import_paths=import_paths,
        allowed_function_imports=function_imports,
        checked_import_paths=checked_paths,
        checked_functions=checked_functions,
        required_env=list(cap.required_env or []) + list(cap.required_secrets or []),
        missing_env=missing_env,
        dependencies=list(cap.dependencies or []),
        missing_dependencies=missing_deps,
        messages=final_messages,
        score=req.score,
        matched_features=req.matched_features,
    )
    logger.info(
        "gate_decision tool_id=%s target_file=%s decision=%s role_hint=%s intent_id=%s slot_id=%s "
        "allowed_helpers=%s missing_env_count=%d missing_deps=%s",
        evt.tool_id, evt.target_file, evt.decision, _role_hint, intent_id, slot_id,
        helper_imports, len(missing_env), missing_deps,
    )
    return evt
