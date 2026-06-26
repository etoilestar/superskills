"""Creator FastAPI endpoint handlers and response assembly."""

from .common import *  # noqa: F403
from .contracts import *  # noqa: F403
from .e2e import *  # noqa: F403
from .repair import *  # noqa: F403
from .generation import *  # noqa: F403

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

    warnings = []
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
                "8. 不得固定套用 payload/user_request/fields/options/input_files 等模板字段。\n"
                "9. 如果脚本需要主题参数，就传主题参数；如果脚本需要图片路径，就传图片路径；如果脚本需要前序输出，就使用对应前序输出 placeholder。\n"
                "10. 动态 placeholder 必须作为 JSON 字符串值出现；不要把未加引号的动态 placeholder 放进 JSON。\n"
                "11. 禁止在 ```bash block 中直接放 JSON 配置对象。\n"
                "12. 禁止在 ```bash block 中放 runner/script/argv 伪命令对象。\n"
                "13. 禁止在 ```bash block 中放说明文字、列表、多条命令或 `<真实参数>` 占位说明。\n"
                "14. 默认不要使用 --argv CLI flag，除非脚本源码明确实现了 --argv；Creator 默认脚本协议是 sys.argv[1] JSON object。\n"
                "15. references/*.md 只作为参考资料说明，不是执行源。不要把 reference 正文全文复制进 SKILL.md。\n"
                "16. assets/** 只能作为上传素材或静态资源引用，不能描述为 Creator 生成素材。\n"
                "17. 不要包含 Creator 创建流程、确认清单、点击开始创建、系统将自动创建文件等平台创建流程文案。\n"
                "18. 不要声称“已通过 E2E 校验”“可直接投入运行”，SKILL.md 是使用说明，不是校验报告。\n\n"
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
    if source in {"script_functional", "script_responsibility"} and file_path.startswith("scripts/"):
        return "localized_patch"

    if attempt >= 2 and file_path.startswith("scripts/") and _strict_contract_rewrite_allowed(source):
        return "strict_contract_rewrite"

    return "minimal_edit"


def _contract_result_to_failure(result: ContractCheckResult) -> dict[str, Any]:
    return {
        "id": result.id,
        "target": result.target,
        "message": result.message,
        "expected": result.expected,
        "minimal_edit": result.minimal_edit,
        "details": result.details,
        "layer": result.layer or _contract_layer_for_check_id(result.id),
    }


def _exception_to_skill_md_failures(exc: Exception, *, source: str = "skill_md") -> list[dict[str, Any]]:
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


async def _repair_skill_md_model_finalizer(
    *,
    previous_content: str,
    failures: list[dict[str, Any]],
    prompt_messages: list[dict[str, str]],
    model: str,
    skill_name: str,
    attempt: int,
) -> str:
    """Repair SKILL.md by exact_replace patch, not full regeneration."""

    failures_text = json.dumps(failures, ensure_ascii=False, indent=2, default=str)

    validation_error = (
        "SKILL.md 合同/责任/蓝图对齐校验未通过。"
        "本轮只能对上一版 SKILL.md 做局部 patch 修复，不能重新生成完整文件。\n\n"
        "注意：Markdown 基础格式错误不会进入本函数，已经由整文件重写阶段处理。\n\n"
        "失败项 JSON：\n"
        f"{failures_text}"
    )

    targeted_repair = (
        "只修复 failures 指向的 target/layer/minimal_edit 对应区域。"
        "未被 failures 指向的 frontmatter、章节、脚本说明、bash fenced block、资源说明、最终产物说明必须保持。"
        "不得重排整篇文档，不得新增蓝图外脚本、reference、asset 或能力。"
        "如果失败是资源提及问题，只在已有资源说明附近补充缺失路径。"
        "\n\n"
        "如果失败涉及 command_block、fenced block、single_command、signature_parseable、json_argv_object、"
        "命令块、bash block 或脚本调用格式："
        "只修改对应的 ```bash fenced block。"
        "bash block 内必须是一条真实可执行 shell 命令，且能解析出 runner、真实 scripts/*.py 路径、一个 JSON object argv 参数。"
        "JSON argv 的 key/value 由当前脚本接口和 workflow 自洽决定；不要固定套用某组字段名。"
        "禁止在 bash block 中保留 JSON 配置对象、伪命令对象、说明文字、列表或多条命令。"
        "如果当前 block 是 JSON 伪命令，只把该 block 改成等价的真实脚本调用命令，不要重写其它章节。"
        "\n\n"
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
            "SKILL.md 是主 Skill 说明文档，必须保持蓝图意图、真实脚本顺序、资源说明和最终输出说明一致。"
            "所有 scripts/*.py 必须通过真实可执行 bash 命令调用，不能使用 JSON 伪命令块。"
        ),
        passed_checks_text="",
        failed_checks_text=failures_text,
        repair_mode="localized_patch",
        skill_plan_entry=None,
    )


@router.post("/finalize-skill-md")
async def finalize_skill_md(request: FinalizeSkillMdRequest):
    """Finalize SKILL.md with staged repair.

    阶段：
    1. Markdown 格式错误：整文件重写，最多 3 轮；
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
    candidate = ""
    content = ""

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
            format_failures = _basic_markdown_format_failures(
                "SKILL.md",
                content,
                require_frontmatter=True,
            )
            if format_failures:
                failures = format_failures

                if attempt >= 3:
                    break

                rewrite_messages = [
                    {
                        "role": "system",
                        "content": (
                            "你是 SKILL.md Markdown 格式修复器。"
                            "你必须输出完整 SKILL.md 文件内容，不要输出 patch，不要输出 JSON，不要解释。"
                            "本轮只修 Markdown 结构格式，不修业务语义。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Skill 名称：{skill_name}\n\n"
                            "后台 Markdown 格式校验失败项：\n"
                            f"{json.dumps(format_failures, ensure_ascii=False, indent=2, default=str)}\n\n"
                            "要求：\n"
                            "1. 输出完整 SKILL.md 文件内容。\n"
                            "2. 不要用 ``` 包裹整个文件。\n"
                            "3. 只修 YAML frontmatter、frontmatter 收尾 ---、fenced block 成对闭合等 Markdown 格式问题。\n"
                            "4. 保留原有业务语义、脚本说明、资源说明、最终产物说明。\n"
                            "5. 不要新增蓝图外能力、脚本、reference 或 asset。\n"
                            "6. 不要声称已经执行或已经通过 E2E。\n\n"
                            "蓝图上下文：\n"
                            f"{(request.blueprint_text or '')[:8000]}\n\n"
                            "当前 SKILL.md 内容：\n"
                            "<<<CURRENT_FILE\n"
                            f"{content}\n"
                            "CURRENT_FILE\n"
                        ),
                    },
                ]

                candidate = await _complete_creator_file_generation(
                    messages=rewrite_messages,
                    model=route.model,
                    skill_name=skill_name,
                    file_path="SKILL.md",
                    prompt_variant="markdown_format_rewrite",
                    retry_index=attempt - 1,
                )
                continue

            # 阶段 2：平台合同、文件引用等非基础 Markdown 格式问题。
            failures = _skill_md_first_round_failures(
                skill_name=skill_name,
                content=content,
                blueprint_text=request.blueprint_text or "",
            )

            # 阶段 3：格式/合同通过后，再做蓝图责任审查。
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
                    failures = _exception_to_skill_md_failures(exc, source="blueprint_alignment")

            if not failures:
                return {
                    "success": True,
                    "content": content,
                    "repair_attempts": attempt - 1,
                    "validation_status": "passed",
                    "editable": True,
                    "disabled": False,
                }

            if attempt >= _MAX_FILE_REPAIR_ATTEMPTS:
                break

            # 非格式错误继续走原来的局部 diff。
            candidate = await _repair_skill_md_model_finalizer(
                previous_content=content,
                failures=failures,
                prompt_messages=prompt_messages,
                model=route.model,
                skill_name=skill_name,
                attempt=attempt,
            )

        except Exception as exc:
            logger.exception(
                "[Creator][skill_md][finalize_attempt_failed] skill=%s attempt=%s",
                skill_name,
                attempt,
            )
            failures = _exception_to_skill_md_failures(exc, source="skill_md_model_finalize")
            break

    return {
        "success": False,
        "content": content or candidate,
        "repair_attempts": max(0, attempt if "attempt" in locals() else 0),
        "validation_status": "needs_repair",
        "needs_repair": True,
        "editable": True,
        "disabled": False,
        "failures": failures,
        "error": "SKILL.md finalize did not pass after repair attempts.",
    }

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

        try:
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

                try:
                    content = _sanitize_generated_file_content(
                        request.file_path,
                        candidate,
                        role=request.role,
                        skill_plan_entry=effective_skill_plan_entry,
                    )

                    # 阶段 1：Markdown 基础格式错误最早判断。
                    # 这里必须在 canonicalize / ensure_reference_metadata 之前，
                    # 避免 reference 里 frontmatter 未闭合等错误被后续逻辑吞掉。
                    if (
                            request.file_path == "SKILL.md"
                            or request.file_path.startswith("references/")
                            or Path(request.file_path).suffix.lower() in {".md", ".markdown"}
                    ):
                        format_failures = _basic_markdown_format_failures(
                            request.file_path,
                            content,
                            require_frontmatter=(
                                    request.file_path == "SKILL.md"
                                    or request.file_path.startswith("references/")
                            ),
                        )
                        if format_failures:
                            raise FileGenerationStageError(
                                source="markdown_format",
                                layer="format",
                                detail=json.dumps(format_failures, ensure_ascii=False, indent=2, default=str),
                            )

                    content, _metadata_patched = _canonicalize_markdown_frontmatter_for_file(
                        file_path=request.file_path,
                        content=content,
                        skill_name=skill_name,
                        purpose=request.purpose,
                    )

                    if request.file_path.startswith("references/") and Path(request.file_path).suffix.lower() == ".md":
                        content = _ensure_reference_metadata_frontmatter(
                            file_path=request.file_path,
                            content=content,
                            purpose=request.purpose,
                            skill_plan_entry=effective_skill_plan_entry,
                        )

                    if not content.strip():
                        raise FileGenerationStageError(
                            source="content_review",
                            layer="content_empty",
                            detail=f"{request.file_path} 生成内容为空。",
                        )

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
                        _raise_file_contract_failures(validate_file_contract(
                            file_path=request.file_path,
                            content=content,
                            blueprint_text=request.blueprint_text,
                            skill_plan_entry={
                                **(effective_skill_plan_entry or {}),
                                "purpose": request.purpose or request.blueprint_text,
                            },
                        ))

                    elif request.file_path.startswith("scripts/"):
                        _raise_file_contract_failures(_check_script_content_review_contract(
                            request.file_path,
                            content,
                            role=request.role,
                            skill_plan_entry=effective_skill_plan_entry,
                        ))

                except Exception as exc:
                    raise _stage_error_from_exception("content_review", exc, default_layer="content_review") from exc

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

                        responsibility_review = await _run_script_responsibility_review(
                            file_path=request.file_path,
                            script_content=content,
                            skill_plan_entry=entry,
                            deterministic_issues=[],
                            requested_model=request.model or route.model,
                            review_context={
                                "phase": "first_round_no_smoke",
                                "policy": (
                                    "第一轮只判断脚本是否完成自身职责；"
                                    "不检查运行、argv、stdout、artifact、字段名或上下游映射；"
                                    "这些由第二轮 E2E 负责。"
                                ),
                            },
                        )

                        if not responsibility_review.get("passed"):
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
                                    "forbidden_scope": "不得修改 SKILL.md、workflow、字段映射、stdout schema、artifact 或其它脚本。",
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
                repair_counts_by_layer[error_layer] = repair_counts_by_layer.get(error_layer, 0) + 1

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
                if error_source == "markdown_format":
                    layer_limit = _first_round_repair_limit(error_source)

                    if repair_counts_by_layer[error_layer] > layer_limit:
                        yield _file_done_error_sse(
                            file_path=request.file_path,
                            role=request.role,
                            error=(
                                f"Markdown 格式修复失败：已整文件重写 {layer_limit} 轮仍未通过。"
                                f"最后错误：{deterministic_error}"
                            ),
                            error_type="markdown_format_rewrite_failed",
                            content=candidate or "",
                            recoverable=True,
                        )
                        return

                    yield _sse({
                        "type": "validation",
                        "status": "rewriting_format",
                        "success": False,
                        "file_path": request.file_path,
                        "role": request.role,
                        "editable": True,
                        "disabled": False,
                        "validation": {
                            "status": "rewriting_format",
                            "attempt": repair_counts_by_layer[error_layer],
                            "source": error_source,
                            "layer": stage_error.layer,
                            "error": deterministic_error,
                        },
                    })

                    rewrite_messages = [
                        {
                            "role": "system",
                            "content": (
                                "你是 Markdown 文件格式修复器。"
                                "你必须输出完整文件内容，不要输出 patch，不要输出 JSON，不要解释。"
                                "本轮只修 Markdown 结构格式，不修业务语义。"
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                f"文件路径：{request.file_path}\n"
                                f"Skill 名称：{skill_name}\n\n"
                                "后台 Markdown 格式校验失败项：\n"
                                f"{deterministic_error}\n\n"
                                "要求：\n"
                                "1. 输出完整 Markdown 文件内容。\n"
                                "2. 不要用 ``` 包裹整个文件。\n"
                                "3. 只修 YAML frontmatter、frontmatter 收尾 ---、fenced block 成对闭合等 Markdown 格式问题。\n"
                                "4. 保留原业务语义、脚本说明、资源说明、最终产物说明。\n"
                                "5. 不要新增蓝图外能力、脚本、reference、asset。\n"
                                "6. 如果是 reference 文件，必须保留 reference 的正文内容和用途。\n"
                                "7. 不要声称已经执行或已经通过 E2E。\n\n"
                                "蓝图上下文：\n"
                                f"{(request.blueprint_text or '')[:8000]}\n\n"
                                "当前文件内容：\n"
                                "<<<CURRENT_FILE\n"
                                f"{candidate or ''}\n"
                                "CURRENT_FILE\n"
                            ),
                        },
                    ]

                    candidate = await _complete_creator_file_generation(
                        messages=rewrite_messages,
                        model=route.model,
                        skill_name=skill_name,
                        file_path=request.file_path,
                        prompt_variant="markdown_format_rewrite",
                        retry_index=repair_counts_by_layer[error_layer] - 1,
                    )
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
                        error_type="repair_layer_limit_exceeded",
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
                    validator_report = await _run_generated_file_validator_round(
                        file_path=request.file_path,
                        content=candidate,
                        deterministic_error=deterministic_error,
                        requested_model=route.model,
                        targeted_repair=targeted_repair,
                        contract_text=contract_text,
                        passed_checks_text=passed_checks_text,
                        failed_checks_text=failed_checks_text,
                        repair_mode=_repair_mode_for_first_round(
                            source=error_source,
                            file_path=request.file_path,
                            attempt=attempt,
                        ),
                    )

                    feedback = _format_file_validator_feedback(
                        deterministic_error,
                        validator_report,
                        targeted_repair=targeted_repair,
                        file_path=request.file_path,
                    )

                    repair_mode = _repair_mode_for_first_round(
                        source=error_source,
                        file_path=request.file_path,
                        attempt=attempt,
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

                except Exception as repair_exc:
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
                        error_type="repair_failed",
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
    repair_logs: list[str] = []
    repair_events: list[dict[str, Any]] = []

    while True:
        external_context = _external_context_from_skill_action_request(request)
        try:
            e2e_errors = validate_workflow_e2e(
                skill_name,
                external_context=external_context,
                requested_model=request.model,
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
            )

        if not request.auto_repair or attempt >= max_attempts:
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
                repair_events=repair_events,
            )

        target_path = _e2e_repair_target_from_errors(e2e_errors)
        try:
            repaired_target = await _repair_existing_file_for_e2e_failure(
                skill_name=skill_name,
                target_path=target_path,
                e2e_errors=e2e_errors,
                requested_model=request.model,
                external_context=external_context,
                repair_events=repair_events,
            )
            attempt += 1
            repair_logs.append(
                f"第 {attempt} 轮：根据端到端失败反馈修复 {repaired_target}"
            )
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
                repair_events=repair_events,
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
