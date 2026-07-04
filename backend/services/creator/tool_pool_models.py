"""Models for per-Skill Creator runtime tool pools."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from pydantic import BaseModel, Field

ToolPoolStatus = Literal['candidate','allowed','missing_config','missing_dependency','blocked','denied','removed']
ToolPoolSource = Literal['blueprint_preselect','uploaded_file_candidate','registry_exploration','repair_request','system_required','manual_admin','fallback_default']
GateDecision = Literal['allow','deny','require_config','require_dependency','blocked_by_policy','not_found','role_mismatch','schema_mismatch']

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

class ToolPoolAddToolRequest(BaseModel):
    target_file: str
    requested_capability: str = ''
    candidate_tool_id: str
    source: ToolPoolSource = 'registry_exploration'
    reason: str = ''
    expected_input: dict[str, Any] = Field(default_factory=dict)
    expected_output: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 1.0
    score: float = 0.0
    matched_features: list[str] = Field(default_factory=list)
    matched_terms: list[str] = Field(default_factory=list)
    rank: int = 0
    candidate_source: str = ''
    semantic_reason: str = ''

class ToolPoolGateEvent(BaseModel):
    decision: GateDecision
    tool_id: str
    target_file: str
    # intent_id and slot_id link this gate event back to the ToolSlot/ToolIntent
    # that triggered the candidate request.  Empty when triggered outside a
    # structured ToolSlot flow (e.g. legacy capability recall).
    intent_id: str = ''
    slot_id: str = ''
    candidate_tool_id: str = ''
    score: float = 0.0
    matched_features: list[str] = Field(default_factory=list)
    allowed_helper_imports: list[str] = Field(default_factory=list)
    allowed_import_paths: list[str] = Field(default_factory=list)
    allowed_function_imports: list[str] = Field(default_factory=list)
    checked_import_paths: list[str] = Field(default_factory=list)
    checked_functions: list[str] = Field(default_factory=list)
    score: float = 0.0
    matched_features: list[str] = Field(default_factory=list)
    denied_helper_imports: list[str] = Field(default_factory=list)
    required_env: list[str] = Field(default_factory=list)
    missing_env: list[str] = Field(default_factory=list)
    dependencies: list[Any] = Field(default_factory=list)
    missing_dependencies: list[str] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)
    suggested_replacements: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now_iso)

class ToolPoolTool(BaseModel):
    tool_id: str
    status: ToolPoolStatus = 'candidate'
    source: ToolPoolSource = 'registry_exploration'
    source_phase: str = ''
    target_files: list[str] = Field(default_factory=list)
    allowed_helper_imports: list[str] = Field(default_factory=list)
    allowed_import_paths: list[str] = Field(default_factory=list)
    allowed_function_imports: list[str] = Field(default_factory=list)
    primary_for_capabilities: list[str] = Field(default_factory=list)
    secondary_for_capabilities: list[str] = Field(default_factory=list)
    score: float = 0.0
    matched_features: list[str] = Field(default_factory=list)
    matched_terms: list[str] = Field(default_factory=list)
    allowed_roles: list[str] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    required_env: list[str] = Field(default_factory=list)
    dependencies: list[Any] = Field(default_factory=list)
    permissions: dict[str, Any] = Field(default_factory=dict)
    reason: str = ''
    gate_result: str = ''
    gate_messages: list[str] = Field(default_factory=list)

class ToolPoolFileBinding(BaseModel):
    target_file: str
    allowed_tool_ids: list[str] = Field(default_factory=list)
    primary_tool_ids: list[str] = Field(default_factory=list)
    secondary_tool_ids: list[str] = Field(default_factory=list)
    allowed_helper_imports: list[str] = Field(default_factory=list)
    allowed_import_paths: list[str] = Field(default_factory=list)
    allowed_function_imports: list[str] = Field(default_factory=list)
    scored_tools: list[dict[str, Any]] = Field(default_factory=list)
    matched_features_by_tool: dict[str, list[str]] = Field(default_factory=dict)
    required_env: list[str] = Field(default_factory=list)
    dependencies: list[Any] = Field(default_factory=list)
    snippets: list[dict[str, Any]] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    denied_helper_imports: list[str] = Field(default_factory=list)
    repair_notes: list[str] = Field(default_factory=list)

class ToolPoolDeniedRequest(BaseModel):
    target_file: str = ''
    tool_id: str = ''
    helper_imports: list[str] = Field(default_factory=list)
    reason: str = ''
    messages: list[str] = Field(default_factory=list)
    suggested_replacements: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now_iso)

class ToolPoolMissingRequest(BaseModel):
    target_file: str = ''
    tool_id: str = ''
    missing_env: list[str] = Field(default_factory=list)
    missing_dependencies: list[str] = Field(default_factory=list)
    reason: str = ''
    created_at: str = Field(default_factory=utc_now_iso)

class ToolPoolModel(BaseModel):
    skill_name: str = ''
    version: str = '1.0'
    source: str = 'creator'
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    tools: list[ToolPoolTool] = Field(default_factory=list)
    file_bindings: list[ToolPoolFileBinding] = Field(default_factory=list)
    denied_requests: list[ToolPoolDeniedRequest] = Field(default_factory=list)
    missing_requests: list[ToolPoolMissingRequest] = Field(default_factory=list)
    gate_events: list[ToolPoolGateEvent] = Field(default_factory=list)
    exploration_candidates: list[dict[str, Any]] = Field(default_factory=list)
    scored_candidates: list[dict[str, Any]] = Field(default_factory=list)
    uploaded_file_triggers: list[dict[str, Any]] = Field(default_factory=list)

class ToolPoolPatch(BaseModel):
    add_tool_requests: list[ToolPoolAddToolRequest] = Field(default_factory=list)
    remove_tool_requests: list[dict[str, Any]] = Field(default_factory=list)
    update_file_bindings: list[dict[str, Any]] = Field(default_factory=list)
    reason: str = ''
    affected_files: list[str] = Field(default_factory=list)

class RuntimeImportGuardResult(BaseModel):
    success: bool = True
    error_type: str = ''
    target_file: str = ''
    missing_imports: list[str] = Field(default_factory=list)
    forbidden_imports: list[str] = Field(default_factory=list)
    allowed_helper_imports: list[str] = Field(default_factory=list)
    suggested_replacements: list[str] = Field(default_factory=list)
    repair_instruction: str = ''
    warnings: list[str] = Field(default_factory=list)
