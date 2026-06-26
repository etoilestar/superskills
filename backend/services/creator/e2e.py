"""E2E workflow validation, script static checks, and trial-run helpers."""

import hashlib
import uuid

from .common import *  # noqa: F403
from .contracts import *  # noqa: F403

@dataclass(frozen=True)
class E2EStepTrace:
    """Creator E2E workflow boundary trace.

    中间步骤只记录边界，不要求平台协议字段：
    - command JSON argv
    - command placeholders
    - real stdout JSON
    - payload.update(stdout_json) 后新增字段

    最后一步才要求 stdout JSON 能被 sandbox 平台消费。
    """

    ordinal: int
    script_path: str
    raw_command: str
    placeholders: list[str] = field(default_factory=list)
    argv_keys: list[str] = field(default_factory=list)
    stdout_keys: list[str] = field(default_factory=list)
    new_keys: list[str] = field(default_factory=list)
    artifact_paths: list[str] = field(default_factory=list)
    argv_shape: dict[str, str] = field(default_factory=dict)
    stdout_shape: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class E2EFailure:
    failed_step_index: int
    target_file: str
    target_region: str
    failed_command: str
    input_payload: dict[str, Any] = field(default_factory=dict)
    rendered_payload: dict[str, Any] = field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""
    return_code: int | None = None
    expected: str = ""
    actual: str = ""
    repair_instruction: str = ""
    layer: str = ""
    artifact_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "failed_step_index": self.failed_step_index,
            "target_file": self.target_file,
            "target_region": self.target_region,
            "failed_command": self.failed_command,
            "input_payload": self.input_payload,
            "rendered_payload": self.rendered_payload,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "return_code": self.return_code,
            "expected": self.expected,
            "actual": self.actual,
            "repair_instruction": self.repair_instruction,
            "layer": self.layer,
            "artifact_paths": self.artifact_paths,
        }


def _format_e2e_failure(failure: E2EFailure) -> str:
    return (
        f"E2E_REPAIR_TARGET={failure.target_file}\n"
        f"E2E_LAYER={failure.layer}\n"
        "E2E_STRUCTURED_FAILURE="
        + json.dumps(failure.to_dict(), ensure_ascii=False, sort_keys=True)
    )


def _validate_skill_md_final_resource_existence(skill_name: str) -> None:
    """Final package-time check: SKILL.md references must exist on disk.

    This is not used during file generation. It should run only after all files
    have been generated/uploaded and before packaging.
    """
    skill_name = _validate_skill_name(skill_name)
    skill_dir = settings.skills_path / skill_name
    skill_md_path = skill_dir / "SKILL.md"

    if not skill_md_path.exists():
        raise ValueError("缺少 SKILL.md，无法进行最终资源存在性校验。")

    content = skill_md_path.read_text(encoding="utf-8")

    _validate_skill_md_against_existing_files(
        skill_name,
        content,
        blueprint_text="",
        require_existing=True,
    )

def _ordered_reference_paths_in_skill_md(skill_md: str) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for match in _SKILL_FILE_PATH_RE.finditer(skill_md or ""):
        path = match.group(1).strip()
        if path.startswith("references/") and path not in seen:
            seen.add(path)
            ordered.append(path)
    return ordered

_E2E_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def _json_shape(value: Any) -> str:
    """Compact runtime shape for E2E trace."""
    if isinstance(value, dict):
        keys = ", ".join(sorted(str(k) for k in value.keys())[:12])
        return f"object({keys})"
    if isinstance(value, list):
        if not value:
            return "list[0]"
        return f"list[{len(value)}]<{_json_shape(value[0])}>"
    if isinstance(value, str):
        return "string(non_empty)" if value else "string(empty)"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if value is None:
        return "null"
    return type(value).__name__


def _json_object_shape(obj: dict[str, Any]) -> dict[str, str]:
    return {str(k): _json_shape(v) for k, v in obj.items()}


def _format_json_shape(obj: dict[str, Any]) -> str:
    if not obj:
        return "{}"
    shape = _json_object_shape(obj)
    return json.dumps(shape, ensure_ascii=False, sort_keys=True)

_SANDBOX_TERMINAL_OUTPUT_KEYS = {
    "text",
    "markdown",
    "image_path",
    "image_paths",
    "pdf_path",
    "docx_path",
    "pptx_path",
    "html_path",
    "file_paths",
    "file_outputs",
}


def _e2e_trace_line(trace: E2EStepTrace) -> str:
    return (
        f"step={trace.ordinal} "
        f"script={trace.script_path} "
        f"placeholders={trace.placeholders} "
        f"argv_keys={trace.argv_keys} "
        f"stdout_keys={trace.stdout_keys} "
        f"new_keys={trace.new_keys} "
        f"artifact_paths={trace.artifact_paths} "
        f"argv_shape={json.dumps(trace.argv_shape, ensure_ascii=False, sort_keys=True)} "
        f"stdout_shape={json.dumps(trace.stdout_shape, ensure_ascii=False, sort_keys=True)}"
    )


def _format_e2e_trace(traces: list[E2EStepTrace]) -> str:
    if not traces:
        return "（暂无成功步骤）"
    return "\n".join(_e2e_trace_line(trace) for trace in traces)


def _terminal_output_expected_type(key: str) -> str:
    if key in {"text", "markdown", "image_path", "pdf_path", "docx_path", "pptx_path", "html_path"}:
        return "non-empty string"
    if key in {"image_paths", "file_paths", "file_outputs"}:
        return "non-empty list[string]"
    return "platform terminal field"


def _valid_terminal_output_value(key: str, value: Any) -> bool:
    if key in {"text", "markdown", "image_path", "pdf_path", "docx_path", "pptx_path", "html_path"}:
        return isinstance(value, str) and bool(value.strip())
    if key in {"image_paths", "file_paths", "file_outputs"}:
        return (
            isinstance(value, list)
            and bool(value)
            and all(isinstance(item, str) and item.strip() for item in value)
        )
    return False


def _invalid_terminal_output_values(payload: dict[str, Any]) -> list[dict[str, str]]:
    invalid: list[dict[str, str]] = []
    for key in sorted(_SANDBOX_TERMINAL_OUTPUT_KEYS):
        if key not in payload:
            continue
        value = payload.get(key)
        if _valid_terminal_output_value(key, value):
            continue
        invalid.append({
            "key": key,
            "expected_type": _terminal_output_expected_type(key),
            "actual_type": _json_shape(value),
        })
    return invalid


def _has_sandbox_terminal_output(payload: dict[str, Any]) -> bool:
    """Return whether final stdout JSON is consumable by sandbox runtime.

    这里校验的是平台与 Skill 交互的最终输出协议，不校验中间步骤。
    """
    return any(
        _valid_terminal_output_value(key, payload.get(key))
        for key in _SANDBOX_TERMINAL_OUTPUT_KEYS
        if key in payload
    )

def _validate_final_platform_output_contract(
    *,
    command: E2EWorkflowCommand,
    stdout_json: dict[str, Any],
    traces: list[E2EStepTrace],
) -> None:
    """Validate only the final workflow output against sandbox platform protocol.

    中间步骤 stdout 可以是任意 JSON object；
    最后一步必须输出 sandbox 能展示/下载的标准字段。
    """
    if _has_sandbox_terminal_output(stdout_json):
        return

    invalid_terminal_values = _invalid_terminal_output_values(stdout_json)
    if invalid_terminal_values:
        details = {
            "stdout_shape": _json_object_shape(stdout_json),
            "invalid_terminal_keys": [item["key"] for item in invalid_terminal_values],
            "invalid_terminal_values": invalid_terminal_values,
        }
        raise ValueError(
            _e2e_error(
                target=command.script_path,
                layer="final_platform_output_value_invalid",
                message=(
                    f"第 {command.ordinal} 步 {command.script_path} 是 workflow 最后一步，"
                    "stdout JSON 包含 sandbox 平台字段，但字段值类型/内容不合法。\n"
                    f"stdout_shape={json.dumps(details['stdout_shape'], ensure_ascii=False, sort_keys=True)}\n"
                    f"invalid_terminal_keys={json.dumps(details['invalid_terminal_keys'], ensure_ascii=False)}\n"
                    f"invalid_terminal_values={json.dumps(invalid_terminal_values, ensure_ascii=False, sort_keys=True)}\n"
                    "每个 invalid_terminal_values 项均包含 expected_type 和 actual_type。\n\n"
                    "已成功执行的前序边界 trace：\n"
                    f"{_format_e2e_trace(traces)}"
                ),
            )
        )

    raise ValueError(
        _e2e_error(
            target=command.script_path,
            layer="final_platform_output_contract",
            message=(
                f"第 {command.ordinal} 步 {command.script_path} 是 workflow 最后一步，"
                "但 stdout JSON 没有包含 sandbox 可消费的最终输出字段。\n"
                f"当前 stdout 字段：{sorted(stdout_json.keys())}\n"
                f"stdout_shape={json.dumps(_json_object_shape(stdout_json), ensure_ascii=False, sort_keys=True)}\n"
                f"平台允许的最终输出字段：{sorted(_SANDBOX_TERMINAL_OUTPUT_KEYS)}\n\n"
                "注意：中间步骤可以使用任意内部字段名，不需要对齐平台协议；"
                "但最后一步必须输出平台字段，例如 text、markdown、image_paths、"
                "pdf_path、docx_path、pptx_path、html_path、file_paths 或 file_outputs。\n\n"
                "已成功执行的前序边界 trace：\n"
                f"{_format_e2e_trace(traces)}"
            ),
        )
    )

def _placeholder_exprs_from_value(value: Any) -> list[str]:
    exprs: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for item in v.values():
                walk(item)
        elif isinstance(v, list):
            for item in v:
                walk(item)
        elif isinstance(v, str):
            exprs.extend(match.group(1).strip() for match in _E2E_PLACEHOLDER_RE.finditer(v))

    walk(value)
    return exprs


def _placeholder_root(expr: str) -> str:
    expr = str(expr or "").strip()
    if not expr:
        return ""
    return re.split(r"[.\[]", expr, maxsplit=1)[0].strip()


def _collect_placeholders_from_payload_template(template: dict[str, Any]) -> set[str]:
    placeholders: set[str] = set()
    for value in template.values():
        placeholders.update(_collect_placeholders_from_value(value))
    return placeholders


def _resolve_e2e_payload_expr(
    expr: str,
    *,
    payload: dict[str, Any],
    missing: list[str],
) -> Any:
    """Resolve placeholder expression against current runtime payload.

    Supports:
    - {{text_content}}
    - {{image_paths.0}}
    - {{foo.bar.0}}
    """
    expr = str(expr or "").strip()
    if not expr:
        missing.append(expr)
        return ""

    parts = expr.split(".")
    root = parts[0].strip()

    if root not in payload:
        missing.append(expr)
        return ""

    value: Any = payload[root]

    for part in parts[1:]:
        part = part.strip()
        if isinstance(value, list):
            try:
                index = int(part)
            except ValueError:
                missing.append(expr)
                return ""
            if index < 0 or index >= len(value):
                missing.append(expr)
                return ""
            value = value[index]
            continue

        if isinstance(value, dict):
            if part not in value:
                missing.append(expr)
                return ""
            value = value[part]
            continue

        missing.append(expr)
        return ""

    return value


def _e2e_command_placeholders(command: E2EWorkflowCommand) -> list[str]:
    return _placeholder_exprs_from_value(command.argv_template)

def _collect_placeholders_from_value(value: Any) -> set[str]:
    """Collect root placeholder names from command argv template.

    支持：
    - {{topic}}
    - {{image_paths.0}}
    - {{result.pdf_path}}

    seed 初始输入时只取 root key。
    """
    placeholders: set[str] = set()

    if isinstance(value, str):
        for match in _E2E_PLACEHOLDER_RE.finditer(value):
            root = _placeholder_root(match.group(1))
            if root:
                placeholders.add(root)

    elif isinstance(value, dict):
        for item in value.values():
            placeholders.update(_collect_placeholders_from_value(item))

    elif isinstance(value, list):
        for item in value:
            placeholders.update(_collect_placeholders_from_value(item))

    return placeholders


def _collect_placeholders_from_payload_template(template: dict[str, Any]) -> set[str]:
    placeholders: set[str] = set()
    for value in template.values():
        placeholders.update(_collect_placeholders_from_value(value))
    return placeholders


def _render_e2e_template_value(
    value: Any,
    *,
    payload: dict[str, Any],
    missing: list[str],
) -> Any:
    if isinstance(value, str):
        whole = re.fullmatch(r"\{\{\s*([^{}]+?)\s*\}\}", value.strip())
        if whole:
            return _resolve_e2e_payload_expr(
                whole.group(1),
                payload=payload,
                missing=missing,
            )

        def replace_match(match: re.Match[str]) -> str:
            rendered = _resolve_e2e_payload_expr(
                match.group(1),
                payload=payload,
                missing=missing,
            )
            if isinstance(rendered, (dict, list)):
                return json.dumps(rendered, ensure_ascii=False)
            return str(rendered)

        return _E2E_PLACEHOLDER_RE.sub(replace_match, value)

    if isinstance(value, dict):
        return {
            str(key): _render_e2e_template_value(item, payload=payload, missing=missing)
            for key, item in value.items()
        }

    if isinstance(value, list):
        return [
            _render_e2e_template_value(item, payload=payload, missing=missing)
            for item in value
        ]

    return value


def _render_e2e_command_payload(
    command: E2EWorkflowCommand,
    *,
    payload: dict[str, Any],
    traces: list[E2EStepTrace] | None = None,
) -> dict[str, Any]:
    missing: list[str] = []

    rendered = {
        str(key): _render_e2e_template_value(value, payload=payload, missing=missing)
        for key, value in command.argv_template.items()
    }

    if missing:
        unique_missing = sorted(set(missing))
        available = sorted(payload.keys())

        raise ValueError(
            _e2e_error(
                target=command.source_path,
                layer="external_input_missing" if command.ordinal == 1 else "e2e_dataflow_missing",
                message=(
                    ("平台外部输入缺失，第一条命令不能引用无确定来源字段。" if command.ordinal == 1 else "Skill 内部 dataflow 缺失，后续命令只能引用已有 context 或前序 stdout 字段。")
                    + "\n"
                    + f"第 {command.ordinal} 步 {command.script_path} 的命令模板引用了当前 payload 中不存在的字段："
                    f"{', '.join(unique_missing)}。\n"
                    f"当前可用字段：{', '.join(available) or '(无)'}。\n"
                    f"命令来源：{command.source_path}\n"
                    f"原始命令：{command.raw_command}\n\n"
                    "已成功执行的前序边界 trace：\n"
                    f"{_format_e2e_trace(traces or [])}\n\n"
                    "这只表示 SKILL.md 当前失败步骤的命令占位符，"
                    "无法从用户初始输入或前序 stdout JSON 中解析。"
                    "优先局部修复当前失败步骤的 SKILL.md 命令块；"
                    "不要修改已成功 trace 对应的前序步骤。"
                ),
            )
        )

    return rendered


def _seed_initial_e2e_payload(
    commands: list[E2EWorkflowCommand],
    *,
    external_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Seed Creator E2E with a non-empty generic external input envelope.

    第二轮 E2E 需要真实跑 workflow。若 validate-skill 没传用户消息，
    也必须给 {{user_request}} / {{input}} / {{text}} / {{payload}}
    一个非空通用测试值，否则第一步会收到空字符串，导致参数接入审查误判。

    这里仍然不发明业务字段，只补平台通用外部输入 envelope。
    """
    base_context = external_context
    if not isinstance(base_context, dict):
        base_context = build_creator_external_input_context(messages=[])

    payload: dict[str, Any] = dict(base_context or {})

    seed_value = ""
    for key in ("user_request", "input", "text", "payload"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            seed_value = value.strip()
            break

    if not seed_value:
        seed_value = (
            "Creator E2E 验证输入：请根据这个请求完成当前 Skill 的主要任务，"
            "内容包含中文、English words 和标点，用于验证参数传递、脚本消费和输出闭环。"
        )

    for key in ("user_request", "input", "text", "payload"):
        if not _json_value_non_empty(payload.get(key)):
            payload[key] = seed_value

    if not isinstance(payload.get("fields"), dict):
        payload["fields"] = {}

    if not isinstance(payload.get("options"), dict):
        payload["options"] = {}

    if not isinstance(payload.get("input_files"), list):
        payload["input_files"] = []

    if not isinstance(payload.get("files"), list):
        payload["files"] = list(payload.get("input_files") or [])

    return payload


def _e2e_repair_target_from_errors(errors: list[str]) -> str:
    for error in errors:
        match = re.search(r"^E2E_REPAIR_TARGET=([^\n]+)", error)
        if match:
            target = match.group(1).strip()
            if target:
                return target
    return "SKILL.md"


def _copy_skill_dir_for_e2e(
    skill_name: str,
    *,
    source_skill_dir: Path | None = None,
) -> tuple[tempfile.TemporaryDirectory, Path]:
    source_dir = (source_skill_dir or (settings.skills_path / skill_name)).resolve()

    tmp = tempfile.TemporaryDirectory(prefix="creator-e2e-skill-")
    tmp_root = Path(tmp.name)
    trial_skill_dir = tmp_root / skill_name

    shutil.copytree(
        source_dir,
        trial_skill_dir,
        ignore=shutil.ignore_patterns(
            ".venv",
            "__pycache__",
            "*.pyc",
            ".pytest_cache",
        ),
    )

    return tmp, trial_skill_dir




@dataclass
class CreatorE2ESession:
    e2e_session_id: str
    skill_name: str
    workspace_dir: Path
    venv_path: Path
    outputs_dir: Path
    deps_signature: str = ""
    command_plan_signature: str = ""
    current_revision: int = 0
    installed_deps_signature: str = ""
    temp_handle: tempfile.TemporaryDirectory | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    resolved_failures: list[dict[str, Any]] = field(default_factory=list)

    def to_event_base(self) -> dict[str, Any]:
        return {
            "phase": "e2e_repair",
            "e2e_session_id": self.e2e_session_id,
            "skill_name": self.skill_name,
            "workspace_dir": str(self.workspace_dir),
            "venv_path": str(self.venv_path),
            "outputs_dir": str(self.outputs_dir),
            "deps_signature": self.deps_signature,
            "command_plan_signature": self.command_plan_signature,
            "workspace_revision": self.current_revision,
        }


def _stable_json_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _file_sha256(path: Path) -> str:
    if not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _command_plan_signature(commands: list[E2EWorkflowCommand]) -> str:
    return _stable_json_hash([
        {
            "ordinal": command.ordinal,
            "script_path": command.script_path,
            "runner": command.runner,
            "argv_template": command.argv_template,
            "raw_command": command.raw_command,
        }
        for command in commands
    ])


def _deps_signature_for_commands(skill_dir: Path, skill_md: str, commands: list[E2EWorkflowCommand]) -> str:
    deps: list[str] = []
    for command in commands:
        if not command.script_path.endswith(".py"):
            continue
        try:
            entry = _skill_plan_entry_for_file(file_path=command.script_path, blueprint_text=skill_md)
            deps.extend(str(item) for item in (entry.required_capabilities or []))
            refined_contract, resolution = _contract_resolution_for_trial(command.script_path, skill_md, None, None)
            deps.extend(str(item) for item in (refined_contract.declared_dependencies or []))
            deps.extend(str(item) for item in (resolution.declared_dependencies or []))
        except Exception as exc:
            deps.append(f"unresolved:{command.script_path}:{type(exc).__name__}:{exc}")
    return _stable_json_hash(sorted(set(deps)))


def _create_e2e_session(skill_name: str, *, source_skill_dir: Path | None = None) -> CreatorE2ESession:
    skill_name = _validate_skill_name(skill_name)
    tmp = tempfile.TemporaryDirectory(prefix="creator-e2e-session-")
    root = Path(tmp.name)
    source_dir = (source_skill_dir or (settings.skills_path / skill_name)).resolve()
    workspace_dir = root / skill_name
    shutil.copytree(
        source_dir,
        workspace_dir,
        ignore=shutil.ignore_patterns(".venv", "__pycache__", "*.pyc", ".pytest_cache"),
    )
    outputs_dir = workspace_dir / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    session = CreatorE2ESession(
        e2e_session_id=f"e2e-{uuid.uuid4().hex[:12]}",
        skill_name=skill_name,
        workspace_dir=workspace_dir,
        venv_path=workspace_dir / ".venv",
        outputs_dir=outputs_dir,
        temp_handle=tmp,
    )
    session.events.append({**session.to_event_base(), "event": "session_created", "reused_venv": False})
    return session


def _checkpoint_dir(session: CreatorE2ESession) -> Path:
    path = session.workspace_dir / ".creator_e2e" / "checkpoints"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _checkpoint_path(session: CreatorE2ESession, step_index: int) -> Path:
    return _checkpoint_dir(session) / f"step_{step_index:03d}.json"


def _shape_for_hash(obj: dict[str, Any]) -> dict[str, str]:
    return _json_object_shape(obj if isinstance(obj, dict) else {})


def _write_step_checkpoint(
    session: CreatorE2ESession,
    *,
    command: E2EWorkflowCommand,
    rendered_payload: dict[str, Any],
    stdout_json: dict[str, Any],
    context_before: dict[str, Any],
    context_after: dict[str, Any],
    new_keys: list[str],
    artifact_paths: list[str],
    proc: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    checkpoint = {
        "e2e_session_id": session.e2e_session_id,
        "skill_name": session.skill_name,
        "command_plan_signature": session.command_plan_signature,
        "step_index": command.ordinal,
        "script_path": command.script_path,
        "argv_json": rendered_payload,
        "argv_shape": _shape_for_hash(rendered_payload),
        "stdout_json": stdout_json,
        "stdout_shape": _shape_for_hash(stdout_json),
        "context_before": context_before,
        "context_after": context_after,
        "new_keys": new_keys,
        "artifact_paths": artifact_paths,
        "file_outputs": artifact_paths,
        "exit_code": getattr(proc, "returncode", 0),
        "stderr_excerpt": str(getattr(proc, "stderr", "") or "")[-2000:],
        "stdout_excerpt": str(getattr(proc, "stdout", "") or "")[-2000:],
        "script_hash": _file_sha256(session.workspace_dir / command.script_path),
        "argv_hash": _stable_json_hash(rendered_payload),
        "context_hash": _stable_json_hash(context_before),
        "workspace_revision": session.current_revision,
        "passed": True,
    }
    _checkpoint_path(session, command.ordinal).write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return checkpoint


def _load_valid_checkpoint(session: CreatorE2ESession, step_index: int) -> dict[str, Any] | None:
    path = _checkpoint_path(session, step_index)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if data.get("e2e_session_id") != session.e2e_session_id:
        return None
    if data.get("skill_name") != session.skill_name:
        return None
    if data.get("command_plan_signature") != session.command_plan_signature:
        return None
    script_path = str(data.get("script_path") or "")
    if script_path and data.get("script_hash") != _file_sha256(session.workspace_dir / script_path):
        return None
    if not data.get("passed"):
        return None
    return data


def _invalidate_checkpoints_from(session: CreatorE2ESession, step_index: int) -> list[int]:
    invalidated: list[int] = []
    for path in _checkpoint_dir(session).glob("step_*.json"):
        match = re.search(r"step_(\d+)\.json$", path.name)
        if not match:
            continue
        index = int(match.group(1))
        if index >= step_index:
            invalidated.append(index)
            path.unlink(missing_ok=True)
    return sorted(invalidated)


def _earliest_invalid_step(
    *,
    changed_file: str,
    commands: list[E2EWorkflowCommand],
    old_command_plan_signature: str,
    new_command_plan_signature: str,
) -> int | None:
    changed_file = str(changed_file or "").replace("\\", "/").removeprefix("a/").removeprefix("b/")
    if changed_file == "SKILL.md":
        if old_command_plan_signature != new_command_plan_signature:
            return 1
        return None
    for command in commands:
        if str(command.script_path or "").replace("\\", "/").removeprefix("a/").removeprefix("b/") == changed_file:
            return command.ordinal
    if changed_file.startswith(("references/", "config/", "assets/")):
        return 1
    return None


def _failure_signature_from_error(error: str) -> str:
    structured = _structured_failure_from_errors([error])
    if structured:
        basis = {
            "failure_kind": structured.get("failure_kind") or structured.get("layer"),
            "target_file": structured.get("target_file"),
            "step_index": structured.get("failed_step_index"),
            "return_code": structured.get("return_code"),
            "stderr_hash": hashlib.sha256(str(structured.get("stderr") or "").encode("utf-8")).hexdigest()[:16],
            "layer": structured.get("layer"),
        }
    else:
        basis = {"error_hash": hashlib.sha256(str(error or "").encode("utf-8")).hexdigest()[:16]}
    return _stable_json_hash(basis)


def _e2e_repair_state_from_errors(errors: list[str], *, resolved_failures: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    failed_checks = [error for error in (errors or []) if str(error or "").strip()]
    resolved = list(resolved_failures or [])
    resolved_signatures = {item.get("failure_signature") for item in resolved}
    remaining = [error for error in failed_checks if _failure_signature_from_error(error) not in resolved_signatures]
    return {
        "full_e2e_passed": not remaining,
        "passed_checks": [] if remaining else ["workflow_e2e"],
        "failed_checks": failed_checks,
        "advisory_notes": [],
        "resolved_failures": resolved,
        "remaining_failed_checks": remaining,
        "current_target_file": _e2e_repair_target_from_errors(remaining) if remaining else "none",
        "current_resume_step": None,
    }

def _runner_matches_command_runtime(command: E2EWorkflowCommand, entry: SkillPlanEntry) -> bool:
    runner = Path(command.runner or "").name
    if entry.runtime == "python":
        return runner.startswith("python")
    if entry.runtime == "node":
        return runner == "node"
    if entry.runtime in {"bash", "shell"}:
        return runner in {"bash", "sh"}
    return True

def _run_e2e_step_argument_effect_review(
    *,
    command: E2EWorkflowCommand,
    script_content: str,
    skill_plan_entry: SkillPlanEntry,
    rendered_payload: dict[str, Any],
    stdout_json: dict[str, Any],
    artifact_paths: list[str],
    trace: E2EStepTrace,
    previous_traces: list[E2EStepTrace],
    requested_model: str | None = None,
    requirements: Any = None,
) -> dict[str, Any]:
    """Second-round E2E step interface + argument-effect review.

    第二轮只做接口闭环，不做第一轮责任审查：

    1. SKILL.md 当前 command 是否把当前 step 所需输入/控制参数传进 rendered_payload；
    2. 当前脚本是否真实读取 rendered_payload；
    3. rendered_payload 是否影响 stdout_json 或 artifact_paths；
    4. 如果失败，归因到 SKILL.md 参数映射，或当前脚本参数消费。

    不写业务字段词表，不要求固定字段名。
    """

    rendered_payload = rendered_payload if isinstance(rendered_payload, dict) else {}
    stdout_json = stdout_json if isinstance(stdout_json, dict) else {}
    artifact_paths = artifact_paths if isinstance(artifact_paths, list) else []

    declared_inputs = [
        str(item).strip()
        for item in (getattr(skill_plan_entry, "inputs", []) or [])
        if str(item).strip()
    ]
    declared_outputs = [
        str(item).strip()
        for item in (getattr(skill_plan_entry, "outputs", []) or [])
        if str(item).strip()
    ]

    placeholders = sorted(_e2e_command_placeholders(command))
    argv_template = command.argv_template if isinstance(command.argv_template, dict) else {}

    req_items = coerce_requirement_items(requirements) or coerce_requirement_items(getattr(skill_plan_entry, "requirements", []))
    artifact_contract = getattr(skill_plan_entry, "artifact_contract", {}) or {}
    substantive_step = bool(rendered_payload or argv_template or placeholders or declared_inputs or artifact_contract or any(getattr(r, "required", False) for r in req_items))
    # 只有完全没有 argv、没有 placeholder、没有声明输入、没有 required requirements/产物合同时，才跳过。
    if not substantive_step:
        return {
            "passed": True,
            "model": None,
            "advisory_notes": [
                "当前 step 没有声明输入、没有 argv_template、没有 placeholder，视为 no-input deterministic step，跳过接口审查。"
            ],
        }

    route = route_model(
        VALIDATOR_TASK,
        requested_model=requested_model,
        reason=f"creator e2e step interface/argument review: {command.script_path}",
    )

    local_block_payload = {
        "raw_command": command.raw_command,
        "argv_template": command.argv_template,
        "placeholders": placeholders,
    }

    messages = [
        {
            "role": "system",
            "content": (
                "你是 Creator 第二轮 E2E 当前 step 接口对齐审查模型，只输出严格 JSON object。\n\n"

                "你只判断当前 step 的接口闭环：\n"
                "1. SKILL.md 当前 bash command 是否把当前脚本需要的输入/控制参数传入 rendered_payload；\n"
                "2. 当前脚本是否真实读取 rendered_payload；\n"
                "3. rendered_payload 中的有效信息是否影响 stdout_json 或 artifact_paths。\n\n"

                "重要边界：\n"
                "- 不做第一轮脚本职责审查；脚本功能是否完整由 _run_script_responsibility_review 负责。\n"
                "- 不判断完整 SKILL.md 写得好不好。\n"
                "- 不判断最终产物审美质量。\n"
                "- 不要求固定字段名。\n"
                "- 不允许套用业务字段词表。\n"
                "- 允许脚本通过 payload、input、fields、options、统一对象、别名字段或等价结构接收参数。\n"
                "- 如果字段名不同但语义已传入并被脚本消费，应 passed=true。\n"
                "- 如果 rendered_payload 缺少当前 step 必需信息，failure_kind=missing_payload，target_file=SKILL.md。\n"
                "- 如果 rendered_payload 已传入合理信息，但脚本没有读取或被默认值覆盖，failure_kind=script_not_consuming_payload，target_file=当前脚本。\n"
                "- 如果当前 step 输出了内容，但后续映射接不上，failure_kind=output_mapping_mismatch，target_file=SKILL.md。\n"
                "- 如果没有接口问题，返回 passed=true。\n\n"

                "返回 JSON：\n"
                "{\n"
                "  \"passed\": true|false,\n"
                "  \"target_file\": \"SKILL.md 或 当前脚本路径\",\n"
                "  \"failure_kind\": \"missing_payload|script_not_consuming_payload|output_mapping_mismatch|none\",\n"
                "  \"problem\": \"具体问题\",\n"
                "  \"evidence\": \"从 command/rendered_payload/script/stdout/trace 中引用证据\",\n"
                "  \"repair_instruction\": \"只修改目标文件的最小区域\",\n"
                "  \"advisory_notes\": []\n"
                "}\n"
            ),
        },
        {
            "role": "user",
            "content": (
                f"当前 step：{command.ordinal}\n"
                f"当前脚本：{command.script_path}\n\n"

                "当前 SKILL.md workflow block 局部信息：\n"
                f"{json.dumps(local_block_payload, ensure_ascii=False, default=str)[:8000]}\n\n"

                "当前脚本 SkillPlanEntry：\n"
                f"{json.dumps(skill_plan_entry.__dict__, ensure_ascii=False, default=str)[:8000]}\n\n"

                "declared_inputs（语义参考，不是字段名硬约束）：\n"
                f"{json.dumps(declared_inputs, ensure_ascii=False, default=str)}\n\n"

                "declared_outputs（语义参考，不是字段名硬约束）：\n"
                f"{json.dumps(declared_outputs, ensure_ascii=False, default=str)}\n\n"

                "E2E rendered_payload：\n"
                f"{json.dumps(rendered_payload, ensure_ascii=False, default=str)[:10000]}\n\n"

                "当前 step stdout_json：\n"
                f"{json.dumps(stdout_json, ensure_ascii=False, default=str)[:10000]}\n\n"

                "当前 step artifact_paths：\n"
                f"{json.dumps(artifact_paths, ensure_ascii=False, default=str)}\n\n"

                "当前 step trace：\n"
                f"{_e2e_trace_line(trace)}\n\n"

                "前序成功 trace 摘要：\n"
                f"{_format_e2e_trace(previous_traces)[-5000:]}\n\n"

                "当前脚本源码（带行号）：\n"
                f"{_numbered_source(script_content)[-18000:]}\n\n"

                "审查要求：\n"
                "1. 先判断 SKILL.md 当前 command 是否把当前 step 必要输入/控制参数传进 rendered_payload。\n"
                "2. 再判断脚本是否真实读取并使用 rendered_payload。\n"
                "3. 不要根据固定字段名判断；只看语义是否传入、是否消费、是否影响输出。\n"
                "4. 如果是 SKILL.md 没传对，target_file=SKILL.md。\n"
                "5. 如果是脚本没接住，target_file=当前脚本。\n"
                "6. 如果没有接口闭环问题，passed=true。\n"
            ),
        },
    ]

    last_text = ""
    data: dict[str, Any] | None = None
    for review_attempt in range(2):
        try:
            active_messages = messages if review_attempt == 0 else [
                *messages,
                {
                    "role": "user",
                    "content": (
                        "上一轮 E2E requirement validator 没有返回合法 JSON object。\n"
                        "请只重试 JSON 格式；不要要求修改 SKILL.md 或脚本。\n"
                        f"上一轮输出片段：{last_text[:1200]}"
                    ),
                },
            ]
            text = _complete_chat_once_sync_for_e2e(active_messages, route.model)
            last_text = str(text or "")
        except Exception as exc:
            if review_attempt == 0:
                last_text = f"validator unavailable: {type(exc).__name__}: {exc}"
                continue
            logger.warning(
                "[Creator][E2E][argument_effect_review_unavailable] step=%s script=%s error=%s",
                command.ordinal,
                command.script_path,
                exc,
            )
            return {
                "passed": False,
                "failure_type": "e2e_requirement_validator_error",
                "target_file": command.script_path if rendered_payload else "SKILL.md",
                "layer": "e2e_requirement_validator_error",
                "problem": f"E2E requirement validator unavailable: {type(exc).__name__}: {exc}",
                "evidence": "validator unavailable after format retry; not a business-file failure",
                "repair_instruction": "Retry validator or return recoverable validator failure; do not patch business files solely for this.",
                "model": route.model,
            }

        parsed = _parse_validator_json_object(last_text)
        if isinstance(parsed, dict) and parsed:
            data = parsed
            break
        if review_attempt == 0:
            continue
        logger.warning(
            "[Creator][E2E][argument_effect_review_invalid_json] step=%s script=%s raw=%s",
            command.ordinal,
            command.script_path,
            last_text[:1000],
        )
        return {
            "passed": False,
            "failure_type": "e2e_requirement_validator_error",
            "target_file": command.script_path if rendered_payload else "SKILL.md",
            "layer": "e2e_requirement_validator_error",
            "problem": "E2E requirement validator did not return valid JSON after format retry.",
            "evidence": last_text[:1000],
            "repair_instruction": "Retry validator or return recoverable validator failure; do not patch business files solely for this.",
            "model": route.model,
        }

    data = data or {}

    if data.get("passed") is True:
        return {
            "passed": True,
            "model": route.model,
            "advisory_notes": data.get("advisory_notes") if isinstance(data.get("advisory_notes"), list) else [],
        }

    failure_kind = str(data.get("failure_kind") or "").strip()
    if failure_kind not in {
        "missing_payload",
        "script_not_consuming_payload",
        "output_mapping_mismatch",
        "none",
    }:
        failure_kind = "script_not_consuming_payload"

    target_file = str(data.get("target_file") or "").strip()

    if failure_kind in {"missing_payload", "output_mapping_mismatch"}:
        target_file = "SKILL.md"
    elif failure_kind == "script_not_consuming_payload":
        target_file = command.script_path
    elif target_file not in {"SKILL.md", command.script_path}:
        target_file = command.script_path

    layer = (
        "e2e_step_argument_mapping"
        if target_file == "SKILL.md"
        else "e2e_step_argument_effect"
    )

    problem = str(
        data.get("problem")
        or data.get("reason")
        or data.get("message")
        or "当前 step 接口没有闭环，或 rendered_payload 没有真实影响当前脚本输出。"
    )

    evidence = str(
        data.get("evidence")
        or data.get("details")
        or "模型未提供 evidence。"
    )

    repair_instruction = str(
        data.get("repair_instruction")
        or data.get("minimal_edit")
        or data.get("fix")
        or (
            f"只修改 {target_file} 中当前 E2E step 接口映射/参数消费相关的最小区域，"
            "不要修改其它文件或已通过步骤。"
        )
    )

    return {
        "passed": False,
        "target_file": target_file,
        "layer": layer,
        "failure_kind": failure_kind,
        "problem": problem,
        "evidence": evidence,
        "repair_instruction": repair_instruction,
        "model": route.model,
        "raw_review": data,
    }

def _e2e_requirement_mapping_failure(**kwargs) -> str:
    return _e2e_argument_effect_failure(**kwargs)

def _e2e_requirement_effect_failure(**kwargs) -> str:
    return _e2e_argument_effect_failure(**kwargs)


def _load_requirement_graph_for_e2e(skill_dir: Path) -> RequirementGraph:
    path = skill_dir / ".creator" / "requirement_graph.json"
    if not path.is_file():
        return RequirementGraph()
    try:
        return normalize_requirement_graph(parse_requirement_graph_result(path.read_text(encoding="utf-8")))
    except RequirementGraphValidationError:
        raise
    except Exception as exc:
        raise RequirementGraphValidationError(
            f"E2E requirement graph metadata is unreadable: {type(exc).__name__}: {exc}",
            code="validator_error",
            details={"path": str(path)},
        ) from exc


def _attach_requirements_to_entry(entry: SkillPlanEntry, requirements: list[RequirementItem]) -> SkillPlanEntry:
    try:
        setattr(entry, "requirements", requirements)
    except Exception:
        pass
    return entry


def _constraint_matches_metadata(constraint: RequirementConstraint, metadata: dict[str, Any]) -> bool:
    name = str(constraint.name or "").strip()
    value = constraint.value
    kind = str(constraint.kind or "").strip()
    buckets = []
    for key in ("styles", "options", "constraint_values", "layout_options"):
        if isinstance(metadata.get(key), dict):
            buckets.append(metadata[key])
    for bucket in buckets:
        if name and name in bucket:
            if value in (None, ""):
                return True
            return bucket.get(name) == value or str(bucket.get(name)) == str(value)
        if value not in (None, "") and any(v == value or str(v) == str(value) for v in bucket.values()):
            return True
    if kind in {"media_property", "media"} and metadata.get("media_items"):
        return True
    if kind in {"layout_style", "layout"} and metadata.get("layout_options"):
        return True
    if kind == "quantity":
        for key in ("block_count",):
            if metadata.get(key) == value or str(metadata.get(key)) == str(value):
                return True
    return False


def _structured_requirement_metadata_missing(requirement: RequirementItem, metadata: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    policy = requirement.evidence_policy if isinstance(requirement.evidence_policy, dict) else {}
    metadata_paths = policy.get("metadata_paths") if isinstance(policy.get("metadata_paths"), list) else []
    for dotted in metadata_paths:
        cursor: Any = metadata
        for part in str(dotted).split("."):
            if isinstance(cursor, dict) and part in cursor:
                cursor = cursor[part]
            else:
                missing.append(f"metadata.{dotted}")
                break
    component_types = metadata.get("component_types") if isinstance(metadata.get("component_types"), list) else []
    policy_components = policy.get("component_types") if isinstance(policy.get("component_types"), list) else []
    for component in policy_components:
        if component not in component_types:
            missing.append(f"component_types contains {component}")
    for constraint in requirement.constraints or []:
        if (
            getattr(constraint, "required", True)
            and str(getattr(constraint, "source", "") or "") in {"user_explicit", "blueprint", "inferred"}
            and not _constraint_matches_metadata(constraint, metadata)
        ):
            missing.append(f"constraint:{constraint.name or constraint.kind}")
    return missing

def _run_e2e_requirement_flow_review(
    *,
    command: E2EWorkflowCommand,
    script_content: str,
    skill_plan_entry: SkillPlanEntry,
    rendered_payload: dict[str, Any],
    stdout_json: dict[str, Any],
    artifact_paths: list[str],
    trace: E2EStepTrace,
    previous_traces: list[E2EStepTrace],
    requested_model: str | None = None,
    requirements: Any = None,
) -> dict[str, Any]:
    reqs = coerce_requirement_items(requirements) or coerce_requirement_items(getattr(skill_plan_entry, "requirements", []))
    review = _run_e2e_step_argument_effect_review(
        command=command, script_content=script_content, skill_plan_entry=skill_plan_entry,
        rendered_payload=rendered_payload, stdout_json=stdout_json, artifact_paths=artifact_paths,
        trace=trace, previous_traces=previous_traces, requested_model=requested_model, requirements=reqs,
    )
    if not review.get("passed"):
        return review
    required = [r for r in reqs if getattr(r, "required", False)]
    metadata = stdout_json.get("artifact_metadata") if isinstance(stdout_json.get("artifact_metadata"), dict) else {}
    if required and not rendered_payload and any(r.semantic_inputs for r in required):
        r = next((x for x in required if x.semantic_inputs), required[0])
        return {"passed": False, "target_file": "SKILL.md", "layer": "e2e_requirement_mapping_failed", "failure_kind": "missing_payload", "problem": "Required semantic input was not delivered to the step payload.", "evidence": "rendered_payload is empty while requirement declares semantic_inputs", "requirement_id": r.id, "missing_evidence": ["semantic input in rendered_payload"], "repair_instruction": "Pass the required semantic input from user input or previous stdout into this command."}
    if required and artifact_paths and metadata:
        for r in required:
            missing = _structured_requirement_metadata_missing(r, metadata)
            if missing:
                return {"passed": False, "target_file": command.script_path, "layer": "runtime_metadata_requirement_failed", "failure_kind": "runtime_metadata_missing_evidence", "problem": "Runtime metadata is missing structured evidence for a required requirement.", "evidence": json.dumps(metadata, ensure_ascii=False, default=str)[:1000], "requirement_id": r.id, "missing_evidence": missing, "repair_instruction": "Ensure stdout or runtime helper metadata exposes structured evidence for the required component/constraint."}
    return review

def _e2e_argument_effect_failure(
    *,
    command: E2EWorkflowCommand,
    review: dict[str, Any],
    rendered_payload: dict[str, Any],
    stdout_json: dict[str, Any],
    artifact_paths: list[str],
    traces: list[E2EStepTrace],
) -> str:
    layer = str(review.get("layer") or "e2e_step_argument_effect")
    target_file = "__validator__" if layer in {"e2e_requirement_validator_error", "e2e_requirement_validator_incomplete"} else str(review.get("target_file") or command.script_path)
    problem = str(review.get("problem") or "当前 E2E step 参数有效性审查失败。")
    evidence = str(review.get("evidence") or "")
    repair_instruction = str(
        review.get("repair_instruction")
        or f"只修改 {target_file} 中与当前 step 参数映射/消费相关的最小区域。"
    )

    return _format_e2e_failure(E2EFailure(
        failed_step_index=command.ordinal,
        target_file=target_file,
        target_region="workflow block" if target_file == "SKILL.md" else "run()",
        failed_command=command.raw_command,
        input_payload=rendered_payload,
        rendered_payload=rendered_payload,
        stdout=json.dumps(stdout_json, ensure_ascii=False, default=str)[-4000:],
        stderr=(
            f"{problem}\n\n"
            f"evidence:\n{evidence}\n\n"
            "已成功执行的前序边界 trace：\n"
            f"{_format_e2e_trace(traces)}"
        ),
        return_code=0,
        expected=(
            "最终 SKILL.md 当前 workflow block 传入的核心业务参数，"
            "必须被当前脚本真实消费，并影响当前 step 的 stdout_json 或 artifact。"
        ),
        actual=problem,
        repair_instruction=repair_instruction,
        layer=layer,
        artifact_paths=artifact_paths,
    ))

def _execute_e2e_python_command(
    *,
    command: E2EWorkflowCommand,
    trial_skill_dir: Path,
    rendered_payload: dict[str, Any],
    venv_python: Path,
) -> subprocess.CompletedProcess[str]:
    script_abs = trial_skill_dir / command.script_path
    return subprocess.run(
        [
            str(venv_python),
            str(script_abs),
            json.dumps(rendered_payload, ensure_ascii=False),
        ],
        cwd=str(trial_skill_dir / "scripts"),
        capture_output=True,
        text=True,
        timeout=_SCRIPT_TRIAL_TIMEOUT_SECONDS,
        env={**_build_script_runtime_env(trial_skill_dir), "SKILL_TRIAL_RUN": "1"},
    )


def _execute_e2e_node_command(
    *,
    command: E2EWorkflowCommand,
    trial_skill_dir: Path,
    rendered_payload: dict[str, Any],
) -> subprocess.CompletedProcess[str]:
    script_abs = trial_skill_dir / command.script_path
    return subprocess.run(
        [
            "node",
            str(script_abs),
            json.dumps(rendered_payload, ensure_ascii=False),
        ],
        cwd=str(trial_skill_dir / "scripts"),
        capture_output=True,
        text=True,
        timeout=_SCRIPT_TRIAL_TIMEOUT_SECONDS,
        env={**_build_script_runtime_env(trial_skill_dir), "SKILL_TRIAL_RUN": "1"},
    )


def _execute_e2e_shell_command(
    *,
    command: E2EWorkflowCommand,
    trial_skill_dir: Path,
    rendered_payload: dict[str, Any],
) -> subprocess.CompletedProcess[str]:
    script_abs = trial_skill_dir / command.script_path
    runner = "sh" if Path(command.runner or "").name == "sh" else "bash"
    return subprocess.run(
        [
            runner,
            str(script_abs),
            json.dumps(rendered_payload, ensure_ascii=False),
        ],
        cwd=str(trial_skill_dir / "scripts"),
        capture_output=True,
        text=True,
        timeout=_SCRIPT_TRIAL_TIMEOUT_SECONDS,
        env={**_build_script_runtime_env(trial_skill_dir), "SKILL_TRIAL_RUN": "1"},
    )


def _parse_e2e_stdout_json(
    *,
    command: E2EWorkflowCommand,
    proc: subprocess.CompletedProcess[str],
    trial_skill_dir: Path,
    content: str,
    entry: SkillPlanEntry,
    rendered_payload: dict[str, Any],
    trial_skill_md: str | None = None,
) -> dict[str, Any]:
    """Parse and validate one E2E step stdout.

    This function must not depend on an outer-scope ``trial_skill_md`` variable.
    Older callers may not pass trial_skill_md, so we recover it from the copied
    trial Skill directory when missing.
    """
    if trial_skill_md is None:
        skill_md_path = trial_skill_dir / "SKILL.md"
        trial_skill_md = skill_md_path.read_text(encoding="utf-8") if skill_md_path.is_file() else ""

    if proc.returncode != 0:
        raise ValueError(_format_e2e_failure(E2EFailure(
            failed_step_index=command.ordinal,
            target_file=command.script_path,
            target_region="run()",
            failed_command=command.raw_command,
            input_payload=rendered_payload,
            rendered_payload=rendered_payload,
            stdout=(proc.stdout or "")[-4000:],
            stderr=(proc.stderr or "")[-4000:],
            return_code=proc.returncode,
            expected="脚本必须成功退出、stdout 输出合法 JSON object，并真实完成该步骤职责。",
            actual=f"return_code={proc.returncode}",
            repair_instruction=f"只修改 {command.script_path} 中 run()/main 执行失败相关区域，不修改其它文件或已通过步骤。",
            layer="script_exit",
        )))

    try:
        refined_contract, _resolution = _contract_resolution_for_trial(
            command.script_path,
            trial_skill_md,
            entry.role,
            entry.__dict__,
        )
        _validate_trial_stdout_json(
            stdout=proc.stdout,
            content=content,
            args=[json.dumps(rendered_payload, ensure_ascii=False)],
            role=entry.role,
            skill_dir=trial_skill_dir,
            skill_plan_entry=entry.__dict__,
            canonical_contract=refined_contract,
        )
    except ValueError as exc:
        raise ValueError(_format_e2e_failure(E2EFailure(
            failed_step_index=command.ordinal,
            target_file=command.script_path,
            target_region="stdout output logic",
            failed_command=command.raw_command,
            input_payload=rendered_payload,
            rendered_payload=rendered_payload,
            stdout=(proc.stdout or "")[-4000:],
            stderr=(proc.stderr or "")[-4000:],
            return_code=proc.returncode,
            expected="stdout 必须是合法 JSON object，required outputs 存在；只有真正 artifact/path/file 语义字段才检查文件产物真实存在。",
            actual=f"stdout_contract_error={exc}",
            repair_instruction=(
                f"只修改 {command.script_path} 的 stdout/artifact 输出逻辑，不修改其它文件。"
                "如果失败字段是普通业务 stdout 字段，不要把它改成文件路径；"
                "如果失败字段是 pdf_path/image_path/file_outputs 等产物字段，则确保真实写入文件并返回正确路径。"
            ),
            layer="stdout_contract",
        ))) from exc

    try:
        parsed = json.loads((proc.stdout or "").strip())
    except json.JSONDecodeError as exc:
        raise ValueError(
            _e2e_error(
                target=command.script_path,
                layer="stdout_json_parse",
                message=(
                    f"第 {command.ordinal} 步 {command.script_path} stdout 不是合法 JSON。\n"
                    f"stdout={(proc.stdout or '')[-4000:]}"
                ),
            )
        ) from exc

    if not isinstance(parsed, dict):
        raise ValueError(
            _e2e_error(
                target=command.script_path,
                layer="stdout_json_type",
                message=f"第 {command.ordinal} 步 {command.script_path} stdout 必须是 JSON object。",
            )
        )

    return parsed


def _validate_e2e_script_static_preflight(*, file_path: str, content: str, skill_md: str) -> None:
    """E2E preflight for local safety/entry/JSON argv only.

    This intentionally does not enforce helper_preferred implementation choices
    or SkillPlan input key exactness. The real workflow run validates rendered
    argv, stdout context propagation, and final artifacts.
    """
    entry = _skill_plan_entry_for_file(file_path=file_path, blueprint_text=skill_md)

    if entry.language == "python":
        try:
            ast.parse(content)
        except SyntaxError as exc:
            raise ValueError(f"{file_path} 不是合法 Python 源码: {exc.msg}") from exc

    if not _script_has_main_entry(content, entry.runtime):
        raise ValueError(f"{file_path} 缺少 runtime={entry.runtime} 的入口或 stdout 输出。")

    commands = _extract_script_command_templates(skill_md, file_path)
    json_argv_commands = [command for command in commands if _command_uses_json_argv(command)]
    if json_argv_commands and not _script_reads_json_argv(content, entry.runtime):
        raise ValueError(
            f"{file_path} SKILL.md 命令传入 JSON argv，但脚本未按 runtime 读取 JSON argv（例如 Python json.loads(sys.argv[1])）。"
        )

    helper_required_capabilities = [
        capability
        for capability in _effective_required_capabilities_for_script(entry)
        if (get_tool_capability(capability) and get_tool_capability(capability).usage_policy == "helper_required")
    ]
    missing_required_helpers = _script_required_capability_failures(content, helper_required_capabilities)
    if missing_required_helpers:
        raise ValueError(
            "脚本没有调用这些 helper_required 能力对应接口："
            + ", ".join(missing_required_helpers)
        )


def _validate_e2e_command_static(
    *,
    command: E2EWorkflowCommand,
    trial_skill_dir: Path,
    skill_md: str,
    available_payload_keys: set[str] | None = None,
) -> SkillPlanEntry:
    source_path = trial_skill_dir / command.script_path
    if not source_path.is_file():
        raise ValueError(
            _e2e_error(
                target=command.source_path,
                layer="script_missing",
                message=f"第 {command.ordinal} 步引用的脚本不存在：{command.script_path}",
            )
        )

    entry = _skill_plan_entry_for_file(file_path=command.script_path, blueprint_text=skill_md)
    if not _runner_matches_command_runtime(command, entry):
        raise ValueError(
            _e2e_error(
                target=command.source_path,
                layer="runtime_mismatch",
                message=(
                    f"第 {command.ordinal} 步 {command.script_path} 的命令 runner={command.runner!r} "
                    f"与 SkillPlan.runtime={entry.runtime!r} 不一致。\n"
                    f"原始命令：{command.raw_command}"
                ),
            )
        )


    content = source_path.read_text(encoding="utf-8")
    try:
        _validate_e2e_script_static_preflight(
            file_path=command.script_path,
            content=content,
            skill_md=skill_md,
        )
    except ValueError as exc:
        raise ValueError(
            _e2e_error(
                target=command.script_path,
                layer="script_static_contract",
                message=f"第 {command.ordinal} 步 {command.script_path} 静态合同失败：{exc}",
            )
        ) from exc

    return entry


def _run_skill_workflow_e2e_once(
    skill_name: str,
    *,
    external_context: dict[str, Any] | None = None,
    source_skill_dir: Path | None = None,
    requested_model: str | None = None,
    e2e_session: CreatorE2ESession | None = None,
    resume_from_step: int = 1,
) -> list[str]:
    """Run SKILL.md workflow once.

    第二轮 E2E 职责：
    - 执行最终 SKILL.md 中的 workflow block；
    - 检查上下游 JSON 字段是否串起来；
    - 检查 stdout/artifact/final platform output；
    - 局部检查当前 SKILL.md block 传入参数是否真实影响当前 step 输出。

    不负责：
    - 判断脚本整体业务质量；
    - 判断完整 SKILL.md 写得好不好；
    - 判断 PDF/图片/文本审美质量；
    - 判断其它脚本职责。
    """

    if e2e_session is not None:
        source_skill_dir = e2e_session.workspace_dir.resolve()
    else:
        source_skill_dir = (source_skill_dir or (settings.skills_path / skill_name)).resolve()
    skill_md_path = source_skill_dir / "SKILL.md"

    if not skill_md_path.is_file():
        return [
            _e2e_error(
                target="SKILL.md",
                layer="missing_skill_md",
                message="无法加载 SKILL.md。",
            )
        ]

    skill_md = skill_md_path.read_text(encoding="utf-8")
    errors: list[str] = []

    try:
        _validate_skill_md_contract(skill_md, skill_md)
    except ValueError as exc:
        errors.append(
            _e2e_error(
                target="SKILL.md",
                layer="skill_md_contract",
                message=f"SKILL.md 合同错误：{exc}",
            )
        )
        return errors

    try:
        commands = _extract_e2e_workflow_commands(source_skill_dir, skill_md)
    except ValueError as exc:
        return [str(exc)]

    script_files = (
        sorted((source_skill_dir / "scripts").glob("*.py"))
        if (source_skill_dir / "scripts").is_dir()
        else []
    )

    if script_files and not commands:
        shell_like_blocks = [
            body
            for info, body in _iter_markdown_fenced_blocks(skill_md)
            if _is_shell_fence_info(info) and "scripts/" in body.replace("\\", "/")
        ]

        hint = ""
        if shell_like_blocks:
            hint = (
                "\n检测到 SKILL.md 中存在疑似 scripts/ 命令块，但未能解析为 E2E workflow。"
                "请检查 fenced code block 是否是标准 Markdown 形态，"
                "以及命令是否形如：python scripts/name.py '{\"key\":\"{{key}}\"}'。"
            )

        return [
            _e2e_error(
                target="SKILL.md",
                layer="workflow_missing",
                message=(
                    "Skill 包含 scripts/*.py，但 SKILL.md 中没有可执行 bash/sh/shell 命令块。\n"
                    "必须在 SKILL.md 中按真实工作流顺序写出脚本调用命令。\n"
                    "references/*.md 只能作为参考资料，不会被 E2E 解析为执行步骤。"
                    f"{hint}"
                ),
            )
        ]

    tmp_handle: tempfile.TemporaryDirectory | None = None

    try:
        if e2e_session is None:
            tmp_handle, trial_skill_dir = _copy_skill_dir_for_e2e(
                skill_name,
                source_skill_dir=source_skill_dir,
            )
        else:
            trial_skill_dir = e2e_session.workspace_dir
        trial_skill_md = (trial_skill_dir / "SKILL.md").read_text(encoding="utf-8")
        requirement_graph = _load_requirement_graph_for_e2e(trial_skill_dir)
        requirements_by_file: dict[str, list[RequirementItem]] = {}
        for req in requirement_graph.requirements:
            requirements_by_file.setdefault(req.target_file, []).append(req)

        payload: dict[str, Any] = _seed_initial_e2e_payload(
            commands,
            external_context=external_context,
        )
        traces: list[E2EStepTrace] = []

        venv_python: Path | None = None

        command_signature = _command_plan_signature(commands)
        deps_signature = _deps_signature_for_commands(trial_skill_dir, trial_skill_md, commands)
        if e2e_session is not None:
            e2e_session.command_plan_signature = command_signature
            e2e_session.deps_signature = deps_signature

        if any(command.script_path.endswith(".py") for command in commands):
            try:
                venv_python = _get_skill_venv_python(trial_skill_dir)

                should_install_deps = e2e_session is None or e2e_session.installed_deps_signature != deps_signature
                if should_install_deps:
                    for command in commands:
                        if not command.script_path.endswith(".py"):
                            continue

                        entry = _attach_requirements_to_entry(_skill_plan_entry_for_file(
                            file_path=command.script_path,
                            blueprint_text=trial_skill_md,
                        ), requirements_by_file.get(command.script_path, []))

                        _install_capability_dependencies(
                            venv_python,
                            entry.required_capabilities,
                        )

                        refined_contract, resolution = _contract_resolution_for_trial(
                            command.script_path,
                            trial_skill_md,
                            None,
                            None,
                        )

                        _install_declared_dependency_packages(
                            venv_python,
                            list(refined_contract.declared_dependencies or [])
                            + list(resolution.declared_dependencies or []),
                            source_label="implementation_resolution",
                        )
                    if e2e_session is not None:
                        e2e_session.installed_deps_signature = deps_signature
                        e2e_session.events.append({**e2e_session.to_event_base(), "event": "dependencies_prepared", "reused_venv": False})
                elif e2e_session is not None:
                    e2e_session.events.append({**e2e_session.to_event_base(), "event": "dependencies_reused", "reused_venv": True})

            except RuntimeError as exc:
                return [
                    _e2e_error(
                        target="scripts",
                        layer="venv_prepare",
                        message=f"端到端试运行环境准备失败：{exc}",
                    )
                ]

        reused_checkpoints: list[int] = []
        if e2e_session is not None and resume_from_step > 1:
            for prior_step in range(1, resume_from_step):
                checkpoint = _load_valid_checkpoint(e2e_session, prior_step)
                if not checkpoint:
                    resume_from_step = prior_step
                    break
                payload = dict(checkpoint.get("context_after") or payload)
                reused_checkpoints.append(prior_step)
                traces.append(E2EStepTrace(
                    ordinal=int(checkpoint.get("step_index") or prior_step),
                    script_path=str(checkpoint.get("script_path") or ""),
                    raw_command="[checkpoint]",
                    placeholders=[],
                    argv_keys=sorted(str(key) for key in (checkpoint.get("argv_json") or {}).keys()),
                    stdout_keys=sorted(str(key) for key in (checkpoint.get("stdout_json") or {}).keys()),
                    new_keys=list(checkpoint.get("new_keys") or []),
                    artifact_paths=list(checkpoint.get("artifact_paths") or []),
                    argv_shape=dict(checkpoint.get("argv_shape") or {}),
                    stdout_shape=dict(checkpoint.get("stdout_shape") or {}),
                ))
            if e2e_session is not None:
                e2e_session.events.append({**e2e_session.to_event_base(), "event": "checkpoints_reused", "resume_from_step": resume_from_step, "reused_checkpoints": reused_checkpoints})

        for index, command in enumerate(commands):
            if command.ordinal < resume_from_step:
                continue
            try:
                entry = _attach_requirements_to_entry(_validate_e2e_command_static(
                    command=command,
                    trial_skill_dir=trial_skill_dir,
                    skill_md=trial_skill_md,
                    available_payload_keys=set(payload.keys()),
                ), requirements_by_file.get(command.script_path, []))

                content = (trial_skill_dir / command.script_path).read_text(encoding="utf-8")

                rendered_payload = _render_e2e_command_payload(
                    command,
                    payload=payload,
                    traces=traces,
                )

                if entry.runtime == "python":
                    if venv_python is None:
                        raise ValueError("python venv 未初始化。")

                    proc = _execute_e2e_python_command(
                        command=command,
                        trial_skill_dir=trial_skill_dir,
                        rendered_payload=rendered_payload,
                        venv_python=venv_python,
                    )

                elif entry.runtime == "node":
                    proc = _execute_e2e_node_command(
                        command=command,
                        trial_skill_dir=trial_skill_dir,
                        rendered_payload=rendered_payload,
                    )

                elif entry.runtime in {"bash", "shell"}:
                    proc = _execute_e2e_shell_command(
                        command=command,
                        trial_skill_dir=trial_skill_dir,
                        rendered_payload=rendered_payload,
                    )

                else:
                    raise ValueError(
                        _e2e_error(
                            target=command.script_path,
                            layer="unsupported_runtime",
                            message=(
                                f"第 {command.ordinal} 步 {command.script_path} "
                                f"runtime={entry.runtime} 暂不支持端到端执行。"
                            ),
                        )
                    )

                stdout_json = _parse_e2e_stdout_json(
                    command=command,
                    proc=proc,
                    trial_skill_dir=trial_skill_dir,
                    trial_skill_md=trial_skill_md,
                    content=content,
                    entry=entry,
                    rendered_payload=rendered_payload,
                )

                artifact_paths = _stdout_artifact_paths(stdout_json, entry, None)

                before_keys = set(payload.keys())
                new_keys = sorted(set(stdout_json.keys()) - before_keys)

                trace = E2EStepTrace(
                    ordinal=command.ordinal,
                    script_path=command.script_path,
                    raw_command=command.raw_command,
                    placeholders=sorted(_e2e_command_placeholders(command)),
                    argv_keys=sorted(str(key) for key in rendered_payload.keys()),
                    stdout_keys=sorted(str(key) for key in stdout_json.keys()),
                    new_keys=new_keys,
                    artifact_paths=artifact_paths,
                    argv_shape=_json_object_shape(rendered_payload),
                    stdout_shape=_json_object_shape(stdout_json),
                )

                argument_effect_review = _run_e2e_requirement_flow_review(
                    command=command,
                    script_content=content,
                    skill_plan_entry=entry,
                    rendered_payload=rendered_payload,
                    stdout_json=stdout_json,
                    artifact_paths=artifact_paths,
                    trace=trace,
                    previous_traces=traces,
                    requested_model=requested_model,
                    requirements=requirements_by_file.get(command.script_path, []),
                )

                if not argument_effect_review.get("passed"):
                    raise ValueError(_e2e_argument_effect_failure(
                        command=command,
                        review=argument_effect_review,
                        rendered_payload=rendered_payload,
                        stdout_json=stdout_json,
                        artifact_paths=artifact_paths,
                        traces=traces,
                    ))

                is_final_step = index == len(commands) - 1
                if is_final_step:
                    _validate_final_platform_output_contract(
                        command=command,
                        stdout_json=stdout_json,
                        traces=traces,
                    )

                context_before = dict(payload)
                payload.update(stdout_json)

                if artifact_paths:
                    payload.setdefault("_artifacts", [])
                    if isinstance(payload["_artifacts"], list):
                        payload["_artifacts"].extend(artifact_paths)
                    payload["_last_artifacts"] = artifact_paths

                if e2e_session is not None:
                    _write_step_checkpoint(
                        e2e_session,
                        command=command,
                        rendered_payload=rendered_payload,
                        stdout_json=stdout_json,
                        context_before=context_before,
                        context_after=dict(payload),
                        new_keys=new_keys,
                        artifact_paths=artifact_paths,
                        proc=proc,
                    )
                    e2e_session.events.append({**e2e_session.to_event_base(), "event": "checkpoint_saved", "step_index": command.ordinal, "script_path": command.script_path})

                traces.append(trace)

                logger.info("[Creator][E2E] %s", _e2e_trace_line(trace))

            except subprocess.TimeoutExpired as exc:
                errors.append(
                    _e2e_error(
                        target=command.script_path,
                        layer="timeout",
                        message=(
                            f"第 {command.ordinal} 步 {command.script_path} 端到端执行超时：{exc}\n\n"
                            "已成功执行的前序边界 trace：\n"
                            f"{_format_e2e_trace(traces)}"
                        ),
                    )
                )
                break

            except ValueError as exc:
                message = str(exc)
                if "已成功执行的前序边界 trace" not in message and "已成功执行的前序步骤" not in message:
                    message += "\n\n已成功执行的前序边界 trace：\n" + _format_e2e_trace(traces)
                errors.append(message)
                break

    finally:
        if tmp_handle is not None:
            tmp_handle.cleanup()

    return errors


def _placeholder_root_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\{\{\s*([A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)*)\s*\}\}", value.strip())
    if not match:
        return None
    return match.group(1).split(".", 1)[0]


def _values_for_skill_plan_command(
    *,
    entry: SkillPlanEntry,
    existing_payload: dict[str, Any] | None,
    available_values: set[str],
) -> dict[str, str]:
    values: dict[str, str] = {}
    existing_payload = existing_payload or {}
    reusable_by_root: dict[str, str] = {}
    for value in existing_payload.values():
        root = _placeholder_root_name(value)
        if root:
            reusable_by_root[root] = value

    for input_name in entry.inputs or ["payload"]:
        current = existing_payload.get(input_name)
        if isinstance(current, str) and _placeholder_root_name(current) in available_values:
            values[input_name] = current
        elif input_name in reusable_by_root and input_name in available_values:
            values[input_name] = reusable_by_root[input_name]
        else:
            values[input_name] = f"{{{{{input_name}}}}}"
    return values


def _patch_skill_md_command_payloads_from_skill_plan(content: str, blueprint_text: str) -> tuple[str, list[dict[str, Any]]]:
    """Deprecated no-op.

    Command payload repair must be based on real E2E traces (argv_shape,
    stdout_shape, missing placeholders, and script parser behavior), not by
    rewriting JSON argv to match SkillPlan.inputs. Keep this function as a
    compatibility shim for older callers/tests, but never mutate content.
    """
    return content, []


def _structured_failure_from_errors(errors: list[str]) -> dict[str, Any]:
    for error in errors or []:
        match = re.search(r"E2E_STRUCTURED_FAILURE=(\{.*?\})(?:\n|$)", error, re.S)
        if not match:
            continue
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return {}

async def _repair_existing_file_for_e2e_failure(
    *,
    skill_name: str,
    target_path: str,
    e2e_errors: list[str],
    requested_model: str | None = None,
    external_context: dict[str, Any] | None = None,
    repair_events: list[dict[str, Any]] | None = None,
    e2e_session: CreatorE2ESession | None = None,
) -> str:
    """Repair existing SKILL.md/script file using local patch + sandbox E2E.

    第二轮原则：

    1. 只修跨模块接口串接：
       - SKILL.md workflow command；
       - 上下游 JSON 字段；
       - 当前脚本 argv/stdout 对齐；
       - 最终平台输出是否能被 sandbox 消费。

    2. 不在这里修单模块功能细节：
       - PDF 字体、字号、行距；
       - 图片分辨率、风格；
       - 表格样式；
       - 内容质量。
       这些属于第一轮 module functional smoke。

    3. 不写平台 IO 词表。
       平台 IO 直接复用现有 sandbox / E2E 试运行协议。

    4. 模型只输出局部 patch。
       E2E 是否通过由临时 skill 沙盒真实试运行决定。

    5. 如果 patch apply / static preflight / sandbox E2E 失败，
       在本函数内部继续把失败反馈给写代码模型重试，直到通过或达到最大轮次。
    """

    _validate_file_path(target_path)

    if target_path.startswith("references/"):
        logger.info(
            "[Creator][E2E] remap reference repair target to SKILL.md target=%s",
            target_path,
        )
        target_path = "SKILL.md"

    skill_dir = settings.skills_path / skill_name
    if e2e_session is None:
        e2e_session = _create_e2e_session(skill_name, source_skill_dir=skill_dir)
    target_file = skill_dir / target_path

    if not target_file.is_file():
        raise ValueError(f"端到端修复目标不存在：{target_path}")

    skill_md_path = skill_dir / "SKILL.md"
    skill_md = skill_md_path.read_text(encoding="utf-8") if skill_md_path.is_file() else ""

    if target_path == "SKILL.md":
        hard_format_failures = detect_markdown_hard_format_failures(
            "SKILL.md",
            skill_md,
            require_frontmatter=True,
        )
        if hard_format_failures:
            if repair_events is not None:
                repair_events.append({
                    "type": "hard_format_requires_full_rewrite",
                    "target_file": "SKILL.md",
                    "failures": hard_format_failures,
                })
            raise ValueError(
                "hard_format_requires_full_rewrite: E2E localized patch cannot repair SKILL.md hard Markdown format; "
                + json.dumps(hard_format_failures, ensure_ascii=False, default=str)
            )

    all_file_summaries: list[str] = []
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file():
            continue

        rel = path.relative_to(skill_dir).as_posix()
        if rel.startswith(".venv/") or "__pycache__" in rel:
            continue
        if rel == target_path:
            continue

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        all_file_summaries.append(f"\n--- FILE {rel} ---\n{text[-6000:]}")

    route = route_creator_file_model(
        file_path=target_path,
        purpose=(
            "第二轮 workflow E2E 局部修复："
            "只修 SKILL.md workflow、跨模块 JSON 字段串接、最终平台输出字段映射；"
            "不修单文件业务功能细节；"
            "平台 IO 由 sandbox/E2E 试运行判断；"
            "输出 single-file local patch proposal。"
        ),
        requested_model=requested_model,
    )
    model = route.model

    _log_creator_model_usage(
        phase="e2e_repair.route",
        skill_name=skill_name,
        file_path=target_path,
        route=route,
        extra=f"errors={len(e2e_errors)} mode=exact_replace_patch_sandbox_e2e",
    )

    deterministic_error = "\n\n".join(e2e_errors)[-12000:]
    repair_state = _e2e_repair_state_from_errors(e2e_errors, resolved_failures=e2e_session.resolved_failures)
    structured_failure = _structured_failure_from_errors(e2e_errors)
    targeted_e2e_hint = _targeted_e2e_repair_hint(e2e_errors)

    scope = CreatorRepairScope(
        phase="workflow_e2e",
        repair_type="cross_step_io_alignment",
        target_file=target_path,
        max_changed_lines=220,
        notes=(
            "第二轮只修 workflow / cross-step IO / final sandbox output。",
            "平台 IO 不在 repair 层用词表判断，直接由 sandbox/E2E 试运行判断。",
            "优先输出 edits old_lines/new_lines exact_replace patch，不要输出完整文件。",
        ),
    )

    e2e_tool_cards = ""
    if target_path.startswith("scripts/"):
        e2e_entry = _skill_plan_entry_for_file(
            file_path=target_path,
            blueprint_text=skill_md,
        )
        e2e_tool_cards = _creator_tool_context_for_script(
            file_path=target_path,
            skill_plan_entry=e2e_entry,
            blueprint_text=skill_md,
            failure_layer=_failure_layer_from_error_text(deterministic_error),
            error_text=deterministic_error,
            include_snippets=True,
        )

    if target_path == "SKILL.md":
        target_rule = (
            "你正在修复 SKILL.md 的 workflow 执行块。\n"
            "第二轮 E2E 的目标是让 workflow 在简单沙盒中真实跑通。\n"
            "E2E 只执行 SKILL.md 中的 bash/sh/shell fenced command block，references/*.md 不是执行步骤。\n"
            "只修 workflow/cross-step IO/final output/artifact 相关问题，不修 Markdown 全局格式。\n"
            "不要重写 SKILL.md 正文。\n"
            "不要修改 frontmatter 边界；不要修改 fenced block 开闭结构。\n"
            "不要在 repair 层重新定义平台 IO；平台 IO 由 sandbox/E2E 试运行判断。\n"
            "优先输出 edits old_lines/new_lines exact_replace patch。不要输出完整 SKILL.md。"
        )

    elif target_path.startswith("scripts/"):
        target_rule = (
            "你正在修复脚本源码的 E2E 接口串接问题。\n"
            "第二轮 E2E 的目标是让 workflow 在简单沙盒中真实跑通。\n"
            "只修当前脚本与 SKILL.md 命令块、上游 stdout、下游输入之间的接口对齐问题。\n"
            "不要重新设计业务功能；PDF 样式、图片风格、表格样式、内容质量属于第一轮功能 smoke。\n"
            "不要在 repair 层重新定义平台 IO；平台 IO 由 sandbox/E2E 试运行判断。\n"
            "优先输出 edits old_lines/new_lines exact_replace patch。不要输出完整源码。"
        )

    else:
        target_rule = (
            "只修复 E2E_REPAIR_TARGET 指向的文件。\n"
            "只修当前 E2E 失败对应的最小接口串接问题。\n"
            "优先输出 edits old_lines/new_lines exact_replace patch。不要输出完整文件。"
        )

    base_task_context = "\n".join([
        f"Skill 名称：{skill_name}",
        "",
        "E2E repair 状态机（只能修 remaining_failed_checks；resolved_failures 禁止重复修复）：",
        json.dumps(repair_state, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        "",
        "结构化失败对象：",
        json.dumps(structured_failure, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        "",
        "定向 E2E 修复提示：",
        targeted_e2e_hint or "无",
        "",
        "sandbox IO 前置协议：",
        _sandbox_io_contract_text_for_creator(),
        "",
        "当前 SKILL.md：",
        skill_md[-12000:],
        "",
        "其它相关文件摘要：",
        "".join(all_file_summaries)[-20000:],
        "",
        "Tool Registry / Snippet 上下文：",
        e2e_tool_cards,
    ])

    repair_feedback = "\n\n".join(repair_state.get("remaining_failed_checks") or e2e_errors)[-12000:]
    last_failure = ""
    max_candidate_attempts = 5
    working_content = (e2e_session.workspace_dir / target_path).read_text(encoding="utf-8")

    for candidate_attempt in range(1, max_candidate_attempts + 1):
        current_content = working_content
        effective_skill_md = current_content if target_path == "SKILL.md" else skill_md
        effective_task_context = base_task_context
        if target_path == "SKILL.md":
            effective_task_context = base_task_context.replace(skill_md[-12000:], effective_skill_md[-12000:], 1)

        current_repair_state = _e2e_repair_state_from_errors(
            repair_feedback.split("\n\n"),
            resolved_failures=e2e_session.resolved_failures,
        )
        effective_task_context += (
            "\n\n当前 E2E repair 状态机：\n"
            + json.dumps(current_repair_state, ensure_ascii=False, indent=2, sort_keys=True, default=str)
            + "\n\n硬性要求：只能修 remaining_failed_checks；不得再次修改 resolved_failures 对应问题。"
        )

        try:
            _proposal, candidate_content, diff_stats = await _request_and_apply_repair_patch(
                model=model,
                file_path=target_path,
                current_content=current_content,
                failure_text=repair_feedback,
                scope=scope,
                task_context=effective_task_context + ("\n\n上一轮候选失败反馈：\n" + last_failure if last_failure else ""),
                target_rule=target_rule,
                patch_retry_limit=3,
            )

            from .generation import _sanitize_generated_file_content

            sanitized = _sanitize_generated_file_content(target_path, candidate_content)

            try:
                if target_path == "SKILL.md":
                    _validate_skill_md_against_existing_files(skill_name, sanitized)

                elif target_path.startswith("references/"):
                    _validate_reference_file_contract(target_path, sanitized, skill_md)

                elif target_path.startswith("assets/"):
                    _validate_asset_file_contract(target_path, sanitized)

                elif target_path.startswith("scripts/"):
                    _validate_e2e_script_static_preflight(
                        file_path=target_path,
                        content=sanitized,
                        skill_md=effective_skill_md,
                    )

            except Exception as preflight_exc:
                last_failure = (
                    "STATIC_PREFLIGHT_FAILED：候选 patch 已应用，但静态预检失败。\n"
                    f"attempt={candidate_attempt}/{max_candidate_attempts}\n"
                    f"error_type={type(preflight_exc).__name__}\n"
                    f"error={preflight_exc}\n"
                    "请基于这个静态错误继续输出新的 exact_replace patch。"
                )
                repair_feedback = deterministic_error + "\n\n" + last_failure
                logger.warning(
                    "[Creator][E2E][repair_candidate_static_failed] skill=%s file=%s attempt=%d/%d error=%s",
                    skill_name,
                    target_path,
                    candidate_attempt,
                    max_candidate_attempts,
                    preflight_exc,
                )
                continue

            old_command_signature = e2e_session.command_plan_signature
            session_target = e2e_session.workspace_dir / target_path
            session_target.parent.mkdir(parents=True, exist_ok=True)
            session_target.write_text(sanitized, encoding="utf-8")
            e2e_session.current_revision += 1

            session_skill_md = (e2e_session.workspace_dir / "SKILL.md").read_text(encoding="utf-8")
            try:
                session_commands = _extract_e2e_workflow_commands(e2e_session.workspace_dir, session_skill_md)
                new_command_signature = _command_plan_signature(session_commands)
            except Exception:
                session_commands = []
                new_command_signature = ""
            earliest_step = _earliest_invalid_step(
                changed_file=target_path,
                commands=session_commands,
                old_command_plan_signature=old_command_signature,
                new_command_plan_signature=new_command_signature,
            )
            resume_from_step = earliest_step or (len(session_commands) + 1 if session_commands else 1)
            invalidated = _invalidate_checkpoints_from(e2e_session, earliest_step) if earliest_step else []
            reused = [idx for idx in range(1, max(1, resume_from_step)) if _load_valid_checkpoint(e2e_session, idx)]

            sandbox_gate = _run_e2e_sandbox_acceptance_gate(
                skill_name=skill_name,
                candidate_skill_dir=e2e_session.workspace_dir,
                patched_file=target_path,
                original_errors=e2e_errors,
                external_context=external_context,
                requested_model=requested_model,
                e2e_session=e2e_session,
                resume_from_step=resume_from_step,
            )

            e2e_session.events.append({
                **e2e_session.to_event_base(),
                "attempt": candidate_attempt,
                "target_file": target_path,
                "resume_from_step": resume_from_step,
                "reused_venv": True,
                "reused_checkpoints": reused,
                "invalidated_checkpoints": invalidated,
                "failed_checks": sandbox_gate.get("errors") or [],
                "resolved_failures": e2e_session.resolved_failures,
                "patch_mode": "exact_replace",
                "fallback_type": (diff_stats.get("applied") or [{}])[0].get("fallback_type", "none"),
                "patch_status": "applied",
                "changed_line_count": diff_stats.get("changed_line_count"),
                "diff_excerpt": diff_stats.get("generated_diff_excerpt"),
                "matched_excerpt": (diff_stats.get("applied") or [{}])[0].get("matched_excerpt"),
                "original_model_old_excerpt": (diff_stats.get("applied") or [{}])[0].get("original_model_old_excerpt"),
                "rerun_status": "passed" if sandbox_gate.get("accepted") else "failed",
                "writeback_status": "candidate_only",
            })

            if not sandbox_gate.get("accepted"):
                gate_errors = sandbox_gate.get("errors") or []
                next_target = _e2e_repair_target_from_errors(gate_errors)
                has_explicit_next_target = any("E2E_REPAIR_TARGET=" in str(error or "") for error in gate_errors)
                if has_explicit_next_target and next_target and next_target != target_path:
                    e2e_session.events.append({
                        **e2e_session.to_event_base(),
                        "attempt": candidate_attempt,
                        "target_file": target_path,
                        "patch_status": "rejected",
                        "rejection_reason": "remaining failure target moved to a different file",
                        "remaining_target_file": next_target,
                        "failed_checks": sandbox_gate.get("errors") or [],
                        "rerun_status": "failed",
                        "writeback_status": "candidate_only",
                    })
                    raise ValueError(
                        "E2E_REPAIR_TARGET_CHANGED：当前 remaining failure 已转移到其它文件，"
                        f"停止继续修旧 target_file={target_path!r}，next_target={next_target!r}。"
                    )
                working_content = sanitized
                last_failure = (
                    "SANDBOX_E2E_FAILED：候选 patch 已应用，但简单沙盒 E2E 仍失败。\n"
                    f"attempt={candidate_attempt}/{max_candidate_attempts}\n"
                    f"diff_stats={json.dumps(diff_stats, ensure_ascii=False, default=str)[:3000]}\n"
                    f"sandbox_gate={json.dumps(sandbox_gate, ensure_ascii=False, default=str)[:12000]}\n"
                    "请基于 sandbox_gate.errors 继续输出新的 exact_replace patch。"
                )
                repair_feedback = "\n\n".join(sandbox_gate.get("errors") or e2e_errors)[-12000:] + "\n\n" + last_failure
                logger.warning(
                    "[Creator][E2E][repair_candidate_e2e_failed] skill=%s file=%s attempt=%d/%d",
                    skill_name,
                    target_path,
                    candidate_attempt,
                    max_candidate_attempts,
                )
                continue

            for error in e2e_errors:
                e2e_session.resolved_failures.append({
                    "failure_signature": _failure_signature_from_error(error),
                    "target_file": target_path,
                    "failure_kind": _failure_layer_from_error_text(error) or "e2e",
                    "step_index": structured_failure.get("failed_step_index"),
                    "resolved_by_revision": e2e_session.current_revision,
                    "verified_by_e2e": True,
                })

            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_text(sanitized, encoding="utf-8")
            if e2e_session.events:
                e2e_session.events[-1]["writeback_status"] = "written"
            if repair_events is not None:
                repair_events.extend(e2e_session.events)

            logger.info(
                "[Creator][E2E][repair_accept] skill=%s file=%s attempt=%d diff_stats=%s sandbox=passed",
                skill_name,
                target_path,
                candidate_attempt,
                json.dumps(diff_stats, ensure_ascii=False, default=str)[:3000],
            )

            return target_path

        except Exception as candidate_exc:
            error_text = str(candidate_exc)
            if "proposal_noop" in error_text or "no-op" in error_text:
                patch_status = "noop"
            elif "FORMAT_VIOLATION" in error_text or "JSON" in error_text or "parse" in error_text:
                patch_status = "parse_failed"
            else:
                patch_status = "rejected"
            e2e_session.events.append({
                **e2e_session.to_event_base(),
                "attempt": candidate_attempt,
                "target_file": target_path,
                "patch_status": patch_status,
                "rejection_reason": error_text[:2000],
                "last_output_excerpt": getattr(candidate_exc, "last_output_excerpt", ""),
                "parser_error": getattr(candidate_exc, "parser_error", "") or (error_text[:1000] if patch_status == "parse_failed" else ""),
                "diff_extraction_attempted": bool(getattr(candidate_exc, "diff_extraction_attempted", False)),
                "old_lines_new_lines_fallback_attempted": bool(getattr(candidate_exc, "lines_fallback_attempted", False)),
                "failed_checks": repair_feedback.split("\n\n")[:8],
                "resolved_failures": e2e_session.resolved_failures,
                "rerun_status": "skipped",
                "writeback_status": "candidate_only",
            })
            if repair_events is not None and patch_status in {"noop", "parse_failed"}:
                repair_events.extend(e2e_session.events)
            last_failure = (
                "REPAIR_CANDIDATE_FAILED：候选 patch 生成、解析或应用失败。\n"
                f"attempt={candidate_attempt}/{max_candidate_attempts}\n"
                f"error_type={type(candidate_exc).__name__}\n"
                f"error={candidate_exc}\n"
                "请继续输出新的 exact_replace patch。"
            )
            repair_feedback = deterministic_error + "\n\n" + last_failure

            logger.warning(
                "[Creator][E2E][repair_candidate_failed] skill=%s file=%s attempt=%d/%d error=%s",
                skill_name,
                target_path,
                candidate_attempt,
                max_candidate_attempts,
                candidate_exc,
            )

            continue

    raise ValueError(
        "端到端自动修复未完成：写代码模型连续提出的 patch 未能通过 apply/static/E2E。\n"
        f"skill={skill_name}\n"
        f"target={target_path}\n"
        f"last_failure={last_failure[:12000]}"
    )


def validate_workflow_e2e(
    skill_name: str,
    *,
    external_context: dict[str, Any] | None = None,
    source_skill_dir: Path | None = None,
    requested_model: str | None = None,
    e2e_session: CreatorE2ESession | None = None,
    resume_from_step: int = 1,
) -> list[str]:
    """Second-round Creator validator.

    第二轮负责：
    - SKILL.md workflow 能否真实执行；
    - 上下游 JSON 字段能否串起来；
    - 当前 step 接口是否对齐；
    - 最后一步 stdout 是否符合现有 sandbox 平台协议；
    - artifact 是否真实存在并基础合法。

    不写业务字段词表。
    不重新做第一轮责任审查。
    """

    return _run_skill_workflow_e2e_once(
        skill_name,
        external_context=external_context,
        source_skill_dir=source_skill_dir,
        requested_model=requested_model,
        e2e_session=e2e_session,
        resume_from_step=resume_from_step,
    )

def _run_e2e_sandbox_acceptance_gate(
    *,
    skill_name: str,
    candidate_skill_dir: Path,
    patched_file: str,
    original_errors: list[str],
    external_context: dict[str, Any] | None = None,
    requested_model: str | None = None,
    e2e_session: CreatorE2ESession | None = None,
    resume_from_step: int = 1,
) -> dict[str, Any]:
    """Second-round E2E acceptance gate.

    E2E 是否通过，必须由真实沙盒试运行决定。
    不写平台字段词表。
    """

    candidate_skill_dir = candidate_skill_dir.resolve()

    if not candidate_skill_dir.is_dir():
        return {
            "accepted": False,
            "phase": "e2e_sandbox",
            "patched_file": patched_file,
            "reason": f"candidate skill dir 不存在：{candidate_skill_dir}",
            "errors": [f"candidate skill dir 不存在：{candidate_skill_dir}"],
            "original_errors": original_errors,
        }

    errors = _run_skill_workflow_e2e_once(
        skill_name,
        external_context=external_context,
        source_skill_dir=candidate_skill_dir,
        requested_model=requested_model,
        e2e_session=e2e_session,
        resume_from_step=resume_from_step,
    )

    if errors:
        return {
            "accepted": False,
            "phase": "e2e_sandbox",
            "patched_file": patched_file,
            "reason": "candidate 在简单沙盒 E2E 试运行中失败。",
            "errors": errors,
            "original_errors": original_errors,
        }

    return {
        "accepted": True,
        "phase": "e2e_sandbox",
        "patched_file": patched_file,
        "reason": "candidate 已通过现有 sandbox/E2E workflow 试运行。",
        "errors": [],
        "original_errors": original_errors,
    }

def _raise_file_contract_failures(results: list[ContractCheckResult]) -> None:
    failed = [result for result in results if not result.passed]
    if failed:
        raise ContractValidationError(
            "文件级合同校验未通过：\n" + _format_contract_checks(results, passed=False),
            results,
        )

def _format_script_functional_issues(issues: list[dict[str, Any]]) -> str:
    lines = ["script_functional 校验未通过："]
    for issue in issues:
        lines.append(
            "- {id}\n"
            "  failed_file: {failed_file}\n"
            "  failed_function: {failed_function}\n"
            "  code_region: {code_region}\n"
            "  reason: {reason}\n"
            "  minimal_edit: {minimal_edit}\n"
            "  allowed_scope: {allowed_scope}\n"
            "  forbidden_scope: {forbidden_scope}".format(
                id=issue.get("id", "script_functional.unknown"),
                failed_file=issue.get("failed_file", ""),
                failed_function=issue.get("failed_function", ""),
                code_region=issue.get("code_region", ""),
                reason=issue.get("reason", ""),
                minimal_edit=issue.get("minimal_edit", ""),
                allowed_scope=issue.get("allowed_scope", ""),
                forbidden_scope=issue.get("forbidden_scope", ""),
            )
        )
    return "\n".join(lines)


class ScriptFunctionalValidationError(ValueError):
    """First-round responsibility/evidence failure after script smoke passed."""

    def __init__(self, issues: list[dict[str, Any]], *, layer: str = "responsibility"):
        super().__init__(_format_script_functional_issues(issues))
        self.issues = issues
        self.layer = layer


def _stage_error_for_script_functional(exc: Exception):
    layer = getattr(exc, "layer", None) or _failure_layer_from_error_text(str(exc)) or "responsibility"
    return FileGenerationStageError(
        source="script_functional",
        layer=layer,
        detail=str(exc),
        original=exc,
    )


def _stdout_artifact_paths(payload: dict[str, Any], entry: SkillPlanEntry, canonical_contract: Any | None = None) -> list[str]:
    fields = _artifact_fields_for_entry(entry, canonical_contract)
    fields.extend([
        "image_path", "pdf_path", "docx_path", "pptx_path", "html_path",
        "image_paths", "file_paths", "file_outputs",
    ])
    paths: list[str] = []
    for field_name in dict.fromkeys(fields):
        value = payload.get(field_name)
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, str) and item.strip():
                paths.append(item.strip())
    return list(dict.fromkeys(paths))


def _resolve_trial_artifact_path(skill_dir: Path, raw_path: str) -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate
    if raw_path.startswith("outputs/"):
        return (skill_dir / candidate).resolve()
    return (skill_dir / "scripts" / candidate).resolve()


def _validate_artifact_content_evidence(
    *,
    stdout_payload: dict[str, Any],
    argv_payload: dict[str, Any],
    entry: SkillPlanEntry,
    skill_dir: Path,
    canonical_contract: Any | None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    artifact_paths = _stdout_artifact_paths(stdout_payload, entry, canonical_contract)
    input_strings = [
        text.strip()
        for text in _flatten_json_strings(argv_payload)
        if len(text.strip()) >= 8
    ]
    for raw_path in artifact_paths:
        path = _resolve_trial_artifact_path(skill_dir, raw_path)
        if not path.is_file():
            issues.append({
                "id": "script_functional.artifact_evidence.missing",
                "failed_file": entry.path,
                "failed_function": "artifact/file output generation",
                "code_region": "file write path and stdout artifact field",
                "reason": f"stdout 声明产物 {raw_path!r}，但试运行目录中不存在该文件。",
                "minimal_edit": "只修当前脚本的产物写入路径和 stdout 路径映射，确保返回的文件真实存在。",
                "allowed_scope": entry.path,
                "forbidden_scope": "不得改 SKILL.md、其它脚本或 SkillPlan；不得返回不存在路径。",
            })
            continue
        size = path.stat().st_size
        if size <= 0:
            issues.append({
                "id": "script_functional.artifact_evidence.empty",
                "failed_file": entry.path,
                "failed_function": "artifact/file output generation",
                "code_region": "file content write",
                "reason": f"产物 {raw_path!r} 已生成但文件为空。",
                "minimal_edit": "只修当前脚本的文件内容生成逻辑，让产物写入真实内容。",
                "allowed_scope": entry.path,
                "forbidden_scope": "不得输出空文件或占位文件骗过路径校验。",
            })
        if path.suffix.lower() == ".pdf":
            try:
                header = path.read_bytes()[:5]
            except OSError:
                header = b""
            if header != b"%PDF-":
                issues.append({
                    "id": "script_functional.artifact_evidence.pdf_header",
                    "failed_file": entry.path,
                    "failed_function": "pdf artifact generation",
                    "code_region": "PDF writer/output file creation",
                    "reason": f"产物 {raw_path!r} 不是有效 PDF 文件头。",
                    "minimal_edit": "只修当前脚本 PDF 写入逻辑，确保生成真实 PDF，而不是普通文本或空占位文件。",
                    "allowed_scope": entry.path,
                    "forbidden_scope": "不得改产物字段协议或返回假 PDF 路径。",
                })
        elif path.suffix.lower() in {".txt", ".md", ".json", ".html", ".htm", ".csv"} and input_strings:
            try:
                artifact_text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                artifact_text = ""
            if artifact_text and not any(text in artifact_text for text in input_strings[:10]):
                issues.append({
                    "id": "script_functional.artifact_evidence.input_dependency",
                    "failed_file": entry.path,
                    "failed_function": "artifact content assembly",
                    "code_region": "input processing through artifact write",
                    "reason": f"文本类产物 {raw_path!r} 没有体现试运行输入内容，可能未消费 argv JSON。",
                    "minimal_edit": "只修当前脚本产物内容组织逻辑，让文本类产物真实依赖输入或工具/模型结果。",
                    "allowed_scope": entry.path,
                    "forbidden_scope": "不得返回固定模板、mock 内容或空壳产物。",
                })
    return issues


def _flatten_json_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for item in value.values():
            out.extend(_flatten_json_strings(item))
        return out
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_flatten_json_strings(item))
        return out
    return []



def _html_output_candidates(payload: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    html_path = payload.get("html_path")
    if isinstance(html_path, str) and html_path.strip():
        candidates.append(html_path.strip())
    for key in ("file_paths", "file_outputs"):
        paths = payload.get(key)
        if isinstance(paths, list):
            candidates.extend(path.strip() for path in paths if isinstance(path, str) and path.strip())
    return candidates


_LEGACY_OUTPUT_ALIASES: dict[str, tuple[str, ...]] = {}


def _payload_has_declared_output(payload: dict[str, Any], output_key: str) -> bool:
    """New Creator E2E requires exact declared output fields; no alias guessing."""
    return output_key in payload


def _payload_output_value(payload: dict[str, Any], output_key: str) -> Any:
    """Return exact declared output value only; aliases belong in migration/warnings."""
    return payload.get(output_key) if output_key in payload else None

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


def _validate_trial_stdout_json(*, stdout: str, content: str, args: list[str], role: str | None = None, skill_dir: Path | None = None, skill_plan_entry: dict[str, Any] | None = None, canonical_contract: Any | None = None) -> None:
    """Validate trial stdout with dynamic, field-name-agnostic rules.

    SkillPlan.outputs is a blueprint hint, not the sole runtime contract.  The
    hard requirements here are: stdout is a JSON object, it has at least one
    non-empty value, it does not report an error, and any file-looking values it
    declares point at real files.
    """
    stripped = (stdout or "").strip()
    if not stripped:
        raise ValueError(f"脚本试运行 stdout 为空：argv={args!r}")
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"脚本试运行 stdout 不是合法 JSON object：argv={args!r} stdout={stripped[-4000:]}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"脚本试运行 stdout 必须是 JSON object：argv={args!r} stdout={stripped[-4000:]}")
    if "error" in payload:
        raise ValueError(f"脚本试运行 stdout JSON 不得包含 error 字段：argv={args!r} stdout={stripped[-4000:]}")
    if not any(_json_value_non_empty(value) for value in payload.values()):
        raise ValueError(f"脚本试运行 stdout JSON 至少需要一个非空字段：argv={args!r} stdout={stripped[-4000:]}")
    if canonical_contract is not None:
        stdout_schema = getattr(canonical_contract, "stdout_schema", {}) or {}
        required = stdout_schema.get("required") if isinstance(stdout_schema, dict) else []
        missing = [str(key) for key in required or [] if str(key) not in payload or not _json_value_non_empty(payload.get(str(key)))]
        if missing:
            raise ValueError(
                "stdout_required_outputs_missing: 当前脚本 stdout 缺少 required_outputs。"
                f" missing={missing!r} actual={list(payload.keys())!r} required={list(required or [])!r} "
                f"argv={args!r} stdout={stripped[-4000:]}"
            )
    elif skill_plan_entry is not None:
        entry = _skill_plan_entry_for_file(file_path=str((skill_plan_entry or {}).get("path") or "scripts/main.py"), skill_plan_entry=skill_plan_entry)
        stdout_schema = _script_stdout_schema_for_entry(entry)
        required = stdout_schema.get("required") if isinstance(stdout_schema, dict) else []
        missing = [str(key) for key in required or [] if str(key) not in payload or not _json_value_non_empty(payload.get(str(key)))]
        if missing:
            raise ValueError(
                "stdout_required_outputs_missing: 当前脚本 stdout 缺少 required_outputs。"
                f" missing={missing!r} actual={list(payload.keys())!r} required={list(required or [])!r} "
                f"argv={args!r} stdout={stripped[-4000:]}"
            )

    try:
        if skill_dir is not None:
            validate_stdout_file_outputs(stripped, skill_dir=skill_dir, cwd=skill_dir / "scripts")
    except FileOutputValidationError as exc:
        raise ValueError(str(exc)) from exc



def _install_capability_dependencies(venv_python: Path, required_capabilities: list[str]) -> None:
    """Install platform-owned runtime dependencies for required capabilities.

    Creator trial runs execute generated scripts in a per-skill venv.  Scripts
    commonly import only ``backend.services.skill_runtime`` while the helper
    itself lazy-imports optional runtime dependencies. Static script import
    scanning cannot see those helper internals, so install the
    dependencies declared by the Creator tool registry before execution.
    """
    dependencies: list[str] = []
    seen: set[str] = set()
    for capability_name in required_capabilities or []:
        capability = get_tool_capability(capability_name)
        if capability is None:
            continue
        for dependency in capability.dependencies or []:
            package = str(dependency.get("package") or dependency.get("name") or "") if isinstance(dependency, dict) else str(dependency or "")
            if package and package not in seen:
                seen.add(package)
                dependencies.append(package)

    _install_declared_dependency_packages(venv_python, dependencies, source_label="capability")


def _install_declared_dependency_packages(venv_python: Path, dependencies: list[str], *, source_label: str = "declared") -> None:
    dependencies = [str(item).strip() for item in dependencies or [] if str(item).strip()]
    if not dependencies:
        return
    missing: list[str] = []
    dependency_import_names = {"python-docx": "docx", "python-pptx": "pptx"}
    for dependency in dependencies:
        module_name = dependency_import_names.get(dependency, dependency).replace("-", "_")
        check = subprocess.run(
            [
                str(venv_python),
                "-c",
                "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec(sys.argv[1]) is not None else 1)",
                module_name,
            ],
            capture_output=True,
            timeout=10,
        )
        if check.returncode != 0:
            missing.append(dependency)

    if not missing:
        return

    logger.info("skill-env: pip installing %s deps into venv: %s", source_label, missing)
    result = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--quiet", *missing],
        timeout=180,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"安装 {source_label} 依赖失败 ({', '.join(missing)}): {result.stderr[:500]}")


def _contract_resolution_for_trial(file_path: str, skill_md: str, role: str | None, skill_plan_entry: dict[str, Any] | None) -> tuple[Any, Any]:
    entry = _skill_plan_entry_for_file(
        file_path=file_path,
        blueprint_text=skill_md,
        role=role,
        skill_plan_entry=skill_plan_entry,
    )
    stdout_schema = _script_stdout_schema_for_entry(entry)
    contract = compile_canonical_file_contract(entry, stdout_schema)
    resolution = resolve_implementation(entry, contract)
    refined_contract = refine_contract_with_resolution(contract, resolution)
    resolution = resolve_implementation(entry, refined_contract)
    return refined_contract, resolution




def _artifact_fields_from_contract_dict(contract: dict[str, Any] | None) -> list[str]:
    """Return only fields that are semantically artifact/file outputs.

    Creator 中需要严格区分两类字段：

    1. stdout data fields:
       普通 stdout JSON 业务字段，只表示脚本输出的数据结构。
       例如任何业务字段、结构化内容字段、统计字段、描述字段等。
       这些字段只应接受 JSON required/non-empty/type/provenance 校验，
       不能被当作文件路径检查。

    2. artifact/file fields:
       文件产物路径字段，字段值应指向真实生成的文件。
       这些字段才进入 artifact existence/content evidence 校验。

    因此：
    - artifact_fields / file_fields / file_outputs 是显式文件产物字段，可以直接采纳。
    - artifact_outputs 是显式 artifact manifest，也可以采纳。
    - stdout_fields / output_fields 只是普通 stdout 字段集合，不能整体提升为 artifact。
      只有字段名本身具备 runtime artifact 语义时，才可作为 artifact 字段。
    """
    fields: list[str] = []
    contract = contract or {}

    # 显式声明为文件产物的字段，直接采纳。
    for key in ("artifact_fields", "file_fields", "file_outputs"):
        raw = contract.get(key)
        if isinstance(raw, str) and raw.strip():
            fields.append(raw.strip())
        elif isinstance(raw, list):
            fields.extend(str(item).strip() for item in raw if str(item).strip())

    # stdout_fields / output_fields 是 stdout JSON 字段，不等于 artifact 字段。
    # 只有字段名本身具备平台 runtime artifact 语义时，才提升为 artifact。
    for key in ("stdout_fields", "output_fields"):
        raw = contract.get(key)
        candidates: list[str] = []

        if isinstance(raw, str) and raw.strip():
            candidates.append(raw.strip())
        elif isinstance(raw, list):
            candidates.extend(str(item).strip() for item in raw if str(item).strip())

        for field_name in candidates:
            try:
                if is_runtime_artifact_semantic(field_name):
                    fields.append(field_name)
            except Exception:
                # 语义判断 helper 异常时，宁可不把普通 stdout 字段误判为 artifact。
                continue

    # 显式 artifact_outputs manifest 继续采纳。
    raw_outputs = contract.get("artifact_outputs")
    if isinstance(raw_outputs, list):
        for item in raw_outputs:
            if isinstance(item, dict):
                field = str(item.get("field") or item.get("name") or "").strip()
                if field:
                    fields.append(field)

    return list(dict.fromkeys(fields))


def _artifact_fields_from_tool_manifests(entry: SkillPlanEntry) -> list[str]:
    fields: list[str] = []
    for capability_name in list(entry.required_capabilities or []) + list(entry.selected_tools if hasattr(entry, "selected_tools") else []):
        cap = get_tool_capability(str(capability_name))
        if not cap:
            continue
        for output in getattr(cap, "artifact_outputs", []) or []:
            if isinstance(output, dict):
                field = str(output.get("field") or output.get("name") or "").strip()
                if field:
                    fields.append(field)
        for fn in getattr(cap, "functions", []) or []:
            for output in getattr(fn, "artifact_outputs", []) or []:
                if isinstance(output, dict):
                    field = str(output.get("field") or output.get("name") or "").strip()
                    if field:
                        fields.append(field)
    return list(dict.fromkeys(fields))


def _artifact_fields_for_entry(entry: SkillPlanEntry, canonical_contract: Any | None = None) -> list[str]:
    fields: list[str] = []
    fields.extend(_artifact_fields_from_contract_dict(entry.artifact_contract))
    if canonical_contract is not None:
        fields.extend(_artifact_fields_from_contract_dict(getattr(canonical_contract, "artifact_contract", {}) or {}))
    fields.extend(_artifact_fields_from_tool_manifests(entry))
    return list(dict.fromkeys(field for field in fields if field))

def _e2e_layer_from_errors(errors: list[str]) -> str:
    for error in errors or []:
        match = re.search(r"^E2E_LAYER=([^\n]+)", str(error), re.M)
        if match:
            return match.group(1).strip()
    return ""


def _targeted_e2e_repair_hint(errors: list[str]) -> str:
    layer = _e2e_layer_from_errors(errors)

    if layer == "final_platform_output_contract":
        return (
            "当前失败只属于最终平台输出字段不对齐。"
            "不要修改 SKILL.md，不要新增模型调用，不要改变已有业务字段。"
            "请保留最后一步脚本 stdout JSON 的原有业务字段，并额外映射到合法平台最终输出字段。"
            "如果最后一步已有可展示的主要结果值，保留原字段并额外映射到 text 或 markdown。"
            "如果最后一步产出文件，输出对应平台文件字段。"
        )

    if layer == "final_platform_output_value_invalid":
        return (
            "当前失败属于最终平台字段值类型不合法。"
            "请保持字段名不变，但修正值类型："
            "text/markdown/pdf_path/docx_path/pptx_path/html_path 必须是非空字符串；"
            "image_paths/file_paths/file_outputs 必须是非空字符串列表。"
        )

    if layer in {"external_input_missing", "e2e_dataflow_missing"}:
        return (
            "当前失败属于命令占位符无法从 payload 或前序 stdout 解析。"
            "优先修 SKILL.md 当前失败步骤的 JSON argv placeholder，"
            "不要改已成功 trace 对应步骤。"
        )

    return ""

__all__ = [name for name in globals() if not name.startswith("__")]
