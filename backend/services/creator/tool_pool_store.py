from __future__ import annotations
import json, os, tempfile
from pathlib import Path
from .tool_pool_models import ToolPoolModel, ToolPoolFileBinding, ToolPoolGateEvent, ToolPoolDeniedRequest, ToolPoolMissingRequest, ToolPoolTool, utc_now_iso
from ..creator_tool_registry import get_tool_capability

TOOL_POOL_RELATIVE_PATH = Path('.creator') / 'tool_pool.json'

def tool_pool_path(skill_dir: str | Path) -> Path:
    return Path(skill_dir) / TOOL_POOL_RELATIVE_PATH

def save_tool_pool(skill_dir: str | Path, pool: ToolPoolModel) -> Path:
    path = tool_pool_path(skill_dir); path.parent.mkdir(parents=True, exist_ok=True)
    pool.updated_at = utc_now_iso()
    data = pool.model_dump(mode='json')
    fd, tmp = tempfile.mkstemp(prefix='tool_pool.', suffix='.json', dir=str(path.parent))
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write('\n')
    os.replace(tmp, path)
    return path

def load_tool_pool(skill_dir: str | Path) -> ToolPoolModel:
    path = tool_pool_path(skill_dir)
    if not path.is_file():
        return ToolPoolModel()
    return ToolPoolModel.model_validate_json(path.read_text(encoding='utf-8'))

def add_gate_event(pool: ToolPoolModel, event: ToolPoolGateEvent) -> None:
    pool.gate_events.append(event); pool.updated_at = utc_now_iso()

def add_denied_request(pool: ToolPoolModel, denied: ToolPoolDeniedRequest) -> None:
    pool.denied_requests.append(denied); pool.updated_at = utc_now_iso()

def add_missing_request(pool: ToolPoolModel, missing: ToolPoolMissingRequest) -> None:
    pool.missing_requests.append(missing); pool.updated_at = utc_now_iso()


def _stable_unique(values):
    out = []
    for value in values or []:
        if value not in out:
            out.append(value)
    return out


def _tool_callable_contracts(tool: ToolPoolTool) -> list[dict]:
    """Return Registry-backed callable contracts.

    ToolFunctionManifest is the only source of callable tool functions.
    Legacy helper/import fields must not create callable contracts.
    """
    capability = get_tool_capability(str(tool.tool_id or "").strip())
    if capability is None:
        return []

    contracts: list[dict] = []

    for function in list(getattr(capability, "functions", []) or []):
        function_name = str(getattr(function, "function_name", "") or "").strip()
        import_path = str(getattr(function, "import_path", "") or "").strip()
        if not function_name or not import_path:
            continue
        contracts.append({
            "tool_id": str(tool.tool_id or "").strip(),
            "function_name": function_name,
            "import_path": import_path,
            "input_schema": dict(getattr(function, "input_schema", None) or {}),
            "output_schema": dict(getattr(function, "output_schema", None) or {}),
        })

    return contracts

def _derive_legacy_fields_from_available_tools(available_tools: list[dict]) -> tuple[list[str], list[str], list[str]]:
    function_names = _stable_unique([tool.get("function_name") for tool in available_tools])
    import_paths = _stable_unique([tool.get("import_path") for tool in available_tools])
    helper_imports = list(function_names)
    function_imports = _stable_unique([
        item
        for tool in available_tools
        for item in (
            tool.get("function_name"),
            f"{tool.get('import_path')}.{tool.get('function_name')}" if tool.get("import_path") and tool.get("function_name") else "",
        )
    ])
    return helper_imports, import_paths, function_imports

def get_skill_tool_binding(
    pool: ToolPoolModel,
    *,
    target_file: str = "",
    include_script_core: bool = False,
) -> ToolPoolFileBinding:
    """Project the Skill-wide ToolPool into one prompt/runtime binding view.

    This is not persisted per-file authorization.

    ToolPool.tools is the only Skill-wide authorization source.

    target_file is only contextual metadata for the consumer.

    include_script_core=True adds Creator's mandatory Python script guard
    capability to the projection. It does not add a business tool to ToolPool.
    """

    allowed_tools = [
        tool
        for tool in pool.tools
        if tool.status == "allowed"
        and str(tool.tool_id or "").strip()
    ]

    core_tool_ids = (
        ["script_argv_guard"]
        if include_script_core
        else []
    )

    core_helpers = (
        ["strict_json_argv_guard"]
        if include_script_core
        else []
    )

    shared_tool_ids = _stable_unique([
        tool.tool_id
        for tool in allowed_tools
        if str(tool.tool_id or "").strip()
    ])

    allowed_tool_ids = _stable_unique([
        *core_tool_ids,
        *shared_tool_ids,
    ])

    available_tools = [
        contract
        for tool in allowed_tools
        for contract in _tool_callable_contracts(tool)
    ]

    if include_script_core:
        available_tools.append({
            "tool_id": "script_argv_guard",
            "function_name": "strict_json_argv_guard",
            "import_path": "backend.services.runtime_tools",
            "input_schema": {},
            "output_schema": {},
        })

    (
        allowed_helper_imports,
        allowed_import_paths,
        allowed_function_imports,
    ) = _derive_legacy_fields_from_available_tools(
        available_tools
    )

    required_env = _stable_unique([
        env_name
        for tool in allowed_tools
        for env_name in (
            tool.required_env
            or []
        )
    ])

    dependencies = _stable_unique([
        dependency
        for tool in allowed_tools
        for dependency in (
            tool.dependencies
            or []
        )
    ])

    scored_tools = [
        {
            "tool_id": tool.tool_id,
            "score": tool.score,
            "matched_features": list(
                tool.matched_features or []
            ),
            "matched_terms": list(
                tool.matched_terms or []
            ),
            "decision": (
                "allowed_in_skill_tool_pool"
            ),
            "reason": tool.reason,
            "source_phase": tool.source_phase,
        }
        for tool in allowed_tools
    ]

    matched_features_by_tool = {
        tool.tool_id: list(
            tool.matched_features or []
        )
        for tool in allowed_tools
    }

    return ToolPoolFileBinding(
        target_file=str(target_file or ""),
        allowed_tool_ids=allowed_tool_ids,

        # There is no longer per-file primary/secondary ranking.
        # All Skill-authorized tools are equally available.
        primary_tool_ids=list(
            allowed_tool_ids
        ),
        secondary_tool_ids=[],

        allowed_helper_imports=(
            allowed_helper_imports
        ),
        allowed_import_paths=(
            allowed_import_paths
        ),
        allowed_function_imports=(
            allowed_function_imports
        ),
        available_tools=available_tools,
        scored_tools=scored_tools,
        matched_features_by_tool=(
            matched_features_by_tool
        ),
        required_env=required_env,
        dependencies=dependencies,
        snippets=[],
        input_schema={},
        output_schema={},
        denied_helper_imports=[],
        repair_notes=[],
    )

def get_file_binding(
    pool: ToolPoolModel,
    target_file: str,
    *,
    raw: bool = False,
) -> ToolPoolFileBinding | None:
    """Return the persisted per-file optional tool view when present."""

    normalized_target = str(target_file or "").replace("\\", "/").strip()

    if not normalized_target.startswith("scripts/"):
        return None

    for binding in pool.file_bindings or []:
        if str(binding.target_file or "").replace("\\", "/").strip() != normalized_target:
            continue
        if raw:
            return binding
        projected = binding.model_copy(deep=True)
        guard_contract = {
            "tool_id": "script_argv_guard",
            "function_name": "strict_json_argv_guard",
            "import_path": "backend.services.runtime_tools",
            "input_schema": {},
            "output_schema": {},
        }
        available_tools = [
            item
            for item in (projected.available_tools or [])
            if not (
                isinstance(item, dict)
                and item.get("tool_id") == "script_argv_guard"
                and item.get("function_name") == "strict_json_argv_guard"
                and item.get("import_path") == "backend.services.runtime_tools"
            )
        ]
        projected.available_tools = [guard_contract, *available_tools]
        helper_imports, import_paths, function_imports = _derive_legacy_fields_from_available_tools(projected.available_tools)
        projected.allowed_tool_ids = _stable_unique(["script_argv_guard", *list(projected.allowed_tool_ids or [])])
        projected.primary_tool_ids = _stable_unique(["script_argv_guard", *list(projected.primary_tool_ids or [])])
        projected.allowed_helper_imports = _stable_unique([*list(projected.allowed_helper_imports or []), *helper_imports])
        projected.allowed_import_paths = _stable_unique([*list(projected.allowed_import_paths or []), *import_paths])
        projected.allowed_function_imports = _stable_unique([*list(projected.allowed_function_imports or []), *function_imports])
        return projected

    if raw:
        return None

    return get_skill_tool_binding(
        pool,
        target_file=normalized_target,
        include_script_core=True,
    )

def get_allowed_helper_imports(
    pool: ToolPoolModel,
    target_file: str,
) -> list[str]:
    """Return helpers allowed by the current Skill ToolPool projection."""

    binding = get_file_binding(
        pool,
        target_file,
    )

    if binding is None:
        return []

    return list(
        binding.allowed_helper_imports
        or []
    )


def tool_pool_snapshot(
    pool: ToolPoolModel,
) -> dict:
    """Return the canonical Skill-wide ToolPool view.

    ToolPool.tools is the only authorization source.

    file_bindings is retained as an empty compatibility field so old frontend
    and stored payload readers do not fail during the migration.
    """

    skill_binding = get_skill_tool_binding(
        pool,
        target_file="",
        include_script_core=False,
    )

    return {
        "skill_name": pool.skill_name,
        "version": pool.version,
        "source": pool.source,
        "created_at": pool.created_at,
        "updated_at": pool.updated_at,

        "authorization_scope": "skill",

        "allowed_tool_ids": list(
            skill_binding.allowed_tool_ids
        ),

        "allowed_helper_imports": list(
            skill_binding.allowed_helper_imports
        ),

        "allowed_import_paths": list(
            skill_binding.allowed_import_paths
        ),

        "allowed_function_imports": list(
            skill_binding.allowed_function_imports
        ),

        "required_env": list(
            skill_binding.required_env
        ),

        "dependencies": list(
            skill_binding.dependencies
        ),

        "skill_binding": (
            skill_binding.model_dump(
                mode="json"
            )
        ),

        "tools": [
            tool.model_dump(
                mode="json"
            )
            for tool in pool.tools
            if tool.status == "allowed"
        ],

        "file_bindings": [
            binding.model_dump(mode="json")
            for binding in (pool.file_bindings or [])
        ],

        "denied_requests": [
            item.model_dump(
                mode="json"
            )
            for item in pool.denied_requests
        ],

        "missing_requests": [
            item.model_dump(
                mode="json"
            )
            for item in pool.missing_requests
        ],

        "gate_events": [
            item.model_dump(
                mode="json"
            )
            for item in pool.gate_events
        ],

        "exploration_candidates": list(
            pool.exploration_candidates
            or []
        ),

        "scored_candidates": list(
            pool.scored_candidates
            or []
        ),

        "uploaded_file_triggers": list(
            pool.uploaded_file_triggers
            or []
        ),
    }
