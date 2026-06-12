"""Creator tool capability management endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services.creator_tool_registry import (
    RESOURCE_ROLES,
    TOOL_OVERRIDE_PERSISTENCE,
    ToolCapability,
    ToolSnippet,
    author_tool,
    build_tool_manifest_draft,
    capabilities_for_role,
    generate_adapter_code,
    get_script_roles,
    get_tool_capability,
    list_tool_capabilities,
    persist_registered_tools,
    register_tool_capability,
    set_tool_capability_override,
    snippets_for_tool,
    tool_snippet_prompt,
    set_tool_snippets,
    format_tool_snippet,
    resolve_tool_snippets_for_context,
    validate_tool_manifest,
    validate_tool_snippet,
    tool_status,
    _capability_from_dict,
    _snippet_from_dict,
)

router = APIRouter(prefix="/api/creator", tags=["creator-tools"])


class ToolPatchRequest(BaseModel):
    enabled: bool | None = None
    allow_creator_use: bool | None = None


class ToolTestRequest(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


class ToolDraftRequest(BaseModel):
    tool_name: str = ""
    description: str = ""
    tool_type: str = "python_helper"
    input_description: str = ""
    output_description: str = ""
    needs_secret: bool = False
    needs_external_network: bool = False
    generates_file: bool = False
    high_risk: bool = False
    required_env: list[str] = Field(default_factory=list)
    required_secrets: list[str] = Field(default_factory=list)
    allowed_roles: list[str] = Field(default_factory=list)
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None


class ToolManifestRequest(BaseModel):
    manifest: dict[str, Any]
    adapter_code: str | None = None
    sample_input: dict[str, Any] = Field(default_factory=dict)
    dynamic: bool = True


class ToolAuthorRequest(BaseModel):
    description: str = ""
    tool_name: str = ""
    tool_type: str = "python_helper"
    code_block: str | None = None
    adapter_code: str | None = None
    input_description: str = ""
    output_description: str = ""
    manifest: dict[str, Any] | None = None
    sample_input: dict[str, Any] = Field(default_factory=dict)
    allowed_roles: list[str] = Field(default_factory=list)
    needs_secret: bool = False
    needs_external_network: bool = False
    generates_file: bool = False
    high_risk: bool = False
    validation: dict[str, Any] | None = None
    stage: str = "draft"


class ToolRegisterRequest(ToolManifestRequest):
    created_by: str = "user"
    enable: bool = False


class ToolSnippetRequest(BaseModel):
    snippet: dict[str, Any]


class ToolSnippetPatchRequest(BaseModel):
    snippet: dict[str, Any]


class ToolSnippetResolveRequest(BaseModel):
    role: str = ""
    capabilities: list[str] = Field(default_factory=list)
    tool_names: list[str] = Field(default_factory=list)
    file_path: str = ""
    failure_layer: str | None = None
    error_text: str | None = None
    max_snippets: int = 5


def _tool_or_404(name: str):
    cap = get_tool_capability(name)
    if cap is None:
        raise HTTPException(status_code=404, detail=f"Unknown creator tool capability: {name}")
    return cap


@router.get("/tools")
def list_creator_tools() -> dict[str, Any]:
    return {
        "tools": [tool_status(cap) for cap in list_tool_capabilities()],
        "override_persistence": TOOL_OVERRIDE_PERSISTENCE,
        "note": "Tool toggles are process-memory overrides in this P0 registry layer; runtime helpers may still be missing until follow-up implementation.",
    }




@router.post("/tools/draft")
def draft_creator_tool(request: ToolDraftRequest) -> dict[str, Any]:
    manifest = build_tool_manifest_draft(request.model_dump(exclude_none=True))
    return {"manifest": manifest, "planner_fallback": True}


@router.post("/tools/generate-code")
def generate_creator_tool_code(request: ToolManifestRequest) -> dict[str, Any]:
    code = generate_adapter_code(request.manifest)
    return {"adapter_code": code, "adapter_path": request.manifest.get("adapter_path"), "requires_validation": True}


@router.post("/tools/author")
async def author_creator_tool(request: ToolAuthorRequest) -> dict[str, Any]:
    try:
        return await author_tool(request.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/tools/validate")
def validate_creator_tool(request: ToolManifestRequest) -> dict[str, Any]:
    return validate_tool_manifest(
        request.manifest,
        adapter_code=request.adapter_code,
        sample_input=request.sample_input,
        dynamic=request.dynamic,
    )


@router.post("/tools/register")
def register_creator_tool(request: ToolRegisterRequest) -> dict[str, Any]:
    validation = validate_tool_manifest(
        request.manifest,
        adapter_code=request.adapter_code,
        sample_input=request.sample_input,
        dynamic=request.dynamic,
    )
    if not validation["success"]:
        raise HTTPException(status_code=400, detail={"message": "tool validation failed", "validation": validation})
    payload = dict(request.manifest)
    payload["enabled"] = bool(request.enable)
    payload["enabled_by_default"] = bool(request.enable)
    payload["allow_creator_use"] = bool(request.enable)
    payload["approval_status"] = "enabled" if request.enable else "validated"
    payload["test_status"] = "passed"
    payload["last_validation_result"] = validation
    payload["created_by"] = request.created_by
    cap = _capability_from_dict(payload)
    register_tool_capability(cap)
    persist_registered_tools()
    return {"tool": tool_status(cap), "validation": validation}


@router.post("/tools/{name}/enable")
def enable_creator_tool(name: str) -> dict[str, Any]:
    cap = set_tool_capability_override(name, enabled=True, allow_creator_use=True)
    if cap is None:
        raise HTTPException(status_code=404, detail=f"Unknown creator tool capability: {name}")
    return {"tool": tool_status(cap)}


@router.post("/tools/{name}/disable")
def disable_creator_tool(name: str) -> dict[str, Any]:
    cap = set_tool_capability_override(name, enabled=False, allow_creator_use=False)
    if cap is None:
        raise HTTPException(status_code=404, detail=f"Unknown creator tool capability: {name}")
    return {"tool": tool_status(cap)}


@router.post("/tools/resolve-snippets")
def resolve_creator_tool_snippets(request: ToolSnippetResolveRequest) -> dict[str, Any]:
    snippets = resolve_tool_snippets_for_context(
        role=request.role,
        capabilities=request.capabilities,
        tool_names=request.tool_names,
        file_path=request.file_path,
        failure_layer=request.failure_layer,
        error_text=request.error_text,
        max_snippets=request.max_snippets,
    )
    return {"snippets": snippets, "prompt_preview": tool_snippet_prompt(snippets)}


@router.get("/tools/{name}/snippets")
def list_creator_tool_snippets(name: str) -> dict[str, Any]:
    cap = _tool_or_404(name)
    snippets = snippets_for_tool(cap)
    return {
        "tool": name,
        "snippets": [{**snippet.__dict__, "formatted": format_tool_snippet(cap, snippet), "validation": validate_tool_snippet(cap, snippet)} for snippet in snippets],
        "prompt_preview": tool_snippet_prompt([{"formatted": format_tool_snippet(cap, snippet)} for snippet in snippets]),
    }


@router.post("/tools/{name}/snippets")
def create_creator_tool_snippet(name: str, request: ToolSnippetRequest) -> dict[str, Any]:
    cap = _tool_or_404(name)
    snippet = _snippet_from_dict(request.snippet)
    validation = validate_tool_snippet(cap, snippet)
    if not validation["success"]:
        raise HTTPException(status_code=400, detail={"message": "snippet validation failed", "validation": validation})
    current = [item for item in snippets_for_tool(cap) if item.id != snippet.id]
    updated = set_tool_snippets(name, [*current, snippet])
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Unknown creator tool capability: {name}")
    if name not in {cap.name for cap in list_tool_capabilities() if cap.created_by == "system"}:
        persist_registered_tools()
    return {"tool": tool_status(updated), "snippet": {**snippet.__dict__, "formatted": format_tool_snippet(updated, snippet)}, "validation": validation}


@router.patch("/tools/{name}/snippets/{snippet_id}")
def update_creator_tool_snippet(name: str, snippet_id: str, request: ToolSnippetPatchRequest) -> dict[str, Any]:
    cap = _tool_or_404(name)
    payload = dict(request.snippet)
    payload["id"] = payload.get("id") or snippet_id
    snippet = _snippet_from_dict(payload)
    validation = validate_tool_snippet(cap, snippet)
    if not validation["success"]:
        raise HTTPException(status_code=400, detail={"message": "snippet validation failed", "validation": validation})
    current = [item for item in snippets_for_tool(cap) if item.id != snippet_id and item.id != snippet.id]
    updated = set_tool_snippets(name, [*current, snippet])
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Unknown creator tool capability: {name}")
    if name not in {cap.name for cap in list_tool_capabilities() if cap.created_by == "system"}:
        persist_registered_tools()
    return {"tool": tool_status(updated), "snippet": {**snippet.__dict__, "formatted": format_tool_snippet(updated, snippet)}, "validation": validation}


@router.delete("/tools/{name}/snippets/{snippet_id}")
def delete_creator_tool_snippet(name: str, snippet_id: str) -> dict[str, Any]:
    cap = _tool_or_404(name)
    remaining = [item for item in snippets_for_tool(cap) if item.id != snippet_id]
    if len(remaining) == len(snippets_for_tool(cap)):
        raise HTTPException(status_code=404, detail=f"Unknown snippet: {snippet_id}")
    updated = set_tool_snippets(name, remaining)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Unknown creator tool capability: {name}")
    if name not in {cap.name for cap in list_tool_capabilities() if cap.created_by == "system"}:
        persist_registered_tools()
    return {"tool": tool_status(updated), "snippets": [snippet.__dict__ for snippet in snippets_for_tool(updated)]}


@router.post("/tools/{name}/snippets/{snippet_id}/test")
def test_creator_tool_snippet(name: str, snippet_id: str) -> dict[str, Any]:
    cap = _tool_or_404(name)
    snippet = next((item for item in snippets_for_tool(cap) if item.id == snippet_id), None)
    if snippet is None:
        raise HTTPException(status_code=404, detail=f"Unknown snippet: {snippet_id}")
    validation = validate_tool_snippet(cap, snippet)
    return {
        "success": validation["success"],
        "dry_run": True,
        "side_effect_performed": False,
        "validation": validation,
        "message": "Snippet smoke test performed static import/helper/contract checks only; no external side effects were executed.",
    }


@router.get("/tools/{name}")
def get_creator_tool(name: str) -> dict[str, Any]:
    return {"tool": tool_status(_tool_or_404(name))}


@router.patch("/tools/{name}")
def update_creator_tool(name: str, patch: ToolPatchRequest) -> dict[str, Any]:
    cap = set_tool_capability_override(
        name,
        enabled=patch.enabled,
        allow_creator_use=patch.allow_creator_use,
    )
    if cap is None:
        raise HTTPException(status_code=404, detail=f"Unknown creator tool capability: {name}")
    return {"tool": tool_status(cap)}


@router.post("/tools/{name}/test")
def test_creator_tool(name: str, request: ToolTestRequest | None = None) -> dict[str, Any]:
    cap = _tool_or_404(name)
    status = tool_status(cap)
    payload_keys = sorted((request.payload if request else {}).keys())
    configured = bool(status["configured"])
    runtime_ready = not status["missing_runtime_helpers"] and not status.get("missing_dependencies")
    creator_available = bool(status["creator_available"])
    success = configured and runtime_ready and creator_available
    if not creator_available:
        message = "tool is disabled for Creator use; no external side effect was performed"
    elif not configured:
        message = "tool configuration is incomplete; no external side effect was performed"
    elif status["missing_runtime_helpers"]:
        message = "tool configuration is complete, but runtime helpers are not implemented; no external side effect was performed"
    elif status.get("missing_dependencies"):
        message = "tool runtime dependencies are not installed; no external side effect was performed"
    else:
        message = "tool configuration and runtime helpers look ready; no external side effect was performed"
    return {
        "success": success,
        "tool": status,
        "trial_mode": cap.trial_mode,
        "dry_run": True,
        "side_effect_performed": False,
        "message": message,
        # Do not echo payload values: callers may pass secrets or sample PII.
        "payload_keys": payload_keys,
    }


@router.get("/tool-roles")
def list_creator_tool_roles() -> dict[str, Any]:
    return {
        "roles": [
            {
                "role": role,
                "required_capabilities": capabilities_for_role(role)[0],
                "forbidden_capabilities": capabilities_for_role(role)[1],
            }
            for role in get_script_roles()
        ],
        "resource_roles": sorted(RESOURCE_ROLES),
    }
