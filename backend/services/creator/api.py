"""Creator FastAPI endpoint handlers and response assembly."""

from .common import *  # noqa: F403
from .contracts import *  # noqa: F403
from .e2e import *  # noqa: F403
from .repair import *  # noqa: F403
from .generation import *  # noqa: F403


async def _extract_requirement_graph_with_validator(
    *,
    blueprint_text: str,
    files_out: list[FileSpecOut],
    requested_model: str | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> RequirementGraph:
    """Build deterministic responsibility graph and optionally apply compact model patches."""
    graph = validate_requirement_graph_schema(build_default_requirement_graph(files_out), files_out)
    route = route_model(VALIDATOR_TASK, requested_model=requested_model, reason="creator responsibility graph patch")
    file_payload = [file_spec.model_dump(mode="json", exclude={"requirements"}) for file_spec in files_out]
    messages = [
        {
            "role": "system",
            "content": (
                "你是 Creator responsibility graph patcher，只输出严格 JSON object。\n"
                "后端已经根据 file_plan/contracts 生成 deterministic responsibility graph；你只能返回 compact patches。\n"
                "patch 只能补充或修正 purpose、must_do、must_not_do、depends_on；不得输出 constraints、evidence_policy、graph_quality、non_requirements、expected、minimal_edit。\n"
                "purpose 必须是简短语义短合同，不复制蓝图长文，格式：来源：... | 动作：... | 交付：... | 约束：...\\n说明：...\n"
                "must_do 只补关键职责缺口，保持短句、少量条目。\n"
                "返回格式：{\"patches\":[{\"target_file\":...,\"purpose\":\"...\",\"must_do\":[],\"must_not_do\":[],\"depends_on\":[]}]}。"
            ),
        },
        {
            "role": "user",
            "content": (
                "blueprint_text:\n" + (blueprint_text or "")[:12000] + "\n\n"
                "file_plan_and_contracts:\n" + json.dumps(file_payload, ensure_ascii=False, default=str)[:20000] + "\n\n"
                "deterministic_responsibility_graph:\n" + graph.model_dump_json()[:12000]
            ),
        },
    ]
    try:
        text = await complete_chat_once(messages, route.model)
        data = parse_requirement_graph_result(text)
        if warnings is None and isinstance(data, dict) and "patches" not in data and "requirements" in data:
            legacy_graph = normalize_requirement_graph(data)
            legacy_graph.requirement_graph_source = "validator"
            legacy_graph.requirement_graph_quality = "full"
            return validate_requirement_graph_schema(legacy_graph, files_out)
        patches = data.get("patches", []) if isinstance(data, dict) else []
        if not isinstance(patches, list):
            raise RequirementGraphValidationError("Responsibility graph patch JSON must contain patches list.", code="validator_incomplete")
        by_file = {item.target_file: item for item in graph.requirements}
        allowed = {"target_file", "purpose", "must_do", "must_not_do", "depends_on"}
        applied_purpose_targets: list[str] = []
        for idx, patch in enumerate(patches):
            if not isinstance(patch, dict):
                raise RequirementGraphValidationError("Responsibility graph patch item must be object.", code="validator_incomplete", details={"index": idx})
            if set(patch) - allowed:
                raise RequirementGraphValidationError("Responsibility graph patch contains unsupported fields.", code="validator_incomplete", details={"index": idx, "fields": sorted(set(patch) - allowed)})
            target = str(patch.get("target_file") or "").strip()
            item = by_file.get(target)
            if not item:
                continue
            purpose = str(patch.get("purpose") or "").strip()
            if purpose:
                item.purpose = purpose
                applied_purpose_targets.append(target)
            for field_name in ("must_do", "must_not_do", "depends_on"):
                values = RequirementItem._coerce_string_list(patch.get(field_name))
                if values:
                    existing = list(getattr(item, field_name))
                    for value in values:
                        if value not in existing:
                            existing.append(value)
                    setattr(item, field_name, existing)
        graph.requirement_graph_source = "deterministic+patch"
        purpose_by_file = {
            item.target_file: item.purpose
            for item in graph.requirements
            if item.target_file and str(item.purpose or "").strip()
        }
        for file_spec in files_out:
            patched_purpose = purpose_by_file.get(file_spec.path)
            if patched_purpose:
                file_spec.purpose = patched_purpose
        logger.info("[Creator][purpose_short_contract_patch][result] %s", json.dumps({
            "event": "purpose_short_contract_patch_result",
            "patch_count": len(patches),
            "purpose_targets": applied_purpose_targets,
        }, ensure_ascii=False, default=str))
        return graph
    except Exception as exc:
        if warnings is not None:
            code = getattr(exc, "code", "validator_error")
            warnings.append({
                "severity": "validator_warning",
                "code": str(code),
                "source": "responsibility_graph",
                "path": "",
                "field": "requirement_graph",
                "message": f"Responsibility graph patch model failed; using deterministic graph: {exc}",
            })
        logger.info("[Creator][purpose_short_contract_patch][failed] %s", json.dumps({
            "event": "purpose_short_contract_patch_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False, default=str))
        return graph


def _persist_requirement_graph(skill_name: str, graph: RequirementGraph) -> None:
    metadata_dir = settings.skills_path / _validate_skill_name(skill_name) / ".creator"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    (metadata_dir / "requirement_graph.json").write_text(
        graph.model_dump_json(indent=2),
        encoding="utf-8",
    )


def _persist_workflow_allocation_summary(skill_name: str, summary: str) -> None:
    metadata_dir = settings.skills_path / _validate_skill_name(skill_name) / ".creator"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    (metadata_dir / "workflow_allocation_summary.txt").write_text(str(summary or "").strip(), encoding="utf-8")


def _load_workflow_allocation_summary(skill_name: str) -> str:
    path = settings.skills_path / _validate_skill_name(skill_name) / ".creator" / "workflow_allocation_summary.txt"
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _persist_workflow_allocation_edges(skill_name: str, edges: list[dict[str, Any]]) -> None:
    metadata_dir = settings.skills_path / _validate_skill_name(skill_name) / ".creator"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    (metadata_dir / "workflow_responsibility_edges.json").write_text(
        json.dumps(list(edges or []), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def _load_workflow_allocation_edges(skill_name: str) -> list[dict[str, Any]]:
    path = settings.skills_path / _validate_skill_name(skill_name) / ".creator" / "workflow_responsibility_edges.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _load_persisted_requirement_graph(skill_name: str) -> RequirementGraph | None:
    path = settings.skills_path / _validate_skill_name(skill_name) / ".creator" / "requirement_graph.json"
    if not path.is_file():
        return None
    return normalize_requirement_graph(parse_requirement_graph_result(path.read_text(encoding="utf-8")))

_PLATFORM_GUARANTEED_INPUT_KEYS = {
    "user_request",
    "input",
    "payload",
    "envelope",
    "fields",
    "options",
    "files",
    "input_files",
    "text",
}

_EXECUTABLE_ITERATION_MECHANISMS = {"internal_iteration", "platform_loop"}
_EXECUTABLE_AGGREGATION_MECHANISMS = {"aggregation", "internal_iteration"}
_RESOURCE_EDGE_MECHANISMS = {"resource", "defaulted"}


def _edge_value(edge: dict[str, Any], key: str) -> str:
    return str(edge.get(key) or "").strip()


def _validate_workflow_allocation_graph(
    *,
    files_out: list[FileSpecOut],
    workflow_allocation_summary: str,
    responsibility_edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate lightweight allocation edges for executable dataflow closure.

    This is intentionally structural and field-interface oriented. It does not
    infer business semantics from role names, filenames, singular/plural forms,
    or domain vocabularies.
    """
    issues: list[dict[str, Any]] = []
    edges = [edge for edge in responsibility_edges or [] if isinstance(edge, dict)]
    required_scripts = [
        item
        for item in sorted(files_out, key=lambda value: (value.generation_order, value.path))
        if item.path.startswith("scripts/") and item.required
    ]
    resources = {
        item.path
        for item in files_out
        if item.path.startswith(("references/", "assets/"))
    }
    script_outputs_by_path = {
        item.path: {str(output).strip() for output in (item.outputs or []) if str(output).strip()}
        for item in required_scripts
    }

    prior_outputs: set[str] = set()
    prior_scripts: set[str] = set()
    for script in required_scripts:
        incoming_edges = [edge for edge in edges if _edge_value(edge, "to") == script.path]
        for input_name in [str(item).strip() for item in (script.inputs or []) if str(item).strip()]:
            has_source = input_name in _PLATFORM_GUARANTEED_INPUT_KEYS
            has_source = has_source or input_name in prior_outputs
            has_source = has_source or input_name in resources
            if not has_source:
                for edge in incoming_edges:
                    mechanism = _edge_value(edge, "mechanism")
                    source = _edge_value(edge, "from")
                    edge_input = _edge_value(edge, "input")
                    edge_output = _edge_value(edge, "output")
                    if edge_input and edge_input != input_name:
                        continue
                    if mechanism in _RESOURCE_EDGE_MECHANISMS:
                        has_source = True
                    elif source in resources or source.startswith(("references/", "assets/")):
                        has_source = True
                    elif source == "platform_input" and (edge_output in _PLATFORM_GUARANTEED_INPUT_KEYS or edge_input in _PLATFORM_GUARANTEED_INPUT_KEYS):
                        has_source = True
                    elif source in prior_scripts and edge_output in script_outputs_by_path.get(source, set()):
                        has_source = True
                    if has_source:
                        break
            if not has_source:
                issues.append({
                    "issue_type": "unresolved_input",
                    "target_file": script.path,
                    "input": input_name,
                    "problem": "input has no producer in platform input, prior outputs, resources, or defaulting edge",
                })
        prior_scripts.add(script.path)
        prior_outputs.update(script_outputs_by_path.get(script.path, set()))

    for edge in edges:
        target = _edge_value(edge, "to")
        mechanism = _edge_value(edge, "mechanism") or "unknown"
        source_granularity = _edge_value(edge, "source_granularity") or "unknown"
        target_granularity = _edge_value(edge, "target_granularity") or "unknown"
        if source_granularity == "collection" and target_granularity == "single" and mechanism not in _EXECUTABLE_ITERATION_MECHANISMS:
            issues.append({
                "issue_type": "implicit_iteration_not_executable",
                "target_file": target,
                "problem": "collection-to-item responsibility exists but no executable loop/map/internal iteration is assigned",
                "edge": edge,
            })
        if source_granularity == "single" and target_granularity == "collection" and mechanism not in _EXECUTABLE_AGGREGATION_MECHANISMS:
            issues.append({
                "issue_type": "implicit_aggregation_not_executable",
                "target_file": target,
                "problem": "single output is being consumed as collection without an executable aggregation mechanism",
                "edge": edge,
            })
        if "unknown" in {source_granularity, target_granularity, mechanism} and (
            source_granularity == "collection" or target_granularity == "collection"
        ):
            issues.append({
                "issue_type": "unknown_critical_granularity",
                "target_file": target,
                "problem": "collection-level responsibility has unknown granularity or mechanism; allocator must assign executable ownership",
                "edge": edge,
            })

    summary_lower = str(workflow_allocation_summary or "").lower()
    mentions_iteration = any(marker in summary_lower for marker in ("逐项", "依次", "每个", "for each", "per item", "each item"))
    has_executable_iteration = any(
        _edge_value(edge, "mechanism") in _EXECUTABLE_ITERATION_MECHANISMS
        and (
            _edge_value(edge, "source_granularity") == "collection"
            or _edge_value(edge, "target_granularity") == "collection"
        )
        for edge in edges
    )
    if mentions_iteration and not has_executable_iteration:
        issues.append({
            "issue_type": "natural_language_iteration_not_executable",
            "target_file": "",
            "problem": "workflow allocation describes per-item processing, but no responsibility edge assigns executable internal iteration or platform loop/map",
        })
    return issues


async def _allocate_workflow_script_responsibilities(
    *,
    blueprint_text: str,
    files_out: list[FileSpecOut],
    requested_model: str | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Patch script purposes so executable workflow responsibilities are executable."""
    targets = [
        file_spec
        for file_spec in files_out
        if file_spec.path.startswith("scripts/") and file_spec.required
    ]
    if not targets:
        return {"summary": "", "responsibility_edges": [], "validation_errors": []}
    route = route_model(VALIDATOR_TASK, requested_model=requested_model, reason="creator workflow responsibility allocation")
    all_nodes = [
        {
            "path": item.path,
            "purpose": item.purpose,
            "role": item.role,
            "required": item.required,
            "inputs": item.inputs,
            "outputs": item.outputs,
            "dependencies": item.dependencies,
        }
        for item in files_out
        if item.path == "SKILL.md" or item.path.startswith(("scripts/", "references/", "assets/"))
    ]
    payload = [
        {
            "path": item.path,
            "purpose": item.purpose,
            "role": item.role,
            "inputs": item.inputs,
            "outputs": item.outputs,
            "dependencies": item.dependencies,
        }
        for item in targets
    ]
    messages = [
        {"role": "system", "content": (
            "你是 Creator workflow executable responsibility allocator，只输出严格 JSON object。\n"
            "先在内部构建轻量责任图谱作为推理依据（不要输出复杂结构）：节点包括平台 guaranteed input envelope、每个 required script、reference、asset、最终产物；边描述上游 stdout/artifact/resource 如何被下游消费。\n"
            "逐边判断：上游交付什么、下游需要什么、中间是否丢失结构、顺序、引用、约束或能力边界。\n"
            "职责分配禁止依据 role 名称、文件名或固定业务词表；必须依据当前脚本的上游输入、下游消费者、声明能力与禁止能力、可观察信息、实际可交付输出、全局最终产物需要的中间结果。\n"
            "workflow_allocation_summary 必须描述图上的责任边界；每个 required script 都说明：消费哪类上游结果、交付哪类下游结果、需保留哪些可观察关系、哪些责任由上游建立当前只保留、哪些责任当前无法观察或验证不能压给它。\n"
            "额外输出 responsibility_edges 作为轻量校验辅助，不要设计复杂 DAG，不暴露给用户；每条边只描述 from/to/output/input/source_granularity/target_granularity/mechanism。\n"
            "如果 workflow 需要对一组上游结果逐项处理，并交付一组对应结果，而平台 workflow 本身没有显式可执行 loop/map/foreach 节点，则该逐项处理必须落到某个脚本内部实现。\n"
            "被分配该责任的脚本必须消费集合级输入、在脚本内部遍历集合元素、为每个元素生成或转换对应结果、输出集合级结果、保持输入集合与输出集合的顺序或显式映射关系。\n"
            "不得只把脚本定义为单元素输入/单元素输出，再用自然语言声称 workflow 会逐项调用。\n"
            "如果下游需要结构化中间结果且上游已有对应结构化输出，可以 patch 当前脚本 purpose，并可在 patch 中给 inputs/outputs 做最小补齐；只能基于图中已有节点和边，不能凭空发明字段。\n"
            "只输出 compact patches；不要新增文件，不硬编码业务案例。purpose 格式：来源：... | 动作：... | 交付：... | 约束：...\\n说明：...\n"
            "返回：{\"workflow_allocation_summary\":\"...\",\"responsibility_edges\":[{\"from\":\"platform_input 或 scripts/x.py 或 references/x.md 或 assets/...\",\"to\":\"scripts/y.py\",\"output\":\"上游交付名称\",\"input\":\"下游消费名称\",\"source_granularity\":\"single|collection|unknown\",\"target_granularity\":\"single|collection|unknown\",\"mechanism\":\"direct|internal_iteration|platform_loop|aggregation|defaulted|resource\"}],\"patches\":[{\"target_file\":\"scripts/x.py\",\"purpose\":\"...\",\"inputs\":[],\"outputs\":[]}]}"
        )},
        {"role": "user", "content": (
            "blueprint_text:\n" + (blueprint_text or "")[:14000] + "\n\n"
            "graph_nodes_from_file_plan:\n" + json.dumps(all_nodes, ensure_ascii=False, default=str)[:20000] + "\n\n"
            "required_scripts_to_patch:\n" + json.dumps(payload, ensure_ascii=False, default=str)[:16000]
        )},
    ]
    logger.info("[Creator][workflow_allocation][start] %s", json.dumps({
        "event": "workflow_allocation_start",
        "script_count": len(targets),
        "scripts": [item.path for item in targets],
    }, ensure_ascii=False, default=str))
    feedback_messages = list(messages)
    last_validation_errors: list[dict[str, Any]] = []
    last_summary = ""
    last_edges: list[dict[str, Any]] = []
    try:
        for attempt in range(3):
            data = _parse_validator_json_object(await complete_chat_once(feedback_messages, route.model))
            patches = data.get("patches") if isinstance(data, dict) else None
            summary = str(data.get("workflow_allocation_summary") or "").strip() if isinstance(data, dict) else ""
            responsibility_edges = data.get("responsibility_edges") if isinstance(data, dict) else []
            if not isinstance(responsibility_edges, list):
                responsibility_edges = []
            responsibility_edges = [edge for edge in responsibility_edges if isinstance(edge, dict)]
            last_summary = summary
            last_edges = responsibility_edges
            if not isinstance(patches, list):
                raise ValueError("missing patches list")
            by_path = {item.path: item for item in targets}
            applied: list[str] = []
            for patch in patches:
                if not isinstance(patch, dict):
                    continue
                target = str(patch.get("target_file") or "").strip()
                purpose = str(patch.get("purpose") or "").strip()
                if target in by_path and purpose:
                    script = by_path[target]
                    script.purpose = purpose
                    for field_name in ("inputs", "outputs"):
                        values = patch.get(field_name)
                        if isinstance(values, list) and all(isinstance(v, str) for v in values):
                            existing = list(getattr(script, field_name) or [])
                            for value in values:
                                value = value.strip()
                                if value and value not in existing:
                                    existing.append(value)
                            setattr(script, field_name, existing)
                    applied.append(target)
            validation_errors = _validate_workflow_allocation_graph(
                files_out=files_out,
                workflow_allocation_summary=summary,
                responsibility_edges=responsibility_edges,
            )
            last_validation_errors = validation_errors
            if not validation_errors:
                logger.info("[Creator][workflow_allocation][result] %s", json.dumps({
                    "event": "workflow_allocation_result",
                    "applied_targets": applied,
                    "edge_count": len(responsibility_edges),
                    "summary": summary,
                }, ensure_ascii=False, default=str))
                return {
                    "summary": summary,
                    "responsibility_edges": responsibility_edges,
                    "validation_errors": [],
                }
            logger.info("[Creator][workflow_allocation][graph_invalid] %s", json.dumps({
                "event": "workflow_allocation_graph_invalid",
                "attempt": attempt + 1,
                "applied_targets": applied,
                "validation_errors": validation_errors,
            }, ensure_ascii=False, default=str))
            feedback_messages = [
                *messages,
                {
                    "role": "assistant",
                    "content": json.dumps(data, ensure_ascii=False, default=str)[:20000],
                },
                {
                    "role": "user",
                    "content": (
                        "当前 responsibility allocation 没有通过执行图闭环校验。\n"
                        "请只修正 purpose / inputs / outputs / responsibility_edges。\n"
                        "不要新增文件。不要硬编码业务案例。\n"
                        "循环/逐项处理任务在没有平台 loop/map 节点时必须落到某个脚本内部。\n"
                        "validator_errors:\n"
                        + json.dumps(validation_errors, ensure_ascii=False, indent=2, default=str)[:12000]
                    ),
                },
            ]
        if warnings is not None:
            warnings.append({
                "severity": "validator_warning",
                "code": "workflow_allocation_graph_failed",
                "source": "analyze_blueprint",
                "path": "",
                "field": "responsibility_edges",
                "message": (
                    "Workflow responsibility allocation did not pass executable graph closure validation; "
                    "returning editable draft with validation feedback: "
                    + json.dumps(last_validation_errors, ensure_ascii=False, default=str)[:2000]
                ),
            })
        return {
            "summary": last_summary,
            "responsibility_edges": last_edges,
            "validation_errors": last_validation_errors,
        }
    except Exception as exc:
        logger.info("[Creator][workflow_allocation][failed] %s", json.dumps({
            "event": "workflow_allocation_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False, default=str))
        if warnings is not None:
            warnings.append({
                "severity": "validator_warning",
                "code": "workflow_allocation_failed",
                "source": "analyze_blueprint",
                "path": "",
                "field": "purpose",
                "message": f"Workflow responsibility allocation failed; keeping parsed purposes: {exc}",
            })
        return {"summary": "", "responsibility_edges": [], "validation_errors": []}

def _looks_like_semantic_short_contract(text: str) -> bool:
    value = str(text or "")
    return all(marker in value for marker in ("来源：", "动作：", "交付：", "约束：", "说明："))

async def _normalize_script_purpose_short_contracts(
    *,
    blueprint_text: str,
    files_out: list[FileSpecOut],
    requested_model: str | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> None:
    targets = [
        file_spec
        for file_spec in files_out
        if file_spec.path.startswith("scripts/") and file_spec.required and not _looks_like_semantic_short_contract(file_spec.purpose)
    ]
    if not targets:
        return
    logger.info("[Creator][purpose_short_contract][start] %s", json.dumps({
        "event": "purpose_short_contract_start",
        "targets": [item.path for item in targets],
    }, ensure_ascii=False, default=str))
    route = route_model(VALIDATOR_TASK, requested_model=requested_model, reason="creator script purpose short-contract normalization")
    messages = [
        {"role": "system", "content": (
            "你是 Creator 脚本职责短合同压缩器，只输出严格 JSON object。\n"
            "为每个 required script 生成简短 purpose；不要新增结构字段，不复制蓝图长文。\n"
            "格式必须是：来源：... | 动作：... | 交付：... | 约束：...\\n说明：...\n"
            "来源/动作/交付/约束要来自蓝图语义；inputs/outputs 只是接口提示。"
        )},
        {"role": "user", "content": (
            "blueprint_text:\n" + (blueprint_text or "")[:12000] + "\n\n"
            "scripts:\n" + json.dumps([
                {
                    "path": item.path,
                    "purpose": item.purpose,
                    "role": item.role,
                    "inputs": item.inputs,
                    "outputs": item.outputs,
                    "dependencies": item.dependencies,
                }
                for item in targets
            ], ensure_ascii=False, default=str)[:16000] + "\n\n"
            "返回：{\"patches\":[{\"target_file\":\"scripts/x.py\",\"purpose\":\"来源：... | 动作：... | 交付：... | 约束：...\\n说明：...\"}]}"
        )},
    ]
    try:
        data = _parse_validator_json_object(await complete_chat_once(messages, route.model))
        patches = data.get("patches") if isinstance(data, dict) else None
        if not isinstance(patches, list):
            raise ValueError("missing patches list")
        by_path = {item.path: item for item in targets}
        for patch in patches:
            if not isinstance(patch, dict):
                continue
            target = str(patch.get("target_file") or "").strip()
            purpose = str(patch.get("purpose") or "").strip()
            if target in by_path and _looks_like_semantic_short_contract(purpose):
                by_path[target].purpose = purpose
        logger.info("[Creator][purpose_short_contract][result] %s", json.dumps({
            "event": "purpose_short_contract_result",
            "patched_targets": [
                item.path for item in targets if _looks_like_semantic_short_contract(item.purpose)
            ],
        }, ensure_ascii=False, default=str))
    except Exception as exc:
        logger.info("[Creator][purpose_short_contract][failed] %s", json.dumps({
            "event": "purpose_short_contract_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False, default=str))
        if warnings is not None:
            warnings.append({
                "severity": "validator_warning",
                "code": "purpose_short_contract_failed",
                "source": "analyze_blueprint",
                "path": "",
                "field": "purpose",
                "message": f"Script purpose short-contract normalization failed; keeping parsed purposes: {exc}",
            })

@router.post("/analyze-blueprint", response_model=AnalyzeBlueprintResponse)
async def analyze_blueprint(request: AnalyzeBlueprintRequest):
    try:
        plan: BlueprintPlan = parse_blueprint(request.messages, strict=request.strict)
    except BlueprintShapeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    effective_messages = request.messages
    blueprint_contract_warnings: list[dict[str, Any]] = []
    entries_by_path = {
        entry.path: entry
        for entry in (plan.skill_plan.files if plan.skill_plan else [])
        if not _is_directory_like_skill_path(entry.path)
    }

    blueprint_text = "\n\n".join(
        str(message.get("content") or "")
        for message in effective_messages
        if isinstance(message, dict)
    )

    def is_directory_placeholder(path: str) -> bool:
        normalized = _normalize_skill_path(path)
        return not normalized or normalized in {"assets", "assets/"} or normalized.endswith("/") or (
            normalized.startswith(("assets/", "references/", "scripts/")) and not _has_file_extension(normalized)
        )

    base_paths = {f.path for f in plan.files if not is_directory_placeholder(f.path)}

    candidate_paths: set[str] = {path for path in _extract_declared_skill_paths(blueprint_text) if not is_directory_placeholder(path)}
    candidate_paths.update(entries_by_path.keys())

    extra_paths = []
    extra_path_warnings: list[str] = []
    for path in sorted(candidate_paths):
        if path in base_paths or not path.startswith("references/"):
            continue
        if is_runtime_artifact_semantic(path, _local_blueprint_text_for_path(path, blueprint_text)):
            extra_path_warnings.append(
                f"已忽略运行时产物文件计划项 {path}；运行时生成文件只能通过脚本 outputs/stdout metadata 表示。"
            )
            continue
        extra_paths.append(path)

    def fallback_role(path: str) -> str | None:
        if path == "SKILL.md":
            return "skill_overview"
        if path.startswith("scripts/"):
            return "generic_script"
        if path.startswith("references/"):
            return "reference"
        if path.startswith("assets/"):
            return "asset"
        return None

    def fallback_file_type(path: str) -> str | None:
        if path == "SKILL.md":
            return "skill"
        if path.startswith("scripts/"):
            return "script"
        if path.startswith("references/"):
            return "reference"
        if path.startswith("assets/"):
            return "asset"
        return None

    def serialize_plan_items(items: Any) -> list[dict[str, Any]]:
        return [
            dict(getattr(item, "__dict__", item))
            for item in (items or [])
            if isinstance(getattr(item, "__dict__", item), dict)
        ]

    def selected_tools_for_entry(entry: SkillPlanEntry | None) -> list[str]:
        if not entry:
            return []
        return list(resolve_tools_for_skill_plan_entry(entry).allowed_tools or [])

    files_out: list[FileSpecOut] = []

    directory_asset_requirements: list[AssetRequirementOut] = []
    for f in plan.files:
        if is_directory_placeholder(f.path):
            local_context = _local_blueprint_text_for_path(f.path, blueprint_text) or f.purpose or blueprint_text
            # TODO: move directory-level upload needs into the normalized plan so
            # asset_requirements are explicit and no longer inferred from text.
            if _normalize_skill_path(f.path).startswith("assets") and re.search(r"上传|user[_ -]?upload|素材|图片|image|asset", local_context, re.IGNORECASE) and not re.search(r"无需|不需要|不用|无需创建|不生成", local_context):
                directory_asset_requirements.append(AssetRequirementOut(
                    path="assets/",
                    generation_order=_generation_order_for_file("assets/", "user_upload"),
                    source="user_upload",
                    required=getattr(f, "required", True),
                    description=f.purpose or "需要用户上传素材",
                ))
            continue
        entry = entries_by_path.get(f.path)
        role = entry.role if entry else fallback_role(f.path)
        file_type = entry.file_type if entry else fallback_file_type(f.path)
        language = entry.language if entry else language_for_path(f.path)
        runtime = entry.runtime if entry else runtime_for_language(language, file_type or "")

        files_out.append(
            FileSpecOut(
                path=f.path,
                generation_order=_generation_order_for_file(f.path, f.asset_source if f.path.startswith("assets/") else ""),
                purpose=f.purpose,
                required=f.required,
                can_skip=f.can_skip,
                file_type=file_type,
                file_kind=entry.file_kind if entry else file_kind_for_path(f.path),
                role=role,
                component_hint=entry.component_hint if entry else (role or ""),
                inputs=entry.inputs if entry else [],
                outputs=entry.outputs if entry else [],
                dependencies=entry.dependencies if entry else [],
                side_effects=entry.side_effects if entry else [],
                required_tool_slots=serialize_plan_items(entry.required_tool_slots) if entry else [],
                implementation_strategy=serialize_plan_items(entry.implementation_strategy) if entry else [],
                selected_tools=selected_tools_for_entry(entry),
                runtime_contract=entry.runtime_contract if entry else {},
                artifact_contract=entry.artifact_contract if entry else {},
                required_capabilities=entry.required_capabilities if entry else [],
                raw_capability_hints=entry.raw_capability_hints if entry else [],
                forbidden_capabilities=entry.forbidden_capabilities if entry else [],
                reference_files=entry.reference_files if entry else [],
                skill_local_references=entry.skill_local_references if entry else [],
                creator_internal_references=entry.creator_internal_references if entry else [],
                language=language,
                runtime=runtime,
                entrypoint=entry.entrypoint if entry else "",
                command_template=entry.command_template if entry else "",
                references=entry.reference_files if entry else [],
                low_confidence=(entry.confidence < 0.7) if entry else False,
                confidence=entry.confidence if entry else 1.0,
                reason=entry.reason if entry else "fallback path classification",
                heuristic_signals=entry.heuristic_signals if entry else [],
                asset_source=f.asset_source if f.path.startswith("assets/") else "",
            )
        )

    for path in extra_paths:
        role = fallback_role(path)
        file_type = fallback_file_type(path)
        language = language_for_path(path)
        runtime = runtime_for_language(language, file_type or "")
        is_asset = path.startswith("assets/")

        required_capabilities, forbidden_capabilities = [], []
        inputs, outputs = default_io_for_file_kind(file_kind_for_path(path))

        files_out.append(
            FileSpecOut(
                path=path,
                generation_order=_generation_order_for_file(path, ""),
                purpose=(
                    f"用户上传的静态素材：{path}"
                    if is_asset
                    else f"参考说明文件：{path}"
                ),
                required=True,
                can_skip=False,
                file_type=file_type,
                file_kind=file_kind_for_path(path),
                role=role,
                component_hint=role or "",
                inputs=list(inputs or []),
                outputs=list(outputs or []),
                dependencies=[],
                side_effects=[],
                required_tool_slots=[],
                implementation_strategy=[],
                selected_tools=[],
                runtime_contract={},
                artifact_contract={},
                required_capabilities=list(required_capabilities or []),
                raw_capability_hints=[],
                forbidden_capabilities=[
                    cap for cap in list(forbidden_capabilities or [])
                    if cap not in set(required_capabilities or [])
                ],
                reference_files=[],
                skill_local_references=[],
                creator_internal_references=[],
                language=language,
                runtime=runtime,
                entrypoint=path if path.startswith("scripts/") else "",
                command_template="",
                references=[],
                low_confidence=False,
                confidence=1.0,
                reason="fallback path classification from declared blueprint path",
                heuristic_signals=["declared_skill_path"],
                asset_source="",
            )
        )

    warnings: list[dict[str, Any]] = []
    workflow_allocation = await _allocate_workflow_script_responsibilities(
        blueprint_text=blueprint_text,
        files_out=files_out,
        requested_model=request.model,
        warnings=warnings,
    )
    workflow_allocation_summary = str(workflow_allocation.get("summary") or "")
    workflow_responsibility_edges = [
        edge for edge in (workflow_allocation.get("responsibility_edges") or []) if isinstance(edge, dict)
    ]
    workflow_allocation_errors = [
        issue for issue in (workflow_allocation.get("validation_errors") or []) if isinstance(issue, dict)
    ]
    if not workflow_allocation_errors:
        await _normalize_script_purpose_short_contracts(
            blueprint_text=blueprint_text,
            files_out=files_out,
            requested_model=request.model,
            warnings=warnings,
        )
    else:
        warnings.append({
            "severity": "validator_warning",
            "code": "purpose_normalization_skipped_after_allocation_graph_failure",
            "source": "analyze_blueprint",
            "path": "",
            "field": "purpose",
            "message": "Skipped purpose normalization because workflow allocation graph validation failed; keep the draft editable for allocator repair.",
        })
    fallback_requirement_graph = build_default_requirement_graph(files_out)
    try:
        requirement_graph = await _extract_requirement_graph_with_validator(
            blueprint_text=blueprint_text,
            files_out=files_out,
            requested_model=request.model,
            warnings=warnings,
        )
    except RequirementGraphValidationError as exc:
        requirement_graph = validate_requirement_graph_schema(fallback_requirement_graph, files_out)
        warnings.append({
            "severity": "validator_warning",
            "code": exc.code,
            "source": "responsibility_graph",
            "path": str((exc.details or {}).get("path") or ""),
            "field": "requirement_graph",
            "message": f"Responsibility graph patch failed; using deterministic graph: {exc}",
        })
    requirements_by_file: dict[str, list[RequirementItem]] = {}
    for req in requirement_graph.requirements:
        requirements_by_file.setdefault(req.target_file, []).append(req)
    for file_spec in files_out:
        file_spec.requirements = list(requirements_by_file.get(file_spec.path, []))
    _persist_requirement_graph(plan.skill_name, requirement_graph)
    _persist_workflow_allocation_summary(plan.skill_name, workflow_allocation_summary)
    _persist_workflow_allocation_edges(plan.skill_name, workflow_responsibility_edges)

    asset_requirements = [
        AssetRequirementOut(
            path=file_spec.path,
            generation_order=file_spec.generation_order,
            source=file_spec.asset_source,
            required=file_spec.required,
            description=file_spec.purpose,
        )
        for file_spec in files_out
        if file_spec.path.startswith("assets/") and file_spec.asset_source == "user_upload"
    ] + directory_asset_requirements

    available_tools = [tool_status(cap) for cap in list_tool_capabilities()]
    required_tool_names = {
        capability
        for file_spec in files_out
        for capability in file_spec.required_capabilities
    }
    missing_tool_configs = []
    def normalize_warning(item: Any) -> dict[str, Any] | None:
        if isinstance(item, dict):
            return {
                "severity": str(item.get("severity") or "normalization_note"),
                "code": str(item.get("code") or "normalization_note"),
                "source": str(item.get("source") or "skill_plan"),
                "path": str(item.get("path") or ""),
                "field": str(item.get("field") or ""),
                "message": str(item.get("message") or ""),
            }
        text = str(item or "").strip()
        if not text:
            return None
        return {
            "severity": "normalization_note",
            "code": "normalization_note",
            "source": "skill_plan",
            "path": "",
            "field": "",
            "message": text,
        }

    seen_warning_keys: set[str] = set()
    for raw_warning in [*list(plan.warnings), *extra_path_warnings, *blueprint_contract_warnings]:
        warning = normalize_warning(raw_warning)
        if not warning:
            continue
        key = ":".join(str(warning.get(part) or "") for part in ("source", "path", "field", "code"))
        if key in seen_warning_keys:
            continue
        seen_warning_keys.add(key)
        warnings.append(warning)
    for capability_name in sorted(required_tool_names):
        cap = get_tool_capability(capability_name)
        if not cap or cap.category == "resource":
            continue
        status = tool_status(cap)
        missing_runtime_helpers = status.get("missing_runtime_helpers") or []
        missing_dependencies = status.get("missing_dependencies") or []
        if not status["creator_available"]:
            warnings.append({"severity": "user_warning", "code": "tool_unavailable", "source": "generator", "path": "", "field": "required_capabilities", "message": f"工具能力 {capability_name} 已被禁用或不允许 Creator 使用，相关脚本不会默认获得该能力。"})
        if missing_runtime_helpers:
            warnings.append({"severity": "user_warning", "code": "tool_runtime_helper_missing", "source": "generator", "path": "", "field": "required_capabilities", "message": f"工具能力 {capability_name} 缺少 runtime helper: {', '.join(missing_runtime_helpers)}。"})
        if missing_dependencies:
            warnings.append({"severity": "user_warning", "code": "tool_runtime_dependency_missing", "source": "generator", "path": "", "field": "required_capabilities", "message": f"工具能力 {capability_name} 缺少 runtime dependency: {', '.join(missing_dependencies)}。"})
        if not status["configured"] or missing_runtime_helpers or missing_dependencies or not status["creator_available"]:
            missing_tool_configs.append(status)

    return AnalyzeBlueprintResponse(
        skill_name=plan.skill_name,
        files=files_out,
        warnings=warnings,
        asset_requirements=asset_requirements,
        final_outputs=_final_outputs_from_plan_entries(list(entries_by_path.values())),
        available_tools=available_tools,
        missing_tool_configs=missing_tool_configs,
        requirement_graph=requirement_graph,
        blueprint_text=blueprint_text,
        blueprint_refined=False,
    )


@router.post("/init-skill", response_model=InitSkillResponse)
async def init_skill(request: InitSkillRequest):
    """Initialise a new Skill directory structure."""
    skill_name = _validate_skill_name(request.skill_name)
    result = run_action({"action": "init", "name": skill_name})
    return InitSkillResponse(
        success=result["success"],
        path=result.get("path"),
        message=result["message"],
    )

@router.post("/upload-asset", response_model=UploadAssetResponse)
async def upload_asset(
    skill_name: str = Form(...),
    file_path: str = Form(...),
    file: UploadFile = File(...),
):
    skill_name = _validate_skill_name(skill_name)
    target_rel_path = _validate_asset_upload_path(file_path)

    skill_dir = settings.skills_path / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)

    target_path = skill_dir / target_rel_path
    target_path.parent.mkdir(parents=True, exist_ok=True)

    total = 0
    try:
        with target_path.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break

                total += len(chunk)
                if total > _MAX_ASSET_UPLOAD_BYTES:
                    try:
                        target_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    raise HTTPException(
                        status_code=413,
                        detail=f"素材文件超过大小限制：{_MAX_ASSET_UPLOAD_BYTES // 1024 // 1024}MB",
                    )

                out.write(chunk)
    finally:
        await file.close()

    return UploadAssetResponse(
        success=True,
        path=target_rel_path,
        size=total,
        message=f"素材已上传：{target_rel_path}",
    )


def _contract_failure_layer(results: list[ContractCheckResult]) -> str:
    failed = [result for result in results if not result.passed]
    if not failed:
        return "content_review"

    first = failed[0]
    if getattr(first, "layer", None):
        return str(first.layer)

    if getattr(first, "id", None):
        return str(first.id)

    return "content_review"


def _stage_error_from_exception(source: str, exc: Exception, *, default_layer: str) -> FileGenerationStageError:
    if isinstance(exc, FileGenerationStageError):
        return exc

    if isinstance(exc, ContractValidationError):
        layer = _contract_failure_layer(exc.results) or default_layer

        # 让 SKILL.md 蓝图一致性失败进入专门的返修 source。
        # 不是新增责任审查，只是把已有审查结果正确分类。
        if layer == "skill_md_blueprint_alignment" or any(
            getattr(result, "layer", None) == "skill_md_blueprint_alignment"
            for result in exc.results
            if not result.passed
        ):
            return FileGenerationStageError(
                source="skill_md_blueprint_alignment",
                layer="skill_md_blueprint_alignment",
                detail=str(exc),
                original=exc,
            )

        return FileGenerationStageError(
            source=source,
            layer=layer,
            detail=str(exc),
            original=exc,
        )

    return FileGenerationStageError(
        source=source,
        layer=default_layer,
        detail=str(exc),
        original=exc,
    )


def _first_round_repair_limit(source: str) -> int:
    return {
        "hard_format": 3,
        "markdown_format": 3,
        "content_review": 4,
        "skill_md_blueprint_alignment": 6,
        "script_responsibility": 5,
        "script_functional": 5,
        "model_empty_content": len(_EMPTY_GENERATION_PROMPT_VARIANTS),
    }.get(source, 4)


def _prompt_chars(messages: list[dict]) -> int:
    return sum(len(str(message.get("content") or "")) for message in messages if isinstance(message, dict))


async def _complete_creator_file_generation(
    *,
    messages: list[dict],
    model: str,
    skill_name: str,
    file_path: str,
    prompt_variant: str,
    retry_index: int,
) -> str:
    """Call the file-generation model with minimum diagnostic logging."""
    messages = _ensure_user_visible_task_message(messages)
    prompt_text = "\n".join(str(message.get("content") or "") for message in messages if isinstance(message, dict))
    logger.info(
        "[Creator][generate_file][llm_request] skill=%s file_path=%s model=%s prompt_variant=%s retry_index=%d prompt_chars=%d message_roles=%s system_chars=%d user_chars=%d uses_full_blueprint=%s uses_platform_protocol_text=%s",
        skill_name,
        file_path,
        model,
        prompt_variant,
        retry_index,
        _prompt_chars(messages),
        json.dumps(_message_role_counts(messages), ensure_ascii=False, sort_keys=True),
        _message_role_chars(messages, "system"),
        _message_role_chars(messages, "user"),
        "已确认的蓝图" in prompt_text or "Skill 架构蓝图" in prompt_text,
        any(
            marker in prompt_text
            for marker in ("宿主 Markdown 执行说明", "SKILL.md workflow", "第二轮 E2E", "平台执行协议")
        ),
    )
    try:
        content = await complete_chat_once(messages, model)
    except Exception as exc:
        logger.exception(
            "[Creator][generate_file][llm_response] skill=%s file_path=%s model=%s prompt_variant=%s retry_index=%d message_roles=%s system_chars=%d user_chars=%d error_type=%s",
            skill_name,
            file_path,
            model,
            prompt_variant,
            retry_index,
            json.dumps(_message_role_counts(messages), ensure_ascii=False, sort_keys=True),
            _message_role_chars(messages, "system"),
            _message_role_chars(messages, "user"),
            type(exc).__name__,
        )
        raise
    logger.info(
        "[Creator][generate_file][llm_response] skill=%s file_path=%s model=%s prompt_variant=%s retry_index=%d message_roles=%s system_chars=%d user_chars=%d finish_reason=%s content_len=%d error_type=%s",
        skill_name,
        file_path,
        model,
        prompt_variant,
        retry_index,
        json.dumps(_message_role_counts(messages), ensure_ascii=False, sort_keys=True),
        _message_role_chars(messages, "system"),
        _message_role_chars(messages, "user"),
        "unknown",
        len(content or ""),
        "",
    )
    return content

def _build_skill_md_model_finalizer_prompt(
    *,
    skill_name: str,
    description: str,
    blueprint_text: str,
    references: list[str] | None,
    assets: list[str] | None,
    final_outputs: list[str] | None,
) -> list[dict[str, str]]:
    resource_reference = {
        "references": references or [],
        "assets": assets or [],
        "final_outputs": final_outputs or [],
    }

    return [
        {
            "role": "system",
            "content": (
                "你是 SKILL.md 最终说明文档编辑器。"
                "请基于蓝图创作完整、自然、用户可读的 SKILL.md。"
                "不要泄露 runtime_contract、artifact_contract、ToolSlot、implementation_strategy 等内部字段。"
                "不要把本文档退化成合同字段清单。"
                "只输出 SKILL.md 文件内容，不要 Markdown 外层代码块。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"Skill 名称：{skill_name}\n"
                f"描述：{description}\n\n"
                "蓝图：\n"
                f"{clean_blueprint_body_text(blueprint_text or '')}\n\n"
                "资源和最终输出参考：\n"
                f"{json.dumps(resource_reference, ensure_ascii=False, indent=2)}\n\n"
                "请输出完整 SKILL.md 文件正文，必须满足：\n"
                "1. 文件必须以 YAML frontmatter 开头，包含 name 和 description。\n"
                "2. 正文应包含适用场景、用户需提供内容、执行流程、脚本调用说明、references/assets 使用说明、最终产物和注意事项。\n"
                "3. 如果蓝图包含 scripts/*.py，必须为每个真实脚本写一个标准、独立、无缩进的 ```bash fenced code block。\n"
                "4. 每个 ```bash block 内只能有一条真实 shell 命令。\n"
                "5. 标准命令格式必须是：python scripts/<真实脚本名>.py '<JSON object argv>'。\n"
                "6. 脚本路径后的第一个参数必须是 json.loads 可解析的 JSON object 字符串。\n"
                "7. JSON argv 必须是 object，但 object 内字段名必须由当前脚本真实接口、蓝图需求和上下游数据流决定。\n"
                "8. 第一条 workflow command 只能引用平台 guaranteed input envelope 中存在的字段；如不确定，传入通用 user_request/input payload/envelope，由入口脚本内部解析。不要引用 envelope 中不存在的独立 placeholder。\n"
                "9. 区分蓝图用户输入的必需项和可选项：依据‘可选/建议/若不指定/可以提供/默认’等语义判断，不写固定业务字段词表；可选项不能在 SKILL.md 中写成必填 placeholder。\n"
                "10. 如果蓝图存在可选用户参数但平台 payload 没有同名字段，应让入口脚本从 fields/options/payload 中存在则读取、不存在则内部默认化，或接收通用 user_request/input payload；不要要求 SKILL.md 传入不存在的独立 placeholder。\n"
                "11. 入口脚本命令必须兼容平台输入 envelope；入口脚本生成合同应在脚本内部填充可选参数默认值。\n"
                "12. 动态 placeholder 必须作为 JSON 字符串值出现；不要把未加引号的动态 placeholder 放进 JSON。\n"
                "13. 禁止在 ```bash block 中直接放 JSON 配置对象。\n"
                "14. 禁止在 ```bash block 中放 runner/script/argv 伪命令对象。\n"
                "15. 禁止在 ```bash block 中放说明文字、列表、多条命令或 `<真实参数>` 占位说明。\n"
                "16. 默认不要使用 --argv CLI flag，除非脚本源码明确实现了 --argv；Creator 默认脚本协议是 sys.argv[1] JSON object。\n"
                "17. references/*.md 只作为参考资料说明，不是执行源。不要把 reference 正文全文复制进 SKILL.md。\n"
                "18. assets/** 只能作为上传素材或静态资源引用，不能描述为 Creator 生成素材。\n"
                "19. 不要包含 Creator 创建流程、确认清单、点击开始创建、系统将自动创建文件等平台创建流程文案。\n"
                "20. 不要声称“已通过 E2E 校验”“可直接投入运行”，SKILL.md 是使用说明，不是校验报告。\n\n"
                "标准命令示例只说明形态，不代表固定字段：\n"
                "```bash\n"
                "python scripts/generate_story.py '{\"topic\":\"{{topic}}\",\"chapter_count\":5}'\n"
                "```\n\n"
                "```bash\n"
                "python scripts/build_pdf.py '{\"story_text\":\"{{story_text}}\",\"image_paths\":\"{{image_paths}}\"}'\n"
                "```\n\n"
                "注意：实际字段必须根据当前蓝图和脚本接口调整，禁止照抄示例字段。"
            ),
        },
    ]

def _strict_contract_rewrite_allowed(source: str) -> bool:
    # First-round repairs must stay localized: the validator identifies the
    # failed function/region, and the repair model edits only that region while
    # preserving entrypoints, JSON argv parsing, stdout fields, and artifact
    # protocol.  Do not escalate script failures into full rewrites.
    return source in {"content_review"}


def _repair_mode_for_first_round(*, source: str, file_path: str, attempt: int) -> str:
    """Choose repair mode for first-round generation failures.

    第一轮 scripts/** 只剩：
    - content_review：裸源码、语法、安全；
    - script_functional / script_responsibility：职责未完成。

    不再存在 script_smoke。
    """
    if source in {"script_functional", "script_responsibility", "script_requirement_failed"} and file_path.startswith("scripts/"):
        return "localized_patch"

    if attempt >= 2 and file_path.startswith("scripts/") and _strict_contract_rewrite_allowed(source):
        return "strict_contract_rewrite"

    return "minimal_edit"


def _contract_result_to_failure(result: ContractCheckResult) -> dict[str, Any]:
    issue = result.details.get("issue") if isinstance(result.details, dict) else None
    issue = issue if isinstance(issue, dict) else {}
    return {
        "id": result.id,
        "target": result.target,
        "message": result.message,
        "expected": result.expected,
        "minimal_edit": result.minimal_edit,
        "details": result.details,
        "layer": result.layer or _contract_layer_for_check_id(result.id),
        "resource_role": issue.get("resource_role"),
        "claim_type": issue.get("claim_type"),
        "repair_ops": issue.get("repair_ops") if isinstance(issue.get("repair_ops"), list) else [],
    }


def _exception_to_skill_md_failures(exc: Exception, *, source: str = "skill_md") -> list[dict[str, Any]]:
    if isinstance(exc, CreatorValidatorReviewError):
        return [{
            "id": f"{source}.validator_error",
            "target": "SKILL.md",
            "message": str(exc),
            "expected": "重试蓝图一致性 reviewer，获得有效 JSON 后再决定是否需要修 SKILL.md。",
            "minimal_edit": "不要修改 SKILL.md；这是审查器输出格式问题。",
            "details": {"raw_excerpt": exc.raw_excerpt},
            "layer": "validator_error",
            "severity": "advisory",
            "advisory": True,
        }]
    if isinstance(exc, ContractValidationError):
        return [_contract_result_to_failure(result) for result in exc.results if not result.passed]
    return [{
        "id": f"{source}.validation_error",
        "target": "SKILL.md",
        "message": str(exc),
        "expected": "SKILL.md 第一轮只修格式、蓝图一致性、脚本说明、资源说明、最终产物说明和平台/内部字段泄露。",
        "minimal_edit": "只修改 SKILL.md；不要修改 scripts、assets、SkillPlan 或 runtime specs。",
        "details": {},
        "layer": source,
    }]


def classify_skill_md_failure_severity(failure: dict[str, Any]) -> str:
    if str(failure.get("severity") or "").lower() in {"advisory", "note", "warning"}:
        return "advisory"
    if str(failure.get("layer") or "").lower() in {"advisory", "advisory_notes", "validator_error"}:
        return "advisory"
    if bool(failure.get("advisory")):
        return "advisory"
    return "hard"

def _failure_signature(failure: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(failure.get("target") or failure.get("target_file") or ""),
        str(failure.get("layer") or failure.get("source_layer") or ""),
        str(failure.get("id") or failure.get("check_id") or ""),
        str(failure.get("evidence") or (failure.get("details") or {}).get("evidence") or ""),
    )


def merge_duplicate_failures(failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for failure in failures:
        if not isinstance(failure, dict):
            continue
        key = _failure_signature(failure)
        if key not in merged:
            merged[key] = dict(failure)
        else:
            messages = [str(merged[key].get("message") or ""), str(failure.get("message") or "")]
            merged[key]["message"] = " / ".join(dict.fromkeys(m for m in messages if m))
            ops = []
            for source in (merged[key].get("repair_ops"), failure.get("repair_ops")):
                if isinstance(source, list):
                    ops.extend(op for op in source if isinstance(op, dict))
            if ops:
                merged[key]["repair_ops"] = ops
    return list(merged.values())


def resolve_resource_role_conflicts(failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize reviewer resource-role conflicts from structured fields only."""
    normalized: list[dict[str, Any]] = []
    for failure in failures:
        item = dict(failure)
        role = str(item.get("resource_role") or item.get("role") or "").lower()
        claim = str(item.get("claim_type") or item.get("resource_claim") or "").lower()
        target = str(item.get("target_file") or item.get("target") or "")
        if role == "reference" or target.startswith("references/"):
            if claim in {"forbid_read", "no_runtime_read", "must_not_read"}:
                item["severity"] = "advisory"
                item["message"] = (
                    str(item.get("message") or "")
                    + "（已按资源角色规则降级：reference 可按需只读加载，但不能执行、修改、生成或作为产物/素材。）"
                )
            elif claim in {"execution_step", "artifact", "asset_material", "model_generated", "modifiable"}:
                item["expected"] = (
                    "references/*.md 是只读、按需加载的参考资料；非执行步骤、非产物、非生成素材，且不得被修改。"
                )
        if role == "asset" or target.startswith("assets/"):
            if claim in {"model_generated", "modifiable", "write_asset"}:
                item["expected"] = "assets/** 只能是用户上传或结构预留的静态素材，不能由模型生成或写入。"
        normalized.append(item)
    return normalized


def _normalize_repair_ops(failure: dict[str, Any]) -> list[dict[str, Any]]:
    """Pass through structured repair ops; do not parse natural language."""
    allowed = {"replace", "delete", "append_after", "append_before"}
    source = failure.get("repair_ops")
    ops: list[dict[str, Any]] = []
    if isinstance(source, list):
        candidates = source
    elif isinstance(source, dict):
        candidates = [source]
    else:
        candidates = []
    for op in candidates:
        if not isinstance(op, dict):
            continue
        op_name = str(op.get("op") or "").lower()
        anchor = op.get("anchor") or op.get("evidence") or failure.get("evidence") or (failure.get("details") or {}).get("evidence")
        if op_name not in allowed or not isinstance(anchor, str) or not anchor:
            continue
        normalized = {"op": op_name, "anchor": anchor}
        if isinstance(op.get("text"), str):
            normalized["text"] = op["text"]
        if isinstance(op.get("replacement"), str):
            normalized["replacement"] = op["replacement"]
        if isinstance(op.get("new"), str):
            normalized["replacement"] = op["new"]
        ops.append(normalized)
    return ops


def normalize_skill_md_failures(failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    resolved = resolve_resource_role_conflicts(merge_duplicate_failures(failures))
    for failure in resolved:
        ops = _normalize_repair_ops(failure)
        if ops:
            failure["repair_ops"] = ops
    return [failure for failure in resolved if classify_skill_md_failure_severity(failure) == "hard"]


def failure_ledger_for_skill_md_finalize(
    failures: list[dict[str, Any]],
    *,
    previous_remaining: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    normalized_all = resolve_resource_role_conflicts(merge_duplicate_failures([*(previous_remaining or []), *failures]))
    hard = normalize_skill_md_failures(failures)
    current_signatures = {_failure_signature(failure) for failure in failures}
    resolved = [
        failure for failure in (previous_remaining or [])
        if _failure_signature(failure) not in current_signatures
    ]
    return {
        "resolved_failures": resolved,
        "remaining_failures": hard,
        "hard_failures": hard,
        "advisory_notes": [
            failure for failure in normalized_all
            if classify_skill_md_failure_severity(failure) != "hard"
        ],
    }


def _non_code_text_near_script(content: str, script_path: str, window: int = 360) -> str:
    normalized = content.replace("\r\n", "\n")
    idx = normalized.find(script_path)
    if idx < 0:
        return ""
    start = max(0, idx - window)
    end = min(len(normalized), idx + len(script_path) + window)
    nearby = normalized[start:end]
    nearby = re.sub(r"```[\s\S]*?```", " ", nearby)
    nearby = re.sub(r"`[^`]*`", " ", nearby)
    nearby = re.sub(r"[#>*_\-\[\]()`]", " ", nearby)
    return re.sub(r"\s+", " ", nearby).strip()


def _is_generic_script_description(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "").lower()
    if len(compact) < 24:
        return True
    generic_patterns = [
        "执行脚本", "运行脚本", "处理任务", "运行该步骤", "执行该步骤",
        "调用脚本", "script", "runthisscript", "processtask",
    ]
    return any(pattern in compact for pattern in generic_patterns) and len(compact) < 60


def _check_skill_md_script_narrative_quality(content: str, script_paths: list[str]) -> list[ContractCheckResult]:
    results: list[ContractCheckResult] = []
    for script_path in dict.fromkeys(path for path in script_paths if path.startswith("scripts/")):
        mentioned = script_path in content
        results.append(ContractCheckResult(
            id="skill_md.script.mentioned",
            passed=mentioned,
            target=script_path,
            message=f"{script_path} 已在 SKILL.md 中出现。" if mentioned else f"{script_path} 未在 SKILL.md 中出现。",
            expected="每个真实 scripts/** 都必须在 SKILL.md 中被提及。",
            minimal_edit=f"添加 {script_path} 的自然语言功能说明和对应 bash fenced block。",
            layer="skill_md_first_round",
        ))
        commands = _extract_script_command_templates(content, script_path) if mentioned else []
        results.append(ContractCheckResult(
            id="skill_md.script.bash_block_nearby",
            passed=bool(commands),
            target=script_path,
            message=f"{script_path} 有对应 bash fenced block。" if commands else f"{script_path} 缺少对应 bash fenced block。",
            expected="每个脚本必须有对应 ```bash fenced block，且 block 调用真实脚本路径。",
            minimal_edit=f"为 {script_path} 添加调用真实脚本路径且 argv 为 JSON object 的 bash fenced block。",
            layer="skill_md_first_round",
        ))
        nearby_text = _non_code_text_near_script(content, script_path)
        has_specific_description = bool(nearby_text) and not _is_generic_script_description(nearby_text)
        results.append(ContractCheckResult(
            id="skill_md.script.narrative_quality",
            passed=has_specific_description,
            target=script_path,
            message=f"{script_path} 附近有非代码块的具体功能说明。" if has_specific_description else f"{script_path} 附近缺少具体自然语言功能说明，或说明过于空泛。",
            expected="每个脚本附近必须说明它在整体流程中的作用，不能只有“执行脚本/处理任务/运行该步骤”。",
            minimal_edit=f"在 {script_path} 的 bash block 前后添加一句具体说明：它读取什么、完成什么流程步骤、产出什么用户可理解结果。",
            details={"nearby_text": nearby_text[:240]},
            layer="skill_md_first_round",
        ))
    return results

def _skill_md_first_round_failures(
    *,
    skill_name: str,
    content: str,
    blueprint_text: str,
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []

    # 1. 最早先做 Markdown 基础格式检查。
    # 格式不对，不进入后面的模型内容审查。
    raw_failures = _basic_markdown_format_failures(
        "SKILL.md",
        content,
        require_frontmatter=True,
    )
    if raw_failures:
        return raw_failures

    # 2. 再跑你现有的平台合同规则。
    try:
        _raise_file_contract_failures(validate_file_contract(
            file_path="SKILL.md",
            content=content,
            blueprint_text=blueprint_text or "",
            skill_plan_entry=None,
        ))
    except Exception as exc:
        failures.extend(_exception_to_skill_md_failures(exc, source="skill_md_contract"))

    # 3. 再检查文件引用。
    try:
        _validate_skill_md_against_existing_files(
            skill_name,
            content,
            blueprint_text=blueprint_text or "",
            require_existing=False,
        )
    except Exception as exc:
        failures.extend(_exception_to_skill_md_failures(exc, source="skill_md_files"))

    return failures


def _is_skill_md_finalize_content_patch_failure(failure: dict[str, Any]) -> bool:
    """Allow finalize localized patch only for ordinary Markdown content gaps."""
    if not isinstance(failure, dict):
        return False
    if _is_skill_md_finalize_format_rewrite_failure(failure):
        return False
    repair_ops = failure.get("repair_ops")
    if isinstance(repair_ops, list) and repair_ops:
        return True
    failure_id = str(failure.get("id") or "")
    return (
        str(failure.get("layer") or "") == "skill_md_first_round"
        and (
            failure_id.endswith(".narrative_quality")
            or failure_id.endswith(".reference.mentioned")
            or failure_id.endswith(".asset.mentioned")
        )
    )


def _is_skill_md_finalize_format_rewrite_failure(failure: dict[str, Any]) -> bool:
    """Route static SKILL.md document/command shape failures to full regeneration."""
    if not isinstance(failure, dict):
        return False
    failure_id = str(failure.get("id") or "")
    layer = str(failure.get("layer") or "")
    details = failure.get("details") if isinstance(failure.get("details"), dict) else {}
    if details.get("repair_strategy") == "full_rewrite" or details.get("model_patch_allowed") is False:
        return True
    if failure_id.startswith("skill_md.command_block."):
        return True
    if "json_argv" in failure_id or "shell_command" in failure_id:
        return True
    return layer == "hard_format"


def _split_skill_md_finalize_failures(
    failures: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    format_rewrite: list[dict[str, Any]] = []
    patchable: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    for failure in failures:
        if _is_skill_md_finalize_format_rewrite_failure(failure):
            format_rewrite.append(failure)
        elif classify_skill_md_failure_severity(failure) == "hard":
            patchable.append(failure)
        else:
            deferred.append(failure)
    return format_rewrite, patchable, deferred


def _build_skill_md_finalize_full_rewrite_messages(
    *,
    prompt_messages: list[dict[str, str]],
    skill_name: str,
    content: str,
    failures: list[dict[str, Any]],
) -> list[dict[str, str]]:
    return [
        *prompt_messages,
        {
            "role": "user",
            "content": (
                "上一版 SKILL.md 的静态文档结构不可稳定使用，请重新输出完整 SKILL.md。\n"
                "不要输出 patch、JSON 或解释。\n\n"
                "失败项 JSON：\n"
                f"{json.dumps(failures, ensure_ascii=False, indent=2, default=str)}\n\n"
                f"Skill 名称：{skill_name}\n\n"
                "上一版内容：\n"
                "<<<CURRENT_SKILL_MD\n"
                f"{content}\n"
                "CURRENT_SKILL_MD\n"
            ),
        },
    ]


def _skill_md_finalize_failure_region(failures: list[dict[str, Any]]) -> str:
    for failure in failures:
        region = markdown_failure_region(failure)
        if region == "metadata_region":
            return region
    return "body_region"


async def _repair_skill_md_model_finalizer(
    *,
    previous_content: str,
    failures: list[dict[str, Any]],
    prompt_messages: list[dict[str, str]],
    model: str,
    skill_name: str,
    attempt: int,
) -> str:
    """Repair ordinary SKILL.md Markdown content gaps by exact_replace patch."""

    failures_text = json.dumps(failures, ensure_ascii=False, indent=2, default=str)

    validation_error = (
        "SKILL.md 第一轮只修真正缺失的语义责任内容，需要做局部 patch。\n"
        "如果 failure 只是说法不够精确、证明不够细、内部字段未写、字段来源/运行时闭环未解释，不要生成 patch。\n"
        "失败项 JSON：\n"
        f"{failures_text}"
    )

    targeted_repair = (
        "只修复 failures 指向的真正缺失内容：用户需求、真实路径、主流程、最终产物或明显写反的资源角色。"
        "不要做措辞优化，不要补 role/source/dependencies/bundled 等内部 manifest 字段，"
        "不要补 stdout/placeholder 来源证明或第二轮 E2E 才负责的运行时闭环说明。"
        "未被 failures 指向的内容必须保持。"
        "不要输出完整 SKILL.md，只输出 exact_replace patch。"
    )

    return await _repair_generated_file_with_feedback(
        prompt_messages=prompt_messages,
        model=model,
        file_path="SKILL.md",
        previous_content=previous_content,
        validation_error=validation_error,
        targeted_repair=targeted_repair,
        contract_text=(
            "SKILL.md 是主 Skill 说明文档；本轮只补用户无法理解或无法启动 Skill 的缺失内容。"
        ),
        passed_checks_text="",
        failed_checks_text=failures_text,
        repair_mode="localized_patch",
        skill_plan_entry=None,
        patch_retry_limit=1,
    )


def _strip_unclosed_or_invalid_frontmatter_for_skill_md(content: str) -> str:
    """Return the best-effort Markdown body without trusting invalid frontmatter."""
    text = str(content or "").replace("\r\n", "\n")
    if not text.lstrip().startswith("---"):
        return text.strip()
    lines = text.splitlines()
    start = next((idx for idx, line in enumerate(lines) if line.strip() == "---"), None)
    if start is None:
        return text.strip()
    end = next((idx for idx in range(start + 1, len(lines)) if lines[idx].strip() == "---"), None)
    markdown_start = next(
        (idx for idx in range(start + 1, len(lines)) if re.match(r"\s*(#{1,6}\s+|```|~~~|[-*+]\s+|\d+[.)]\s+)", lines[idx])),
        None,
    )
    if end is not None:
        raw_meta = "\n".join(lines[start + 1:end])
        try:
            parsed = yaml.safe_load(raw_meta) or {}
            if isinstance(parsed, dict):
                return "\n".join(lines[end + 1:]).strip()
        except Exception:
            pass
        if markdown_start is not None and markdown_start < end:
            body_lines = lines[markdown_start:]
            if body_lines and body_lines[-1].strip() == "---":
                body_lines = body_lines[:-1]
            return "\n".join(body_lines).strip()
        return "\n".join(lines[end + 1:]).strip()
    # Frontmatter never closed. Prefer preserving the first Markdown-looking body
    # boundary if present; otherwise keep non-boundary text as editable body.
    if markdown_start is not None:
        return "\n".join(lines[markdown_start:]).strip()
    return "\n".join(line for line in lines[start + 1:] if line.strip() != "---").strip()


def _balance_markdown_fences_for_skill_md_body(body: str) -> str:
    markers = re.findall(r"(?m)^\s*(```|~~~)", body or "")
    if len(markers) % 2 == 1:
        return (body or "").rstrip() + "\n" + markers[-1] + "\n"
    return body or ""


def _minimal_skill_md_hard_format_fallback(
    *,
    skill_name: str,
    description: str,
    current_content: str,
) -> str:
    """Deterministically repair only SKILL.md hard Markdown boundaries.

    This is a last-resort hard-format fallback: rebuild legal frontmatter and
    preserve the best-effort body without changing business semantics.
    """
    safe_name = _validate_skill_name(skill_name)
    safe_description = str(description or "Skill usage instructions.").strip() or "Skill usage instructions."
    body = _strip_unclosed_or_invalid_frontmatter_for_skill_md(current_content)
    body = _balance_markdown_fences_for_skill_md_body(body).strip()
    if not body:
        body = f"# {safe_name}\n\n{safe_description}"
    elif not re.search(r"(?m)^#{1,6}\s+\S", body):
        body = f"# {safe_name}\n\n{body}"
    fallback = (
        "---\n"
        f"name: {json.dumps(safe_name, ensure_ascii=False)[1:-1]}\n"
        f"description: {json.dumps(safe_description, ensure_ascii=False)[1:-1]}\n"
        "---\n"
        f"{body.rstrip()}\n"
    )
    hard_failures = detect_markdown_hard_format_failures("SKILL.md", fallback, require_frontmatter=True)
    return fallback if not hard_failures else ""


def _safe_finalize_failure_content(
    *,
    skill_name: str,
    description: str,
    content: str,
    candidate: str,
) -> str:
    raw = content or candidate or ""
    if not detect_markdown_hard_format_failures("SKILL.md", raw, require_frontmatter=True):
        return raw
    return _minimal_skill_md_hard_format_fallback(
        skill_name=skill_name,
        description=description,
        current_content=raw,
    )


@router.post("/finalize-skill-md")
async def finalize_skill_md(request: FinalizeSkillMdRequest):
    """Finalize SKILL.md with staged repair.

    阶段：
    1. Markdown 格式错误：metadata/body 区域重写，最多 3 轮；
    2. 合同/责任/蓝图错误：局部 diff 修复；
    3. 最终失败也返回可编辑草稿，不抛 400，避免前端文件变灰。
    """
    skill_name = _validate_skill_name(request.skill_name)
    route = route_creator_file_model(
        file_path="SKILL.md",
        purpose=request.description or "final SKILL.md",
        requested_model=request.model,
    )

    prompt_messages = _build_skill_md_model_finalizer_prompt(
        skill_name=skill_name,
        description=request.description or "",
        blueprint_text=request.blueprint_text or "",
        references=request.references,
        assets=request.assets,
        final_outputs=request.final_outputs,
    )

    failures: list[dict[str, Any]] = []
    repair_events: list[dict[str, Any]] = []
    previous_remaining_failures: list[dict[str, Any]] = []
    candidate = ""
    content = ""
    skip_semantic_review_after_patch = False

    for attempt in range(1, _MAX_FILE_REPAIR_ATTEMPTS + 1):
        try:
            if attempt == 1:
                candidate = await _complete_creator_file_generation(
                    messages=prompt_messages,
                    model=route.model,
                    skill_name=skill_name,
                    file_path="SKILL.md",
                    prompt_variant="model_finalizer",
                    retry_index=0,
                )

            content = _sanitize_generated_file_content("SKILL.md", candidate)

            # 阶段 1：Markdown 基础格式错误，直接整文件重写，不走 diff。
            format_failures = detect_markdown_hard_format_failures(
                "SKILL.md",
                content,
                require_frontmatter=True,
            )
            if format_failures:
                failures = format_failures
                repair_events.append({
                    "attempt": attempt,
                    "target_file": "SKILL.md",
                    "patch_status": "hard_format_failed",
                    "format_rewrite_status": "format_region_rewrite",
                    "failures": format_failures,
                })

                if attempt >= 3:
                    fallback = _minimal_skill_md_hard_format_fallback(
                        skill_name=skill_name,
                        description=request.description or "final SKILL.md",
                        current_content=content,
                    )
                    fallback_failures = (
                        detect_markdown_hard_format_failures("SKILL.md", fallback, require_frontmatter=True)
                        if fallback
                        else format_failures
                    )
                    repair_events.append({
                        "attempt": attempt,
                        "target_file": "SKILL.md",
                        "patch_status": "deterministic_hard_format_fallback",
                        "success": bool(fallback) and not fallback_failures,
                        "failures": fallback_failures,
                    })
                    if fallback and not fallback_failures:
                        content = fallback
                        candidate = fallback
                    break

                failed_region = markdown_failure_region(format_failures[0])
                rewrite_messages = _build_markdown_region_rewrite_prompt(
                    file_path="SKILL.md",
                    skill_name=skill_name,
                    blueprint_text=request.blueprint_text or "",
                    deterministic_error=json.dumps(format_failures, ensure_ascii=False, indent=2, default=str),
                    current_content=content,
                    region=failed_region,
                )

                rewritten_region = await _complete_creator_file_generation(
                    messages=rewrite_messages,
                    model=route.model,
                    skill_name=skill_name,
                    file_path="SKILL.md",
                    prompt_variant=f"rewrite_markdown_{failed_region}",
                    retry_index=attempt - 1,
                )
                candidate = _merge_markdown_region_rewrite(content, rewritten_region, failed_region)
                rewritten_content = _sanitize_generated_file_content("SKILL.md", candidate)
                rewritten_failures = detect_markdown_hard_format_failures(
                    "SKILL.md",
                    rewritten_content,
                    require_frontmatter=True,
                )
                repair_events.append({
                    "attempt": attempt,
                    "target_file": "SKILL.md",
                    "patch_status": "format_full_rewrite_validation",
                    "success": not rewritten_failures,
                    "failures": rewritten_failures,
                })
                continue

            # 阶段 2：平台合同、文件引用等非基础 Markdown 格式问题。
            failures = _skill_md_first_round_failures(
                skill_name=skill_name,
                content=content,
                blueprint_text=request.blueprint_text or "",
            )
            ledger = failure_ledger_for_skill_md_finalize(
                failures,
                previous_remaining=previous_remaining_failures,
            )
            failures = list(ledger["remaining_failures"])
            previous_remaining_failures = failures

            # 阶段 3：格式/合同通过后，再做第一轮语义覆盖审查。
            # 如果上一轮已经应用 localized patch，本轮只做通用安全校验
            # （Markdown/命令块/本地路径），避免 reviewer 为新措辞反复返修。
            if not failures and skip_semantic_review_after_patch:
                return {
                    "success": True,
                    "content": content,
                    "repair_attempts": attempt - 1,
                    "validation_status": "passed",
                    "editable": True,
                    "disabled": False,
                    "repair_events": repair_events,
                }

            if not failures:
                try:
                    await _validate_skill_md_blueprint_alignment(
                        skill_name=skill_name,
                        content=content,
                        blueprint_text=request.blueprint_text or "",
                        skill_plan_entry=None,
                        model=request.model or route.model,
                    )
                except Exception as exc:
                    raw_failures = _exception_to_skill_md_failures(exc, source="blueprint_alignment")
                    ledger = failure_ledger_for_skill_md_finalize(
                        raw_failures,
                        previous_remaining=previous_remaining_failures,
                    )
                    failures = list(ledger["remaining_failures"])
                    previous_remaining_failures = failures

            if not failures:
                return {
                    "success": True,
                    "content": content,
                    "repair_attempts": attempt - 1,
                    "validation_status": "passed",
                    "editable": True,
                    "disabled": False,
                    "repair_events": repair_events,
                }

            if attempt >= _MAX_FILE_REPAIR_ATTEMPTS:
                break

            format_rewrite_failures, patchable_failures, deferred_failures = _split_skill_md_finalize_failures(failures)
            if format_rewrite_failures:
                failed_region = _skill_md_finalize_failure_region(format_rewrite_failures)
                repair_events.append({
                    "attempt": attempt,
                    "target_file": "SKILL.md",
                    "patch_status": "format_region_rewrite",
                    "region": failed_region,
                    "failures": format_rewrite_failures,
                })
                rewrite_messages = _build_markdown_region_rewrite_prompt(
                    file_path="SKILL.md",
                    skill_name=skill_name,
                    blueprint_text=request.blueprint_text or "",
                    deterministic_error=json.dumps(format_rewrite_failures, ensure_ascii=False, indent=2, default=str),
                    current_content=content,
                    region=failed_region,
                )
                rewritten_region = await _complete_creator_file_generation(
                    messages=rewrite_messages,
                    model=route.model,
                    skill_name=skill_name,
                    file_path="SKILL.md",
                    prompt_variant=f"finalize_rewrite_markdown_{failed_region}",
                    retry_index=attempt - 1,
                )
                candidate = _merge_markdown_region_rewrite(content, rewritten_region, failed_region)
                continue

            if not patchable_failures:
                repair_events.append({
                    "attempt": attempt,
                    "target_file": "SKILL.md",
                    "patch_status": "deferred_non_content_failures",
                    "failures": failures,
                })
                break

            try:
                candidate = await _repair_skill_md_model_finalizer(
                    previous_content=content,
                    failures=patchable_failures,
                    prompt_messages=prompt_messages,
                    model=route.model,
                    skill_name=skill_name,
                    attempt=attempt,
                )
                skip_semantic_review_after_patch = True
                if deferred_failures:
                    previous_remaining_failures = deferred_failures
            except CreatorRepairProposalParseError as parse_exc:
                repair_events.append({
                    "attempt": attempt,
                    "target_file": "SKILL.md",
                    "patch_status": "parse_failed",
                    "parser_error": parse_exc.parser_error,
                    "last_output_excerpt": parse_exc.last_output_excerpt,
                    "diff_extraction_attempted": parse_exc.diff_extraction_attempted,
                    "old_lines_new_lines_fallback_attempted": parse_exc.lines_fallback_attempted,
                })
                failures = failures or patchable_failures
                break
            except Exception as repair_exc:
                repair_events.append({
                    "attempt": attempt,
                    "target_file": "SKILL.md",
                    "patch_status": "patch_failed",
                    "error": f"{type(repair_exc).__name__}: {repair_exc}",
                    "failures": patchable_failures,
                })
                failures = failures or patchable_failures
                break

        except Exception as exc:
            logger.exception(
                "[Creator][skill_md][finalize_attempt_failed] skill=%s attempt=%s",
                skill_name,
                attempt,
            )
            failures = _exception_to_skill_md_failures(exc, source="skill_md_model_finalize")
            break

    safe_failure_content = _safe_finalize_failure_content(
        skill_name=skill_name,
        description=request.description or "final SKILL.md",
        content=content,
        candidate=candidate,
    )
    return {
        "success": False,
        "content": safe_failure_content,
        "repair_attempts": max(0, attempt if "attempt" in locals() else 0),
        "validation_status": "needs_repair",
        "needs_repair": True,
        "editable": True,
        "disabled": False,
        "failures": failures,
        "repair_events": repair_events,
        "error": "SKILL.md finalize did not pass after repair attempts.",
    }



def _structured_failure_signature(stage_error: FileGenerationStageError, deterministic_error: str) -> str:
    """Stable signature for repeated first-round failures, independent of candidate text."""
    original = getattr(stage_error, "original", None)
    records: list[dict[str, Any]] = []
    if isinstance(original, ContractValidationError):
        for result in original.results:
            if getattr(result, "passed", False):
                continue
            details = getattr(result, "details", {}) or {}
            records.append({
                "check_id": getattr(result, "id", ""),
                "layer": getattr(result, "layer", "") or getattr(stage_error, "layer", ""),
                "target": getattr(result, "target", ""),
                "missing_evidence": details.get("missing_evidence") or details.get("missing_requirement_ids") or [],
                "matched": details.get("matches") or details.get("matched_paths") or getattr(result, "matched_paths", []),
                "code_region": details.get("code_region") or details.get("line_region") or "",
                "evidence": details.get("evidence") or getattr(result, "message", ""),
            })
    elif isinstance(original, ScriptFunctionalValidationError):
        for issue in getattr(original, "issues", []) or []:
            if not isinstance(issue, dict):
                continue
            records.append({
                "check_id": issue.get("id") or issue.get("issue_type") or "script_functional",
                "layer": getattr(original, "layer", "") or getattr(stage_error, "layer", ""),
                "target": issue.get("failed_file") or issue.get("target_file") or "",
                "requirement_id": issue.get("requirement_id") or "",
                "missing_evidence": issue.get("missing_evidence") or [],
                "matched": issue.get("matched_ranges") or issue.get("matches") or [],
                "code_region": issue.get("code_region") or issue.get("line_region") or "",
                "evidence": issue.get("evidence") or issue.get("reason") or "",
            })
    if not records:
        records.append({
            "check_id": getattr(stage_error, "source", ""),
            "layer": getattr(stage_error, "layer", ""),
            "evidence": str(deterministic_error or "")[:1000],
        })
    payload = {"source": stage_error.source, "layer": stage_error.layer, "records": records}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()

def _canonicalize_generated_candidate(
    *,
    file_path: str,
    content: str,
    role: str | None = None,
    skill_plan_entry: dict[str, Any] | None = None,
    skill_name: str = "",
    purpose: str = "",
) -> str:
    """Return the canonical single-file candidate for validation/repair/SSE."""
    canonical = _sanitize_generated_file_content(
        file_path,
        content,
        role=role,
        skill_plan_entry=skill_plan_entry,
    )
    canonical, _metadata_patched = _canonicalize_markdown_frontmatter_for_file(
        file_path=file_path,
        content=canonical,
        skill_name=skill_name,
        purpose=purpose,
    )
    if file_path.startswith("references/") and Path(file_path).suffix.lower() == ".md":
        canonical = _ensure_reference_metadata_frontmatter(
            file_path=file_path,
            content=canonical,
            purpose=purpose,
            skill_plan_entry=skill_plan_entry,
        )
    return canonical

_SCRIPT_RAW_SOURCE_FORMAT_ERROR_IDS = {
    "script.raw_source.single_file",
    "script.raw_source.ambiguous_multi_code_blocks",
    "script.raw_source.multi_file_bundle",
    "script.raw_source.ambiguous_script_candidate",
}


def is_script_raw_source_format_error(stage_error: FileGenerationStageError) -> bool:
    """Route scripts/* raw-source structure failures to regeneration only."""
    if getattr(stage_error, "source", "") not in {"content_review", "script_raw_source", "format_stage"}:
        return False
    original = getattr(stage_error, "original", None)
    if isinstance(original, ContractValidationError):
        return any((not result.passed) and result.id in _SCRIPT_RAW_SOURCE_FORMAT_ERROR_IDS for result in original.results)
    detail = str(getattr(stage_error, "detail", "") or "")
    return any(error_id in detail for error_id in _SCRIPT_RAW_SOURCE_FORMAT_ERROR_IDS)


def is_generation_format_error(stage_error: FileGenerationStageError) -> bool:
    return is_script_raw_source_format_error(stage_error) or (
        getattr(stage_error, "source", "") == "format_stage"
        and _stage_error_has_full_format_rewrite_contract(stage_error)
    )


def _result_requires_full_format_rewrite(result: Any) -> bool:
    """Detect structured first-step format failures without matching prose/id text."""
    if isinstance(result, ContractCheckResult):
        if result.passed:
            return False
        details = result.details if isinstance(result.details, dict) else {}
        severity = str(details.get("severity") or "").strip()
        repair_strategy = str(details.get("repair_strategy") or "").strip()
        model_patch_allowed = details.get("model_patch_allowed")
        return (
            result.layer == "hard_format"
            or severity == "hard_format"
            or repair_strategy == "full_rewrite"
            or model_patch_allowed is False
        )

    if isinstance(result, dict):
        if result.get("passed") is True:
            return False
        return (
            str(result.get("layer") or "").strip() == "hard_format"
            or str(result.get("severity") or "").strip() == "hard_format"
            or str(result.get("repair_strategy") or "").strip() == "full_rewrite"
            or result.get("model_patch_allowed") is False
        )

    return False


def _stage_error_has_full_format_rewrite_contract(stage_error: FileGenerationStageError) -> bool:
    original = getattr(stage_error, "original", None)
    if isinstance(original, ContractValidationError):
        return any(_result_requires_full_format_rewrite(result) for result in original.results)

    detail = str(getattr(stage_error, "detail", "") or "").strip()
    if not detail:
        return False
    try:
        parsed = json.loads(detail)
    except Exception:
        return False
    items = parsed if isinstance(parsed, list) else [parsed]
    return any(_result_requires_full_format_rewrite(item) for item in items)


def is_markdown_hard_format_error(stage_error: FileGenerationStageError) -> bool:
    if getattr(stage_error, "source", "") == "hard_format" or getattr(stage_error, "layer", "") == "hard_format":
        return True
    return _stage_error_has_full_format_rewrite_contract(stage_error)


def _first_round_format_stage_error(
    *,
    file_path: str,
    content: str,
    role: str | None = None,
    skill_plan_entry: dict[str, Any] | None = None,
) -> FileGenerationStageError | None:
    """FORMAT_STAGE: decide only whether candidate is legal content for file_path.

    This stage runs before any responsibility review.  Failures returned here
    must be handled by full current-file regeneration, not patch repair.
    """
    if not str(content or "").strip():
        return FileGenerationStageError(
            source="format_stage",
            layer="format_stage",
            detail="FORMAT_STAGE failed: generated candidate is empty.",
        )

    if (
        file_path == "SKILL.md"
        or file_path.startswith("references/")
        or Path(file_path).suffix.lower() in {".md", ".markdown"}
    ):
        format_failures = detect_markdown_hard_format_failures(
            file_path,
            content,
            require_frontmatter=(file_path == "SKILL.md"),
        )
        if format_failures:
            return FileGenerationStageError(
                source="hard_format",
                layer="hard_format",
                detail=json.dumps(format_failures, ensure_ascii=False, indent=2, default=str),
            )

    if file_path.startswith("scripts/"):
        raw_source_error_id = script_raw_source_candidate_error_id(content)
        if raw_source_error_id:
            return FileGenerationStageError(
                source="format_stage",
                layer="script_raw_source",
                detail=raw_source_error_id,
                original=ContractValidationError("FORMAT_STAGE script source structure failed.", [
                    ContractCheckResult(
                        id=raw_source_error_id,
                        passed=False,
                        target=file_path,
                        message="FORMAT_STAGE failed: candidate is not a single raw script source file.",
                        expected="Candidate must be one complete source file for the requested path.",
                        minimal_edit="Regenerate the complete current file; do not patch.",
                        details={"repair_strategy": "full_rewrite", "model_patch_allowed": False},
                        layer="format_stage",
                    )
                ]),
            )

    return None


def _build_markdown_format_full_rewrite_prompt(
    *,
    file_path: str,
    skill_name: str,
    blueprint_text: str,
    deterministic_error: str,
    current_content: str,
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "你是 Markdown 文件 hard format 全量重写器。"
                "你必须只输出完整目标 Markdown 文件内容。"
                "不要 JSON patch；不要 old_lines/new_lines；不要 diff；不要解释；不要日志；"
                "不要把 repair proposal JSON 嵌进 Markdown。"
                "frontmatter 必须完整闭合；所有 fenced block 必须成对闭合。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"文件路径：{file_path}\n"
                f"Skill 名称：{skill_name}\n\n"
                "后台 Markdown hard format 校验失败项如下；这是格式重试，不是业务语义 repair：\n"
                f"{deterministic_error}\n\n"
                "硬性要求：\n"
                "1. 只输出完整目标 Markdown 文件内容。\n"
                "2. 不要 JSON patch。\n"
                "3. 不要 old_lines/new_lines。\n"
                "4. 不要解释、日志或分析。\n"
                "5. frontmatter 必须完整闭合。\n"
                "6. 所有 fenced block 必须成对闭合。\n"
                "7. 不要把 repair proposal JSON 嵌进 Markdown。\n"
                "8. 不要混入 command argv / 字段对齐 / workflow dataflow 的局部修复；格式合法后由后续校验处理。\n\n"
                "蓝图上下文：\n"
                f"{(blueprint_text or '')[:8000]}\n\n"
                "当前文件内容：\n"
                "<<<CURRENT_FILE\n"
                f"{current_content or ''}\n"
                "CURRENT_FILE\n"
            ),
        },
    ]


def _build_markdown_region_rewrite_prompt(
    *,
    file_path: str,
    skill_name: str,
    blueprint_text: str,
    deterministic_error: str,
    current_content: str,
    region: str,
) -> list[dict[str, str]]:
    regions = split_markdown_regions(current_content or "")
    metadata_summary = regions.metadata_region[:1200] if regions.metadata_region else "（无 metadata_region）"
    if region == "metadata_region":
        system = (
            "你是 Markdown metadata_region 格式修复器。"
            "只输出修复后的 metadata_region；不要输出正文 body；不要解释。"
            "metadata_region 必须是闭合、可解析的 YAML frontmatter。"
        )
        user = (
            f"文件路径：{file_path}\nSkill 名称：{skill_name}\n\n"
            "失败项：\n"
            f"{deterministic_error}\n\n"
            "只修 metadata_region。禁止输出 body_region，禁止修改正文语义。\n"
            "输出必须以 --- 开始，并以单独一行 --- 闭合。\n"
            "metadata 只描述当前文件自身，不要写其它 scripts 的 capability/runtime/tool 边界。\n\n"
            "当前 metadata_region：\n<<<METADATA_REGION\n"
            f"{regions.metadata_region or ''}\n"
            "METADATA_REGION\n\n"
            "当前 body_region 摘要（仅供理解，禁止输出）：\n<<<BODY_SUMMARY\n"
            f"{(regions.body_region or '')[:3000]}\n"
            "BODY_SUMMARY\n\n"
            "蓝图上下文：\n"
            f"{(blueprint_text or '')[:6000]}"
        )
    else:
        system = (
            "你是 Markdown body_region 格式修复器。"
            "只输出修复后的 body_region；不要输出 YAML frontmatter；不要解释。"
            "body_region 可包含普通 Markdown、bash/json/code fence，但 fence 必须闭合。"
        )
        user = (
            f"文件路径：{file_path}\nSkill 名称：{skill_name}\n\n"
            "失败项：\n"
            f"{deterministic_error}\n\n"
            "只修 body_region。禁止重新生成 metadata/frontmatter，禁止输出文件开头 ---。\n"
            "如果 command/bash block 格式错误，只修正文中的 block。"
            "reference 正文不得重新定义 scripts/*.py 的 capability/runtime/tool 边界。\n\n"
            "已校验 metadata_region 摘要（只供遵循，禁止输出）：\n<<<METADATA_REGION\n"
            f"{metadata_summary}\n"
            "METADATA_REGION\n\n"
            "当前 body_region：\n<<<BODY_REGION\n"
            f"{regions.body_region or current_content or ''}\n"
            "BODY_REGION\n\n"
            "蓝图上下文：\n"
            f"{(blueprint_text or '')[:6000]}"
        )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _merge_markdown_region_rewrite(current_content: str, rewritten_region: str, region: str) -> str:
    regions = split_markdown_regions(current_content or "")
    if region == "metadata_region":
        return merge_markdown_regions(rewritten_region, regions.body_region)
    body = rewritten_region
    # Guard against model accidentally returning a second frontmatter while body
    # repair is requested; preserve already-validated metadata.
    accidental = split_markdown_regions(body)
    if accidental.metadata_region and accidental.metadata_closed:
        body = accidental.body_region
    return merge_markdown_regions(regions.metadata_region, body)


def _build_markdown_initial_region_prompt(
    *,
    file_path: str,
    skill_name: str,
    purpose: str,
    blueprint_text: str,
    region: str,
    metadata_region: str = "",
) -> list[dict[str, str]]:
    if region == "metadata_region":
        return [
            {"role": "system", "content": "你是 Markdown metadata_region 生成器。只输出闭合 YAML frontmatter。"},
            {"role": "user", "content": (
                f"为 {file_path} 生成 metadata_region。\n"
                f"Skill 名称：{skill_name}\n职责：{purpose}\n\n"
                "metadata 只描述当前文件自身；必须可解析、闭合；不要写正文长段落；"
                "不要定义其它 scripts/*.py 的 capability/runtime/tool 边界。\n"
                + ("SKILL.md 必须包含 name 和 description。\n" if file_path == "SKILL.md" else "")
                + "只输出 metadata_region，不输出 body。\n\n蓝图：\n"
                f"{(blueprint_text or '')[:8000]}"
            )},
        ]
    return [
        {"role": "system", "content": "你是 Markdown body_region 生成器。只输出正文，不输出 YAML frontmatter。"},
        {"role": "user", "content": (
            f"为 {file_path} 生成 body_region。\n"
            f"Skill 名称：{skill_name}\n职责：{purpose}\n\n"
            "已校验 metadata_region：\n<<<METADATA_REGION\n"
            f"{metadata_region}\n"
            "METADATA_REGION\n\n"
            "body 可包含普通说明、工作流、bash/json/markdown code block、示例和注意事项；"
            "所有 code fence 必须闭合。SKILL.md 的 bash command block 仍由你生成。"
            "reference body 不要重新定义 scripts/*.py 的 capability/runtime/tool 边界。\n"
            "只输出 body_region，不输出 frontmatter。\n\n蓝图：\n"
            f"{(blueprint_text or '')[:8000]}"
        )},
    ]


async def _generate_markdown_initial_regions(
    *,
    file_path: str,
    skill_name: str,
    purpose: str,
    blueprint_text: str,
    model: str,
) -> str:
    """Generate .md files as metadata/body regions with metadata rewrite retry."""

    metadata_region = ""
    metadata_failures: list[dict[str, Any]] = []
    retry_limit = _first_round_repair_limit("hard_format")
    for retry_index in range(0, retry_limit + 1):
        if retry_index == 0:
            messages = _build_markdown_initial_region_prompt(
                file_path=file_path,
                skill_name=skill_name,
                purpose=purpose,
                blueprint_text=blueprint_text,
                region="metadata_region",
            )
            prompt_variant = "generate_markdown_metadata_region"
        else:
            messages = _build_markdown_region_rewrite_prompt(
                file_path=file_path,
                skill_name=skill_name,
                blueprint_text=blueprint_text,
                deterministic_error=json.dumps(metadata_failures, ensure_ascii=False, indent=2, default=str),
                current_content=merge_markdown_regions(metadata_region, ""),
                region="metadata_region",
            )
            prompt_variant = "rewrite_markdown_metadata_region"

        metadata_region = await _complete_creator_file_generation(
            messages=messages,
            model=model,
            skill_name=skill_name,
            file_path=file_path,
            prompt_variant=prompt_variant,
            retry_index=retry_index,
        )
        metadata_failures = [
            failure for failure in detect_markdown_hard_format_failures(
                file_path,
                merge_markdown_regions(metadata_region, "# temporary body\n"),
                require_frontmatter=(file_path == "SKILL.md"),
            )
            if markdown_failure_region(failure) == "metadata_region"
        ]
        if not metadata_failures:
            break
    else:
        raise FileGenerationStageError(
            source="hard_format",
            layer="hard_format",
            detail=json.dumps(metadata_failures, ensure_ascii=False, default=str),
        )

    body_region = await _complete_creator_file_generation(
        messages=_build_markdown_initial_region_prompt(
            file_path=file_path,
            skill_name=skill_name,
            purpose=purpose,
            blueprint_text=blueprint_text,
            region="body_region",
            metadata_region=metadata_region,
        ),
        model=model,
        skill_name=skill_name,
        file_path=file_path,
        prompt_variant="generate_markdown_body_region",
        retry_index=0,
    )
    return merge_markdown_regions(metadata_region, body_region)


def _markdown_warning_error_type(file_path: str, default: str = "md_format_warning") -> str:
    if file_path.startswith("references/"):
        return "reference_content_warning"
    return default


def _build_strict_script_source_only_regeneration_prompt(
    *,
    file_path: str,
    skill_name: str,
    purpose: str,
    blueprint_text: str,
    role: str | None,
    skill_plan_entry: dict[str, Any] | None,
    deterministic_error: str,
    previous_content: str,
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "你是 scripts/* 单文件源码生成器。必须直接重新生成完整单文件脚本源码。"
                "只输出目标脚本源码；不要 Markdown；不要 ``` fence；不要解释；不要多个版本；"
                "不要 Wait / Actually / Let me correct 自我修正；不要文件路径标题；不要多文件包；"
                "不要把旧内容做 patch；不要输出 diff/JSON；不要输出 old_lines/new_lines。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"文件路径：{file_path}\n"
                f"Skill 名称：{skill_name}\n"
                f"角色：{role or ''}\n"
                f"用途：{purpose or ''}\n\n"
                "上一次生成被判定为 scripts/* raw source 格式失败；这不是业务语义 repair，必须重新生成。\n"
                "失败信息：\n"
                f"{deterministic_error}\n\n"
                "硬性输出要求：\n"
                "- 只输出目标脚本源码。\n"
                "- 不要 Markdown。\n"
                "- 不要 ``` fence。\n"
                "- 不要解释。\n"
                "- 不要多个版本。\n"
                "- 不要 Wait / Actually / Let me correct 自我修正。\n"
                "- 不要文件路径标题。\n"
                "- 不要多文件包。\n"
                "- 不要把旧内容做 patch。\n"
                "- 直接重新生成完整单文件脚本源码。\n\n"
                "SkillPlanEntry：\n"
                f"{json.dumps(skill_plan_entry or {}, ensure_ascii=False, default=str)[:8000]}\n\n"
                "蓝图上下文：\n"
                f"{(blueprint_text or '')[:12000]}\n\n"
                "上一轮错误内容仅供避免重复格式错误，不要 patch：\n"
                "<<<PREVIOUS_CONTENT\n"
                f"{(previous_content or '')[:12000]}\n"
                "PREVIOUS_CONTENT\n"
            ),
        },
    ]


@router.post("/generate-file")
async def generate_file(request: GenerateFileRequest):
    """Generate one Creator file and stream it back as SSE.

    Important:
    - This endpoint must not write files to disk.
    - The frontend expects streamed content and then calls /write-file.
    - assets/** are upload-only and must never be generated by model.
    - SKILL.md must pass blueprint-intent alignment before returned.
    - references/*.md may omit YAML frontmatter; if present it must use ordinary document metadata only.
    """
    skill_name = _validate_skill_name(request.skill_name)
    _validate_file_path(request.file_path)

    if request.file_path.startswith("assets/") and (request.skill_plan_entry or {}).get("asset_source") != "bundled":
        raise HTTPException(
            status_code=400,
            detail=f"{request.file_path} 属于 assets 静态素材目录；只有 source=bundled 的预置静态资源可由 Creator 生成，source=user_upload 必须上传。",
        )

    # request.skill_plan_entry 来自前端请求态，只能作为 hint，不能作为唯一可信合同。
    # 这里不再因为 outputs/artifact_contract/file_kind 不完整而在生成循环外 422；
    # 真正的单文件合同会在 event_stream 内由后端基于 file_path、role、purpose、SKILL.md/blueprint 重新归一化。
    # 只有明确的 path 冲突才属于不可恢复请求错误。
    raw_skill_plan_entry = request.skill_plan_entry if isinstance(request.skill_plan_entry, dict) else {}
    raw_entry_path = str(raw_skill_plan_entry.get("path") or "").strip()
    if request.file_path.startswith("scripts/") and raw_entry_path and raw_entry_path != request.file_path:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "contract_path_mismatch",
                "severity": "user_warning",
                "source": "generator",
                "path": request.file_path,
                "field": "skill_plan_entry.path",
                "message": f"skill_plan_entry.path={raw_entry_path} 与当前 file_path={request.file_path} 不一致。",
            },
        )

    async def event_stream():
        try:
            route = route_creator_file_model(
                file_path=request.file_path,
                purpose=request.purpose,
                requested_model=request.model,
            )
            _log_creator_model_usage(
                phase="generate.route",
                skill_name=skill_name,
                file_path=request.file_path,
                route=route,
            )

            effective_skill_plan_entry = request.skill_plan_entry if isinstance(request.skill_plan_entry, dict) else None
            if request.file_path.startswith("scripts/"):
                # 后端生成阶段重新构建 canonical entry，避免依赖前端传来的不完整 entry。
                # 这仍然是单文件合同，不做跨文件 E2E 判断。
                skill_md_for_entry = (
                    (settings.skills_path / skill_name / "SKILL.md").read_text(encoding="utf-8")
                    if (settings.skills_path / skill_name / "SKILL.md").is_file()
                    else request.blueprint_text
                )
                entry_obj = _skill_plan_entry_for_file(
                    file_path=request.file_path,
                    purpose=request.purpose,
                    blueprint_text=skill_md_for_entry or request.blueprint_text,
                    role=request.role,
                    skill_plan_entry=effective_skill_plan_entry,
                )
                effective_skill_plan_entry = dict(getattr(entry_obj, "__dict__", {}) or {})
                effective_skill_plan_entry.setdefault("path", request.file_path)
                effective_skill_plan_entry.setdefault("purpose", request.purpose)

            prompt_messages = _build_generate_file_prompt(
                request.file_path,
                skill_name,
                request.purpose,
                request.blueprint_text,
                request.conversation_history,
                role=request.role,
                skill_plan_entry=effective_skill_plan_entry,
            )
            prompt_variant = "standard"
        except Exception as exc:
            logger.exception("Creator generate_file prepare failed: %s", exc)
            yield _file_done_error_sse(
                file_path=request.file_path,
                role=request.role,
                error=f"生成前准备失败：{exc}",
                error_type="prepare_failed",
            )
            return

        candidate = ""
        repair_counts_by_layer: dict[str, int] = {}
        format_retry_count = 0
        markdown_format_retry_count = 0
        business_repair_count = 0
        repair_failure_signatures: dict[str, tuple[int, str]] = {}

        try:
            if request.file_path == "SKILL.md" or request.file_path.startswith("references/") or Path(request.file_path).suffix.lower() in {".md", ".markdown"}:
                candidate = await _generate_markdown_initial_regions(
                    file_path=request.file_path,
                    skill_name=skill_name,
                    purpose=request.purpose,
                    blueprint_text=request.blueprint_text,
                    model=route.model,
                )
            else:
                candidate = await _complete_creator_file_generation(
                    messages=prompt_messages,
                    model=route.model,
                    skill_name=skill_name,
                    file_path=request.file_path,
                    prompt_variant=prompt_variant,
                    retry_index=0,
                )
        except Exception as exc:
            logger.exception("Creator generate_file initial model call failed: %s", exc)
            yield _file_done_error_sse(
                file_path=request.file_path,
                role=request.role,
                error=f"模型调用失败：{exc}",
                error_type="model_call_failed",
            )
            return

        for attempt in range(1, _MAX_FILE_REPAIR_ATTEMPTS + 1):
            try:
                if len(candidate or "") == 0:
                    raise FileGenerationStageError(
                        source="model_empty_content",
                        layer="file_generation",
                        detail="model_empty_content: 模型生成结果 content_chars=0，跳过 validator/repair；进入 prompt 降级重试。",
                    )

                candidate = _canonicalize_generated_candidate(
                    file_path=request.file_path,
                    content=candidate,
                    role=request.role,
                    skill_plan_entry=effective_skill_plan_entry,
                    skill_name=skill_name,
                    purpose=request.purpose,
                )

                content = candidate

                format_stage_error = _first_round_format_stage_error(
                    file_path=request.file_path,
                    content=content,
                    role=request.role,
                    skill_plan_entry=effective_skill_plan_entry,
                )
                if format_stage_error is not None:
                    raise format_stage_error

                try:

                    if request.file_path == "SKILL.md":
                        _raise_file_contract_failures(validate_file_contract(
                            file_path=request.file_path,
                            content=content,
                            blueprint_text=request.blueprint_text,
                            skill_plan_entry=effective_skill_plan_entry,
                        ))

                        _validate_skill_md_against_existing_files(
                            skill_name,
                            content,
                            blueprint_text=request.blueprint_text,
                            require_existing=False,
                        )

                        await _validate_skill_md_blueprint_alignment(
                            skill_name=skill_name,
                            content=content,
                            blueprint_text=request.blueprint_text,
                            skill_plan_entry=effective_skill_plan_entry,
                            model=request.model or route.model,
                        )

                    elif request.file_path.startswith("references/"):
                        reference_entry = {
                            **(effective_skill_plan_entry or {}),
                            "purpose": request.purpose or request.blueprint_text,
                        }
                        reference_results = validate_file_contract(
                            file_path=request.file_path,
                            content=content,
                            blueprint_text=request.blueprint_text,
                            skill_plan_entry=reference_entry,
                        )
                        if any((not result.passed and result.id == "reference.no_placeholder_phrases") for result in reference_results):
                            patched_reference = _sanitize_reference_placeholders(content)
                            if patched_reference != content:
                                patched_results = validate_file_contract(
                                    file_path=request.file_path,
                                    content=patched_reference,
                                    blueprint_text=request.blueprint_text,
                                    skill_plan_entry=reference_entry,
                                )
                                if not any(not result.passed for result in patched_results):
                                    content = patched_reference
                                    reference_results = patched_results
                        _raise_file_contract_failures(reference_results)

                    elif request.file_path.startswith("scripts/"):
                        pass

                except Exception as exc:
                    raise _stage_error_from_exception("responsibility_stage", exc, default_layer="responsibility_stage") from exc

                if request.file_path.startswith("scripts/"):
                    try:
                        skill_md = (
                            (settings.skills_path / skill_name / "SKILL.md").read_text(encoding="utf-8")
                            if (settings.skills_path / skill_name / "SKILL.md").is_file()
                            else ""
                        )

                        entry = _skill_plan_entry_for_file(
                            file_path=request.file_path,
                            blueprint_text=skill_md,
                            role=request.role,
                            skill_plan_entry=effective_skill_plan_entry,
                        )

                        entry_requirements = []
                        persisted_graph = _load_persisted_requirement_graph(skill_name)
                        if persisted_graph is not None:
                            entry_requirements = [req for req in persisted_graph.requirements if req.target_file == request.file_path]
                        if not entry_requirements and isinstance(effective_skill_plan_entry, dict):
                            entry_requirements = effective_skill_plan_entry.get("requirements") or []
                        workflow_allocation_summary = _load_workflow_allocation_summary(skill_name)
                        workflow_responsibility_edges = _load_workflow_allocation_edges(skill_name)
                        responsibility_review = await _run_script_responsibility_review(
                            file_path=request.file_path,
                            script_content=content,
                            skill_plan_entry=entry,
                            requirements=entry_requirements,
                            deterministic_issues=[],
                            requested_model=request.model or route.model,
                            review_context={
                                "phase": "RESPONSIBILITY_STAGE",
                                "policy": "只判断当前文件职责是否完成。",
                                "blueprint_text": request.blueprint_text,
                                "purpose_short_contract": getattr(entry, "purpose", request.purpose),
                                "workflow_allocation_summary": workflow_allocation_summary,
                                "responsibility_edges": workflow_responsibility_edges,
                                "trial_stdout": "第一轮责任审查在局部 patch 前可能尚未执行试运行；如为空，不得把缺 stdout 当接口失败。",
                                "artifact_info": "第一轮责任审查只用 artifact 信息辅助判断语义交付；真实存在性由运行/E2E 检查。",
                            },
                        )

                        if not responsibility_review.get("passed"):
                            failure_type = str(responsibility_review.get("failure_type") or "script_requirement_failed")
                            if failure_type in {"script_requirement_validator_error", "script_requirement_validator_incomplete"}:
                                raise FileGenerationStageError(
                                    source=failure_type,
                                    layer=failure_type,
                                    detail=json.dumps(responsibility_review, ensure_ascii=False, default=str),
                                )
                            issues = (
                                responsibility_review.get("issues")
                                if isinstance(responsibility_review.get("issues"), list)
                                else []
                            )
                            raise ScriptFunctionalValidationError(
                                issues or [{
                                    "id": "script_responsibility.failed",
                                    "failed_file": request.file_path,
                                    "failed_function": "current script",
                                    "code_region": "current file responsibility logic",
                                    "reason": "职责审查模型判定当前脚本没有完成自身职责。",
                                    "minimal_edit": str(
                                        responsibility_review.get("repair_instructions")
                                        or "只修改当前脚本中未完成职责的业务逻辑。"
                                    ),
                                    "allowed_scope": "只允许修改当前脚本职责实现区域。",
                                    "repair_boundary": "当前文件职责实现区域。",
                                    "details": {"review": responsibility_review},
                                }],
                                layer="responsibility",
                            )

                    except ScriptFunctionalValidationError as exc:
                        raise _stage_error_for_script_functional(exc) from exc
                    except Exception as exc:
                        raise _stage_error_from_exception(
                            "script_responsibility",
                            exc,
                            default_layer="responsibility",
                        ) from exc

                logger.info(
                    "[Creator][generate_file] validation passed file=%s role=%s content_chars=%d",
                    request.file_path,
                    request.role or "",
                    len(content),
                )

                yield _sse({
                    "type": "file_content",
                    "status": "success",
                    "success": True,
                    "file_path": request.file_path,
                    "role": request.role,
                    "content": content,
                    "editable": True,
                    "disabled": False,
                })

                yield _sse({
                    "type": "file_done",
                    "status": "success",
                    "success": True,
                    "file_path": request.file_path,
                    "role": request.role,
                    "done": True,
                    "editable": True,
                    "disabled": False,
                })

                return

            except Exception as exc:
                stage_error = (
                    exc
                    if isinstance(exc, FileGenerationStageError)
                    else _stage_error_from_exception("content_review", exc, default_layer="content_review")
                )
                deterministic_error = str(stage_error)
                error_source = stage_error.source
                error_layer = f"{stage_error.source}:{stage_error.layer}"
                if is_generation_format_error(stage_error) and request.file_path.startswith("scripts/"):
                    format_retry_count += 1
                elif is_markdown_hard_format_error(stage_error) and (
                    request.file_path == "SKILL.md"
                    or request.file_path.startswith("references/")
                    or Path(request.file_path).suffix.lower() in {".md", ".markdown"}
                ):
                    markdown_format_retry_count += 1
                else:
                    business_repair_count += 1
                    repair_counts_by_layer[error_layer] = repair_counts_by_layer.get(error_layer, 0) + 1
                failure_signature = _structured_failure_signature(stage_error, deterministic_error)
                candidate_digest = hashlib.sha256((candidate or "").encode("utf-8")).hexdigest()
                signature_key = f"{error_layer}:{failure_signature}"
                previous_repeat_count, previous_digest = repair_failure_signatures.get(signature_key, (0, ""))
                repeated_same_failure = previous_repeat_count >= 1
                repeated_same_candidate = previous_digest == candidate_digest
                repair_failure_signatures[signature_key] = (previous_repeat_count + 1, candidate_digest)

                if is_script_raw_source_format_error(stage_error) and request.file_path.startswith("scripts/"):
                    layer_limit = _first_round_repair_limit("content_review")
                    if format_retry_count > layer_limit:
                        yield _file_done_error_sse(
                            file_path=request.file_path,
                            role=request.role,
                            error=(
                                f"脚本源码格式重新生成失败：已重新生成 {layer_limit} 次仍未通过。"
                                f"最后错误：{deterministic_error}"
                            ),
                            error_type="script_source_format_regenerate_failed",
                            content=candidate or "",
                            recoverable=True,
                        )
                        return

                    yield _sse({
                        "type": "validation",
                        "status": "regenerating",
                        "success": False,
                        "file_path": request.file_path,
                        "role": request.role,
                        "validation": {
                            "status": "regenerating",
                            "attempt": format_retry_count,
                            "format_retry_count": format_retry_count,
                            "business_repair_count": business_repair_count,
                            "source": error_source,
                            "layer": stage_error.layer,
                            "error": deterministic_error,
                        },
                    })

                    next_messages = _build_strict_script_source_only_regeneration_prompt(
                        file_path=request.file_path,
                        skill_name=skill_name,
                        purpose=request.purpose,
                        blueprint_text=request.blueprint_text,
                        role=request.role,
                        skill_plan_entry=effective_skill_plan_entry,
                        deterministic_error=deterministic_error,
                        previous_content=candidate or "",
                    )
                    candidate = await _complete_creator_file_generation(
                        messages=next_messages,
                        model=route.model,
                        skill_name=skill_name,
                        file_path=request.file_path,
                        prompt_variant="strict_source_only_regeneration",
                        retry_index=format_retry_count - 1,
                    )
                    prompt_messages = next_messages
                    prompt_variant = "strict_source_only_regeneration"
                    continue

                if error_source == "model_empty_content":
                    empty_retry_index = repair_counts_by_layer[error_layer]
                    if empty_retry_index >= len(_EMPTY_GENERATION_PROMPT_VARIANTS):
                        prompt_messages = _ensure_user_visible_task_message(prompt_messages)
                        logger.warning(
                            "[Creator][generate_file][model_empty_content] skill=%s file_path=%s model=%s prompt_variant=%s retry_index=%d prompt_chars=%d message_roles=%s system_chars=%d user_chars=%d content_len=%d finish_reason=%s variants=%s error_type=%s",
                            skill_name,
                            request.file_path,
                            route.model,
                            prompt_variant,
                            empty_retry_index,
                            _prompt_chars(prompt_messages),
                            json.dumps(_message_role_counts(prompt_messages), ensure_ascii=False, sort_keys=True),
                            _message_role_chars(prompt_messages, "system"),
                            _message_role_chars(prompt_messages, "user"),
                            len(candidate or ""),
                            "unknown",
                            "->".join(_EMPTY_GENERATION_PROMPT_VARIANTS),
                            "model_empty_content",
                        )
                        yield _file_done_error_sse(
                            file_path=request.file_path,
                            role=request.role,
                            error="文件内容生成失败：same-model prompt degradation 已尝试 standard -> simplified -> minimal 后仍为空。",
                            error_type="model_empty_content",
                            content=candidate or "",
                            recoverable=True,
                        )
                        return

                    next_variant = _EMPTY_GENERATION_PROMPT_VARIANTS[empty_retry_index]
                    next_messages = (
                        _build_script_generate_file_prompt_variant(
                            file_path=request.file_path,
                            skill_name=skill_name,
                            purpose=request.purpose,
                            blueprint_text=request.blueprint_text,
                            role=request.role,
                            skill_plan_entry=effective_skill_plan_entry,
                            variant=next_variant,
                        )
                        if request.file_path.startswith("scripts/")
                        else prompt_messages
                    )

                    yield _sse({
                        "type": "validation",
                        "status": "regenerating",
                        "success": False,
                        "file_path": request.file_path,
                        "role": request.role,
                        "validation": {
                            "status": "regenerating",
                            "attempt": attempt,
                            "source": error_source,
                            "layer": stage_error.layer,
                            "error": deterministic_error,
                        },
                    })

                    candidate = await _complete_creator_file_generation(
                        messages=next_messages,
                        model=route.model,
                        skill_name=skill_name,
                        file_path=request.file_path,
                        prompt_variant=next_variant,
                        retry_index=empty_retry_index,
                    )
                    prompt_messages = next_messages
                    prompt_variant = next_variant
                    continue
                if error_source in {"script_requirement_validator_error", "script_requirement_validator_incomplete"}:
                    static_blockers: list[dict[str, Any]] = []
                    if request.file_path.startswith("scripts/"):
                        try:
                            static_skill_md = (
                                (settings.skills_path / skill_name / "SKILL.md").read_text(encoding="utf-8")
                                if (settings.skills_path / skill_name / "SKILL.md").is_file()
                                else ""
                            )
                            static_entry = _skill_plan_entry_for_file(
                                file_path=request.file_path,
                                blueprint_text=static_skill_md,
                                role=request.role,
                                skill_plan_entry=effective_skill_plan_entry,
                            )
                            static_requirements: list[Any] = []
                            static_graph = _load_persisted_requirement_graph(skill_name)
                            if static_graph is not None:
                                static_requirements = [req for req in static_graph.requirements if req.target_file == request.file_path]
                            if not static_requirements and isinstance(effective_skill_plan_entry, dict):
                                static_requirements = effective_skill_plan_entry.get("requirements") or []
                            static_blockers = _runtime_tool_contract_static_blockers(
                                candidate or "",
                                static_entry,
                                static_requirements,
                            )
                            static_blockers += _detect_script_responsibility_static_blockers(
                                candidate or "",
                                static_entry,
                                static_requirements,
                            )
                        except Exception:
                            static_blockers = []
                    if not static_blockers:
                        # Single-file production validation failures must still enter
                        # the patch repair loop.  A validator error/incomplete review
                        # is not a reason to return a terminal error to the frontend;
                        # repair should make the current script's responsibility path
                        # explicit enough for the next validation round.
                        static_blockers = [{
                            "id": error_source,
                            "failed_file": request.file_path,
                            "failed_function": "single_file_production_validation",
                            "code_region": "current file responsibility implementation",
                            "reason": (
                                "Single-file production validator failed or returned incomplete checks; "
                                "auto-repair the current file instead of returning directly to the frontend."
                            ),
                            "missing_evidence": [
                                "validator-readable current-file responsibility evidence",
                                "input/tool result participates in constructed output",
                            ],
                            "minimal_edit": (
                                "只修改当前文件职责实现区域，让职责证据更明确。"
                            ),
                            "allowed_scope": "current file responsibility implementation",
                            "details": {"validator_error": deterministic_error},
                        }]
                    stage_error = FileGenerationStageError(
                        source="script_requirement_failed",
                        layer="responsibility",
                        detail=json.dumps({"issues": static_blockers}, ensure_ascii=False, default=str),
                        original=ScriptFunctionalValidationError(static_blockers, layer="responsibility"),
                    )
                    deterministic_error = str(stage_error)
                    error_source = stage_error.source
                    error_layer = f"{stage_error.source}:{stage_error.layer}"
                    repair_counts_by_layer[error_layer] = repair_counts_by_layer.get(error_layer, 0) + 1

                if is_markdown_hard_format_error(stage_error) and (
                    request.file_path == "SKILL.md"
                    or request.file_path.startswith("references/")
                    or Path(request.file_path).suffix.lower() in {".md", ".markdown"}
                ):
                    layer_limit = _first_round_repair_limit(error_source)

                    if markdown_format_retry_count > layer_limit:
                        yield _file_done_error_sse(
                            file_path=request.file_path,
                            role=request.role,
                            error=(
                                f"Markdown 格式修复失败：已区域重写 {layer_limit} 轮仍未通过。"
                                f"最后错误：{deterministic_error}"
                            ),
                            error_type=_markdown_warning_error_type(request.file_path),
                            content=candidate or "",
                            recoverable=True,
                        )
                        return

                    yield _sse({
                        "type": "validation",
                        "status": "format_region_rewrite",
                        "success": False,
                        "file_path": request.file_path,
                        "role": request.role,
                        "editable": True,
                        "disabled": False,
                        "validation": {
                            "status": "format_region_rewrite",
                            "attempt": markdown_format_retry_count,
                            "markdown_format_retry_count": markdown_format_retry_count,
                            "business_repair_count": business_repair_count,
                            "source": error_source,
                            "layer": stage_error.layer,
                            "error": deterministic_error,
                        },
                    })

                    failed_region = markdown_failure_region(deterministic_error)
                    rewrite_messages = _build_markdown_region_rewrite_prompt(
                        file_path=request.file_path,
                        skill_name=skill_name,
                        blueprint_text=request.blueprint_text,
                        deterministic_error=deterministic_error,
                        current_content=candidate or "",
                        region=failed_region,
                    )

                    rewritten_region = await _complete_creator_file_generation(
                        messages=rewrite_messages,
                        model=route.model,
                        skill_name=skill_name,
                        file_path=request.file_path,
                        prompt_variant=f"rewrite_markdown_{failed_region}",
                        retry_index=markdown_format_retry_count - 1,
                    )
                    candidate = _merge_markdown_region_rewrite(candidate or "", rewritten_region, failed_region)
                    continue
                layer_limit = _first_round_repair_limit(error_source)
                if repair_counts_by_layer[error_layer] > layer_limit:
                    yield _file_done_error_sse(
                        file_path=request.file_path,
                        role=request.role,
                        error=(
                            f"文件内容生成失败：同一阶段/层 {error_layer} "
                            f"已修复 {layer_limit} 次仍未通过。最后错误：{deterministic_error}"
                        ),
                        error_type=(
                            _markdown_warning_error_type(request.file_path, default="md_content_repair_warning")
                            if request.file_path == "SKILL.md" or request.file_path.startswith("references/") or Path(request.file_path).suffix.lower() in {".md", ".markdown"}
                            else "repair_layer_limit_exceeded"
                        ),
                        content=candidate or "",
                        recoverable=True,
                    )
                    return

                targeted_repair = _targeted_generated_file_repair_instructions(
                    file_path=request.file_path,
                    deterministic_error=deterministic_error,
                )

                if request.file_path == "SKILL.md":
                    targeted_repair += (
                        "\n\n额外修复目标：SKILL.md 必须与蓝图意图一致。"
                        "不得新增蓝图外能力、脚本、reference 或 asset；"
                        "必须覆盖 required_capabilities；不得包含 forbidden_capabilities；"
                        "assets/** 只能描述为上传/静态素材。"
                    )

                if request.file_path.startswith("references/"):
                    targeted_repair += (
                        "\n\n额外修复目标：references/*.md 必须是一份正式 Markdown 参考资料文档。"
                        "必须包含 YAML frontmatter，且 title/description 非空；"
                        "frontmatter 顶层只允许 title、description、source、license、metadata。"
                        "frontmatter 后必须有 Markdown 正文，正文必须包含标题，并提供可复用参考内容。"
                        "不要输出聊天式澄清问题、确认选项、状态说明或计划询问。"
                    )

                contract_text = _build_generated_file_contract_text(
                    request.file_path,
                    request.blueprint_text,
                    request.purpose,
                    role=request.role,
                    skill_plan_entry=effective_skill_plan_entry,
                )

                passed_checks_text = ""
                failed_checks_text = ""
                original_exc = stage_error.original
                if isinstance(original_exc, ContractValidationError):
                    passed_checks_text = _format_contract_checks(original_exc.results, passed=True)
                    failed_checks_text = _format_contract_checks(original_exc.results, passed=False)

                if attempt >= _MAX_FILE_REPAIR_ATTEMPTS:
                    error_message = (
                        f"文件内容生成失败：已自动修复 {attempt - 1} 次仍未通过。"
                        f"最后错误：{deterministic_error}"
                    )

                    logger.info(
                        "[Creator][generate_file] validation failed finally file=%s role=%s attempts=%d error=%s",
                        request.file_path,
                        request.role or "",
                        attempt,
                        deterministic_error,
                    )

                    yield _file_done_error_sse(
                        file_path=request.file_path,
                        role=request.role,
                        error=error_message,
                        error_type="repair_limit_exceeded",
                        content=candidate or "",
                        recoverable=True,
                    )
                    return

                yield _sse({
                    "type": "validation",
                    "status": "repairing",
                    "success": False,
                    "file_path": request.file_path,
                    "role": request.role,
                    "validation": {
                        "status": "repairing",
                        "attempt": attempt,
                        "source": error_source,
                        "layer": stage_error.layer,
                        "error": deterministic_error,
                    },
                })

                try:
                    repair_mode = "strict_patch" if repeated_same_failure else _repair_mode_for_first_round(
                        source=error_source,
                        file_path=request.file_path,
                        attempt=attempt,
                    )

                    if (
                        error_source in {"script_requirement_failed", "script_functional", "script_responsibility"}
                        and request.file_path.startswith("scripts/")
                    ):
                        responsibility_issues = []
                        original_exc = stage_error.original
                        if isinstance(original_exc, ScriptFunctionalValidationError):
                            responsibility_issues = original_exc.issues
                        feedback = (
                            "RESPONSIBILITY_PATCH_STAGE\n"
                            "只根据 RESPONSIBILITY_STAGE 明确给出的当前文件职责缺失做最小修改。\n\n"
                            "当前文件职责缺失说明：\n"
                            f"{json.dumps(responsibility_issues, ensure_ascii=False, indent=2, default=str)}"
                        )
                        passed_checks_text = ""
                        failed_checks_text = ""
                        contract_text = ""
                        targeted_repair = "只在当前文件内做满足职责缺失的最小功能实现修改。"
                    else:
                        validator_report = await _run_generated_file_validator_round(
                            file_path=request.file_path,
                            content=candidate,
                            deterministic_error=deterministic_error,
                            requested_model=route.model,
                            targeted_repair=targeted_repair,
                            contract_text=contract_text,
                            passed_checks_text=passed_checks_text,
                            failed_checks_text=failed_checks_text,
                            repair_mode=repair_mode,
                        )

                        feedback = _format_file_validator_feedback(
                            deterministic_error,
                            validator_report,
                            targeted_repair=targeted_repair,
                            file_path=request.file_path,
                        )

                    repaired_candidate = await _repair_generated_file_with_feedback(
                        prompt_messages=prompt_messages,
                        model=route.model,
                        file_path=request.file_path,
                        previous_content=candidate,
                        validation_error=feedback,
                        targeted_repair=targeted_repair,
                        contract_text=contract_text,
                        passed_checks_text=passed_checks_text,
                        failed_checks_text=failed_checks_text,
                        repair_mode=repair_mode,
                        skill_plan_entry=effective_skill_plan_entry,
                    )
                    repaired_candidate = _canonicalize_generated_candidate(
                        file_path=request.file_path,
                        content=repaired_candidate,
                        role=request.role,
                        skill_plan_entry=effective_skill_plan_entry,
                        skill_name=skill_name,
                        purpose=request.purpose,
                    )

                except Exception as repair_exc:
                    if (
                        request.file_path == "SKILL.md"
                        or request.file_path.startswith("references/")
                        or Path(request.file_path).suffix.lower() in {".md", ".markdown"}
                    ) and (
                        "hard_format_regression" in str(repair_exc)
                        or "hard_format failure must not enter localized patch repair" in str(repair_exc)
                        or "model_patch_allowed" in str(repair_exc)
                    ):
                        stage_error = FileGenerationStageError(
                            source="hard_format",
                            layer="hard_format",
                            detail=str(repair_exc),
                        )
                        deterministic_error = str(stage_error)
                        markdown_format_retry_count += 1
                        layer_limit = _first_round_repair_limit("hard_format")
                        if markdown_format_retry_count > layer_limit:
                            yield _file_done_error_sse(
                                file_path=request.file_path,
                                role=request.role,
                                error=(
                                    f"Markdown 格式修复失败：已区域重写 {layer_limit} 轮仍未通过。"
                                    f"最后错误：{deterministic_error}"
                                ),
                                error_type=_markdown_warning_error_type(request.file_path),
                                content=candidate or "",
                                recoverable=True,
                            )
                            return
                        yield _sse({
                            "type": "validation",
                            "status": "format_region_rewrite",
                            "success": False,
                            "file_path": request.file_path,
                            "role": request.role,
                            "editable": True,
                            "disabled": False,
                            "validation": {
                                "status": "format_region_rewrite",
                                "attempt": markdown_format_retry_count,
                                "markdown_format_retry_count": markdown_format_retry_count,
                                "business_repair_count": business_repair_count,
                                "source": "hard_format",
                                "layer": "hard_format",
                                "error": deterministic_error,
                            },
                        })
                        failed_region = markdown_failure_region(deterministic_error)
                        rewrite_messages = _build_markdown_region_rewrite_prompt(
                            file_path=request.file_path,
                            skill_name=skill_name,
                            blueprint_text=request.blueprint_text,
                            deterministic_error=deterministic_error,
                            current_content=candidate or "",
                            region=failed_region,
                        )
                        rewritten_region = await _complete_creator_file_generation(
                            messages=rewrite_messages,
                            model=route.model,
                            skill_name=skill_name,
                            file_path=request.file_path,
                            prompt_variant=f"rewrite_markdown_{failed_region}",
                            retry_index=markdown_format_retry_count - 1,
                        )
                        candidate = _merge_markdown_region_rewrite(candidate or "", rewritten_region, failed_region)
                        continue
                    logger.exception(
                        "[Creator][generate_file][repair_failed] file=%s source=%s layer=%s attempt=%d",
                        request.file_path,
                        error_source,
                        stage_error.layer,
                        attempt,
                    )
                    yield _file_done_error_sse(
                        file_path=request.file_path,
                        role=request.role,
                        error=f"文件内容修复阶段异常：{type(repair_exc).__name__}: {repair_exc}",
                        error_type=(
                            _markdown_warning_error_type(request.file_path, default="md_format_warning")
                            if request.file_path.startswith("references/")
                            else "repair_failed"
                        ),
                        content=candidate or "",
                        recoverable=True,
                    )
                    return
                if request.file_path.startswith("scripts/") and repaired_candidate.strip() == (candidate or "").strip():
                    logger.warning(
                        "[Creator][generate_file][repair_noop] file=%s source=%s layer=%s attempt=%d repair_mode=%s",
                        request.file_path,
                        error_source,
                        stage_error.layer,
                        attempt,
                        repair_mode,
                    )

                    if repair_mode == "strict_patch":
                        if error_source in {"script_requirement_validator_error", "script_requirement_validator_incomplete"}:
                            candidate = repaired_candidate
                            continue
                        yield _file_done_error_sse(
                            file_path=request.file_path,
                            role=request.role,
                            error=(
                                "repair_noop_with_same_failure_signature: strict_patch 返回 no-op；"
                                f" failure_signature={failure_signature} error={deterministic_error}"
                            ),
                            error_type="repair_noop_with_same_failure_signature",
                            content=candidate or "",
                            recoverable=True,
                        )
                        return

                    repaired_candidate = await _repair_generated_file_with_feedback(
                        prompt_messages=prompt_messages,
                        model=route.model,
                        file_path=request.file_path,
                        previous_content=candidate,
                        validation_error=(
                            feedback
                            + "\n\n上一轮 repair 没有改变文件内容，这是无效修复。"
                            + "现在必须执行 strict_patch：只修改 deterministic_error / localization 指出的失败行附近代码，"
                            + "必须落实 localization.minimal_edit，不得保留 traceback 指出的错误表达式。"
                        ),
                        targeted_repair=targeted_repair,
                        contract_text=contract_text,
                        passed_checks_text=passed_checks_text,
                        failed_checks_text=failed_checks_text,
                        repair_mode="strict_patch",
                        skill_plan_entry=effective_skill_plan_entry,
                    )
                    repaired_candidate = _canonicalize_generated_candidate(
                        file_path=request.file_path,
                        content=repaired_candidate,
                        role=request.role,
                        skill_plan_entry=effective_skill_plan_entry,
                        skill_name=skill_name,
                        purpose=request.purpose,
                    )

                candidate = repaired_candidate

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )

@router.post("/write-file", response_model=WriteFileResponse)
async def write_file(request: WriteFileRequest):
    """Write already-validated generated content to disk.

    /write-file 只落盘：
    - 不做 content contract 校验；
    - 不做 script responsibility review；
    - 不做 script smoke trial run；
    - 不做 SKILL.md 蓝图一致性或跨文件检查；
    - 不重新 canonicalize，避免前端展示内容与落盘内容不一致。

    所有生成内容是否合格，必须在 /generate-file 的生成循环中解决。
    """
    skill_name = _validate_skill_name(request.skill_name)
    _validate_file_path(request.file_path)

    if request.file_path.startswith("assets/") and (request.skill_plan_entry or {}).get("asset_source") != "bundled":
        raise HTTPException(
            status_code=400,
            detail=f"{request.file_path} 属于 assets 静态素材目录；只有 source=bundled 的预置静态资源可写入，source=user_upload 必须通过 /api/creator/upload-asset 上传。",
        )

    skill_dir = settings.skills_path / skill_name
    if not skill_dir.exists():
        raise HTTPException(status_code=404, detail=f"Skill 不存在：{skill_name}")

    content = request.content or ""

    if request.file_path == "SKILL.md":
        format_failures = _basic_markdown_format_failures(
            "SKILL.md",
            content,
            require_frontmatter=True,
        )
        if format_failures:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "markdown_format",
                    "message": "SKILL.md frontmatter/body 结构校验失败，已阻止写入。",
                    "failed_checks": format_failures,
                },
            )

    target_path = skill_dir / request.file_path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(content, encoding="utf-8")

    return WriteFileResponse(
        success=True,
        path=str(target_path),
        bytes=len(content.encode("utf-8")),
        message=f"已写入：{request.file_path}",
    )

def _validate_skill_package_smoke(
    skill_name: str,
    *,
    mode: str = "trial",
    external_context: dict[str, Any] | None = None,
    requested_model: str | None = None,
) -> list[str]:
    """Strict end-to-end workflow validation."""
    return validate_workflow_e2e(
        skill_name,
        external_context=external_context,
        requested_model=requested_model,
    )

def _external_context_from_skill_action_request(request: SkillActionRequest) -> dict[str, Any]:
    """Build external context for Creator E2E.

    validate-skill / package-skill 经常不是从真实用户运行入口触发，
    request.messages 可能为空。如果不补一个非空通用输入，
    SKILL.md 中 {{user_request}} 会被渲染成空字符串，进而让
    argument-effect review 误判脚本没有消费参数。

    这里补的是平台外部输入 envelope，不是业务字段名：
    - user_request
    - input
    - text
    - payload

    不补 theme/topic/story_text 这类业务字段。
    """
    context = build_creator_external_input_context(
        messages=request.messages,
        input_files=request.input_files,
        fields=request.fields,
        options=request.options,
    )

    if not isinstance(context, dict):
        context = {}

    # 优先从真实 request.messages 取最后一条用户文本。
    user_text = ""
    for message in reversed(request.messages or []):
        if not isinstance(message, dict):
            continue
        if message.get("role") != "user":
            continue
        value = str(message.get("content") or "").strip()
        if value:
            user_text = value
            break

    # 如果没有真实用户文本，使用通用 E2E 测试输入。
    # 注意：这是平台外部 envelope 的测试值，不是业务字段名硬编码。
    fallback_text = (
        user_text
        or "Creator E2E 验证输入：请根据这个请求完成当前 Skill 的主要任务，"
           "内容包含中文、English words 和标点，用于验证参数传递、脚本消费和输出闭环。"
    )

    # 如果 build_creator_external_input_context 已经给了非空值，则保留。
    for key in ("user_request", "input", "text", "payload"):
        if not _json_value_non_empty(context.get(key)):
            context[key] = fallback_text

    if not isinstance(context.get("fields"), dict):
        context["fields"] = dict(request.fields or {})

    if not isinstance(context.get("options"), dict):
        context["options"] = dict(request.options or {})

    if not isinstance(context.get("input_files"), list):
        context["input_files"] = list(request.input_files or [])

    if not _json_value_non_empty(context.get("files")):
        context["files"] = list(context.get("input_files") or [])

    return context


@router.post("/validate-skill", response_model=SkillActionResponse)
async def validate_skill(request: SkillActionRequest):
    """Validate and strictly E2E-run a Skill package.

    Flow:
    1. basic SKILL.md validation
    2. strict SKILL.md workflow E2E execution
    3. if failed, route feedback to MD/code model and rewrite the failing file
    4. retry until success or max attempts exhausted
    """
    skill_name = _validate_skill_name(request.skill_name)

    result = run_action({"action": "validate", "name": skill_name})
    if not result["success"]:
        return SkillActionResponse(
            success=False,
            path=result.get("path"),
            message=result["message"],
        )

    max_attempts = max(0, min(int(request.max_e2e_repair_attempts or 0), 10))
    attempt = 0
    attempts_by_target: dict[str, int] = {}
    completed_targets: set[str] = set()
    repair_logs: list[str] = []
    repair_events: list[dict[str, Any]] = []
    e2e_session = _create_e2e_session(skill_name, source_skill_dir=settings.skills_path / skill_name)

    while True:
        external_context = _external_context_from_skill_action_request(request)
        try:
            e2e_errors = validate_workflow_e2e(
                skill_name,
                external_context=external_context,
                requested_model=request.model,
                e2e_session=e2e_session,
            )
        except Exception as exc:
            logger.exception("validate-skill e2e validator crashed skill=%s", skill_name)
            e2e_errors = [
                _e2e_error(
                    target="SKILL.md",
                    layer="e2e_internal_exception",
                    message=(
                        "严格端到端工作流校验内部异常，已按校验失败返回而不是 HTTP 500。\n"
                        f"exception_type={type(exc).__name__}\n"
                        f"exception={exc}"
                    ),
                )
            ]
        if not e2e_errors:
            suffix = ""
            if repair_logs:
                suffix = "\n\n端到端自动修复记录：\n" + "\n".join(repair_logs)
            return SkillActionResponse(
                success=True,
                path=result.get("path"),
                message=result["message"] + "\n严格端到端工作流校验通过：SKILL.md 命令已按顺序真实执行，中间 JSON 边界已流转，最终 stdout 已对齐 sandbox 平台输出协议。" + suffix,
                repair_events=repair_events or e2e_session.events,
            )

        if not request.auto_repair:
            return SkillActionResponse(
                success=False,
                path=None,
                message=(
                    "严格端到端工作流校验失败：\n"
                    + "\n\n".join(e2e_errors)
                    + (
                        "\n\n端到端自动修复记录：\n" + "\n".join(repair_logs)
                        if repair_logs else ""
                    )
                ),
                repair_events=repair_events or e2e_session.events,
            )

        if any(re.search(r"^E2E_LAYER=e2e_requirement_validator_(?:error|incomplete)", err, re.M) for err in e2e_errors):
            return SkillActionResponse(
                success=False,
                path=None,
                message=(
                    "严格端到端 requirement validator 失败；这不是业务文件修复目标，请重试 validator 或切换 validator 模型：\n"
                    + "\n\n".join(e2e_errors)
                ),
                repair_events=repair_events or e2e_session.events,
            )

        target_path = _e2e_repair_target_from_errors(e2e_errors)
        if target_path in completed_targets:
            return SkillActionResponse(
                success=False,
                path=None,
                message=(
                    "严格端到端工作流校验失败：已修复目标出现同目标回归，停止重复修复：\n"
                    + "\n\n".join(e2e_errors)
                    + f"\n\n回归目标：{target_path}"
                    + (
                        "\n\n端到端自动修复记录：\n" + "\n".join(repair_logs)
                        if repair_logs else ""
                    )
                ),
                repair_events=repair_events or e2e_session.events,
            )
        if attempts_by_target.get(target_path, 0) >= max_attempts:
            return SkillActionResponse(
                success=False,
                path=None,
                message=(
                    "严格端到端工作流校验失败，且自动修复达到当前目标最大次数：\n"
                    + "\n\n".join(e2e_errors)
                    + f"\n\n自动修复目标：{target_path}"
                    + f"\n当前目标尝试次数：{attempts_by_target.get(target_path, 0)}/{max_attempts}"
                    + (
                        "\n\n端到端自动修复记录：\n" + "\n".join(repair_logs)
                        if repair_logs else ""
                    )
                ),
                repair_events=repair_events or e2e_session.events,
            )
        try:
            attempts_by_target[target_path] = attempts_by_target.get(target_path, 0) + 1
            repair_result = await _repair_existing_file_for_e2e_failure(
                skill_name=skill_name,
                target_path=target_path,
                e2e_errors=e2e_errors,
                requested_model=request.model,
                external_context=external_context,
                repair_events=repair_events,
                e2e_session=e2e_session,
            )
            attempt += 1
            status = repair_result.get("status")
            repaired_target = repair_result.get("repaired_target") or target_path
            if status == "target_changed":
                completed_targets.add(repaired_target)
                next_target = repair_result.get("next_target")
                repair_logs.append(
                    f"第 {attempt} 轮：{repaired_target} 当前目标错误已消失，失败转移到 {next_target}，继续修复下一个目标"
                )
                continue
            if status == "repaired":
                completed_targets.add(repaired_target)
                repair_logs.append(
                    f"第 {attempt} 轮：根据端到端失败反馈修复 {repaired_target}"
                )
                continue
            if status == "still_failed_same_target":
                repair_logs.append(
                    f"第 {attempt} 轮：{repaired_target} 仍报同目标错误，未完成修复"
                )
                return SkillActionResponse(
                    success=False,
                    path=None,
                    message=(
                        "严格端到端工作流校验失败，且内容补丁修复未完成；文件保持可编辑草稿：\n"
                        + "\n\n".join(e2e_errors)
                        + f"\n\n自动修复目标：{target_path}"
                        + f"\n自动修复反馈：{repair_result.get('last_failure') or 'still_failed_same_target'}"
                        + (
                            "\n\n端到端自动修复记录：\n" + "\n".join(repair_logs)
                            if repair_logs else ""
                        )
                    ),
                    repair_events=repair_events or e2e_session.events,
                    validation_status="needs_repair",
                    error_type="e2e_content_repair_warning",
                    editable=True,
                    disabled=False,
                    recoverable=True,
                )
            raise ValueError(json.dumps(repair_result, ensure_ascii=False, default=str))
        except Exception as exc:
            logger.exception(
                "validate-skill e2e auto repair failed skill=%s target=%s",
                skill_name,
                target_path,
            )
            return SkillActionResponse(
                success=False,
                path=None,
                message=(
                    "严格端到端工作流校验失败，且自动修复未完成：\n"
                    + "\n\n".join(e2e_errors)
                    + f"\n\n自动修复目标：{target_path}"
                    + f"\n自动修复异常：{exc}"
                    + (
                        "\n\n端到端自动修复记录：\n" + "\n".join(repair_logs)
                        if repair_logs else ""
                    )
                ),
                repair_events=repair_events or e2e_session.events,
            )


@router.post("/package-skill", response_model=SkillActionResponse)
async def package_skill(request: PackageSkillRequest):
    """Package a Skill directory into a distributable .skill archive.

    Packaging is intentionally gated by strict E2E validation so the frontend
    or any direct API caller cannot download a package that failed the real
    workflow trial run.

    Final local-resource existence check is performed only at package time:
    - During SKILL.md generation, scripts/references may not exist yet.
    - During packaging, all SKILL.md referenced scripts/references/assets
      must already exist on disk or the package is invalid.
    """
    skill_name = _validate_skill_name(request.skill_name)

    if request.validate_before_package:
        external_context = _external_context_from_skill_action_request(request)
        e2e_errors = _validate_skill_package_smoke(
            skill_name,
            mode="trial",
            external_context=external_context,
            requested_model=request.model,
        )
        if e2e_errors:
            return SkillActionResponse(
                success=False,
                path=None,
                message=(
                    "打包已中止：严格端到端工作流校验未通过。\n"
                    "请先调用 /api/creator/validate-skill 完成自动修复，"
                    "或根据以下错误手动修改后重试：\n"
                    + "\n\n".join(e2e_errors)
                ),
            )

    try:
        _validate_skill_md_final_resource_existence(skill_name)
    except Exception as exc:
        return SkillActionResponse(
            success=False,
            path=None,
            message=(
                "打包已中止：最终资源存在性校验失败。\n"
                "原因：SKILL.md 引用了尚未生成、尚未上传或不存在的本地资源。\n"
                "请确认 scripts/**、references/** 已生成，assets/** 已上传。\n\n"
                f"{exc}"
            ),
        )

    result = run_action({"action": "package", "name": skill_name})
    if not result["success"]:
        return SkillActionResponse(
            success=False,
            path=result.get("path"),
            message=result["message"],
        )

    return SkillActionResponse(
        success=True,
        path=result.get("path"),
        message=result["message"],
    )

@router.post("/init-from-blueprint", response_model=InitFromBlueprintResponse)
async def init_from_blueprint(request: InitFromBlueprintRequest):
    """Initialize Skill directory structure from blueprint file list.

    只创建 Skill 根目录和必要子目录，不再 touch 空文件。

    原因：
    - 文件内容必须由 /generate-file 成功生成后，再由 /write-file 写入；
    - 如果这里预先 touch 文件，前端会看到 0 B 文件，并可能误显示为“已写入”；
    - 这会掩盖模型生成失败或空内容问题。
    """
    skill_name = _validate_skill_name(request.skill_name)
    skill_root = settings.skill_public_dir / skill_name

    try:
        skill_root.mkdir(parents=True, exist_ok=True)

        dirs_created = 0
        seen_dirs: set[Path] = set()

        for file_spec in request.files:
            rel_path = _normalize_skill_path(file_spec.path)
            if not rel_path:
                continue

            target_path = skill_root / rel_path

            if _is_directory_like_skill_path(rel_path):
                dir_path = target_path
            else:
                dir_path = target_path.parent

            if dir_path in seen_dirs:
                continue

            existed = dir_path.exists()
            dir_path.mkdir(parents=True, exist_ok=True)
            seen_dirs.add(dir_path)

            if not existed:
                dirs_created += 1

        return InitFromBlueprintResponse(
            success=True,
            path=str(skill_root),
            files_created=0,
            message=(
                f"已初始化 Skill 目录结构，创建目录 {dirs_created} 个。"
                "文件将在 generate-file 成功返回非空内容后写入，不再预创建 0 B 空文件。"
            ),
        )

    except Exception as exc:
        logger.exception("init-from-blueprint error")
        return InitFromBlueprintResponse(
            success=False,
            path=None,
            files_created=0,
            message=f"初始化失败：{exc}",
        )

@router.post("/list-files", response_model=ListFilesResponse)
async def list_files(request: ListFilesRequest):
    """List all files in a Skill directory.
    
    Returns the actual file structure on disk, useful for displaying
    to the user after initializing the Skill directory structure.
    """
    skill_name = _validate_skill_name(request.skill_name)
    
    skill_root = settings.skill_public_dir / skill_name
    if not skill_root.exists():
        return ListFilesResponse(
            success=False,
            files=[],
            message=f"Skill '{skill_name}' 不存在",
        )
    
    files: list[FileInfo] = []
    
    def scan_dir(base: Path, rel_path: Path = Path("")):
        for entry in sorted(base.iterdir()):
            entry_rel = rel_path / entry.name
            if entry.is_dir():
                files.append(FileInfo(
                    path=str(entry_rel),
                    is_directory=True,
                ))
                scan_dir(entry, entry_rel)
            else:
                files.append(FileInfo(
                    path=str(entry_rel),
                    is_directory=False,
                    size=entry.stat().st_size,
                ))
    
    scan_dir(skill_root)
    
    return ListFilesResponse(
        success=True,
        files=files,
        message=f"已列出 {len(files)} 个文件",
    )

__all__ = [name for name in globals() if not name.startswith("__")]
