"""Skill/file contract and blueprint validation helpers."""

from .common import *  # noqa: F403
from typing import Any, Iterable, Mapping
from .command_normalizer import parse_skill_md_bash_command_blocks
from .tool_pool_models import (ToolPoolModel, ToolPoolTool, ToolPoolFileBinding, ToolPoolGateEvent, ToolPoolDeniedRequest, ToolPoolMissingRequest, ToolPoolPatch, ToolPoolAddToolRequest, RuntimeImportGuardResult)

@dataclass(frozen=True)
class ContractCheckResult:
    id: str
    passed: bool
    target: str
    message: str
    expected: str
    minimal_edit: str
    matched_paths: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    layer: str = ""


class ContractValidationError(ValueError):
    """Validation error carrying structured contract check results."""

    def __init__(self, message: str, results: list[ContractCheckResult]):
        super().__init__(message)
        self.results = results


class CreatorValidatorReviewError(ValueError):
    """Validator/model-review infrastructure failed; do not repair SKILL.md content."""

    def __init__(self, message: str, *, raw_excerpt: str = "") -> None:
        super().__init__(message)
        self.raw_excerpt = str(raw_excerpt or "")[:1000]


@dataclass(frozen=True)
class MarkdownRegions:
    """Two independently repairable regions for generated Markdown files."""

    metadata_region: str
    body_region: str
    metadata_closed: bool


def split_markdown_regions(content: str) -> MarkdownRegions:
    """Split Markdown into metadata/frontmatter and body without mutating text."""
    text = (content or "").lstrip("\ufeff")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return MarkdownRegions("", text, False)
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return MarkdownRegions("".join(lines[: idx + 1]), "".join(lines[idx + 1:]), True)
    return MarkdownRegions(text, "", False)


def merge_markdown_regions(metadata_region: str, body_region: str) -> str:
    metadata = (metadata_region or "").rstrip() + "\n"
    body = (body_region or "").lstrip("\n")
    return metadata + ("\n" if body else "") + body


def markdown_failure_region(failure: Mapping[str, Any] | str) -> str:
    """Map a Markdown failure to the region that must be repaired."""
    failure_id = ""
    text = ""
    if isinstance(failure, Mapping):
        failure_id = str(failure.get("id") or failure.get("check_id") or "")
        text = json.dumps(failure, ensure_ascii=False, default=str)
    else:
        text = str(failure or "")
    combined = f"{failure_id}\n{text}".lower()
    if "frontmatter" in combined or "metadata" in combined:
        return "metadata_region"
    return "body_region"


def _infer_script_input_keys_from_blueprint(script_path: str, blueprint_text: str) -> list[str]:
    """Compatibility shim: Creator no longer guesses business argv keys.

    Input fields must come from SkillPlan.inputs, SKILL.md command JSON argv, or
    E2E trace payload/stdout. The generic fallback is a single payload object.
    """
    return ["payload"]


def _script_command_template(script_path: str, blueprint_text: str, entry: SkillPlanEntry | None = None) -> str:
    """Render the command template from SkillPlanEntry, the sole execution contract."""
    if entry is None:
        entry = _skill_plan_entry_for_file(file_path=script_path, blueprint_text=blueprint_text)
    return render_script_command_from_skill_plan(entry)


def _command_signature(command: str, script_path: str) -> dict[str, Any] | None:
    """Parse one standard Markdown bash command as a real script invocation.

    Creator 默认协议：
    - 只要求它是可解析 shell 命令；
    - runner 调用 scripts/*.py；
    - 脚本路径后必须传入一个可解析为 object 的 JSON argv；
    - 字段级参数对齐交给第二轮 E2E。
    """
    try:
        parts = shlex.split((command or "").strip(), posix=True)
    except ValueError:
        return None

    if len(parts) < 2:
        return None

    expected_script = script_path.replace("\\", "/").strip()
    runner = Path(parts[0]).name
    normalized_script = parts[1].replace("\\", "/").strip()

    if runner not in {"python", "python3"}:
        return None

    if normalized_script != expected_script:
        return None

    if not expected_script.startswith("scripts/") or Path(expected_script).suffix.lower() != ".py":
        return None

    args = parts[2:]

    json_payload: dict[str, Any] | None = None
    arg_mode = "no_args"

    if len(args) == 1:
        arg0 = args[0].strip()
        if arg0.startswith("{") and arg0.endswith("}"):
            try:
                parsed = _loads_templated_json_argv_object(arg0)
                if isinstance(parsed, dict):
                    json_payload = parsed
                    arg_mode = "json_arg"
                else:
                    arg_mode = "invalid_json_arg"
            except json.JSONDecodeError:
                arg_mode = "invalid_json_arg"
        elif arg0:
            arg_mode = "positional_args"
    elif args:
        if any(str(arg).startswith("-") for arg in args):
            arg_mode = "argparse_flags"
        else:
            arg_mode = "positional_args"

    placeholders: dict[str, str] = {}
    keys: set[str] = set()

    if json_payload is not None:
        keys = set(str(key) for key in json_payload.keys())
        for key, value in json_payload.items():
            if isinstance(value, str):
                match = re.fullmatch(r"\{\{\s*([A-Za-z_][\w.-]*)\s*\}\}", value.strip())
                placeholders[str(key)] = match.group(1) if match else value.strip()
            else:
                placeholders[str(key)] = ""

    return {
        "runner": runner,
        "script_path": expected_script,
        "args": args,
        "arg_mode": arg_mode,
        "json_payload": json_payload,
        "keys": keys,
        "placeholders": placeholders,
    }


_UNQUOTED_TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][\w.-]*)\s*\}\}")


def _replace_unquoted_json_template_placeholders(text: str) -> str:
    """Replace unquoted {{placeholder}} JSON-template values with null.

    Quoted placeholders are ordinary JSON strings and are preserved.
    """
    source = str(text or "")
    result: list[str] = []
    idx = 0
    in_string = False
    escape = False

    while idx < len(source):
        ch = source[idx]
        if in_string:
            result.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            idx += 1
            continue

        if ch == '"':
            in_string = True
            result.append(ch)
            idx += 1
            continue

        if source.startswith("{{", idx):
            end = source.find("}}", idx + 2)
            if end >= 0:
                token = source[idx:end + 2]
                if _UNQUOTED_TEMPLATE_PLACEHOLDER_RE.fullmatch(token):
                    result.append("null")
                    idx = end + 2
                    continue

        result.append(ch)
        idx += 1

    return "".join(result)


def _loads_templated_json_argv_object(text: str) -> Any:
    """Load a JSON argv template, allowing unquoted placeholders as values."""
    return json.loads(_replace_unquoted_json_template_placeholders(text))


def _command_template_equivalent(command: str, script_path: str, entry: SkillPlanEntry) -> bool:
    """Compare command blocks by normalized execution shape, not business payload.

    C 方案下，第一轮不要求 argv key 完全等于模板 key。
    """
    command_sig = _command_signature(command, script_path)
    template_sig = _command_signature(_script_command_template(script_path, "", entry), script_path)

    if not command_sig or not template_sig:
        return False

    if command_sig["runner"] != template_sig["runner"]:
        return False

    if command_sig["script_path"] != template_sig["script_path"]:
        return False

    return True



def _command_payload_object(command: str, script_path: str) -> dict[str, Any] | None:
    """Return JSON argv object for Creator default executable commands."""
    command_sig = _command_signature(command, script_path)
    if not command_sig:
        return None

    payload = command_sig.get("json_payload")
    if isinstance(payload, dict):
        return {str(key): value for key, value in payload.items()}

    return None


def _command_payload_keys(command: str, script_path: str) -> set[str] | None:
    """Return JSON argv keys passed to script_path, or None if unparsable/non-JSON."""
    payload = _command_payload_object(command, script_path)
    if payload is None:
        return None
    return set(payload.keys())


def _command_runtime_matches(command: str, script_path: str, entry: SkillPlanEntry) -> bool:
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    for idx, part in enumerate(parts):
        normalized = part.replace("\\", "/")
        if normalized == script_path or normalized.endswith("/" + script_path):
            runner = parts[idx - 1] if idx > 0 else ""
            if entry.runtime == "python":
                return Path(runner).name.startswith("python")
            if entry.runtime == "node":
                return Path(runner).name == "node"
            if entry.runtime == "bash":
                return Path(runner).name in {"bash", "sh"}
            if entry.runtime == "shell":
                return Path(runner).name in {"sh", "bash"}
            return True
    return False

def _check_command_block_contract(script_path: str, commands: list[str], entry: SkillPlanEntry) -> list[ContractCheckResult]:
    """Validate command blocks as workflow-local execution contracts.

    - Enforce parseable JSON argv and runtime consistency.
    - Do not enforce cross-step dataflow or exact SkillPlan input-key matching;
      E2E workflow validation owns argv/stdout handoff checks.
    """
    results: list[ContractCheckResult] = []

    for idx, command in enumerate(commands, start=1):
        command = command.strip()
        target = f"{script_path}#command-{idx}"

        command_sig = _command_signature(command, script_path)
        parsed_ok = command_sig is not None
        results.append(ContractCheckResult(
            id="command_block.signature.parseable",
            passed=parsed_ok,
            target=target,
            message="命令块可解析为 runner/script_path/JSON argv。" if parsed_ok else f"{script_path} 命令块无法解析为有效执行命令。",
            expected="命令块应形如：python scripts/name.py '{\"some_key\":\"{{some_key}}\"}'。",
            minimal_edit="保留脚本路径并确保 JSON argv 可解析；字段名可由 workflow 自由定义。",
        ))
        if not command_sig:
            continue

        runtime_matches = _command_runtime_matches(command, script_path, entry)
        results.append(ContractCheckResult(
            id="command_block.runtime.matches_skillplan",
            passed=runtime_matches,
            target=target,
            message="命令块 runner 与脚本 runtime 一致。" if runtime_matches else f"命令块 runner 与脚本 runtime={entry.runtime} 不一致。",
            expected="Python 用 python，Node 用 node，Bash/Shell 用 bash/sh；JSON keys 只需保持 workflow/脚本自洽。",
            minimal_edit="修正 runner 或脚本路径；不要仅因 SkillPlan.inputs 改写可运行 payload。",
        ))

        keys = _command_payload_keys(command, script_path)
        json_ok = keys is not None
        results.append(ContractCheckResult(
            id="command_block.json_argv.parseable",
            passed=json_ok,
            target=target,
            message="命令块使用可解析 JSON argv。" if json_ok else f"{script_path} 命令块必须在脚本路径后传入 JSON object argv。",
            expected="脚本路径后跟一个 JSON object argv；JSON keys 可由 workflow envelope 自由定义。",
            minimal_edit="确保 JSON 可解析；字段对齐由第二轮 E2E trace 定位。",
        ))

        # First-round file contracts stop at command syntax/runtime/JSON shape.
        # Exact argv key alignment with SkillPlan inputs is a workflow dataflow
        # concern and is validated during second-round E2E execution.

    return results

def _build_skill_md_contract_text(blueprint_text: str) -> str:
    """Hard-format authoring contract for SKILL.md.

    Semantic coverage is checked by model review.
    This contract only defines document format and fenced command norms.
    """
    return "\n".join([
        "必须满足以下 SKILL.md 合同（硬格式与平台边界）：",
        "",
        "A. YAML frontmatter:",
        "- 文件必须以 YAML frontmatter 开始。",
        "- frontmatter 必须包含 name 和 description。",
        "- frontmatter metadata 只能包含基础内容，例如 name、description；不得包含 workflow dataflow、内部字段、脚本 IO、循环/聚合职责。",
        "- frontmatter 在 metadata 后用 --- 关闭；不要要求文件末尾以 --- 结束。",
        "",
        "B. Markdown fenced block 规范:",
        "- 所有脚本执行命令必须使用标准 Markdown fenced code block。",
        "- shell 命令必须使用 ```bash 作为 info string，不要使用普通文本、行内代码或缩进代码块表达执行命令。",
        "- 机器可读 JSON 示例、配置、stdout 示例必须使用 ```json fenced code block。",
        "- 不要使用 '''bash 或 '''json；Markdown 标准 fence 使用三个反引号 ```。",
        "",
        "C. SKILL.md 正文边界:",
        "- 正文只负责说明 Skill 用途、用户输入、结果输出、高层 workflow 或可选分支、静态命令入口。",
        "- SKILL.md 不负责打通运行链路，不负责生成/转换中间字段，不负责从 stdout 提取子字段，不负责循环/聚合/映射/产物构造。",
        "- 如果存在分支，SKILL.md 可以描述分支或静态命令组；但实际运行时分支选择必须由平台显式支持，或由脚本完成。",
        "",
        "D. scripts 命令块标准:",
        "- 对蓝图真实规划的每个脚本，SKILL.md 应提供一个独立的 ```bash fenced code block。",
        "- 每个 ```bash block 内只能放一条真实 shell 命令。",
        "- 命令必须直接调用真实 scripts/*.py 路径。",
        "- 命令必须在脚本路径后直接传入一个完整、shell-quoted 的 JSON object 位置参数。",
        "- 该 JSON object 必须能被 json.loads 解析为 object；动态 placeholder 必须作为 JSON 字符串值出现。",
        "- 外部已有脚本若使用其它 CLI 风格，应先由包装脚本适配为上述输入 JSON 形式，再在 SKILL.md 调用该包装入口。",
        "- 不得固定套用 payload/user_request/fields/options/input_files 等模板字段。",
        "- 禁止在 ```bash block 内直接写 JSON 配置对象。",
        "- 禁止在 ```bash block 内写 runner/script/输入 JSON 伪命令对象。",
        "- 禁止在 ```bash block 内写说明文字、列表、多条命令或 `<真实参数>` 这类占位说明。",
        "",
        "D. workflow / 平台边界:",
        "- SKILL.md 应说明 Skill 用途、真实脚本调用顺序（如有）和最终产物类型，但第一轮不要求证明内部 stdout/placeholder 闭环。",
        "- 用户输入要区分必需项和可选项：依据蓝图语义中的可选、建议、若不指定、可以提供、默认等表达判断，不写固定业务字段词表。",
        "- 第一条 workflow command 不能引用平台输入 envelope 中不存在的 placeholder；只能引用 guaranteed input envelope 字段，或传入通用 user_request/input payload/envelope 由入口脚本解析。",
        "- 蓝图可选用户参数若平台 payload 没有同名字段，不应写成必填 placeholder；应由入口脚本内部提供默认值，或从 fields/options/payload 中存在则读取、不存在则默认。",
        "- 命令 placeholder 应从用户输入、显式字段、默认值、上传文件、前序 stdout 中选择当前脚本真正需要的值。",
        "- 第一轮不要求固定字段名；可建议字段名，但不能让字段名成为判错依据。",
        "- 不要固定特定中间字段名；内部脚本流转只在第二轮 E2E 真实执行时验证。",
        "- 多场景、多图片、多页 PDF 等循环应由脚本实现；SKILL.md 第一轮只需保持命令块静态可解析。",
        "",
        "E. references/assets:",
        "- references/*.md 为只读参考资料，可按需由相关脚本按路径只读加载，用于获取格式、布局、模板或规则说明。",
        "- references/*.md 不作为独立执行步骤，不被修改，不产出文件，不作为上传素材，也不作为最终 artifact。",
        "- 如果脚本不需要运行时读取 reference，也可以说明其内容已在脚本设计阶段被吸收为实现规范。",
        "- reference 正文不要全文塞进 SKILL.md。",
        "- assets/** 只能作为上传素材/静态资源引用，不能描述为模型生成。",
        "",
        "F. 禁止项:",
        "- 不输出 placeholder/mock/fake API。",
        "- 不要泄露 Creator 内部流程或 kernel references。",
        "- 不要把蓝图说明文字中的示例/反例路径当成真实文件计划。",
    ])

def _build_skill_md_e2e_authoring_guide(blueprint_text: str) -> str:
    """Build first-round static authoring guidance for SKILL.md.

    Despite the historical function name, this guide intentionally does not
    impose internal workflow dataflow.  First-round SKILL.md generation owns
    static Markdown/platform boundaries only; second-round E2E owns placeholder
    provenance, stdout field closure, and downstream parser alignment.
    """
    script_paths = _paths_requiring_skill_md_mentions(blueprint_text, prefix="scripts/")
    reference_paths = _paths_requiring_skill_md_mentions(blueprint_text, prefix="references/")

    if not script_paths:
        return (
            "SKILL.md first-round static authoring guide:\n"
            "- 当前蓝图没有 scripts/ 文件；SKILL.md 不要编造脚本命令块。\n"
            "- 若任务可直接回答，明确写“直接回答用户问题”，不要生成伪脚本流程。"
        )

    lines: list[str] = [
        "SKILL.md first-round static authoring guide（只约束静态格式和平台边界，不验证内部 dataflow）:",
        "A. 命令块静态形态:",
        "- 对蓝图真实规划的 scripts/ 文件，使用标准 Markdown 独立 ```bash fenced code block。",
        "- 每个 fence 内只放一条命令；命令必须直接调用 scripts/ 路径。",
        "- 脚本路径后直接传入一个完整、shell-quoted、json.loads 可解析为 object 的 JSON 位置参数；所有动态 {{placeholder}} 必须作为 JSON 字符串值出现。",
        "- 第一条命令只能引用平台 guaranteed input envelope 中存在的字段；结构化业务参数必须使用平台结构化输入 root 与图谱/schema 派生的目标字段组成整值占位符。",
        "- 命令 placeholder 优先引用 external envelope 字段：user_request、input、text、payload、input_files、files、resources、fields、options，或显式 input_binding。",
        "- 禁止在命令输入 JSON 中写动态用户内容、前序产物内容、运行时文件路径或 E2E seed 值；这些动态数据只能由图谱边派生的占位符表达。",
        "- 允许写入图谱/schema 明确声明为静态配置的 literal 常量；不得用 literal 冒充用户输入、stdout 或产物路径。",
        "- 蓝图语义为可选/建议/若不指定/可以提供/默认的用户参数，不要写成必填 placeholder；入口脚本应存在则读，不存在则默认化。",
        "- 后续命令只能引用由前序 stdout 字段和图谱边派生的占位符；不得写 literal 充当前序 stdout。",
        "- 第一轮不要证明后续 placeholder 来自前序 stdout；不要固定平台词表为内部字段；内部流转交给第二轮 E2E 执行验证。",
        "",
        "B. 资源边界:",
        "- references/ 是只读参考资料：可按需读取用于格式/模板/规则，但不替代主流程命令块，不作为产物或上传素材。",
        "- assets/ 只能作为上传素材/静态资源引用，不能描述为模型生成。",
        "",
        "C. 可用脚本路径与静态命令形态:",
    ]

    for idx, script_path in enumerate(script_paths, start=1):
        entry = _skill_plan_entry_for_file(file_path=script_path, blueprint_text=blueprint_text)
        runner = {"python": "python", "node": "node", "bash": "bash", "shell": "sh"}.get(entry.runtime, "python")

        input_keys = [
            str(item).strip()
            for item in (getattr(entry, "inputs", []) or [])
            if str(item).strip()
        ]

        bindings = [
            binding for binding in (getattr(entry, "command_arg_bindings", []) or [])
            if isinstance(binding, dict)
            and str(binding.get("argv_key") or "").strip()
            and str(binding.get("value_template") or "").strip()
        ]
        payload = {
            str(binding.get("argv_key")).strip(): str(binding.get("value_template")).strip()
            for binding in bindings
        }

        json_command = f"{runner} {script_path} {shlex.quote(json.dumps(payload, ensure_ascii=False, separators=(',', ':')))}"

        lines.extend([
            f"{idx}. {script_path}",
            f"   role: {entry.role}",
            f"   suggested inputs: {', '.join(input_keys) if input_keys else '无显式输入字段'}",
            "   command shape（只说明形态，实际参数必须由脚本真实接口决定）:",
            "```bash",
            json_command if payload else "# 待 E2E dataflow binding 修复：缺少 graph edge / command_arg_bindings 时不要发明输入字段。",
            "```",
            "   Creator 默认生成只使用上述输入 JSON 命令形态；外部已有 CLI 应由包装入口适配。",
        ])

    if reference_paths:
        lines.extend([
            "",
            "D. references:",
            "- SKILL.md 应在参考资料/资源小节逐字引用以下本地 reference，并说明何时读取：",
        ])
        for path in reference_paths:
            lines.append(f"- {path}")

    lines.extend([
        "",
        "E. 第二轮 E2E 责任边界:",
        "- placeholder 来源、前后脚本 stdout 字段闭环、最终 stdout 平台输出字段，不在第一轮 SKILL.md prompt 中证明。",
        "- 第二轮 E2E 会按真实运行链路严格检查已选择字段名是否对齐：输入字段、脚本读取字段、上游 stdout 字段、下游 placeholder 不能错位。",
        "- 如果这些内容不一致，第二轮 E2E 真实执行会基于实际 stdout/文件产物反馈修复 SKILL.md 或脚本。",
    ])

    return "\n".join(lines)

def _declared_skill_paths_from_blueprint(blueprint_text: str) -> set[str]:
    """Extract all skill-local paths declared in the blueprint.

    This must represent the generation plan, not files already on disk.
    SKILL.md is generated before scripts/references, so scripts/references
    mentioned by SKILL.md are valid as long as they are declared here.
    """
    text = blueprint_text or ""
    paths: set[str] = set()

    # 1. Existing parser path extraction.
    for prefix in ("scripts/", "references/", "assets/"):
        paths.update(_paths_requiring_skill_md_mentions(text, prefix=prefix))

    # 2. Explicit SkillPlan lines:
    # - path: `scripts/generate_story.py`
    # - path: scripts/generate_story.py
    for match in re.finditer(
        r"(?im)^\s*[-*]?\s*path\s*:\s*`?([A-Za-z0-9_.\-/]+)`?\s*$",
        text,
    ):
        path = match.group(1).strip().strip("`")
        if path.startswith(("scripts/", "references/", "assets/")):
            paths.add(path)

    # 3. Inline local resource paths anywhere in blueprint.
    for match in re.finditer(
        r"(?<![A-Za-z0-9_./-])((?:scripts|references|assets)/[A-Za-z0-9_.\-/]+)",
        text,
    ):
        path = match.group(1).strip().rstrip("`，,。；;:)）]}")
        if path.startswith(("scripts/", "references/", "assets/")):
            paths.add(path)

    # 4. Directory tree fallback:
    # love-skill/
    # ├── scripts/
    # │   ├── generate_story.py
    # ├── references/
    # │   └── output-patterns.md
    # └── assets/
    #     └── placeholder-logo.png
    current_dir: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()

        dir_match = re.search(r"(scripts|references|assets)/\s*$", line)
        if dir_match:
            current_dir = dir_match.group(1)
            continue

        file_match = re.search(r"(?:├──|└──|[-*])\s*([A-Za-z0-9_.-]+\.[A-Za-z0-9]+)\s*$", line)
        if file_match and current_dir:
            filename = file_match.group(1).strip()
            paths.add(f"{current_dir}/{filename}")

    return {p.replace("\\", "/").strip("/") for p in paths if p}


def _skill_local_paths_in_markdown(content: str) -> set[str]:
    return {match.group(1).strip() for match in _SKILL_FILE_PATH_RE.finditer(content or "")}


def _kernel_resource_leak_paths(content: str) -> list[str]:
    """Return explicit kernel/references paths mentioned in final Skill text."""
    seen: set[str] = set()
    paths: list[str] = []
    for match in _KERNEL_RESOURCE_LEAK_RE.finditer(content or ""):
        path = match.group(1).rstrip("`，,。；;:)）]")
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _normalize_similarity_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _kernel_reference_content_copy_paths(content: str) -> list[str]:
    """Detect large verbatim copying from kernel references without banning same names."""
    import difflib

    candidate = _normalize_similarity_text(content)
    if len(candidate) < 500:
        return []
    matches: list[str] = []
    kernel_refs = settings.kernel_path / "references"
    if not kernel_refs.is_dir():
        return []
    for ref in sorted(kernel_refs.glob("*.md")):
        try:
            kernel_text = _normalize_similarity_text(ref.read_text(encoding="utf-8"))
        except OSError:
            continue
        if len(kernel_text) < 500:
            continue
        matcher = difflib.SequenceMatcher(None, candidate, kernel_text, autojunk=False)
        longest = max((block.size for block in matcher.get_matching_blocks()), default=0)
        # A contiguous 500+ char copy is almost certainly a leaked reference;
        # for shorter kernel refs, also catch near-whole-file copies.
        copied_ratio = longest / max(1, min(len(candidate), len(kernel_text)))
        if longest >= 500 or (longest >= 300 and copied_ratio >= 0.60):
            matches.append(f"kernel/references/{ref.name}")
    return matches


def _existing_skill_local_paths_for_skill(skill_name: str) -> set[str]:
    skill_dir = settings.skills_path / skill_name
    paths: set[str] = set()
    if not skill_dir.exists():
        return paths
    for folder in ("scripts", "references", "assets"):
        folder_path = skill_dir / folder
        if not folder_path.is_dir():
            continue
        for child in folder_path.rglob("*"):
            if child.is_file():
                paths.add(child.relative_to(skill_dir).as_posix())
    return paths

def _skill_md_frontmatter_errors(content: str) -> list[str]:
    meta, _body, _had = parse_frontmatter(content)
    return validate_skill_frontmatter(meta)


def _has_valid_skill_md_frontmatter(content: str) -> bool:
    """Validate SKILL.md frontmatter using the Creator top-level schema."""
    return not _skill_md_frontmatter_errors(content)



def _canonicalize_markdown_frontmatter_for_file(
    *,
    file_path: str,
    content: str,
    skill_name: str = "",
    purpose: str = "",
) -> tuple[str, bool]:
    """Deterministically fix metadata schema errors by replacing frontmatter only."""
    if file_path == "SKILL.md":
        meta, _body, _had = parse_frontmatter(content)
        if not validate_skill_frontmatter(meta):
            return content, False
        canonical = canonicalize_skill_frontmatter(
            meta,
            default_name=skill_name,
            default_description=purpose or "Skill description",
        )
        patched = apply_frontmatter_patch(content, canonical)
        return patched, patched != content

    if file_path.startswith("references/") and Path(file_path).suffix.lower() == ".md":
        meta, _body, had = parse_frontmatter(content)
        if not had or not validate_reference_frontmatter(meta):
            return content, False
        canonical = canonicalize_reference_frontmatter(meta, file_path=file_path, purpose=purpose)
        patched = apply_frontmatter_patch(content, canonical)
        return patched, patched != content

    return content, False

def _check_skill_md_command_dataflow(content: str, blueprint_text: str) -> list[ContractCheckResult]:
    """Validate SKILL.md command placeholders against parsed SkillPlan dataflow."""
    try:
        parsed = parse_blueprint([{"role": "assistant", "content": blueprint_text}])
        entries = [entry for entry in (parsed.skill_plan.files if parsed.skill_plan else []) if entry.file_type == "script"]
    except Exception as exc:
        return [ContractCheckResult(
            id="skill_md.dataflow.plan_parseable",
            passed=False,
            target="SKILL.md",
            message=f"无法解析蓝图脚本数据流：{exc}",
            expected="蓝图应包含可解析的 SkillPlan / 文件职责计划。",
            minimal_edit="补充每个 scripts/* 的 role、inputs、outputs。",
        )]

    results: list[ContractCheckResult] = []
    produced: set[str] = set()
    consumed: set[str] = set()
    initial_user_inputs: set[str] = set(entries[0].inputs or []) if entries else set()
    available_values: set[str] = set(initial_user_inputs)

    for idx, entry in enumerate(entries, start=1):
        commands = _extract_script_command_templates(content, entry.path)
        if not commands:
            # A reference may intentionally hold command details; existing
            # command-existence checks decide that style, so dataflow skips it.
            continue
        placeholders = command_payload_placeholders(commands[0].strip().splitlines()[0], entry.path)
        if placeholders is None:
            results.append(ContractCheckResult(
                id="skill_md.dataflow.command_json_parseable",
                passed=False,
                target=entry.path,
                message=f"{entry.path} 命令无法解析 JSON argv，无法校验输入输出链路。",
                expected="命令必须向脚本传入 JSON object argv。",
                minimal_edit=f"改为 python {entry.path} '{{\"payload\":\"{{{{user_request}}}}\"}}' 形态。",
            ))
            continue
        for input_name in entry.inputs:
            placeholder = placeholders.get(input_name)
            passed = placeholder is not None
            if input_name in produced:
                passed = placeholder == input_name or placeholder in produced
            results.append(ContractCheckResult(
                id="skill_md.dataflow.input_available",
                passed=passed,
                target=f"{entry.path}:{input_name}",
                message=(
                    f"{entry.path} 输入 {input_name} 已通过 JSON argv 传递，且前序 stdout 依赖保持对齐。"
                    if passed
                    else f"{entry.path} 输入 {input_name} 未通过 JSON argv 或前序 stdout 字段正确传递。"
                ),
                expected="脚本 inputs 必须出现在命令 JSON argv 中；若 input 对应前序 stdout 字段，placeholder 必须引用该前序字段。",
                minimal_edit=(
                    f"为 {entry.path} 的 JSON argv 补齐 {input_name}；"
                    "如果该字段来自前序 stdout，请直接引用对应 stdout 字段 placeholder。"
                ),
                details={
                    "target_script": entry.path,
                    "input_name": input_name,
                    "placeholder": placeholder,
                    "upstream_available_outputs": sorted(produced),
                    "available_values": sorted(available_values),
                },
            ))
            if placeholder in produced:
                consumed.add(placeholder)

            unresolved_plan_input = idx > 1 and input_name not in available_values and (placeholder not in available_values if placeholder else True)
            if unresolved_plan_input:
                results.append(ContractCheckResult(
                    id="skill_plan.dataflow_unresolved",
                    passed=False,
                    target=f"{entry.path}:{input_name}",
                    message=f"{entry.path} 的 SkillPlan input {input_name} 无法从初始用户输入或前序 outputs 解析。",
                    expected="SkillPlan.inputs 必须来自 user_request/首步用户字段、references/assets 静态资源或前序 SkillPlan.outputs；后续 inputs/outputs 命名不能断链。",
                    minimal_edit="修 SkillPlan 或重新生成蓝图/文件计划；不要反复只修 SKILL.md 命令块。",
                    details={
                        "target_script": entry.path,
                        "input_name": input_name,
                        "available_values": sorted(available_values),
                        "upstream_available_outputs": sorted(produced),
                    },
                ))
        produced.update(entry.outputs or [])
        available_values.update(entry.outputs or [])

    final_outputs = set(_final_outputs_from_plan_entries(entries))
    for output in sorted(produced - consumed - final_outputs):
        results.append(ContractCheckResult(
            id="skill_md.dataflow.output_consumed_or_final",
            passed=False,
            target=output,
            message=f"脚本输出 {output} 既未被后续脚本引用，也不是最终输出。",
            expected="每个脚本 outputs 必须被后续命令引用，或属于最终结果 metadata。",
            minimal_edit="让后续脚本接收该 stdout 字段，或把它声明为最终输出。",
        ))

    return results





def _check_skill_md_contract(content: str, blueprint_text: str) -> list[ContractCheckResult]:
    """Hard format checks for SKILL.md.

    Deterministic checks here only enforce:
    - YAML frontmatter
    - no Creator/runtime leakage
    - declared local resources are structurally mentioned
    - scripts mentioned in SKILL.md are represented with ```bash fenced blocks
    - command argv is parseable JSON object

    不做语义覆盖裁决，不要求固定文档模板。
    """
    stripped = content.strip()
    results: list[ContractCheckResult] = []

    frontmatter_errors = _skill_md_frontmatter_errors(stripped)
    has_frontmatter = not frontmatter_errors
    structure_failures = _skill_md_body_structure_failures("SKILL.md", stripped)
    results.extend(ContractCheckResult(
        id=str(item.get("id") or "skill_md.markdown_body_structure"),
        passed=False,
        target="SKILL.md",
        message=str(item.get("message") or "SKILL.md frontmatter/body boundary invalid."),
        expected=str(item.get("expected") or "frontmatter 后必须有正文。"),
        minimal_edit=str(item.get("minimal_edit") or "重新生成格式正确的 SKILL.md。"),
        details=dict(item.get("details") or {}),
        layer="markdown_format",
    ) for item in structure_failures)

    results.append(ContractCheckResult(
        id="skill_md.frontmatter",
        passed=has_frontmatter and not structure_failures,
        target="SKILL.md",
        message=(
            "SKILL.md frontmatter 合格。"
            if has_frontmatter
            else "SKILL.md frontmatter 必须只包含允许的顶层 YAML 字段，并包含 name/description。错误：" + "; ".join(frontmatter_errors)
        ),
        expected="顶层 YAML 只允许 name、description、license、allowed-tools、metadata；Creator 规划字段只能放正文或 metadata.creator。",
        minimal_edit="只修正文件开头 YAML frontmatter；不改正文、workflow block、脚本路径或 scripts。",
    ))

    has_runtime_contract = bool(_SKILL_CUSTOM_RUNTIME_CONTRACT_RE.search(content))
    results.append(ContractCheckResult(
        id="skill_md.forbidden_runtime_contract",
        passed=not has_runtime_contract,
        target="SKILL.md",
        message=(
            "未包含自定义 Runtime Contract JSON 协议。"
            if not has_runtime_contract
            else "SKILL.md 不应包含自定义 Runtime Contract JSON 协议；请使用普通 Markdown 说明和 fenced command block。"
        ),
        expected="不要包含 Runtime Contract JSON。",
        minimal_edit="删除 Runtime Contract JSON/协议小节，改为普通 Markdown 说明和 ```bash 命令块。",
    ))

    has_creator_flow = bool(_CREATOR_FLOW_LEAK_RE.search(content))
    results.append(ContractCheckResult(
        id="skill_md.forbidden_creator_flow",
        passed=not has_creator_flow,
        target="SKILL.md",
        message=(
            "未包含 Creator 界面流程文案。"
            if not has_creator_flow
            else "SKILL.md 包含 Creator 界面流程/确认清单文本，这属于平台创建流程泄露。"
        ),
        expected="不要包含 Creator 创建流程、确认清单、点击开始创建等平台流程文案。",
        minimal_edit="删除 Creator UI/确认清单/点击开始创建相关文案，只保留 Skill 使用说明。",
    ))

    kernel_leak_paths = _kernel_resource_leak_paths(content)
    results.append(ContractCheckResult(
        id="skill_md.resource.no_kernel_leak",
        passed=not kernel_leak_paths,
        target="SKILL.md",
        message=(
            "SKILL.md 未引用 Creator 内部 kernel resources。"
            if not kernel_leak_paths
            else "SKILL.md 显式引用了 Creator 内部 kernel resources：" + ", ".join(kernel_leak_paths)
        ),
        expected="最终业务 SKILL.md 只能引用业务 Skill 本地 resources；不得引用 kernel/references 等内部资源。",
        minimal_edit="删除 kernel/references/... 内部 Creator 资源引用。",
        matched_paths=kernel_leak_paths,
    ))

    kernel_copy_paths = _kernel_reference_content_copy_paths(content)
    results.append(ContractCheckResult(
        id="skill_md.resource.no_kernel_content_copy",
        passed=not kernel_copy_paths,
        target="SKILL.md",
        message=(
            "SKILL.md 未大段复制 Creator kernel reference 内容。"
            if not kernel_copy_paths
            else "SKILL.md 大段复制了 Creator kernel reference 内容：" + ", ".join(kernel_copy_paths)
        ),
        expected="最终业务 SKILL.md 不得大段复制 kernel/references 中的 Creator 内部说明。",
        minimal_edit="删除复制的 kernel 内部说明，改写为面向该业务 Skill 的使用说明。",
        matched_paths=kernel_copy_paths,
    ))

    for reference_path in _paths_requiring_skill_md_mentions(blueprint_text, prefix="references/"):
        mentioned = _markdown_mentions_skill_resource_path(content, reference_path)
        results.append(ContractCheckResult(
            id="skill_md.reference.mentioned",
            passed=mentioned,
            target=reference_path,
            message=(
                f"SKILL.md 已结构化引用参考资料 {reference_path}。"
                if mentioned
                else f"SKILL.md 缺少对参考资料 {reference_path} 的结构化引用。"
            ),
            expected=(
                "蓝图真实规划的 references/ 资源必须在 SKILL.md 中被结构化提及；"
                "允许完整路径，也允许在同一 Markdown section 中出现 references/ 目录上下文和对应文件名。"
            ),
            minimal_edit=(
                f"只在 SKILL.md 的资源说明局部补充 `{reference_path}`；"
                "不要重写其它章节、脚本命令块或已通过内容。"
            ),
            details={"resource_path": reference_path},
        ))

    for asset_path in _paths_requiring_skill_md_mentions(blueprint_text, prefix="assets/"):
        mentioned = _markdown_mentions_skill_resource_path(content, asset_path)
        results.append(ContractCheckResult(
            id="skill_md.asset.mentioned",
            passed=mentioned,
            target=asset_path,
            message=(
                f"SKILL.md 已结构化引用静态资源 {asset_path}。"
                if mentioned
                else f"SKILL.md 缺少对静态资源 {asset_path} 的结构化引用。"
            ),
            expected=(
                "蓝图真实规划的 assets/ 资源必须在 SKILL.md 中被结构化提及；"
                "允许完整路径，也允许在同一 Markdown section 中出现 assets/ 目录上下文和对应文件名。"
            ),
            minimal_edit=(
                f"只在 SKILL.md 的资源说明局部补充 `{asset_path}`；"
                "不要重写其它章节、脚本命令块或已通过内容。"
            ),
            details={"resource_path": asset_path},
        ))

    results.extend(_check_skill_md_fenced_command_contracts(
        content=content,
        blueprint_text=blueprint_text,
        required_script_paths=None,
    ))

    return results



def _contract_layer_for_check_id(check_id: str) -> str:
    """Map deterministic validator check ids to repair layers."""
    cid = check_id or ""
    if cid.startswith("skill_md.frontmatter"):
        return "skill_md_metadata"
    if cid.startswith("skill_md.command_block") or cid.startswith("command_block"):
        return "skill_md_command_block"
    if cid.startswith("skill_md.reference"):
        return "reference_metadata"
    if cid.startswith("skill_md.asset"):
        return "asset_source_contract"
    if cid.startswith("skill_md.forbidden") or cid.startswith("skill_md.resource"):
        return "skill_md_intent_alignment"
    if cid.startswith("reference."):
        return "reference_body"
    if cid.startswith("asset."):
        return "asset_source_contract"
    if cid.startswith("script."):
        return "script_static_contract"
    return ""

def _format_contract_checks(results: list[ContractCheckResult], *, passed: bool) -> str:
    selected = [result for result in results if result.passed is passed]
    if not selected:
        return "- 无"
    lines: list[str] = []
    for result in selected:
        matched = f"\n  matched_paths: {', '.join(result.matched_paths)}" if result.matched_paths else ""
        details_payload = result.details
        if result.id == "reference.no_placeholder_phrases" and isinstance(result.details, dict):
            safe_matches = []
            for item in result.details.get("matches") or []:
                if isinstance(item, dict):
                    safe_matches.append({
                        "line_number": item.get("line_number"),
                        "context_hash": hashlib.sha256(str(item.get("context_excerpt") or "").encode("utf-8")).hexdigest()[:12],
                        "in_fenced_block": item.get("in_fenced_block"),
                    })
            details_payload = {"matches": safe_matches, "terms_policy": "matched terms are omitted from repair prompts and must not be written into the body"}
        details = (
            "\n  details: " + json.dumps(details_payload, ensure_ascii=False, sort_keys=True)
            if details_payload else ""
        )
        layer = result.layer or _contract_layer_for_check_id(result.id)
        layer_text = f" layer={layer}" if layer else ""
        message = result.message
        expected = result.expected
        minimal_edit = result.minimal_edit
        if result.id == "reference.no_placeholder_phrases":
            message = "reference 正文包含未完成状态说明。"
            expected = "reference 正文必须填入实际规则、示例或约束，不保留未完成状态说明。"
            minimal_edit = "删除未完成状态说明；保留已有有效内容。"
        lines.append(
            f"- {result.id} target={result.target}{layer_text}: {message}\n"
            f"  expected: {expected}\n"
            f"  minimal_edit: {minimal_edit}"
            f"{matched}"
            f"{details}"
        )
    return "\n".join(lines)


def _format_contract_failures(results: list[ContractCheckResult]) -> str:
    failed = [result for result in results if not result.passed]
    if not failed:
        return ""
    return (
        "SKILL.md contract 未通过：\n"
        + _format_contract_checks(failed, passed=False)
    )

def _format_contract_failures_safe(results: list[ContractCheckResult]) -> str:
    """Format contract failures without letting formatter bugs crash repair flow."""
    try:
        return _format_contract_failures(results)
    except Exception as exc:
        lines = [f"合同失败格式化异常：{exc}"]
        for result in results or []:
            try:
                if getattr(result, "passed", False):
                    continue
                lines.append(
                    f"- {getattr(result, 'id', 'unknown')}: "
                    f"target={getattr(result, 'target', '')}; "
                    f"message={getattr(result, 'message', '')}; "
                    f"expected={getattr(result, 'expected', '')}; "
                    f"minimal_edit={getattr(result, 'minimal_edit', '')}"
                )
            except Exception as inner_exc:
                lines.append(f"- 无法格式化某个失败项：{inner_exc}")
        return "\n".join(lines)

def _validate_skill_md_contract(content: str, blueprint_text: str) -> None:
    """Validate generated SKILL.md against blueprint-declared resources."""
    results = _check_skill_md_contract(content, blueprint_text)
    failed = [result for result in results if not result.passed]
    if failed:
        raise ContractValidationError(_format_contract_failures(results), results)


def _json_loads_loose_object(text: str) -> dict[str, Any]:
    """Parse a JSON object from model output.

    Accepts raw JSON, ```json fenced JSON, or text containing one JSON object.
    """
    raw = (text or "").strip()
    if not raw:
        return {}

    parsed = _parse_validator_json_object(raw)
    if isinstance(parsed, dict):
        return parsed

    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I).strip()
    raw = re.sub(r"\s*```$", "", raw).strip()

    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(raw[start:end + 1])
            return obj if isinstance(obj, dict) else {}
        except json.JSONDecodeError:
            return {}

    return {}


def _collect_blueprint_skillplan_constraints(
    *,
    blueprint_text: str,
    skill_plan_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect hard constraints that SKILL.md must reflect.

    This intentionally combines:
    - declared file paths parsed from blueprint text
    - SKILL.md FileSpecOut / SkillPlanEntry passed by frontend
    """
    declared_paths = sorted(_extract_declared_skill_paths(blueprint_text))
    declared_scripts = sorted(p for p in declared_paths if p.startswith("scripts/"))
    declared_references = sorted(p for p in declared_paths if p.startswith("references/"))
    declared_assets = sorted(p for p in declared_paths if p.startswith("assets/"))

    entry = skill_plan_entry or {}

    def as_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, tuple):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, str):
            return [v.strip() for v in re.split(r"[,，、\n]+", value) if v.strip()]
        return [str(value).strip()] if str(value).strip() else []

    required_capabilities = as_list(entry.get("required_capabilities"))
    forbidden_capabilities = as_list(entry.get("forbidden_capabilities"))
    inputs = as_list(entry.get("inputs"))
    outputs = as_list(entry.get("outputs"))
    dependencies = as_list(entry.get("dependencies"))
    reference_files = as_list(entry.get("reference_files") or entry.get("references"))

    for p in reference_files:
        if p.startswith("references/") and p not in declared_references:
            declared_references.append(p)

    for p in dependencies:
        if p.startswith("scripts/") and p not in declared_scripts:
            declared_scripts.append(p)
        if p.startswith("references/") and p not in declared_references:
            declared_references.append(p)
        if p.startswith("assets/") and p not in declared_assets:
            declared_assets.append(p)

    return {
        "declared_paths": sorted(set(declared_paths)),
        "declared_scripts": sorted(set(declared_scripts)),
        "declared_references": sorted(set(declared_references)),
        "declared_assets": sorted(set(declared_assets)),
        "required_capabilities": required_capabilities,
        "raw_capability_hints": list(required_capabilities or []),
        "forbidden_capabilities": forbidden_capabilities,
        "inputs": inputs,
        "outputs": outputs,
        "dependencies": dependencies,
        "reference_files": reference_files,
    }


def _deterministic_skill_md_blueprint_alignment_checks(
    *,
    content: str,
    blueprint_text: str,
    skill_plan_entry: dict[str, Any] | None = None,
) -> list[ContractCheckResult]:
    """Do not use regex to judge SKILL.md blueprint semantic alignment.

    SKILL.md 是否覆盖蓝图规划任务、是否误把示例路径当真实文件、
    capability 是否越界、workflow 是否完整，全部由模型审查。
    """
    return []


def _compact_requirement_graph_for_skill_md_review(raw_graph: Any) -> dict[str, Any]:
    """Keep only high-level responsibility facts for the SKILL.md semantic reviewer.

    Field-level edge transport is intentionally excluded. First-round command
    argv/key/value/source checks are handled later by per-block reviewers.
    """
    if raw_graph is None:
        return {"requirements": []}
    if hasattr(raw_graph, "model_dump"):
        raw_graph = raw_graph.model_dump(mode="json")
    if not isinstance(raw_graph, dict):
        return {"requirements": []}

    def trunc(value: Any, limit: int = 300) -> str:
        return str(value or "").strip()[:limit]

    def string_list(value: Any) -> list[str]:
        if isinstance(value, (list, tuple)):
            raw_items = list(value)
        elif value in (None, ""):
            raw_items = []
        else:
            raw_items = [value]
        return [text for text in (trunc(item) for item in raw_items) if text][:50]

    requirements: list[dict[str, Any]] = []
    raw_requirements = raw_graph.get("requirements")
    if not isinstance(raw_requirements, list):
        raw_requirements = []
    for item in raw_requirements[:50]:
        if hasattr(item, "model_dump"):
            item = item.model_dump(mode="json")
        if not isinstance(item, dict):
            continue
        target_file = trunc(item.get("target_file"))
        if not target_file:
            continue
        requirements.append({
            "target_file": target_file,
            "role": trunc(item.get("role")),
            "runtime": trunc(item.get("runtime")),
            "purpose": trunc(item.get("purpose")),
            "inputs": string_list(item.get("inputs")),
            "outputs": string_list(item.get("outputs")),
            "depends_on": string_list(item.get("depends_on")),
        })
    return {"requirements": requirements}


def _skill_md_overall_semantic_repair_scope(issue: dict[str, Any]) -> str:
    return (
        "只允许修改 SKILL.md 的说明文字、职责说明、文件清单、资源说明和最终产物说明；"
        "不得修改任何 ```bash/sh/shell fenced command block。"
    )


def _skill_md_command_block_repair_scope(script_path: str) -> str:
    return (
        f"只允许 exact-replace 修改当前失败的 {script_path} command block；"
        "不得修改其他 command block、SKILL.md 说明区域、scripts、references、assets 或责任图谱。"
    )


def _skill_md_block_locator(block: Any) -> dict[str, Any]:
    start = int(getattr(block, "start", -1))
    end = int(getattr(block, "end", -1))
    content = str(getattr(block, "content", "") or "")
    return {
        "start": start,
        "end": end,
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "content_excerpt": content[:1000],
    }


def _skill_md_reviewer_issue_contradicts_passed_true(issue: Any) -> bool:
    if not isinstance(issue, dict):
        return False
    severity = str(issue.get("severity") or "").strip().lower()
    return issue.get("blocking") is True or (severity == "error" and issue.get("blocking") is not False)


def _skill_md_reviewer_schema_error(data: Any) -> str:
    if not isinstance(data, dict) or not data:
        return "SKILL.md semantic reviewer did not return a JSON object."
    if "passed" not in data:
        return "SKILL.md semantic reviewer JSON missing required bool field passed."
    if not isinstance(data.get("passed"), bool):
        return "SKILL.md semantic reviewer JSON field passed must be bool."
    if "required_script_paths" in data and not isinstance(data.get("required_script_paths"), list):
        return "SKILL.md semantic reviewer JSON field required_script_paths must be list when present."
    if "required_reference_paths" in data and not isinstance(data.get("required_reference_paths"), list):
        return "SKILL.md semantic reviewer JSON field required_reference_paths must be list when present."
    if "required_asset_paths" in data and not isinstance(data.get("required_asset_paths"), list):
        return "SKILL.md semantic reviewer JSON field required_asset_paths must be list when present."
    if "reviewers" in data and not isinstance(data.get("reviewers"), dict):
        return "SKILL.md semantic reviewer JSON field reviewers must be object when present."
    if "issues" in data and not isinstance(data.get("issues"), list):
        return "SKILL.md semantic reviewer JSON field issues must be list when present."
    if "repair_suggestions" in data and not isinstance(data.get("repair_suggestions"), str):
        return "SKILL.md semantic reviewer JSON field repair_suggestions must be string when present."
    if data.get("passed") is True:
        for issue in data.get("issues") or []:
            if _skill_md_reviewer_issue_contradicts_passed_true(issue):
                return "SKILL.md semantic reviewer protocol contradiction: passed=true with blocking/error issue."
        reviewers = data.get("reviewers") or {}
        if isinstance(reviewers, dict):
            for reviewer_name, reviewer_result in reviewers.items():
                if not isinstance(reviewer_result, dict):
                    continue
                if reviewer_result.get("passed") is False:
                    return f"SKILL.md semantic reviewer protocol contradiction: passed=true with nested reviewer passed=false: {reviewer_name}."
                for issue in reviewer_result.get("issues") or []:
                    if _skill_md_reviewer_issue_contradicts_passed_true(issue):
                        return "SKILL.md semantic reviewer protocol contradiction: passed=true with nested blocking/error issue."
    return ""




def _collect_skill_md_review_script_paths(
    *,
    blueprint_text: str,
    skill_plan_entry: dict[str, Any] | None = None,
    requirement_graph: Any = None,
) -> list[str]:
    """Collect true script paths from blueprint, SkillPlan-like payloads, and graph facts."""
    paths: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        path = str(value or "").replace("\\", "/").strip().strip("`")
        if not path.startswith("scripts/") or not path.endswith(".py") or path in seen:
            return
        seen.add(path)
        paths.append(path)

    for path in _extract_declared_skill_paths(blueprint_text):
        add(path)

    def walk_skill_plan(value: Any) -> None:
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        if isinstance(value, dict):
            add(value.get("path") or value.get("target_file") or value.get("entrypoint"))
            for key in ("files", "skill_plan", "dependencies", "scripts", "steps", "items", "requirements"):
                child = value.get(key)
                if isinstance(child, (dict, list, tuple)):
                    walk_skill_plan(child)
                elif isinstance(child, str):
                    add(child)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                walk_skill_plan(item)
        elif isinstance(value, str):
            add(value)

    walk_skill_plan(skill_plan_entry or {})

    raw_graph = requirement_graph
    if hasattr(raw_graph, "model_dump"):
        raw_graph = raw_graph.model_dump(mode="json")
    if isinstance(raw_graph, dict):
        for item in raw_graph.get("requirements") or raw_graph.get("function_items") or []:
            if hasattr(item, "model_dump"):
                item = item.model_dump(mode="json")
            if isinstance(item, dict):
                add(item.get("target_file") or item.get("path"))
        for edge in raw_graph.get("dataflow_edges") or []:
            if isinstance(edge, dict):
                add(edge.get("from_node"))
                add(edge.get("to_node"))

    return paths

def _skill_md_script_interface_context_for_review(
    *,
    skill_name: str,
    script_paths: Iterable[str],
    requirement_graph: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Project existing script argv contracts and local graph facts for SKILL.md review.

    This is not a standalone validator and does not infer or rewrite command
    argv mappings. It gives the existing SKILL.md review model the same factual
    script interface + FunctionItem edge context used during generation, so it
    can block only when the evidence is explicit.
    """
    try:
        skill_dir = settings.skills_path / _validate_skill_name(skill_name)
    except Exception:
        return []

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_path in script_paths or []:
        script_path = str(raw_path or "").replace("\\", "/").strip().strip("`")
        if (
            not script_path.startswith("scripts/")
            or not script_path.endswith(".py")
            or script_path in seen
        ):
            continue
        seen.add(script_path)

        abs_path = skill_dir / script_path
        if not abs_path.is_file():
            continue

        try:
            content = abs_path.read_text(encoding="utf-8")
            schema = extract_python_strict_argv_schema(content)
        except Exception as exc:
            content = ""
            schema = {"error": f"{type(exc).__name__}: {exc}"}

        try:
            run_analysis = _python_run_args_analysis(content)
        except Exception:
            run_analysis = {}

        try:
            function_execution_context = build_function_execution_context(
                graph=requirement_graph,
                target_file=script_path,
            )
        except Exception as exc:
            function_execution_context = {"error": f"{type(exc).__name__}: {exc}"}

        items.append({
            "script_path": script_path,
            "strict_json_argv_schema": schema,
            "run_args_analysis": run_analysis,
            "function_execution_context": function_execution_context,
        })

    return items

async def _review_skill_md_blueprint_intent_with_model(
    *,
    skill_name: str,
    content: str,
    blueprint_text: str,
    skill_plan_entry: dict[str, Any] | None,
    requirement_graph: dict[str, Any] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Model review for first-round SKILL.md semantic coverage.

    第一轮只判断 SKILL.md 是否基本表达用户需求和执行入口；
    证明性细节、内部 manifest 字段和运行时闭环交给后续静态/E2E 校验。
    """
    constraints = _collect_blueprint_skillplan_constraints(
        blueprint_text=blueprint_text,
        skill_plan_entry=skill_plan_entry,
    )
    graph_context = _compact_requirement_graph_for_skill_md_review(requirement_graph)

    route = route_model(
        VALIDATOR_TASK,
        requested_model=model,
        reason=f"creator SKILL.md semantic coverage review: {skill_name}",
    )
    _log_creator_model_usage(
        phase="skill_md.intent_review.route",
        file_path="SKILL.md",
        route=route,
        model=model,
        skill_name=skill_name,
    )

    parser_paths = _extract_declared_skill_paths(blueprint_text)

    prompt = (
        "你是 superskills Creator 的第一轮 SKILL.md 语义覆盖审查器，只输出严格 JSON object。\n\n"

        "审查目标：只回答 SKILL.md 整体语义是否符合用户需求和蓝图责任。\n"
        "第一轮整体语义只看：这个 skill 是做什么的、用户输入是什么、大致执行哪些脚本、文件计划是否完整、脚本职责和执行顺序是否合理、references/assets 职责是否正确、最终产物是什么、是否引入蓝图外能力或文件。\n"
        "不得审查 command JSON key、placeholder、argv schema、stdout 字段、incoming/outgoing edge 字段映射、类型序列化、静态值或动态值来源。\n"
        "不要审查证明是否足够细；不要审查第二轮 E2E 才能通过真实执行发现的问题。\n\n"

        "输出 error 必须满足以下任一条件：\n"
        "1. 该问题会导致用户无法理解或无法启动这个 Skill，且不能交给第二轮 E2E 验证；\n"
        "2. 文件计划、资源角色、最终产物或蓝图外能力存在明显整体语义错误。\n"
        "不满足以上条件时，必须 passed=true 或最多输出 warning；warning 不进入修复。\n\n"

        "只有以下明显语义责任失败才能 error：\n"
        "- SKILL.md 明显无法表达用户需求或主流程；\n"
        "- 真实 scripts/references/assets 路径缺失；\n"
        "- 最终产物缺失；\n"
        "- reference/asset 角色明显写反；\n"
        "- 引入蓝图外能力、脚本、资源、外部 API、伪 key、伪数据库或 Creator UI 流程；\n"

        "以下情况必须 passed=true 或最多 warning，不能 error，不能提出 blocking repair：\n"
        "- 说明不够细、资源用途说法不够精确、只是想优化措辞；\n"
        "- command JSON key、placeholder、argv schema、stdout 字段、edge 字段映射、类型序列化、静态值或动态值来源问题；\n"
        "- 没证明哪个脚本读取哪个 reference/asset；\n"
        "- 没写 role/source/dependencies/bundled 等内部 manifest 字段；\n"
        "- 触发词、章节模板、固定话术没有逐字一致。\n\n"
        "不要为了让文案更精确而提出 blocking repair；不要要求 SKILL.md 写内部 manifest 字段。\n"
        "整体语义失败时，repair 只能定位说明文字、职责说明、文件清单、资源说明和最终产物说明；不得要求修改 bash command block。\n\n"
        "结构化 issue 字段规范：\n"
        "- blocking 可选；若该问题不影响资源角色/最终产物契约/用户关键要求传递，必须明确 blocking=false。\n"
        "- contract_impact 可选 object；只用布尔字段表达是否影响 resource_role/final_artifact/user_requirement_transfer。\n"
        "- resource_role 仅在资源职责问题时填写 reference|asset，否则可省略。\n"
        "- category 可选；不得使用 command_mapping_explicit_evidence，整体 reviewer 不处理 command mapping。\n"
        "- repair_ops 可选；只能定位非 command 说明区域，不能把自然语言 minimal_edit 当 repair_ops。\n\n"

        "真实文件和资源角色判断原则：\n"
        "- 出现在目录结构、SkillPlan path、dependencies、reference_files、asset_source 中的路径是真实文件。\n"
        "- 出现在“例如/示例/反例/不要这样写/禁止”等语境中的路径不是实际文件，除非也出现在目录结构或 SkillPlan path 中。\n"
        "- reference 是只读参考资源：可被参考，但不能作为执行步骤、不能修改、不能作为 artifact、不能作为 asset。\n"
        "- assets 只有在蓝图要求上传或存在实际 asset 时才严格校验，空 assets 不触发固定话术要求。\n\n"

        "返回格式必须是：\n"
        "{\n"
        '  "passed": true,\n'
        '  "required_script_paths": ["scripts/example.py"],\n'
        '  "required_reference_paths": ["references/example.md"],\n'
        '  "required_asset_paths": ["assets/example.png"],\n'
        '  "reviewers": {\n'
        '    "intent_reviewer": {"passed": true, "issues": []},\n'
        '    "file_plan_reviewer": {"passed": true, "issues": []},\n'
        '    "workflow_reviewer": {"passed": true, "issues": []},\n'
        '    "capability_reviewer": {"passed": true, "issues": []},\n'
        '    "resource_reviewer": {"passed": true, "issues": []},\n'
        '    "user_facing_reviewer": {"passed": true, "issues": []}\n'
        "  },\n"
        '  "issues": [\n'
        '    {\n'
        '      "severity": "error|warning",\n'
        '      "blocking": true,\n'
        '      "contract_impact": {"execution_closure": false, "resource_role": false, "platform_io": false, "final_artifact": false, "user_requirement_transfer": false},\n'
        '      "category": "semantic_alignment|null",\n'
        '      "field": "intent|file_plan|workflow|capabilities|resources|user_facing",\n'
        '      "message": "不一致点",\n'
        '      "evidence": "引用 SKILL.md 或蓝图中的证据",\n'
        '      "expected": "应当如何与蓝图一致",\n'
        '      "minimal_edit": "只修改 SKILL.md 的哪个区域，不要整文件重写",\n'
        '      "resource_role": "reference|asset|null",\n'
        '      "claim_type": "forbid_read|execution_step|artifact|asset_material|model_generated|modifiable|write_asset|null",\n'
        '      "repair_ops": [{"op": "replace|delete|append_after|append_before", "anchor": "当前文件中唯一定位的原文", "text": "追加文本", "replacement": "替换文本"}]\n'
        '    }\n'
        "  ],\n"
        '  "repair_suggestions": "给修复模型的最小局部编辑建议"\n'
        "}\n\n"

        f"Skill 名称：{skill_name}\n\n"

        "【蓝图约束 JSON，供参考；如和蓝图原文语境冲突，以蓝图原文为准】\n"
        f"{json.dumps(constraints, ensure_ascii=False, indent=2, default=str)[:12000]}\n\n"

        "【解析器提取路径，供参考；不是最终裁决】\n"
        f"{json.dumps(parser_paths, ensure_ascii=False, indent=2, default=str)}\n\n"

        "【compact requirement_graph 上下文，仅用于大致理解流程；不得用于阻断跨步骤精确字段/placeholder 来源】\n"
        f"{json.dumps(graph_context, ensure_ascii=False, indent=2, default=str)[:12000]}\n\n"

        "【蓝图原文】\n"
        f"{(blueprint_text or '')[-18000:]}\n\n"

        "【待审查 SKILL.md】\n"
        f"{(content or '')[-22000:]}\n"
    )

    base_messages = [
        {
            "role": "system",
            "content": (
                "你是严格 JSON 输出的第一轮 SKILL.md 语义覆盖审查器。"
                "只输出 JSON object，不要输出 Markdown。"
            ),
        },
        {"role": "user", "content": prompt},
    ]

    raw = ""
    data: dict[str, Any] | None = None
    for review_attempt in range(3):
        active_messages = base_messages if review_attempt == 0 else [
            *base_messages,
            {
                "role": "user",
                "content": (
                    "上一轮结论只做 JSON schema 格式重写，不重新审查 SKILL.md，不修改语义结论。\n"
                    "SKILL.md content 不变，blueprint 不变；请严格按当前 reviewer 已定义的 JSON schema 返回 JSON object，"
                    "不要输出 Markdown 或解释。\n"
                    f"上一轮 raw output excerpt：{str(raw or '')[:1200]}"
                ),
            },
        ]
        raw = await complete_chat_once(active_messages, route.model)
        parsed = _json_loads_loose_object(raw)
        schema_error = _skill_md_reviewer_schema_error(parsed)
        if not schema_error:
            data = parsed
            break
        if review_attempt < 2:
            raw = json.dumps({"schema_error": schema_error, "raw": parsed if isinstance(parsed, dict) else str(raw or "")[:1200]}, ensure_ascii=False, default=str)
            continue

    if not isinstance(data, dict) or not data:
        raise CreatorValidatorReviewError(
            "蓝图一致性审查模型连续 3 次未返回有效 JSON object/schema；这是 validator failure，不应进入 SKILL.md 内容返修。",
            raw_excerpt=str(raw or "")[:1000],
        )

    data.setdefault("passed", False)
    data.setdefault("required_script_paths", [])
    data.setdefault("required_reference_paths", [])
    data.setdefault("required_asset_paths", [])
    data.setdefault("reviewers", {})
    data.setdefault("issues", [])
    data.setdefault("repair_suggestions", "")

    if not data["issues"]:
        reviewer_issues: list[dict[str, Any]] = []
        for reviewer_name, reviewer_result in data["reviewers"].items():
            if not isinstance(reviewer_result, dict):
                continue
            if reviewer_result.get("passed") is False:
                for issue in reviewer_result.get("issues") or []:
                    if isinstance(issue, dict):
                        reviewer_issues.append({
                            "severity": issue.get("severity", "error"),
                            "field": issue.get("field", reviewer_name),
                            "message": issue.get("message", f"{reviewer_name} 审查未通过。"),
                            "evidence": issue.get("evidence", ""),
                            "expected": issue.get("expected", "该审查角度应与蓝图一致。"),
                            "minimal_edit": issue.get("minimal_edit", "只修改 SKILL.md 中相关区域。"),
                            "resource_role": issue.get("resource_role"),
                            "claim_type": issue.get("claim_type"),
                            "repair_ops": issue.get("repair_ops") if isinstance(issue.get("repair_ops"), list) else [],
                        })
                    else:
                        reviewer_issues.append({
                            "severity": "error",
                            "field": reviewer_name,
                            "message": str(issue),
                            "evidence": str(issue),
                            "expected": "该审查角度应与蓝图一致。",
                            "minimal_edit": "只修改 SKILL.md 中相关区域。",
                        })
        data["issues"] = reviewer_issues

    data["issues"] = _dedupe_review_issues(data["issues"])
    if data.get("passed") is not True and any(str(issue.get("severity") or "error").lower() in {"error", "blocking", "blocker"} for issue in data["issues"] if isinstance(issue, dict)):
        data["passed"] = False

    # 如果顶层 passed=false 但没有 issues，补一个可返修错误，避免只报空失败。
    if data.get("passed") is False and not data["issues"]:
        data["issues"].append({
            "severity": "error",
            "field": "blueprint_alignment",
            "message": "模型判定 SKILL.md 与蓝图不一致，但未返回具体 issue。",
            "evidence": "passed=false with empty issues",
            "expected": "SKILL.md 必须覆盖蓝图真实业务意图、文件计划、资源使用和 workflow 说明。",
            "minimal_edit": "检查 SKILL.md 的用途说明、资源说明、脚本顺序和最终产物说明，只修改不一致区域。",
        })

    def _normalize_paths(raw_paths: Any, prefix: str) -> list[str]:
        if not isinstance(raw_paths, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for item in raw_paths:
            path = str(item or "").replace("\\", "/").strip().strip("`")
            if not path.startswith(prefix):
                continue
            if path in seen:
                continue
            seen.add(path)
            out.append(path)
        return out

    data["required_script_paths"] = _normalize_paths(data["required_script_paths"], "scripts/")
    data["required_reference_paths"] = _normalize_paths(data["required_reference_paths"], "references/")
    data["required_asset_paths"] = _paths_requiring_skill_md_mentions(blueprint_text, prefix="assets/")

    return data

def _skill_md_blueprint_review_to_contract_results(
    review: dict[str, Any],
) -> list[ContractCheckResult]:
    """Convert SKILL.md model blueprint review into local repairable failures.

    这些 failures 会进入现有 generate-file repair loop，
    由 _repair_generated_file_with_feedback 走 exact_replace 局部 patch。
    """
    if not isinstance(review, dict):
        return [ContractCheckResult(
            id="skill_md.blueprint_alignment.invalid_review",
            passed=False,
            target="SKILL.md:blueprint_alignment",
            message="SKILL.md 蓝图一致性审查结果不是 JSON object。",
            expected="审查器必须返回结构化 JSON；SKILL.md 必须与蓝图真实意图一致。",
            minimal_edit="只修改 SKILL.md 中与蓝图意图不一致的最小区域。",
            details={"review_type": type(review).__name__},
            layer="skill_md_blueprint_alignment",
        )]

    issues: list[Any] = []
    raw_issues = review.get("issues")
    if isinstance(raw_issues, list):
        issues.extend(raw_issues)

    reviewers = review.get("reviewers")
    if not issues and isinstance(reviewers, dict):
        for reviewer_name, reviewer_result in reviewers.items():
            if not isinstance(reviewer_result, dict):
                continue
            if reviewer_result.get("passed") is not False:
                continue
            for issue in reviewer_result.get("issues") or []:
                if isinstance(issue, dict):
                    merged = dict(issue)
                    merged.setdefault("field", reviewer_name)
                    issues.append(merged)
                else:
                    issues.append({
                        "severity": "error",
                        "field": reviewer_name,
                        "message": str(issue),
                        "evidence": str(issue),
                    })

    if review.get("passed") is False and not issues:
        issues.append({
            "severity": "error",
            "field": "blueprint_alignment",
            "message": "SKILL.md 与蓝图不一致，但模型未返回具体 issue。",
            "evidence": "passed=false with empty issues",
            "expected": "SKILL.md 应覆盖蓝图真实业务意图、文件计划、资源说明、workflow 和最终产物。",
            "minimal_edit": "只修改 SKILL.md 中缺失或偏离蓝图的区域。",
        })

    issues = _dedupe_review_issues(issues)
    results: list[ContractCheckResult] = []
    for idx, issue in enumerate(issues, start=1):
        if not isinstance(issue, dict):
            issue = {
                "severity": "error",
                "field": "blueprint_alignment",
                "message": str(issue),
                "evidence": str(issue),
            }

        severity = str(issue.get("severity") or "error").strip().lower()

        if not _review_issue_is_blocking(issue):
            continue

        field_name = str(issue.get("field") or "blueprint_alignment").strip() or "blueprint_alignment"
        safe_field = re.sub(r"[^a-zA-Z0-9_.-]+", "_", field_name).strip("_") or "blueprint_alignment"

        message = str(
            issue.get("message")
            or issue.get("problem")
            or issue.get("reason")
            or "SKILL.md 与蓝图要求不一致。"
        ).strip()

        evidence = str(issue.get("evidence") or issue.get("details") or "").strip()
        expected = str(
            issue.get("expected")
            or "SKILL.md 必须与蓝图真实业务意图、文件计划、资源说明和 workflow 保持一致。"
        ).strip()
        minimal_edit = str(
            issue.get("minimal_edit")
            or issue.get("fix")
            or issue.get("suggested_fix")
            or "只修改 SKILL.md 中与该蓝图不一致相关的小节、列表项；不得修改 bash command block。"
        ).strip()
        minimal_edit = f"{minimal_edit}\n{_skill_md_overall_semantic_repair_scope(issue)}"

        results.append(ContractCheckResult(
            id=f"skill_md.blueprint_alignment.{safe_field}.{idx}",
            passed=False,
            target=f"SKILL.md:{field_name}",
            message=message + (f"\nevidence: {evidence}" if evidence else ""),
            expected=expected,
            minimal_edit=minimal_edit,
            details={
                "review": review,
                "issue": issue,
                "field": field_name,
                "severity": severity,
                "repair_suggestions": str(review.get("repair_suggestions") or ""),
            },
            layer="skill_md_blueprint_alignment",
        ))

    return results



def _skill_md_block_check_failed(check: Any) -> bool:
    if not isinstance(check, dict):
        return True
    if check.get("passed") is False:
        return True
    result = str(check.get("result") or check.get("status") or "").strip().lower()
    return result in {"fail", "failed", "error", "blocking", "invalid", "missing"}


def _skill_md_block_check_schema_error(check: Any, *, check_type: str, index: int) -> str:
    if not isinstance(check, dict):
        return f"{check_type}_checks[{index}] must be object."
    if not any(str(check.get(key) or "").strip() for key in ("object", "check_object", "target", "field", "key", "value_path", "subject", "path")):
        return f"{check_type}_checks[{index}] missing checked object."
    if "passed" not in check and not str(check.get("result") or check.get("status") or "").strip():
        return f"{check_type}_checks[{index}] missing result."
    if "passed" in check and not isinstance(check.get("passed"), bool):
        return f"{check_type}_checks[{index}] passed must be bool when present."
    if not str(check.get("evidence") or "").strip():
        return f"{check_type}_checks[{index}] missing evidence."
    return ""


def _skill_md_block_review_schema_error(data: Any) -> str:
    if not isinstance(data, dict) or not data:
        return "SKILL.md block reviewer did not return a JSON object."
    if not isinstance(data.get("passed"), bool):
        return "SKILL.md block reviewer field passed must be bool."
    if not str(data.get("target_script_path") or "").strip():
        return "SKILL.md block reviewer missing target_script_path."
    for check_type in ("key", "value", "type"):
        key = f"{check_type}_checks"
        checks = data.get(key)
        if not isinstance(checks, list):
            return f"SKILL.md block reviewer field {key} must be list."
        for index, check in enumerate(checks):
            error = _skill_md_block_check_schema_error(check, check_type=check_type, index=index)
            if error:
                return error
    issues = data.get("issues")
    if issues is None:
        data["issues"] = []
        issues = data["issues"]
    if not isinstance(issues, list):
        return "SKILL.md block reviewer field issues must be list."
    if "repair_suggestions" in data and not isinstance(data.get("repair_suggestions"), str):
        return "SKILL.md block reviewer field repair_suggestions must be string when present."
    if data.get("passed") is True:
        failed_checks = [
            check
            for key in ("key_checks", "value_checks", "type_checks")
            for check in (data.get(key) or [])
            if _skill_md_block_check_failed(check)
        ]
        if failed_checks:
            return "SKILL.md block reviewer protocol contradiction: passed=true with failed check."
        for issue in issues:
            if _skill_md_reviewer_issue_contradicts_passed_true(issue):
                return "SKILL.md block reviewer protocol contradiction: passed=true with blocking/error issue."
    return ""


def _skill_md_block_review_failed_checks(review: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    failed: list[tuple[str, dict[str, Any]]] = []
    for check_type in ("key", "value", "type"):
        for check in review.get(f"{check_type}_checks") or []:
            if _skill_md_block_check_failed(check):
                failed.append((check_type, dict(check)))
    return failed


async def _review_skill_md_command_block_with_model(
    *,
    skill_name: str,
    script_path: str,
    command_block: str,
    ordinal: int,
    prior_stdout: list[str],
    requirement_graph: Any,
    model: str | None = None,
) -> dict[str, Any]:
    """Review exactly one real SKILL.md script command block.

    The context is deliberately local: one script path, one command block, that
    script's argv/read facts, local graph context, incoming edges, prior stdout,
    and allowed platform/static sources. It never receives the full SKILL.md,
    full graph, or unrelated script interfaces.
    """
    script_context = _skill_md_script_interface_context_for_review(
        skill_name=skill_name,
        script_paths=[script_path],
        requirement_graph=requirement_graph,
    )
    local = script_context[0] if script_context else {
        "script_path": script_path,
        "strict_json_argv_schema": {},
        "run_args_analysis": {},
        "function_execution_context": {},
    }
    function_context = local.get("function_execution_context") or {}
    incoming_edges = function_context.get("incoming_edges") if isinstance(function_context, dict) else []
    payload = {
        "script_path": script_path,
        "command_block": command_block,
        "ordinal": ordinal,
        "argv_schema": local.get("strict_json_argv_schema") or {},
        "run_args_analysis": local.get("run_args_analysis") or {},
        "local_graph": {
            "function_item": (function_context or {}).get("function_item", {}) if isinstance(function_context, dict) else {},
            "outgoing_edges": (function_context or {}).get("outgoing_edges", []) if isinstance(function_context, dict) else [],
        },
        "incoming_edges": incoming_edges or [],
        "prior_available_stdout": prior_stdout,
        "available_sources": {
            "platform_input_envelope": ["user_request", "input", "text", "payload", "fields", "options", "input_files", "files", "resources"],
            "incoming_edges": incoming_edges or [],
            "references_assets_static": ["references/*", "assets/*"],
            "legal_static_sources": ["literal_default", "runtime_constant", "script_default", "reference_file", "asset_file"],
        },
    }
    route = route_model(
        VALIDATOR_TASK,
        requested_model=model,
        reason=f"creator SKILL.md single command block review: {script_path}",
    )
    _log_creator_model_usage(
        phase="skill_md.command_block_review.route",
        file_path="SKILL.md",
        route=route,
        model=model,
        skill_name=skill_name,
    )
    logger.info("[Creator][skill_md][block_review] skill=%s script=%s ordinal=%d", skill_name, script_path, ordinal)
    prompt = (
        "你是 Creator 第一轮 SKILL.md 单 command block 接口审查器，只输出严格 JSON object。\n"
        "你一次只审查当前 script_path 的当前 command_block；不得审查完整 SKILL.md 或其他脚本。\n"
        "分别判断：1) JSON key 是否符合目标脚本接口；2) JSON value 是否来自当前局部可用来源；3) value 类型和序列化是否兼容。\n"
        "不得因为图谱字段名与脚本 argv key 名不同就要求修改 argv key；不得把 optional key 判断为不存在；不得要求所有 value 都必须使用 placeholder；不得自行创造平台输入字段、图谱字段或 runtime sentinel。\n"
        "失败时 repair_scope 必须是只修改当前 command block；不得修改其他 block、说明区域、scripts、references、assets 或图谱。\n"
        "返回格式固定为：{\"passed\": true, \"target_script_path\": \"scripts/x.py\", \"key_checks\": [], \"value_checks\": [], \"type_checks\": [], \"issues\": [], \"repair_suggestions\": \"\"}\n"
        "每个 key_checks/value_checks/type_checks item 必须包含 object、passed、evidence；失败项可包含 message/expected/minimal_edit/blocking。\n"
    )
    base_messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
    ]
    raw = ""
    last_schema_error = ""
    for review_attempt in range(3):
        active_messages = base_messages if review_attempt == 0 else [
            *base_messages,
            {
                "role": "user",
                "content": (
                    "上一轮输出只做 JSON 格式/schema 重写，不重新审查 command block，不改变上一轮语义结论。\n"
                    "请保留上一轮对 key_checks/value_checks/type_checks/issues/repair_suggestions 的语义判断，仅修正为固定 JSON object schema："
                    "passed, target_script_path, key_checks, value_checks, type_checks, issues, repair_suggestions。\n"
                    "不要输出 Markdown 或解释。\n"
                    f"上一轮 schema_error：{last_schema_error}\n"
                    f"上一轮 raw output excerpt：{str(raw or '')[:1200]}"
                ),
            },
        ]
        raw = await complete_chat_once(active_messages, route.model)
        try:
            data = _json_loads_loose_object(raw)
        except Exception as exc:
            data = {}
            last_schema_error = f"invalid JSON: {exc}"
        else:
            if isinstance(data, dict):
                data.setdefault("target_script_path", script_path)
            last_schema_error = _skill_md_block_review_schema_error(data)
            if not last_schema_error:
                data["target_script_path"] = script_path
                data["command_block_ordinal"] = ordinal
                return data
        if review_attempt < 2:
            logger.info(
                "[Creator][skill_md][block_review][schema_retry] skill=%s script=%s ordinal=%d attempt=%d error=%s",
                skill_name,
                script_path,
                ordinal,
                review_attempt + 1,
                last_schema_error[:300],
            )
            continue
    raise CreatorValidatorReviewError(
        f"SKILL.md block reviewer schema invalid for {script_path} after 3 attempts: {last_schema_error}",
        raw_excerpt=str(raw)[:1000],
    )


def _skill_md_block_review_to_contract_results(
    review: dict[str, Any],
    *,
    block_text: str = "",
    block_locator: dict[str, Any] | None = None,
) -> list[ContractCheckResult]:
    script_path = str(review.get("target_script_path") or "unknown")
    if review.get("passed") is True:
        return []
    if not block_text:
        block_text = str(review.get("command_block") or "")
    if block_locator is None:
        block_locator = {
            "start": review.get("block_start"),
            "end": review.get("block_end"),
            "content_sha256": hashlib.sha256(str(block_text or "").encode("utf-8")).hexdigest(),
        }
    typed_failures = _skill_md_block_review_failed_checks(review)
    issues = _dedupe_review_issues(review.get("issues") if isinstance(review.get("issues"), list) else [])
    if not typed_failures and not issues:
        issues = [{"message": "当前 command block 接口校验失败。", "field": "command_block"}]
    merged_failures: list[tuple[str, dict[str, Any]]] = typed_failures + [
        ("issue", issue if isinstance(issue, dict) else {"message": str(issue)})
        for issue in issues
        if _skill_md_reviewer_issue_contradicts_passed_true(issue) or not typed_failures
    ]
    results: list[ContractCheckResult] = []
    for idx, (failure_type, issue) in enumerate(merged_failures, 1):
        message = str(issue.get("message") or issue.get("problem") or f"当前 command block {failure_type} 校验失败。")
        expected = str(issue.get("expected") or "当前 command block 的 JSON key/value/type 必须与当前脚本接口和局部可用来源兼容。")
        minimal = str(issue.get("minimal_edit") or issue.get("repair_suggestions") or review.get("repair_suggestions") or "只修当前 command block。")
        locator = dict(block_locator or {})
        results.append(ContractCheckResult(
            id=f"skill_md.command_block.interface.{failure_type}.{idx}",
            passed=False,
            target=f"SKILL.md:{script_path}:command_block",
            message=message,
            expected=expected,
            minimal_edit=f"{minimal}\n{_skill_md_command_block_repair_scope(script_path)}",
            details={
                "review": review,
                "issue": issue,
                "check_type": failure_type,
                "script_path": script_path,
                "current_block": block_text,
                "block_text": block_text,
                "block_start": locator.get("start"),
                "block_end": locator.get("end"),
                "block_locator": locator,
                "block_ordinal": review.get("command_block_ordinal"),
                "structured_checks": {
                    "key_checks": review.get("key_checks") or [],
                    "value_checks": review.get("value_checks") or [],
                    "type_checks": review.get("type_checks") or [],
                },
                "skill_md_block_repair_scope": {
                    "script_path": script_path,
                    "block_text": block_text,
                    "block_locator": locator,
                    "block_ordinal": review.get("command_block_ordinal"),
                    "block_sha256": hashlib.sha256(str(block_text or "").encode("utf-8")).hexdigest(),
                },
            },
            layer="skill_md_command_block_interface",
        ))
    return results


def _dedupe_review_issues(issues: Any) -> list[dict[str, Any]]:
    if not isinstance(issues, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for raw in issues:
        issue = raw if isinstance(raw, dict) else {"message": str(raw), "evidence": str(raw)}
        key = (
            str(issue.get("field") or ""),
            str(issue.get("category") or issue.get("claim_type") or ""),
            re.sub(r"\s+", " ", str(issue.get("message") or "")).strip().lower(),
            re.sub(r"\s+", " ", str(issue.get("expected") or "")).strip().lower(),
            str(issue.get("target") or issue.get("target_file") or ""),
            str(issue.get("locator") or issue.get("anchor") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(issue))
    return out


_DETAIL_OR_PROOF_REVIEW_TERMS = (
    "证明", "更精确", "不够精确", "说明不够细", "措辞", "优化",
    "placeholder", "stdout", "序列化", "解析", "JSON 运行时类型", "闭环",
    "哪个脚本读取", "读取哪个", "role:", "role：", "source:", "source：",
    "dependencies", "bundled", "manifest", "SkillPlan", "触发词", "章节模板", "固定话术", "逐字",
)

_HARD_SEMANTIC_REVIEW_TERMS = (
    "路径缺失", "未提及真实路径", "缺真实路径", "缺少真实路径",
    "最终产物缺失", "缺最终产物", "缺少最终产物",
    "角色写反", "资源角色写反", "明显写反",
    "蓝图外", "引入额外", "外部 API", "伪 key", "伪数据库", "Creator UI",
    "无法理解", "无法启动",
)


def _review_issue_text(issue: dict[str, Any]) -> str:
    return "\n".join(
        str(issue.get(key) or "")
        for key in (
            "field", "message", "problem", "reason", "evidence", "details",
            "expected", "minimal_edit", "fix", "suggested_fix",
        )
    )


def _review_issue_is_detail_or_proof_request(issue: dict[str, Any]) -> bool:
    text = _review_issue_text(issue)
    return any(term in text for term in _DETAIL_OR_PROOF_REVIEW_TERMS)



def _review_issue_is_explicit_command_mapping_error(issue: dict[str, Any]) -> bool:
    """Allow only evidence-backed command argv mapping errors to bypass proof filtering."""
    category = str(issue.get("category") or issue.get("claim_type") or "").strip().lower()
    text = _review_issue_text(issue).lower()
    if category != "command_mapping_explicit_evidence" and "command mapping" not in text and "argv" not in text:
        return False

    has_script_probe = any(token in text for token in (
        "strict_json_argv_schema",
        "run_args_analysis",
        "allowed_keys",
        "required_keys",
        "required_read_keys",
        "actual guard",
        "actual run",
        "脚本探针",
    ))
    has_graph_edge = any(token in text for token in (
        "incoming_edges",
        "from_output",
        "to_input",
        "platform_input_node",
        "图谱边",
        "入边",
    ))
    is_mapping_error = any(token in text for token in (
        "command",
        "json argv",
        "argv key",
        "argv value",
        "placeholder",
        "stdout",
        "命令",
        "参数映射",
    ))
    return has_script_probe and has_graph_edge and is_mapping_error

def _review_issue_is_command_template_source_proof_error(issue: dict[str, Any]) -> bool:
    category = str(issue.get("category") or issue.get("claim_type") or "").strip().lower()
    field = str(issue.get("field") or "").strip().lower()
    if field not in {"workflow", "execution", "platform_io", "command_template"}:
        return False
    impact = issue.get("contract_impact") or issue.get("impact")
    if not isinstance(impact, dict):
        return False
    has_relevant_impact = any(bool(impact.get(key)) for key in (
        "execution_closure", "platform_io", "user_requirement_transfer"
    ))
    if not has_relevant_impact:
        return False
    if category == "command_template_source_proof":
        return True
    text = _review_issue_text(issue)
    return (
        "来源证明" in text
        and ("command block" in text or "bash" in text or "命令" in text or "JSON argv" in text or "运行模板" in text)
    )


def _review_issue_is_clear_reverse_resource_role(issue: dict[str, Any]) -> bool:
    role = str(issue.get("resource_role") or "").lower()
    claim = str(issue.get("claim_type") or "").lower()
    if role == "reference" and claim in {"execution_step", "artifact", "asset_material", "model_generated", "modifiable", "write_asset"}:
        return True
    if role == "asset" and claim in {"reference_document", "context_reference", "read_into_context", "model_generated", "modifiable", "write_asset"}:
        return True
    return "角色写反" in _review_issue_text(issue) or "明显写反" in _review_issue_text(issue)


def _review_issue_is_blocking(issue: dict[str, Any]) -> bool:
    """Classify first-round SKILL.md semantic coverage issues.

    Do not blindly trust model-provided blocking=true. First-round review blocks
    only obvious semantic responsibility failures; proof/detail/internal-field and
    runtime-closure requests are advisory and must not trigger localized patches.
    """
    severity = str(issue.get("severity") or "error").strip().lower()
    if severity in {"warning", "info", "note", "advisory"}:
        return False
    if issue.get("blocking") is False:
        return False

    if _review_issue_is_command_template_source_proof_error(issue):
        return False

    if _review_issue_is_explicit_command_mapping_error(issue):
        return False

    if _review_issue_is_detail_or_proof_request(issue):
        return False

    if _review_issue_is_clear_reverse_resource_role(issue):
        return True

    text = _review_issue_text(issue)
    if any(term in text for term in _HARD_SEMANTIC_REVIEW_TERMS):
        return True

    impact = issue.get("contract_impact") or issue.get("impact")
    if isinstance(impact, dict):
        return any(bool(impact.get(key)) for key in (
            "execution_closure", "resource_role", "platform_io", "final_artifact", "final_output", "user_requirement_transfer"
        ))

    if issue.get("blocking") is True:
        field = str(issue.get("field") or "").lower()
        text_for_blocking = _review_issue_text(issue).lower()
        if field in {"file_plan", "intent", "workflow", "resources", "user_requirement"} and any(
            term in text_for_blocking for term in ("missing", "缺失", "无法", "cannot", "真实路径", "script path")
        ):
            return True

    user_requirement = issue.get("user_requirement") or issue.get("key_requirement")
    if isinstance(user_requirement, dict):
        return bool(user_requirement.get("required") and user_requirement.get("missing"))

    # Plain severity=error or blocking=true is not enough. The reviewer must
    # identify one of the first-round semantic failures above.
    return False

def _format_skill_md_intent_review_failure(review: dict[str, Any]) -> str:
    """Format model review failure for UI/repair prompt.

    Must never crash. If this crashes, auto-repair flow may break.
    """
    try:
        if not isinstance(review, dict):
            return (
                "SKILL.md 与蓝图意图不一致：审查结果不是 JSON object。\n"
                f"actual_type: {type(review).__name__}"
            )

        issues = review.get("issues")
        if not isinstance(issues, list):
            issues = []

        lines = ["SKILL.md 与蓝图意图不一致："]

        if not issues:
            reviewers = review.get("reviewers")
            if isinstance(reviewers, dict):
                for reviewer_name, reviewer_result in reviewers.items():
                    if not isinstance(reviewer_result, dict):
                        continue
                    reviewer_issues = reviewer_result.get("issues")
                    if isinstance(reviewer_issues, list):
                        for issue in reviewer_issues:
                            issues.append(issue)

        if not issues:
            lines.append("模型审查未通过，但未返回具体 issues。")
            lines.append("请检查蓝图真实文件计划、workflow、资源说明和命令块。")
        else:
            for idx, issue in enumerate(issues, start=1):
                if isinstance(issue, dict):
                    severity = issue.get("severity", "error")
                    field = issue.get("field", "unknown")
                    message = issue.get("message", "")
                    expected = issue.get("expected", "")
                    minimal_edit = issue.get("minimal_edit", "")

                    lines.append(f"{idx}. [{severity}] {field}: {message}")
                    if expected:
                        lines.append(f"   expected: {expected}")
                    if minimal_edit:
                        lines.append(f"   minimal_edit: {minimal_edit}")
                else:
                    lines.append(f"{idx}. {issue}")

        repair = str(review.get("repair_suggestions") or "").strip()
        if repair:
            lines.append("给修复模型的建议：")
            lines.append(repair)

        return "\n".join(lines)

    except Exception as exc:
        logger.exception("[Creator][skill_md] failed to format intent review failure")
        return (
            "SKILL.md 与蓝图意图不一致，但格式化失败信息时发生异常。\n"
            f"格式化异常：{exc}\n"
            f"原始审查结果：{review}"
        )


async def _validate_skill_md_blueprint_alignment(
    *,
    skill_name: str,
    content: str,
    blueprint_text: str,
    skill_plan_entry: dict[str, Any] | None = None,
    requirement_graph: dict[str, Any] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Validate SKILL.md against blueprint as a first-round hard repair gate.

    不再 warning-only：
    - 基础格式失败：进入局部返修；
    - 蓝图语义不一致：进入局部返修；
    - 命令块格式失败：进入局部返修。

    这里仍然不做第二轮 E2E：
    - 不检查上下游 stdout 字段闭环；
    - 不检查最终平台输出；
    - 不检查脚本真实运行。
    """
    try:
        hard_results = _check_skill_md_contract(content, blueprint_text)
    except Exception as exc:
        logger.exception(
            "[Creator][skill_md] hard contract validator crashed skill=%s",
            skill_name,
        )
        raise ValueError(
            "SKILL.md 基础合同校验器内部异常，已转为可返修错误。\n"
            f"错误：{type(exc).__name__}: {exc}\n"
            "请检查 frontmatter、Creator 流程泄露、Runtime Contract 泄露、"
            "scripts 命令块格式、references/assets 资源说明等基础结构。"
        ) from exc

    hard_failed = [result for result in hard_results if not result.passed]
    if hard_failed:
        message = (
            "SKILL.md 基础格式/资源合同校验未通过。\n"
            "这属于第一轮当前文件责任失败，请只修 SKILL.md 的失败区域。\n"
            + _format_contract_failures_safe(hard_results)
        )
        logger.info(
            "[Creator][skill_md] hard contract failed skill=%s failures=\n%s",
            skill_name,
            message,
        )
        raise ContractValidationError(message, hard_results)

    deterministic_results = _deterministic_skill_md_blueprint_alignment_checks(
        content=content,
        blueprint_text=blueprint_text,
        skill_plan_entry=skill_plan_entry,
    )
    deterministic_failed = [result for result in deterministic_results if not result.passed]
    if deterministic_failed:
        message = (
            "SKILL.md 蓝图一致性确定性检查未通过。\n"
            "请只修 SKILL.md 中与蓝图不一致的区域。\n"
            + _format_contract_failures_safe(deterministic_results)
        )
        raise ContractValidationError(message, deterministic_results)

    try:
        model_review = await _review_skill_md_blueprint_intent_with_model(
            skill_name=skill_name,
            content=content,
            blueprint_text=blueprint_text,
            skill_plan_entry=skill_plan_entry,
            requirement_graph=requirement_graph,
            model=model,
        )
    except CreatorValidatorReviewError:
        logger.exception(
            "[Creator][skill_md] semantic coverage reviewer returned invalid output skill=%s",
            skill_name,
        )
        raise
    except Exception as exc:
        logger.exception(
            "[Creator][skill_md] model blueprint intent review crashed skill=%s",
            skill_name,
        )
        raise ValueError(
            "SKILL.md 蓝图一致性审查模型异常，不能放行当前 SKILL.md。\n"
            f"错误：{type(exc).__name__}: {exc}\n"
            "这不是最终失败；生成循环会继续尝试局部返修或重试。"
        ) from exc

    if not isinstance(model_review, dict):
        raise CreatorValidatorReviewError(
            f"第一轮 SKILL.md 语义覆盖 reviewer 返回类型错误：{type(model_review).__name__}"
        )

    review = model_review

    if review.get("passed") is not True:
        results = _skill_md_blueprint_review_to_contract_results(review)
        if not results:
            review["passed"] = True
            review["advisory_only"] = True
        else:
            message = (
                "SKILL.md 与蓝图不一致，不能 warning-only 放行。\n"
                "该失败会进入当前文件局部返修；不要整文件重写，不要修改 scripts/references/assets。\n"
                + _format_skill_md_intent_review_failure(review)
            )
            logger.info(
                "[Creator][skill_md] blueprint alignment failed skill=%s failures=\n%s",
                skill_name,
                message,
            )
            raise ContractValidationError(message, results)

    if review.get("passed") is not True:
        review["passed"] = True

    required_script_paths = list(review.get("required_script_paths") or [])
    if not required_script_paths:
        required_script_paths = [
            path
            for path in _extract_declared_skill_paths(blueprint_text)
            if isinstance(path, str) and path.startswith("scripts/")
        ]

    try:
        fenced_results = _check_skill_md_fenced_command_contracts(
            content=content,
            blueprint_text=blueprint_text,
            required_script_paths=required_script_paths,
        )
    except Exception as exc:
        logger.exception(
            "[Creator][skill_md] fenced command validator crashed skill=%s",
            skill_name,
        )
        raise ValueError(
            "SKILL.md 命令块校验器内部异常，已转为可返修错误。\n"
            f"错误：{type(exc).__name__}: {exc}\n"
            "请检查 SKILL.md 中真实脚本是否使用标准 ```bash fenced code block，"
            "且脚本参数是否为 json.loads 可解析的 JSON object。"
        ) from exc

    fenced_failed = [result for result in fenced_results if not result.passed]
    if fenced_failed:
        message = (
            "SKILL.md 命令块格式校验未通过。\n"
            "蓝图语义已对齐，但真实脚本命令块仍不满足后台可解析规范。\n"
            "请只修复以下命令块问题，不要新增蓝图外脚本。\n"
            + _format_contract_failures_safe(fenced_results)
        )
        logger.info(
            "[Creator][skill_md] fenced command contract failed skill=%s failures=\n%s",
            skill_name,
            message,
        )
        raise ContractValidationError(message, fenced_results)

    command_blocks = [
        block
        for block in parse_skill_md_bash_command_blocks(content)
        if block.script_path and str(block.script_path).startswith("scripts/")
    ]
    prior_stdout: list[str] = []
    for ordinal, block in enumerate(command_blocks, start=1):
        script_path = str(block.script_path or "")
        logger.info(
            "[Creator][skill_md][single_block_review][start] skill=%s script=%s ordinal=%d",
            skill_name,
            script_path,
            ordinal,
        )
        block_review = await _review_skill_md_command_block_with_model(
            skill_name=skill_name,
            script_path=script_path,
            command_block=block.content,
            ordinal=ordinal,
            prior_stdout=prior_stdout,
            requirement_graph=requirement_graph,
            model=model,
        )
        block_review.update({
            "command_block": block.content,
            "block_start": block.start,
            "block_end": block.end,
            "block_ordinal": ordinal,
        })
        block_results = _skill_md_block_review_to_contract_results(
            block_review,
            block_text=block.content,
            block_locator=_skill_md_block_locator(block),
        )
        if block_results:
            logger.info(
                "[Creator][skill_md][single_block_review][failed] skill=%s script=%s ordinal=%d",
                skill_name,
                script_path,
                ordinal,
            )
            raise ContractValidationError(
                "SKILL.md 单 command block 接口校验未通过；只修当前失败 block。\n"
                + _format_contract_failures_safe(block_results),
                block_results,
            )
        try:
            local_context = _skill_md_script_interface_context_for_review(
                skill_name=skill_name,
                script_paths=[script_path],
                requirement_graph=requirement_graph,
            )
            schema_outputs = (
                (local_context[0].get("function_execution_context") or {})
                .get("function_item", {})
                .get("outputs", [])
                if local_context else []
            )
            for item in schema_outputs or []:
                text = str(item or "").strip()
                if text and text not in prior_stdout:
                    prior_stdout.append(text)
        except Exception:
            pass

    review["passed"] = True
    review["fenced_check_passed"] = True
    review["fenced_check_failed"] = []

    logger.info(
        "[Creator][skill_md] blueprint alignment passed skill=%s required_scripts=%s",
        skill_name,
        required_script_paths,
    )

    return review


def _strip_orphan_trailing_fence(content: str) -> str:
    """Remove isolated Markdown fence markers at file boundaries.

    This is intentionally narrower than generic fence stripping: it deletes only
    standalone trailing ```/~~~ lines left by model output, plus an optional
    standalone opening fence when no matching closing fence remains.
    """
    lines = content.strip().splitlines()
    changed = False
    while lines and re.fullmatch(r"\s*(`{3,}|~{3,})\s*", lines[-1]):
        lines.pop()
        changed = True
    if lines and re.fullmatch(r"\s*(`{3,}|~{3,})[A-Za-z0-9_-]*\s*", lines[0]):
        body = "\n".join(lines[1:])
        if "```" not in body and "~~~" not in body:
            lines = lines[1:]
            changed = True
    return ("\n".join(lines).strip() if changed else content.strip())


def _build_script_file_contract_text(
    file_path: str,
    blueprint_text: str,
    *,
    purpose: str = "",
    role: str | None = None,
    skill_plan_entry: dict[str, Any] | None = None,
) -> str:
    entry = _skill_plan_entry_for_file(
        file_path=file_path,
        blueprint_text=blueprint_text,
        role=role,
        skill_plan_entry=skill_plan_entry,
    )
    recommended_command = _script_command_template(file_path, blueprint_text, entry)

    declared_stdout_fields = [str(item).strip() for item in (entry.outputs or []) if str(item).strip()]
    declared_stdout_text = ", ".join(declared_stdout_fields) if declared_stdout_fields else "无显式声明"

    lines = [
        f"脚本合同：{file_path}",
        f"Role: {entry.role}",
        f"file_type: {entry.file_type}",
        f"runtime: {entry.runtime}",
        f"entrypoint: {entry.entrypoint or file_path}",
        f"recommended_command_template: {recommended_command}",
        f"suggested_inputs: {', '.join(entry.inputs or ['payload'])}",
        f"declared_stdout_fields: {declared_stdout_text}",
        "",
        "职责原则:",
        f"- 当前脚本的语义职责以 purpose 中的短合同为准：{entry.purpose or purpose or '未声明'}",
        "- inputs/outputs 是接口提示，不是完整职责的替代。",
        "- 脚本可以兼容不同 argv 形式，但不能弱化 purpose 中的来源、动作、交付、约束。",
        "- 可执行职责必须落到脚本内部，不能依赖 SKILL.md 自然语言隐式循环、隐式聚合或人工理解来补完。",
        "- 如果 purpose 或 workflow_allocation_summary 表明当前脚本负责整体处理、逐项处理、批量生成、一一对应、聚合交付、保留顺序或结构映射，则脚本必须在自身逻辑中真实完成。",
        "- 当前平台没有显式 loop/map/foreach 时，逐项处理必须在当前脚本内部循环完成；不得只取首项、join 压扁、只返回单个结果、放宽 guard 或让下游猜测补齐。",
        "- 脚本不只负责调用工具本身，也负责完成自身输入范围内必要的解析、遍历、字段提取、顺序保持、结果组装和 stdout 交付。",
        "- 如果当前脚本消费上游结构化对象或集合，它必须在脚本内部读取所需子字段；不能假设 SKILL.md 或平台会自动把子字段拆成顶层 argv。",
        "- 如果当前脚本产出供下游消费的集合结果，它必须真实返回集合级 stdout 字段；不能只返回单个结果并依赖下游或 SKILL.md 补齐。",
        "- role 只是实现提示；不得覆盖或缩小 purpose 短合同。",
        "",
        "A. 输出形态:",
        "- 单文件源码，Python 脚本必须通过 ast.parse。",
        "- 最终只能输出当前脚本源码本身，不要 Markdown fence、文件标题、写入文件标签或多文件包。",
        "",
        "B. 参数接口（输入宽松）:",
        "- 脚本必须能读取一个 JSON argv object。",
        "- 可以宽松兼容 payload / user_request / input / text / fields / options / input_files / files / 上游 stdout 字段。",
        "- 当前脚本只实现自己的单步职责；不要在脚本内调用、编排或转发执行其它 scripts/*.py；SKILL.md 只能说明静态入口，不能承担运行时 dataflow。",
        "- 不要求第一轮读取所有 input_sources 或 SkillPlan inputs；不因可选输入未使用而失败。",
        "- 但是，脚本不得用与任务无关的默认 prompt、固定示例值、固定模板或常量结果替代核心业务输入。",
        "- 如果核心输入缺失，可以从 payload/user_request/input/text/fields/options/input_files 或上游 stdout 中选择最合理来源；不要静默退回到无关任务。",
        "",
        "C. stdout 字段语义:",
        "- declared_stdout_fields 只表示 stdout JSON 中应出现的业务字段，不等于文件路径字段。",
        "- 普通 stdout 字段只做 JSON required/non-empty/type 校验，并由内容职责审查判断是否真正完成业务职责。",
        "- 只有 artifact_fields、file_fields、file_outputs、artifact_outputs，或字段名本身具有 artifact/path/file 语义时，才按文件产物路径检查。",
        "- 不要把普通业务字段的字符串值或字符串列表当成文件路径。",
        "",
        "D. 输出来源 / provenance:",
        "- declared_stdout_fields 中的核心业务字段必须由 argv JSON、上游 stdout、reference 内容、uploaded assets、工具结果、模型结果或确定性计算推导出来。",
        "- 不得为了满足 required_outputs 返回固定示例值、固定模板、无关默认值或与输入无关的常量。",
        "- 如果使用 generate_text_with_llm、图像 helper、文档 helper 或其它工具，工具返回结果必须参与核心 declared_stdout_fields 的构造。",
        "- 如果 declared_stdout_fields 包含多个结构化业务字段，优先让写作模型返回严格 JSON object，再解析为 stdout JSON；不要只把模型结果放到 text 字段，再硬编码其它字段。",
        "- 如果只需要自由文本输出，可以返回 text/markdown 等文本字段；如果需要结构化字段，应让模型或确定性逻辑直接产生这些字段。",
        "- 只有 SkillPlan 或输入明确声明为 constant/default/config 的字段，才允许固定值。",
        "",
        "E. 角色输出合同:",
    ]

    if entry.role == "text_generator":
        lines.extend([
            "- stdout JSON 至少有一个非空字段；字段名由 workflow 决定。",
            "- 如果 declared_stdout_fields 多于一个普通业务字段，应优先使用结构化生成模式：让文本模型返回 JSON object，并将解析结果作为 stdout。",
            "- 可以调用 text_generation helper，但 helper 结果必须参与核心 stdout 字段构造；不得把 helper 调用当作装饰后再用固定值补其它字段。",
        ])
    elif entry.role == "image_generator":
        lines.extend([
            "- stdout JSON 至少有一个非空字段；字段名由 workflow 决定。",
            "- 可优先调用 generate_stable_diffusion_image helper，但不强制具体实现方式。",
            "- 如果输出图片路径，必须返回具有 artifact/path 语义的平台字段，例如 image_path 或 image_paths，且真实文件存在性由试运行/E2E 校验。",
            "- 如果还输出普通业务字段，这些字段仍然是 stdout data fields，不自动当作文件路径。",
        ])
    elif entry.role == "pdf_builder":
        lines.extend([
            "- stdout JSON 必须返回真实存在的文件产物路径；字段名由 workflow 决定。",
            "- 推荐使用 pdf_path 或 file_paths/file_outputs 等具有 artifact/path 语义的字段。",
            "- 工具/helper 如何组合不作为第一轮 hard gate；最终以运行、stdout 合同和 artifact 真实存在为准。",
            "- PDF 内容必须来自输入、上游 stdout、reference/assets、工具结果或确定性组装；不得生成空壳 PDF 或无关固定模板。",
        ])
    elif entry.role in {"docx_builder", "pptx_builder", "html_asset_builder", "asset_builder"}:
        lines.extend([
            "- stdout JSON 必须返回真实存在的文件产物路径；字段名由 workflow 决定。",
            "- 推荐使用 docx_path、pptx_path、html_path、file_paths 或 file_outputs 等具有 artifact/path 语义的字段。",
            "- 文件内容必须来自输入、上游 stdout、reference/assets、工具结果或确定性组装；不得生成空壳文件或无关固定模板。",
        ])
    else:
        lines.extend([
            "- stdout JSON 至少有一个非空字段；字段名由 workflow 决定。",
            "- 如果 stdout 字段是普通业务数据，不要把它写成文件路径；如果 stdout 字段是文件产物路径，字段名应具备 artifact/path/file 语义。",
        ])

    try:
        from .e2e import _extract_e2e_workflow_commands

        workflow_commands = _extract_e2e_workflow_commands(Path("."), blueprint_text or "")
        script_commands = [cmd for cmd in workflow_commands if cmd.script_path == file_path]
        is_last_workflow_step = bool(
            script_commands
            and workflow_commands
            and script_commands[-1].ordinal == workflow_commands[-1].ordinal
        )
    except Exception:
        is_last_workflow_step = False

    if is_last_workflow_step:
        lines.extend([
            "",
            "F. 最后一步平台输出合同:",
            "- 这是 SKILL.md workflow 的最后一步：stdout JSON 必须至少包含一个 sandbox 平台最终字段：",
            "  text、markdown、image_path、image_paths、pdf_path、docx_path、pptx_path、html_path、file_paths 或 file_outputs。",
            "- 可以同时保留业务内部字段，例如 {\"time_output\": \"12:00:00\", \"text\": \"当前时间：12:00:00\"}。",
            "- 中间步骤不强制平台最终字段，只要下一步 placeholder 可解析。",
            "- 平台最终字段只用于最终展示/下载；普通业务字段仍然不自动成为文件路径。",
        ])

    lines.extend([
        "",
        "G. 第一轮验收边界:",
        "- 第一轮 deterministic 校验只验协议 + 运行 + 产物：argv JSON、入口、运行成功、stdout JSON object、required outputs、真实 artifact、import/dependency 和危险系统操作。",
        "- 工具/helper 如何组合、required/optional/allowed_capabilities、placeholder/mock/template 关键词不作为第一轮源码正则 hard gate。",
        "- 但内容职责审查模型可以 hard veto：如果脚本只凑字段、返回固定示例值、没有使用核心输入、模型/工具结果没有参与核心输出构造，则视为当前脚本内容职责失败。",
        "- 不要通过 print {'error':...}、{}、空路径、空文件、固定模板或 mock 数据绕过运行和产物校验。",
    ])

    return "\n".join(lines)

_REFERENCE_FRONTMATTER_RE = re.compile(r"^---\s*\n([\s\S]*?)\n---\s*\n?", re.M)


def _slug_from_reference_path(file_path: str) -> str:
    stem = Path(file_path).stem.strip().lower()
    slug = re.sub(r"[^a-z0-9\u4e00-\u9fff-]+", "-", stem).strip("-")
    return slug or "reference"


def _reference_frontmatter_metadata(content: str) -> tuple[dict[str, Any], str]:
    """Return YAML frontmatter metadata and body."""
    text = content or ""
    match = _REFERENCE_FRONTMATTER_RE.match(text.strip())
    if not match:
        return {}, text.strip()

    raw_meta = match.group(1).strip()
    body = text.strip()[match.end():].strip()
    try:
        meta = yaml.safe_load(raw_meta) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}

    return meta, body

def _markdown_sections_for_resource_matching(content: str) -> list[tuple[str, str]]:
    """Split Markdown into heading-scoped sections for resource path matching.

    返回 (heading, section_text)。
    不依赖中文/英文业务词，只用 Markdown heading 结构。
    """
    text = content or ""
    lines = text.splitlines()
    sections: list[tuple[str, str]] = []

    current_heading = ""
    current_lines: list[str] = []

    for line in lines:
        if re.match(r"^\s{0,3}#{1,6}\s+\S", line):
            if current_lines:
                sections.append((current_heading, "\n".join(current_lines)))
            current_heading = line.strip()
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_lines:
        sections.append((current_heading, "\n".join(current_lines)))

    return sections or [("", text)]


def _markdown_mentions_skill_resource_path(content: str, resource_path: str) -> bool:
    """Return whether SKILL.md structurally mentions a skill-local resource.

    允许两种非硬编码表达：
    1. 完整路径出现在任意位置：references/x.md
    2. basename 出现在同一个 Markdown section，且该 section 同时出现资源目录前缀：
       heading/body 中有 references/，列表项中有 x.md

    不根据具体文件名、业务名、中文标题、英文标题做词表判断。
    """
    normalized = _normalize_skill_path(resource_path)
    if not normalized:
        return False

    text = content or ""
    if normalized in text:
        return True

    if "/" not in normalized:
        return normalized in text

    folder, basename = normalized.split("/", 1)
    if not folder or not basename:
        return False

    basename_pattern = re.compile(rf"(?<![\w./-])`?{re.escape(basename)}`?(?![\w./-])")
    folder_marker = f"{folder}/"

    for _heading, section_text in _markdown_sections_for_resource_matching(text):
        if folder_marker not in section_text:
            continue
        if basename_pattern.search(section_text):
            return True

    return False

def _ensure_reference_metadata_frontmatter(
    *,
    file_path: str,
    content: str,
    purpose: str = "",
    skill_plan_entry: dict[str, Any] | None = None,
) -> str:
    """Ensure references/*.md has valid ordinary document frontmatter.

    简单原则：
    - 只处理 references/*.md；
    - 不依赖 LLM；
    - 不修正文；
    - title/description 必须在顶层；
    - 旧 frontmatter 解析失败时直接重建；
    - Creator 内部信息只放 metadata.creator。
    """
    if not file_path.startswith("references/") or Path(file_path).suffix.lower() != ".md":
        return content

    text = (content or "").lstrip("\ufeff").strip()
    if not text:
        stem = Path(file_path).stem.replace("-", " ").replace("_", " ").strip() or "reference"
        text = f"# {stem}\n\n本文件提供可复用参考信息。\n"

    # raw split，不依赖 YAML 解析成功。
    body = text
    old_meta: dict[str, Any] = {}

    if text.startswith("---"):
        lines = text.splitlines(keepends=True)
        end_index = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end_index = i
                break

        if end_index is not None:
            raw_meta = "".join(lines[1:end_index])
            body = "".join(lines[end_index + 1:]).lstrip("\n")
            try:
                parsed = yaml.safe_load(raw_meta) or {}
                if isinstance(parsed, dict):
                    old_meta = parsed
            except Exception:
                old_meta = {}

    plan = skill_plan_entry if isinstance(skill_plan_entry, dict) else {}

    def _nonempty(value: Any) -> str:
        return value.strip() if isinstance(value, str) and value.strip() else ""

    metadata = old_meta.get("metadata") if isinstance(old_meta.get("metadata"), dict) else {}
    creator = metadata.get("creator") if isinstance(metadata.get("creator"), dict) else {}

    title = (
        _nonempty(old_meta.get("title"))
        or _nonempty(creator.get("title"))
        or _nonempty(plan.get("title"))
        or _nonempty(plan.get("name"))
        or Path(file_path).stem.replace("-", " ").replace("_", " ").strip()
        or "reference"
    )

    description = (
        _nonempty(old_meta.get("description"))
        or _nonempty(creator.get("description"))
        or _nonempty(purpose)
        or _nonempty(plan.get("purpose"))
        or _nonempty(plan.get("description"))
        or f"{file_path} reference document"
    )

    canonical: dict[str, Any] = {
        "title": title,
        "description": description,
    }

    for key in ("source", "license"):
        if old_meta.get(key) not in (None, "", [], {}):
            canonical[key] = old_meta[key]

    creator = dict(creator) if isinstance(creator, dict) else {}
    creator.pop("title", None)
    creator.pop("description", None)
    creator.setdefault("path", file_path)
    if purpose:
        creator.setdefault("purpose", purpose)

    canonical["metadata"] = {"creator": creator}

    yaml_text = yaml.safe_dump(
        canonical,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()

    return f"---\n{yaml_text}\n---\n\n{body.strip()}\n"


def _reference_metadata_contract_checks(
    *,
    file_path: str,
    content: str,
    purpose: str = "",
) -> list[ContractCheckResult]:
    """Validate Creator-generated references/*.md frontmatter.

    references/*.md 是正式 Markdown 参考资料文件，不是轻量片段。
    Creator 生成的 reference 必须像 SKILL.md 一样有基础 metadata，
    但 schema 使用 reference 自己的普通文档元数据 schema。
    """
    meta, body, had_frontmatter = parse_frontmatter(content)
    metadata_errors = validate_reference_frontmatter(meta) if had_frontmatter else []
    results: list[ContractCheckResult] = []

    missing_required: list[str] = []
    if not had_frontmatter:
        missing_required.append("frontmatter")
    else:
        for key in ("title", "description"):
            value = meta.get(key) if isinstance(meta, dict) else None
            if not isinstance(value, str) or not value.strip():
                missing_required.append(key)

    passed = had_frontmatter and not metadata_errors and not missing_required

    results.append(ContractCheckResult(
        id="reference.metadata.frontmatter_schema",
        passed=passed,
        target=file_path,
        message=(
            "reference frontmatter schema 合格。"
            if passed
            else "reference 必须包含普通文档 frontmatter，且 title/description 必须非空。错误："
                 + "; ".join([*metadata_errors, *(f"missing {item}" for item in missing_required)])
        ),
        expected=(
            "references/*.md 必须以 YAML frontmatter 开头；"
            "顶层只允许 title、description、source、license、metadata；"
            "title 和 description 必须非空。"
        ),
        minimal_edit=(
            "只修当前 reference md 的 YAML frontmatter；"
            "添加非空 title/description；"
            "删除 role/path/type/scope/loading/when_to_use/inputs/outputs/capabilities/"
            "required_tool_slots/implementation_strategy/command_template 等 Creator 内部顶层字段；"
            "如需保留内部信息，只能放到 metadata.creator 下。"
        ),
        details={
            "had_frontmatter": had_frontmatter,
            "missing_required": missing_required,
            "metadata_errors": metadata_errors,
        },
    ))

    body_text = body.strip() if had_frontmatter else (content or "").strip()
    results.append(ContractCheckResult(
        id="reference.metadata.body_exists",
        passed=bool(body_text),
        target=file_path,
        message=("reference 存在 Markdown 正文。" if body_text else "reference 正文为空。"),
        expected="frontmatter 后必须输出该 reference 的 Markdown 正文。",
        minimal_edit="补充非空 reference 正文；正文必须是可复用参考资料，不是聊天问题、确认选项或状态说明。",
    ))

    return results

def _build_reference_file_contract_text(file_path: str, purpose: str, blueprint_text: str) -> str:
    script_paths = _paths_requiring_skill_md_mentions(blueprint_text, prefix="scripts/")
    script_lines: list[str] = []

    for script_path in script_paths:
        entry = _skill_plan_entry_for_file(file_path=script_path, blueprint_text=blueprint_text)
        if file_path in entry.reference_files or not entry.reference_files:
            script_lines.extend([
                f"- 本 reference 可以为 {script_path} 提供内容规范、格式规则、风格要求、质量标准、示例或反例；但不要重新定义该脚本的 role/inputs/outputs/capabilities/command_template。",
                f"- 如果 SKILL.md 已包含 {script_path} 的可执行命令块，本 reference 不要再写可执行命令块。",
                "- reference 正文可以提到相关脚本路径、阶段名或模块名，但不能把其它文件完整打包进来。",
            ])

    if not script_lines:
        script_lines.append(
            "- 本 reference 对应一个独立子任务/模块；正文必须提供可复用参考资料，而不是聊天回复、确认问题、状态说明或创建计划。"
        )

    metadata_example = yaml.safe_dump(
        {
            "title": _slug_from_reference_path(file_path),
            "description": purpose or f"{file_path} reference",
            "metadata": {
                "creator": {
                    "path": file_path,
                    "purpose": purpose or "按 SKILL.md 工作流需要读取正文",
                }
            },
        },
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()

    return "\n".join([
        f"必须满足以下参考资料文件合同：{file_path}",
        "",
        "A. YAML frontmatter（硬格式要求）:",
        "- 文件必须以 YAML frontmatter 开头，和 SKILL.md 一样必须有明确 metadata 区。",
        "- frontmatter 顶层只允许：title、description、source、license、metadata。",
        "- title 和 description 必须非空。",
        "- path/purpose/role/type/scope/loading/when_to_use/inputs/outputs/capabilities/"
        "required_tool_slots/implementation_strategy/command_template 等 Creator 内部信息不得作为顶层字段；"
        "如需保留，只能放在 metadata.creator 下。",
        "metadata 示例:",
        "---",
        metadata_example,
        "---",
        "",
        "B. Markdown 正文格式（硬格式要求）:",
        "- frontmatter 后必须有 Markdown 正文。",
        "- 正文必须使用 Markdown 文档结构，至少包含一个 # 或 ## 标题。",
        "- 正文可以包含段落、列表、表格、json/text 示例、质量检查项等。",
        "- 不要把整个文件包裹在 ```markdown 代码块里；最终输出就是当前 .md 文件内容本身。",
        "",
        "C. 内容职责（参考价值要求）:",
        f"- 职责说明：{purpose or '根据蓝图提供可操作参考资料'}",
        "- reference 正文必须是参考资料本身，而不是对用户的澄清问题、确认选项、聊天回复、状态说明或计划询问。",
        "- 第一轮只检查 reference 是否覆盖自身负责的参考内容；不因固定字段名、固定章节名或措辞不同判失败。",
        "- 正文必须提供可复用的规则、约束、示例、格式说明、风格要求、质量标准或其它参考信息。",
        "- 可以阻断非常明确的无效内容：空内容、空集合、纯占位符、明显默认模板、与 reference 职责明显无关的内容。",
        "- 每个 reference 只对应一个子任务/模块，不要把整个 Skill 包打包到一个 reference。",
        "- 正文是辅助参考资料；不要重新定义 SkillPlan 的 role/capability/input/output 合同；如果 SKILL.md 已有可执行命令块，reference 不要重复写命令块。",
        "- 不要重新定义 role / inputs / outputs / capabilities / command_template。",
        *script_lines,
        "",
        "D. 禁止项:",
        "- 不要包含 Creator 创建流程、确认清单、点击开始创建等平台流程文案。",
        "- 不要包含其它 SKILL.md/scripts/assets/references 文件的完整打包内容。",
        "- 不要包含 placeholder/TODO/待补充等占位文本。",
    ])


def _build_asset_file_contract_text(file_path: str, purpose: str) -> str:
    return "\n".join([
        f"必须满足以下 asset 文件合同：{file_path}",
        "A. 输出形态:",
        "- 只输出当前 asset 文件内容，不要写入文件标签、说明文字或多文件包。",
        "- 文件必须非空；JSON 资源必须可被 json.loads 解析。",
        "B. 内容职责:",
        f"- 职责说明：{purpose or '根据蓝图提供模板或静态资源'}",
        "C. 禁止项:",
        "- asset 是模板或静态资源，不得包含运行时代码、图片生成调用或 Creator 创建流程文案。",
    ])


def _build_generated_file_contract_text(
    file_path: str,
    blueprint_text: str,
    purpose: str = "",
    *,
    role: str | None = None,
    skill_plan_entry: dict[str, Any] | None = None,
) -> str:
    if file_path == "SKILL.md":
        return _build_skill_md_contract_text(blueprint_text)
    if file_path.startswith("scripts/"):
        return _build_script_file_contract_text(file_path, blueprint_text, purpose=purpose, role=role, skill_plan_entry=skill_plan_entry)
    if file_path.startswith("references/"):
        return _build_reference_file_contract_text(file_path, purpose, blueprint_text)
    if file_path.startswith("assets/"):
        return _build_asset_file_contract_text(file_path, purpose)
    return ""

def _reference_contains_write_file_directive(markdown_body: str) -> bool:
    """Detect whether a reference body contains an actual write-file directive.

    只扫描 reference 正文，不扫描 YAML frontmatter。
    避免 metadata.creator.path 被误判成 Path/File 写入标签。

    这里检测的是通用文件写入指令语法，不绑定具体业务案例。
    """
    body = markdown_body or ""
    return bool(re.search(
        r"(?m)^\s*(?:写入文件|创建文件|保存为|File|Filename|Path)\s*[:：]\s*(?:SKILL\.md|scripts/|references/|assets/)",
        body,
    ))



def _reference_placeholder_matches(markdown_body: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    in_fenced = False
    lines = str(markdown_body or "").splitlines()
    for index, line in enumerate(lines, start=1):
        if re.match(r"^\s*```", line):
            in_fenced = not in_fenced
        for match in _REFERENCE_PLACEHOLDER_RE.finditer(line):
            start = max(0, index - 2)
            end = min(len(lines), index + 1)
            matches.append({
                "term": match.group(0),
                "line_number": index,
                "line_text": line,
                "context_excerpt": "\n".join(lines[start:end]),
                "in_fenced_block": in_fenced,
            })
    return matches


def _sanitize_reference_placeholders(content: str) -> str:
    meta, body = _reference_frontmatter_metadata(content)
    had_frontmatter = str(content or "").lstrip().startswith("---")
    body_lines = body.splitlines()
    sanitized: list[str] = []
    in_fenced = False
    for line in body_lines:
        if re.match(r"^\s*```", line):
            in_fenced = not in_fenced
            sanitized.append(line)
            continue
        if in_fenced or not _REFERENCE_PLACEHOLDER_RE.search(line):
            sanitized.append(line)
            continue
        matches = list(_REFERENCE_PLACEHOLDER_RE.finditer(line))
        non_match_text = _REFERENCE_PLACEHOLDER_RE.sub("", line)
        cleaned = re.sub(r"[，,、/；;：:（）()\[\]{}]+", " ", non_match_text).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        if not cleaned or len(cleaned) < max(4, len(line.strip()) // 4):
            continue
        # If the line was a rule listing the banned forms, keep the rule intent
        # without repeating any matched term.
        if any(token in line for token in ("禁止", "不要", "不得", "避免")) and len(matches) >= 1:
            sanitized.append("不得保留未完成状态说明。")
            continue
        sanitized.append(cleaned)
    new_body = "\n".join(sanitized).strip() + "\n"
    if not had_frontmatter:
        return new_body
    frontmatter_match = re.match(r"\s*(---\s*\n.*?\n---\s*\n?)", str(content or ""), flags=re.S)
    if not frontmatter_match:
        return str(content or "")
    return frontmatter_match.group(1).rstrip() + "\n" + new_body


def _check_reference_file_contract(file_path: str, content: str, *, purpose: str = "") -> list[ContractCheckResult]:
    """Validate only objective Markdown/container structure for references.

    Backend deliberately does not judge whether the reference body is valuable,
    sufficiently long, aligned with its purpose, or semantically complete. Those
    questions belong to the model semantic judge.
    """
    raw_failures = _basic_markdown_format_failures(
        file_path,
        content,
        require_frontmatter=True,
    )
    if raw_failures:
        return [
            ContractCheckResult(
                id=str(item.get("id") or "reference.markdown_format"),
                passed=False,
                target=file_path,
                message=str(item.get("message") or "reference Markdown 格式错误。"),
                expected=str(item.get("expected") or "reference Markdown 格式必须合法。"),
                minimal_edit=str(item.get("minimal_edit") or "只修 Markdown 格式区域。"),
                details=item,
                layer="markdown_format",
            )
            for item in raw_failures
        ]

    content = _ensure_reference_metadata_frontmatter(
        file_path=file_path,
        content=content,
        purpose=purpose,
        skill_plan_entry=None,
    )

    meta, body, had_frontmatter = parse_frontmatter(content)
    stripped = (body if had_frontmatter else content or "").strip()
    full_text = content.strip()

    results: list[ContractCheckResult] = []

    # Metadata/frontmatter structure and required fields.
    results.extend(_reference_metadata_contract_checks(
        file_path=file_path,
        content=content,
        purpose=purpose,
    ))

    wrapped_entire_file = bool(re.match(r"^\s*(```|~~~)", full_text)) and bool(re.search(r"(```|~~~)\s*$", full_text))
    results.append(ContractCheckResult(
        id="reference.markdown.raw_file_not_fenced",
        passed=not wrapped_entire_file,
        target=file_path,
        message=(
            "reference 是原始 Markdown 文件内容。"
            if not wrapped_entire_file
            else f"{file_path} 被整体包裹在 Markdown fenced code block 中。"
        ),
        expected="最终输出应是 references/*.md 文件正文自身，不要外层 ```markdown fence。",
        minimal_edit="删除最外层 Markdown fence，只保留 frontmatter 和正文。",
        layer="markdown_format",
    ))

    fence_markers = re.findall(r"(?m)^\s*(```|~~~)", stripped)
    balanced_fences = len(fence_markers) % 2 == 0
    results.append(ContractCheckResult(
        id="reference.markdown.fences_balanced",
        passed=balanced_fences,
        target=file_path,
        message=(
            "reference Markdown fence 成对闭合。"
            if balanced_fences
            else f"{file_path} 存在未闭合的 Markdown fenced code block。"
        ),
        expected="Markdown fenced code block 必须成对闭合。",
        minimal_edit="补齐或删除未闭合的 ```/~~~ fenced block。",
        layer="markdown_format",
    ))

    results.append(ContractCheckResult(
        id="reference.not_empty",
        passed=bool(stripped),
        target=file_path,
        message=("参考资料正文非空。" if stripped else f"{file_path} 参考资料正文为空。"),
        expected="frontmatter 后必须存在 Markdown 正文。",
        minimal_edit="补充正文内容。",
        layer="markdown_format",
    ))

    has_write_file_label = _reference_contains_write_file_directive(stripped)
    has_packaged_file_block = False
    lines = stripped.splitlines()
    for idx, line in enumerate(lines):
        line_text = line.strip()
        if re.match(r"^(?:#{1,6}\s*)?(?:SKILL\.md|scripts/[^\s]+|references/[^\s]+|assets/[^\s]+)\s*$", line_text):
            following = "\n".join(lines[idx + 1: idx + 4])
            if "```" in following or "~~~" in following:
                has_packaged_file_block = True
                break

    single_file_ok = not has_write_file_label and not has_packaged_file_block
    results.append(ContractCheckResult(
        id="reference.single_file",
        passed=single_file_ok,
        target=file_path,
        message=(
            "参考资料是单文件内容。"
            if single_file_ok
            else f"{file_path} 包含多文件包、其它文件完整内容或写入文件标签。"
        ),
        expected="只输出当前 reference 文件内容；不能包含其它文件的完整内容或写入文件标签。",
        minimal_edit="删除其它文件完整内容和写入文件标签，只保留当前 reference metadata 和正文。",
        layer="markdown_format",
    ))

    return results


def _reference_script_commands(content: str) -> list[tuple[str, str]]:
    """Return executable script commands declared by references.

    Current design intentionally returns no executable commands:
    references/*.md are documentation resources, not workflow sources.
    They may mention scripts/** in prose or examples, but those mentions must
    never create an executable command contract.
    """
    return []


def _declared_list_in_text(field_name: str, content: str) -> list[str] | None:
    pattern = re.compile(rf"(?:^|\b){re.escape(field_name)}\s*[：:=]\s*\[?([^\]\n;]+)\]?", re.I | re.M)
    match = pattern.search(content or "")
    if not match:
        return None
    return [re.sub(r"[^A-Za-z0-9_./-]", "", item.strip().strip("'\"")) for item in re.split(r"[,，、]\s*", match.group(1)) if item.strip()]


def _declared_role_in_text(content: str) -> str | None:
    match = re.search(r"(?:^|\b)role\s*[：:=]\s*(text_generator|image_generator|composite_generator|pdf_builder|docx_builder|pptx_builder|html_asset_builder|asset_builder|generic_script)", content or "", re.I | re.M)
    return match.group(1) if match else None


def _anti_example_sections(content: str) -> str:
    chunks: list[str] = []
    matches = list(re.finditer(r"(?im)^#{1,3}.*(?:反例|错误示例|Anti[- ]?examples?).*$", content or ""))
    for idx, match in enumerate(matches):
        start = match.end()
        next_heading = re.search(r"(?m)^#{1,3}\s+", content[start:])
        end = start + next_heading.start() if next_heading else len(content)
        chunks.append(content[start:end])
    return "\n".join(chunks)


def _check_reference_skillplan_redefinitions(file_path: str, content: str, entry: SkillPlanEntry) -> list[ContractCheckResult]:
    """Ensure references do not invent a second script interface contract."""
    results: list[ContractCheckResult] = []
    declared_role = _declared_role_in_text(content)
    role_ok = declared_role is None or declared_role == entry.role
    results.append(ContractCheckResult(
        id="reference.role.matches_skillplan",
        passed=role_ok,
        target=f"{file_path}#{entry.path}",
        message=("reference 未重新定义冲突 role。" if role_ok else f"reference 重新定义 role={declared_role}，与 SkillPlan.role={entry.role} 冲突。"),
        expected=f"reference 默认不要定义 role；如提及只能是 role={entry.role}。",
        minimal_edit="删除 reference 中的 role/能力合同定义，改写为写作规范、风格要求、示例和质量标准。",
    ))
    for field_name, expected_values in (
        ("inputs", entry.inputs or ["payload"]),
        ("outputs", entry.outputs),
        ("required_capabilities", entry.required_capabilities),
        ("forbidden_capabilities", entry.forbidden_capabilities),
    ):
        declared = _declared_list_in_text(field_name, content)
        ok = declared is None or declared == expected_values
        results.append(ContractCheckResult(
            id=f"reference.{field_name}.matches_skillplan",
            passed=ok,
            target=f"{file_path}#{entry.path}",
            message=(f"reference 未重新定义冲突 {field_name}。" if ok else f"reference {field_name}={declared} 与 SkillPlan {field_name}={expected_values} 冲突。"),
            expected=f"reference 默认不要定义 {field_name}；如提及必须逐字等于 SkillPlan: {expected_values}。",
            minimal_edit=f"删除或修正 {field_name} 小节，避免产生第二套接口合同。",
        ))
    anti = _anti_example_sections(content)
    correct_command_in_anti = bool(anti and entry.command_template and entry.command_template in anti)
    correct_keys_in_anti = False
    for command in re.findall(r"```(?:bash|sh|shell)?\s*\n([\s\S]*?)\n```", anti, flags=re.I):
        keys = _command_payload_keys(command.strip(), entry.path)
        if keys == set(entry.inputs or ["payload"]):
            correct_keys_in_anti = True
    results.append(ContractCheckResult(
        id="reference.anti_example.not_skillplan_command",
        passed=not correct_command_in_anti and not correct_keys_in_anti,
        target=f"{file_path}#anti-examples",
        message=("reference 未把 SkillPlan 正确命令/JSON keys 写成反例。" if not correct_command_in_anti and not correct_keys_in_anti else "reference 把 SkillPlan.command_template 或正确 JSON keys 写入反例，导致合同冲突。"),
        expected="反例只能展示 extra key、缺失 key、错误 runner 或非 JSON argv；不得否定 SkillPlan.command_template。",
        minimal_edit="从反例中移除正确命令，改为错误示例例如 extra 参数或 payload 包装。",
    ))
    return results

def _validate_reference_file_contract(file_path: str, content: str, purpose: str = "") -> None:
    results = _check_reference_file_contract(file_path, content, purpose)
    if any(not result.passed for result in results):
        raise ContractValidationError(
            _format_contract_failures(results).replace("SKILL.md contract", f"{file_path} contract"),
            results,
        )


def _asset_extension_check(file_path: str, stripped: str) -> tuple[bool, str, str]:
    ext = Path(file_path).suffix.lower()
    if not stripped:
        return True, "空内容由 asset.not_empty 检查处理。", "当前 asset 文件内容非空。"
    if ext == ".json":
        try:
            json.loads(stripped)
        except json.JSONDecodeError as exc:
            return False, f"{file_path} 不是合法 JSON: {exc.msg}", "JSON asset 必须可被 json.loads 解析。"
        return True, "JSON asset 可解析。", "JSON asset 必须可被 json.loads 解析。"
    if ext in {".yaml", ".yml"}:
        try:
            yaml.safe_load(stripped)
        except yaml.YAMLError as exc:
            return False, f"{file_path} 不是合法 YAML: {exc}", "YAML asset 必须可被 yaml.safe_load 解析。"
        return True, "YAML asset 可解析。", "YAML asset 必须可被 yaml.safe_load 解析。"
    if ext == ".csv":
        rows = list(csv.reader(io.StringIO(stripped)))
        header = rows[0] if rows else []
        if len(rows) < 3 or not header or any(not cell.strip() for cell in header):
            return False, f"{file_path} CSV 必须包含非空表头和至少 2 行数据。", "CSV asset 必须包含 header 和至少 2 行数据。"
        return True, "CSV asset 包含表头和至少 2 行数据。", "CSV asset 必须包含 header 和至少 2 行数据。"
    if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        data = stripped.encode("latin1", errors="ignore")
        if re.fullmatch(r"[A-Za-z0-9+/=\s]+", stripped) and len(stripped) > 24:
            try:
                data = base64.b64decode(stripped, validate=True)
            except ValueError:
                data = stripped.encode("latin1", errors="ignore")
        magic_ok = (
            data.startswith(b"\x89PNG\r\n\x1a\n")
            or data.startswith(b"\xff\xd8\xff")
            or data.startswith(b"GIF87a")
            or data.startswith(b"GIF89a")
            or data.startswith(b"RIFF") and b"WEBP" in data[:16]
        )
        dims = _image_dimensions(data)
        size_ok = dims is not None and dims[0] >= 64 and dims[1] >= 64
        image_ok = magic_ok and size_ok
        return (
            image_ok,
            "image asset 头部和尺寸合法。" if image_ok else f"{file_path} 不是有效图片或尺寸小于 64x64。",
            "图片 asset 必须是有效图片字节或 base64，且尺寸 >= 64x64。",
        )
    if ext == ".pdf":
        pdf_ok = stripped.startswith("%PDF-") and "%%EOF" in stripped and len(stripped.encode("latin1", errors="ignore")) > 100
        return (
            pdf_ok,
            "PDF asset 结构合法。" if pdf_ok else f"{file_path} 必须以 %PDF- 开头、包含 %%EOF 且大于 100 bytes。",
            "PDF asset 必须是有效、非空 PDF 内容。",
        )
    if ext in {".md", ".txt"}:
        quality_ok = len(stripped) >= 40 and not _REFERENCE_PLACEHOLDER_RE.search(stripped)
        return (
            quality_ok,
            "Markdown/text asset 满足最低质量要求。" if quality_ok else f"{file_path} 文本资源过短或包含占位短语。",
            "Markdown/text asset 至少 40 个字符且不能包含占位短语。",
        )
    return True, "asset 格式可解析。", "当前 asset 文件内容必须符合其扩展名对应格式。"



def _image_dimensions(data: bytes) -> tuple[int, int] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data.startswith(b"\xff\xd8"):
        idx = 2
        while idx + 9 < len(data):
            if data[idx] != 0xFF:
                idx += 1
                continue
            marker = data[idx + 1]
            idx += 2
            if marker in {0xD8, 0xD9}:
                continue
            if idx + 2 > len(data):
                break
            length = int.from_bytes(data[idx:idx + 2], "big")
            if length < 2:
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and idx + 7 <= len(data):
                height = int.from_bytes(data[idx + 3:idx + 5], "big")
                width = int.from_bytes(data[idx + 5:idx + 7], "big")
                return width, height
            idx += length
    return None


def _check_asset_file_contract(file_path: str, content: str) -> list[ContractCheckResult]:
    stripped = content.strip()
    has_runtime_code = bool(_PLATFORM_IMAGE_HELPER_RE.search(stripped))
    parse_ok, parse_message, parse_expected = _asset_extension_check(file_path, stripped)
    return [
        ContractCheckResult(
            id="asset.not_empty",
            passed=bool(stripped),
            target=file_path,
            message=("asset 内容非空。" if stripped else f"{file_path} asset 内容为空。"),
            expected="输出当前 asset 的模板或静态资源内容。",
            minimal_edit="补充真实模板/静态资源内容，不要输出空壳。",
        ),
        ContractCheckResult(
            id="asset.parseable",
            passed=parse_ok,
            target=file_path,
            message=parse_message,
            expected=parse_expected,
            minimal_edit="按文件扩展名修正格式：JSON/YAML/CSV/image/PDF/Markdown 文本必须可解析且非空。",
        ),
        ContractCheckResult(
            id="asset.no_runtime_capability",
            passed=not has_runtime_code,
            target=file_path,
            message=(
                "asset 未包含运行时图片生成能力。"
                if not has_runtime_code
                else f"{file_path} 是 asset，但包含图片生成 helper/运行时代码。"
            ),
            expected="asset 只能是模板或静态资源，不得执行 image_generation 等能力。",
            minimal_edit="删除运行时代码或将该职责拆分为 scripts/ 文件。",
        ),
    ]


def _validate_asset_file_contract(file_path: str, content: str) -> None:
    results = _check_asset_file_contract(file_path, content)
    if any(not result.passed for result in results):
        raise ContractValidationError(_format_contract_failures(results).replace("SKILL.md contract", f"{file_path} contract"), results)



def _script_uses_registry_helpers(content: str, capability: str) -> bool:
    cap = get_tool_capability(capability)
    if not cap or not cap.helper_imports:
        return False
    helper_pattern = "|".join(re.escape(helper) for helper in sorted(cap.helper_imports, key=len, reverse=True))
    return bool(re.search(rf"\b(?:{helper_pattern})\b", content, re.IGNORECASE))



_FORBIDDEN_GUESSED_HELPER_IMPORTS = {
    "pdf_generation",
    "file_output",
    "platform_helpers",
    "helpers.pdf_builder",
    "tool_registry.pdf_builder",
}


def _registry_function_import_paths() -> set[str]:
    paths: set[str] = set()
    for capability in list_tool_capabilities():
        for fn in getattr(capability, "functions", []) or []:
            import_path = str(getattr(fn, "import_path", "") or "").strip()
            signature = str(getattr(fn, "signature", "") or "").strip()
            if import_path and signature:
                paths.add(import_path)
    return paths


def _python_imported_modules(content: str) -> set[str]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name:
                    modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _forbidden_guessed_helper_imports(content: str) -> list[str]:
    allowed_cards = _registry_function_import_paths()
    hits: list[str] = []
    for module in _python_imported_modules(content):
        for forbidden in _FORBIDDEN_GUESSED_HELPER_IMPORTS:
            if module == forbidden or module.startswith(forbidden + "."):
                if module not in allowed_cards:
                    hits.append(module)
    return sorted(set(hits))


def _script_satisfies_required_capability(content: str, capability: str) -> bool:
    """Statically enforce only helper_required capabilities.

    helper_preferred/self_implementation_allowed capabilities are validated by
    trial run, E2E stdout, and artifact existence checks instead of source-code
    implementation regexes.
    """
    capability = capability.lower()
    cap = get_tool_capability(capability)
    if cap and cap.usage_policy == "helper_required":
        return _script_uses_registry_helpers(content, capability)
    return True


_ARTIFACT_OUTPUT_KEYS = {"pdf_path", "docx_path", "pptx_path", "html_path", "file_paths"}
_ARTIFACT_CAPABILITIES = {"pdf_generation", "docx_generation", "pptx_generation", "html_generation", "html_asset_generation", "file_output"}


def _script_has_real_file_creation_logic(content: str, *, outputs: list[str], capabilities: list[str]) -> bool:
    """Do not infer artifact implementation from source regexes.

    Static validation keeps syntax/interface/capability boundaries; actual file
    creation is verified by trial run/E2E artifact checks.
    """
    return True



_MODEL_CAPABILITIES = {"text_generation", "image_generation"}
_DETERMINISTIC_BUILDER_ROLES = {"pdf_builder", "docx_builder", "pptx_builder", "html_asset_builder", "asset_builder"}


def _effective_required_capabilities_for_script(plan_entry: SkillPlanEntry) -> list[str]:
    """Return capabilities that this script source must visibly exercise.

    File builders/exporters are deterministic by default.  If a global
    SKILL.md/blueprint model declaration was accidentally copied into a
    builder's required_capabilities, do not turn that into a requirement for
    ``build_pdf.py`` (or sibling exporters) to call LLM/IMAGE_MODEL.  Model
    scripts keep their text/image requirements through their own generator
    roles, while builders are validated for real artifact creation.
    """
    capabilities = list(plan_entry.required_capabilities or [])
    if plan_entry.role in _DETERMINISTIC_BUILDER_ROLES:
        capabilities = [capability for capability in capabilities if capability not in _MODEL_CAPABILITIES]
    return capabilities

async def _refine_blueprint_contract_with_model(
    *,
    messages: list[dict],
    initial_plan: BlueprintPlan,
    requested_model: str | None,
    strict: bool = False,
    max_rounds: int = 3,
) -> tuple[list[dict], list[dict[str, Any]]]:
    """Phase2 pre-display blueprint contract refinement.

    只发生在蓝图展示给用户之前。

    原则：
    - 不在后台写业务规则；
    - 不让后台判断哪个脚本该产出哪个字段；
    - 不 patch FileSpecOut；
    - 模型只判断蓝图/合同是否自洽；
    - 需要修改时，模型输出 exact_replace patch；
    - 后台 apply patch 后重新 parse_blueprint；
    - 多轮直到模型认为通过、parse 通过，或达到 max_rounds。
    """

    def dump_item(item: Any) -> Any:
        if hasattr(item, "model_dump"):
            return item.model_dump()
        if hasattr(item, "dict"):
            return item.dict()
        if hasattr(item, "__dict__"):
            return dict(item.__dict__)
        return item

    def messages_to_blueprint_text(items: list[dict]) -> str:
        return "\n\n".join(
            str(message.get("content") or "")
            for message in items
            if isinstance(message, dict)
        )

    current_messages = list(messages or [])
    current_plan = initial_plan
    diagnostics: list[dict[str, Any]] = []
    carry_feedback = ""

    route = route_model(
        VALIDATOR_TASK,
        requested_model=requested_model,
        reason="creator phase2 blueprint contract refinement",
    )

    _log_creator_model_usage(
        phase="blueprint_contract_refine.route",
        file_path="__blueprint__",
        route=route,
        model=requested_model,
    )

    rounds = max(1, int(max_rounds or 1))

    for round_index in range(1, rounds + 1):
        blueprint_text = messages_to_blueprint_text(current_messages)

        plan_snapshot = {
            "skill_name": current_plan.skill_name,
            "files": [dump_item(item) for item in (current_plan.files or [])],
            "skill_plan": {
                "files": [
                    dump_item(item)
                    for item in ((current_plan.skill_plan.files if current_plan.skill_plan else []) or [])
                ]
            },
            "warnings": list(current_plan.warnings or []),
        }

        review_messages = [
            {
                "role": "system",
                "content": (
                    "你是 superskills Creator 的蓝图合同审查模型，只输出严格 JSON object。\n"
                    "你只检查当前蓝图文本和当前解析计划是否自洽，不写代码，不输出完整重写蓝图。\n"
                    "不要按固定案例套规则；只根据当前用户需求、当前蓝图和当前解析计划判断。\n"
                    "不要修改平台宿主协议。\n"
                    "不要输出 FileSpecOut patch。\n"
                    "如果蓝图已经自洽，passed=true。\n"
                    "如果不自洽，passed=false，并给出 repair_goal，后续会由 patch 模型做局部修改。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"当前轮次：{round_index}/{rounds}\n\n"
                    "当前蓝图文本：\n"
                    "```markdown\n"
                    f"{blueprint_text[-70000:]}\n"
                    "```\n\n"
                    "当前 parse_blueprint 解析结果：\n"
                    "```json\n"
                    f"{json.dumps(plan_snapshot, ensure_ascii=False, indent=2, default=str)[:70000]}\n"
                    "```\n\n"
                    + (
                        "上一轮 patch / parse 反馈：\n"
                        f"{carry_feedback[-8000:]}\n\n"
                        if carry_feedback
                        else ""
                    )
                    + (
                        "请判断蓝图与合同是否已经自洽，包括但不限于："
                        "文件职责、脚本输入输出闭环、workflow 顺序、最终产物来源、references/assets 职责边界。"
                        "这些只是检查方向，不是固定规则；你必须依据当前蓝图语义判断。\n\n"
                        "返回 JSON：\n"
                        "{\n"
                        "  \"passed\": true,\n"
                        "  \"diagnostics\": [\n"
                        "    {\"severity\": \"note|warning|error\", \"message\": \"说明\"}\n"
                        "  ],\n"
                        "  \"repair_goal\": \"如果 passed=false，说明需要对蓝图文本做的最小修改；否则为空\"\n"
                        "}"
                    )
                ),
            },
        ]

        try:
            review_raw = await complete_chat_once(review_messages, route.model)
            review = _parse_validator_json_object(review_raw)
            if not isinstance(review, dict):
                raise ValueError("蓝图审查模型未返回 JSON object。")
        except Exception as exc:
            diagnostics.append({
                "severity": "warning",
                "code": "blueprint_contract_review_failed",
                "source": "blueprint_contract_refine",
                "path": "",
                "field": "",
                "message": f"第 {round_index} 轮蓝图审查失败，保留当前蓝图：{type(exc).__name__}: {exc}",
            })
            return current_messages, diagnostics

        for item in review.get("diagnostics", []) or []:
            if isinstance(item, dict):
                diagnostics.append({
                    "severity": str(item.get("severity") or "note"),
                    "code": "blueprint_contract_refine",
                    "source": "blueprint_contract_refine",
                    "path": "",
                    "field": "",
                    "message": f"[round {round_index}] {str(item.get('message') or '')}",
                })
            else:
                diagnostics.append({
                    "severity": "note",
                    "code": "blueprint_contract_refine",
                    "source": "blueprint_contract_refine",
                    "path": "",
                    "field": "",
                    "message": f"[round {round_index}] {str(item)}",
                })

        repair_goal = str(review.get("repair_goal") or "").strip()
        passed = bool(review.get("passed")) and not repair_goal

        if passed:
            diagnostics.append({
                "severity": "note",
                "code": "blueprint_contract_refine_passed",
                "source": "blueprint_contract_refine",
                "path": "",
                "field": "",
                "message": f"蓝图合同自检在第 {round_index} 轮通过。",
            })
            return current_messages, diagnostics

        if not repair_goal:
            diagnostics.append({
                "severity": "warning",
                "code": "blueprint_contract_refine_no_goal",
                "source": "blueprint_contract_refine",
                "path": "",
                "field": "",
                "message": f"第 {round_index} 轮模型判定未通过但没有给出 repair_goal，保留当前蓝图。",
            })
            return current_messages, diagnostics

        scope = CreatorRepairScope(
            phase="phase2_blueprint_contract_refine",
            repair_type="blueprint_text_patch",
            target_file="BLUEPRINT.md",
            max_changed_lines=max(120, len(blueprint_text.splitlines()) + 40),
            notes=(
                "Phase2 展示前蓝图修复。",
                "只改蓝图文本，不写代码。",
                "使用 exact_replace old_lines/new_lines patch；old/new 单字符串仅兼容旧格式。",
                "保持用户需求，不新增平台协议。",
            ),
        )

        try:
            _proposal, candidate_text, diff_stats = await _request_and_apply_repair_patch(
                model=route.model,
                file_path="BLUEPRINT.md",
                current_content=blueprint_text,
                failure_text=(
                    "蓝图合同自检未通过，需要在展示给用户之前修正蓝图文本。\n\n"
                    f"第 {round_index} 轮 repair_goal：\n{repair_goal}\n\n"
                    "诊断信息：\n"
                    f"{json.dumps(diagnostics[-10:], ensure_ascii=False, indent=2, default=str)}"
                ),
                scope=scope,
                task_context=(
                    "当前 parse_blueprint 解析结果：\n"
                    f"{json.dumps(plan_snapshot, ensure_ascii=False, indent=2, default=str)[:70000]}"
                ),
                target_rule=(
                    "你正在修复 Creator Phase2 蓝图文本。"
                    "只能修改导致蓝图、合同、workflow 或职责不自洽的局部文本。"
                    "不要输出完整蓝图。"
                    "不要输出代码。"
                    "不要输出 FileSpecOut patch。"
                    "不要新增平台宿主协议。"
                    "必须使用 exact_replace old_lines/new_lines patch；old/new 单字符串仅兼容旧格式。"
                ),
                patch_retry_limit=3,
            )
        except Exception as exc:
            diagnostics.append({
                "severity": "warning",
                "code": "blueprint_contract_patch_failed",
                "source": "blueprint_contract_refine",
                "path": "",
                "field": "",
                "message": f"第 {round_index} 轮蓝图 patch 失败，保留当前蓝图：{type(exc).__name__}: {exc}",
            })
            return current_messages, diagnostics

        try:
            next_messages = [{"role": "user", "content": candidate_text}]
            next_plan = parse_blueprint(next_messages, strict=strict)
        except BlueprintShapeError as exc:
            carry_feedback = (
                f"上一轮 patch 后 parse_blueprint 失败：{exc}\n"
                "请基于原蓝图重新给出更小、更安全的 exact_replace patch。"
            )
            diagnostics.append({
                "severity": "warning",
                "code": "blueprint_contract_patch_parse_failed",
                "source": "blueprint_contract_refine",
                "path": "",
                "field": "",
                "message": f"第 {round_index} 轮 patch 后无法重新解析，继续下一轮：{exc}",
            })
            continue

        current_messages = next_messages
        current_plan = next_plan
        carry_feedback = ""

        diagnostics.append({
            "severity": "note",
            "code": "blueprint_contract_patch_applied",
            "source": "blueprint_contract_refine",
            "path": "",
            "field": "",
            "message": (
                f"第 {round_index} 轮蓝图 patch 已应用并重新解析成功："
                f"{json.dumps(diff_stats, ensure_ascii=False, default=str)[:1200]}"
            ),
        })

    diagnostics.append({
        "severity": "warning",
        "code": "blueprint_contract_refine_round_limit",
        "source": "blueprint_contract_refine",
        "path": "",
        "field": "",
        "message": f"蓝图合同自检达到 {rounds} 轮上限，返回最后一次可解析蓝图。",
    })

    return current_messages, diagnostics

def _script_required_capability_failures(content: str, capabilities: list[str]) -> list[str]:
    return [capability for capability in capabilities if not _script_satisfies_required_capability(content, capability)]

def _check_script_file_contract(
    file_path: str,
    content: str,
    role: str | None = None,
    skill_plan_entry: dict[str, Any] | None = None,
) -> list[ContractCheckResult]:
    """First-round generic script file contract checks.

    这是 Creator 第一轮“单脚本文件合同”检查，只做通用平台协议与安全检查。

    不做：
    - 不按 role 名称判断职责路线；
    - 不按业务 capability 名称判断是否允许调用某类工具；
    - 不按业务输出字段名判断是否生成某类产物；
    - 不通过业务词表判断写作、画图、PDF、数据库等任务；
    - 不把 SkillPlanEntry.inputs / outputs 的具体字段名当 hard gate。

    责任完成度由 _run_script_responsibility_review 判断；
    字段名、上下游映射、真实参数消费由第二轮 E2E 判断；
    artifact 真实存在与格式由 smoke/E2E 判断。
    """

    plan_entry = _skill_plan_entry_for_file(
        file_path=file_path,
        role=role,
        skill_plan_entry=skill_plan_entry,
    )
    strict_interface = skill_plan_entry is not None
    stripped = content.strip()
    effective_required_capabilities = _effective_required_capabilities_for_script(plan_entry)

    has_markdown_or_bundle = (
        "```" in stripped
        or "~~~" in stripped
        or bool(_MULTI_FILE_MARKER_RE.search(stripped))
    )
    raw_ok = bool(stripped) and not has_markdown_or_bundle

    results: list[ContractCheckResult] = [
        ContractCheckResult(
            id="script.raw_source.single_file",
            passed=raw_ok,
            target=file_path,
            message=(
                "脚本是单个裸源码文件。"
                if raw_ok
                else f"{file_path} 生成内容包含 Markdown 代码块或多文件包，不是单个脚本源码。请重新生成该文件。"
            ),
            expected="只输出单个脚本源码本身，不要 Markdown fence、说明文字、写入文件标签或多文件包。",
            minimal_edit="从上一次内容中只保留目标脚本源码；删除所有 ``` fence、文件路径标题、写入文件标签和说明文字。",
        )
    ]

    if not raw_ok:
        return results

    # ------------------------------------------------------------------
    # 1. Runtime syntax / entry checks
    # ------------------------------------------------------------------

    syntax_ok = True
    syntax_message = f"{plan_entry.language} 源码基础校验通过。"
    syntax_expected = "脚本源码必须符合 language/runtime 的基础语法与入口约定。"

    if plan_entry.language == "python":
        try:
            ast.parse(stripped)
        except SyntaxError as exc:
            syntax_ok = False
            syntax_message = f"{file_path} 生成内容不是合法 Python 源码: {exc.msg}"
        syntax_expected = "Python 脚本必须能通过 ast.parse 语法检查。"

    elif plan_entry.runtime == "node":
        syntax_ok = "process.argv" in stripped and "console.log" in stripped
        syntax_message = (
            "Node/JS 脚本包含 process.argv 和 stdout 输出。"
            if syntax_ok
            else f"{file_path} Node/JS 脚本必须使用 process.argv 读取 argv 并通过 console.log 输出结果。"
        )
        syntax_expected = "Node/JS 脚本必须使用 process.argv 读取参数，并通过 console.log 输出可解析结果。"

    elif plan_entry.runtime in {"bash", "shell"}:
        syntax_ok = "$1" in stripped or "${1" in stripped
        syntax_message = (
            "Shell/Bash 脚本读取 $1 argv。"
            if syntax_ok
            else f"{file_path} Shell/Bash 脚本必须读取 $1 argv。"
        )
        syntax_expected = "Shell/Bash 脚本必须读取 $1 参数，并向 stdout 输出结果或写入声明产物。"

    results.append(
        ContractCheckResult(
            id="script.source.syntax",
            passed=syntax_ok,
            target=file_path,
            message=syntax_message,
            expected=syntax_expected,
            minimal_edit="修正源码语法/入口错误，同时保持 stdout JSON 和参数接口不变。",
        )
    )

    if strict_interface:
        reads_json = _script_reads_json_argv(stripped, plan_entry.runtime)
        results.append(
            ContractCheckResult(
                id="script.json_argv.runtime",
                passed=reads_json,
                target=file_path,
                message=(
                    f"脚本按 {plan_entry.runtime} runtime 读取 JSON argv。"
                    if reads_json
                    else f"{file_path} 必须按 {plan_entry.runtime} runtime 读取 JSON argv。"
                ),
                expected="Python: sys.argv[1]+json.loads；Node: process.argv[2]+JSON.parse；Bash: $1 JSON。",
                minimal_edit="补充 runtime 对应 JSON argv 解析入口。",
            )
        )

        # 字段名只做 warning，不做 hard gate。
        # 第一轮只判断单脚本是否可运行、是否有内容责任实现；
        # 具体字段名和最终 SKILL.md block 对齐交给 E2E。
        missing_inputs = [
            str(key)
            for key in (plan_entry.inputs or [])
            if str(key or "").strip() and str(key) not in stripped
        ]
        results.append(
            ContractCheckResult(
                id="script.skillplan_inputs.recommended_names",
                passed=True,
                target=file_path,
                message=(
                    "脚本源码未发现明显缺失的推荐输入名。"
                    if not missing_inputs
                    else (
                        "warning: 脚本源码未静态引用部分推荐输入名："
                        f"{', '.join(missing_inputs)}；第一轮不阻断，字段名映射由 E2E 验证。"
                    )
                ),
                expected=(
                    "SkillPlanEntry.inputs 只作为推荐变量名和语义提示；"
                    "第一轮不得把具体字段名作为 hard gate。"
                ),
                minimal_edit=(
                    "仅当责任审查确认输入内容没有影响核心输出，或 E2E 发现最终参数映射失败时，"
                    "才修当前脚本参数读取与输出逻辑。"
                ),
            )
        )

        has_entry = _script_has_main_entry(stripped, plan_entry.runtime)
        results.append(
            ContractCheckResult(
                id="script.runtime.entrypoint",
                passed=has_entry,
                target=file_path,
                message=(
                    "脚本包含 runtime 入口与 stdout 输出。"
                    if has_entry
                    else f"{file_path} 缺少 {plan_entry.runtime} 入口或 stdout 输出。"
                ),
                expected="脚本包含对应 runtime 的入口函数/语句，并向 stdout 输出 JSON。",
                minimal_edit="补齐 main/入口调用和 JSON stdout 输出。",
            )
        )

    # ------------------------------------------------------------------
    # 2. Tool registry grounding warnings only.
    # ------------------------------------------------------------------

    tool_resolve = resolve_tools_for_skill_plan_entry(plan_entry)

    guessed_helper_imports = (
        _forbidden_guessed_helper_imports(stripped)
        if plan_entry.language == "python"
        else []
    )
    results.append(
        ContractCheckResult(
            id="tool_usage_contract.unverified_helper_import",
            passed=True,
            target=file_path,
            message=(
                "脚本未发现疑似猜测 helper import path。"
                if not guessed_helper_imports
                else (
                    f"warning: {file_path} import 了未由 Tool Registry function card 明确提供 "
                    f"import path 和调用签名的 helper：{', '.join(guessed_helper_imports)}；"
                    "第一轮不因此阻断，实际以 import/trial run 结果为准。"
                )
            ),
            expected=(
                "第一轮不按 helper import 路线做 hard validation；"
                "仅在 import 失败、调用失败或 stdout/artifact 不满足合同时失败。"
            ),
            minimal_edit=(
                "如 trial run/import 失败，只修当前脚本的 import/call；"
                "不要修改 SkillPlan、capability 声明或 workflow。"
            ),
        )
    )

    helper_required_capabilities = [
        capability
        for capability in effective_required_capabilities
        if (
            get_tool_capability(capability)
            and get_tool_capability(capability).usage_policy == "helper_required"
        )
    ]
    missing_capabilities = _script_required_capability_failures(
        stripped,
        helper_required_capabilities,
    )
    results.append(
        ContractCheckResult(
            id="script.required_capabilities.route_warning",
            passed=True,
            target=file_path,
            message=(
                "第一轮不强制 helper_required/required_capabilities 的具体工具路线。"
                if not missing_capabilities
                else (
                    "warning: 脚本未调用这些 helper_required 能力对应接口："
                    f"{', '.join(missing_capabilities)}；第一轮不阻断，"
                    "实际以 stdout/artifact/trial run 闭环为准。"
                )
            ),
            expected=(
                "scripts/** 可按责任模块自主组合已有工具；"
                "第一轮不使用 required/optional/allowed_capabilities 卡死实现路线。"
            ),
            minimal_edit=(
                "repair 阶段只修当前脚本的 argv/run/stdout/artifact/import/输入使用/真实实现问题；"
                "不要修 SkillPlan 或 capability 声明。"
            ),
        )
    )

    # forbidden_capabilities 只做 registry 层面的通用 warning。
    # 不对任何具体 capability 名称做特殊分支。
    registry_forbidden_helper_hits: list[str] = []
    for forbidden_capability in plan_entry.forbidden_capabilities or []:
        capability_name = str(forbidden_capability or "").strip()
        if not capability_name:
            continue
        if _script_uses_registry_helpers(stripped, capability_name):
            registry_forbidden_helper_hits.append(capability_name)

    results.append(
        ContractCheckResult(
            id="tool_usage_contract.forbidden_registry_helpers",
            passed=True,
            target=file_path,
            message=(
                "脚本未调用 forbidden_capabilities 中禁止的 registry helper。"
                if not registry_forbidden_helper_hits
                else (
                    f"warning: {file_path} 调用了这些 forbidden_capabilities 对应的 registry helper："
                    f"{', '.join(registry_forbidden_helper_hits)}；第一轮不阻断，"
                    "如运行或安全 gate 失败再修。"
                )
            ),
            expected=(
                "第一轮不按 capability/helper 路线阻断；"
                "真实失败以 import/dependency、运行、stdout、artifact 合同和安全 gate 为准。"
            ),
            minimal_edit=(
                "如该 helper 导致运行失败，只修当前脚本；"
                "不要扩大 SkillPlan/capability 声明。"
            ),
        )
    )

    forbidden_direct_hits = [
        item
        for item in tool_resolve.forbidden_imports
        if re.search(rf"\b{re.escape(item)}\b", stripped, re.IGNORECASE)
    ]
    results.append(
        ContractCheckResult(
            id="tool_usage_contract.forbidden_direct_imports",
            passed=True,
            target=file_path,
            message=(
                "脚本未绕过平台 helper 直接调用被禁止的底层工具库。"
                if not forbidden_direct_hits
                else (
                    f"warning: {file_path} 直接调用了 Tool Resolve 禁止的底层工具/库："
                    f"{', '.join(forbidden_direct_hits)}；第一轮不阻断，"
                    "实际以 dependency/import/trial run 和安全 gate 为准。"
                )
            ),
            expected=(
                "第一轮不因工具路线选择阻断；"
                "底层库是否可用由 dependency/import/trial run 和 artifact 合同验证。"
            ),
            minimal_edit="如 dependency/import/trial run 失败，只修当前脚本依赖和调用路径。",
        )
    )

    undeclared_helper_hits: list[str] = []
    declared_caps = (
        set(effective_required_capabilities)
        | set(plan_entry.optional_capabilities or [])
        | set(plan_entry.allowed_capabilities or [])
    )
    for capability in [cap.name for cap in list_tool_capabilities() if cap.helper_imports]:
        if capability not in declared_caps and _script_uses_registry_helpers(stripped, capability):
            undeclared_helper_hits.append(capability)

    results.append(
        ContractCheckResult(
            id="tool_usage_contract.undeclared_helper",
            passed=True,
            target=file_path,
            message=(
                "脚本未调用未声明 registry helper，或无需记录 warning。"
                if not undeclared_helper_hits
                else (
                    f"warning: {file_path} 调用了未在 required/optional/allowed_capabilities "
                    f"声明的工具能力：{', '.join(undeclared_helper_hits)}；第一轮不阻断。"
                )
            ),
            expected=(
                "undeclared_helper 降级为 warning；"
                "scripts/** 可根据自身责任自主组合已有工具。"
            ),
            minimal_edit=(
                "不要修 SkillPlan、capability 声明或 workflow；"
                "仅当调用本身失败或 stdout/artifact 不满足合同时修当前脚本。"
            ),
        )
    )

    # ------------------------------------------------------------------
    # 3. Artifact creation: no static business inference.
    # ------------------------------------------------------------------

    artifact_required = bool(_ARTIFACT_CAPABILITIES & set(effective_required_capabilities))
    artifact_outputs_declared = bool(_ARTIFACT_OUTPUT_KEYS & set(plan_entry.outputs or []))
    enforce_artifact_outputs = strict_interface or artifact_required or artifact_outputs_declared

    has_real_file_output = _script_has_real_file_creation_logic(
        stripped,
        outputs=list(plan_entry.outputs or []) if enforce_artifact_outputs else [],
        capabilities=effective_required_capabilities if enforce_artifact_outputs else [],
    )
    results.append(
        ContractCheckResult(
            id="script.file_outputs.runtime_verified",
            passed=True,
            target=file_path,
            message=(
                "文件产物实现方式不由第一轮源码词表判断；真实产物由 smoke/E2E 校验。"
                if has_real_file_output
                else (
                    f"warning: {file_path} 可能声明或暗含文件产物输出；"
                    "第一轮仅提示风险，真实产物由 smoke/E2E 校验。"
                )
            ),
            expected=(
                "第一轮不通过业务词表判断文件产物；"
                "如声明 artifact 输出，后续 smoke/E2E 必须验证真实文件、路径返回和平台可消费性。"
            ),
            minimal_edit=(
                "如 smoke/E2E 失败，再修脚本确保运行时创建真实文件，"
                "并通过平台可消费字段返回路径。"
            ),
        )
    )

    results.append(
        ContractCheckResult(
            id="tool_usage_contract.artifact_output_e2e",
            passed=True,
            target=file_path,
            message=(
                f"{file_path} 的产物实现方式不由 Creator 后台源码正则判断；"
                "最终由试运行/E2E 校验真实产物与 stdout 字段。"
            ),
            expected=(
                "如果当前脚本职责包含文件产物，必须在运行时创建真实文件，"
                "并通过平台可消费字段返回路径。"
            ),
            minimal_edit=(
                "保持 argv/stdout 协议；如 smoke/E2E 失败，"
                "修复真实文件创建、路径返回和平台字段。"
            ),
        )
    )

    # ------------------------------------------------------------------
    # 4. Fake implementation is a responsibility concern, not keyword hard gate.
    # ------------------------------------------------------------------

    has_fake = bool(_SCRIPT_FAKE_IMPLEMENTATION_RE.search(stripped))
    results.append(
        ContractCheckResult(
            id="script.no_fake_implementation.warning",
            passed=True,
            target=file_path,
            message=(
                "脚本未发现明显占位/模拟/假实现信号。"
                if not has_fake
                else (
                    f"warning: {file_path} 包含占位/模拟/假实现信号；"
                    "第一轮文件合同不因此阻断，由责任审查和 smoke/E2E 判断是否真实完成职责。"
                )
            ),
            expected=(
                "第一轮不使用 placeholder/mock/template 关键词正则作为 hard gate；"
                "真实失败由责任审查、运行、stdout required outputs 和 artifact 验证决定。"
            ),
            minimal_edit=(
                "仅当脚本责任审查、运行闭环、stdout 合同或 artifact 真实生成失败时修当前脚本。"
            ),
        )
    )

    # ------------------------------------------------------------------
    # 5. Generic security gate.
    # ------------------------------------------------------------------

    dangerous_import_hits = sorted(set(re.findall(
        r"^\s*(?:import|from)\s+(paramiko|ftplib|telnetlib|subprocess)\b",
        stripped,
        re.MULTILINE,
    )))
    dangerous_call_hits = sorted(set(re.findall(
        r"\b(?:os\.system|subprocess\.(?:run|Popen|call|check_call|check_output))\b",
        stripped,
    )))
    forbidden_path_hits = sorted(set(re.findall(
        r"[\'\"]((?:/etc/passwd|/etc/shadow|/root/\.ssh/[^\'\"]*|~/.ssh/[^\'\"]*|\.\./[^\'\"]*))[\'\"]",
        stripped,
    )))
    security_hits = [*dangerous_import_hits, *dangerous_call_hits, *forbidden_path_hits]

    results.append(
        ContractCheckResult(
            id="script.security.dangerous_operations",
            passed=not security_hits,
            target=file_path,
            message=(
                "脚本未包含危险 import、shell 调用或禁止路径访问。"
                if not security_hits
                else f"{file_path} 包含危险操作或禁止路径：{', '.join(security_hits)}。"
            ),
            expected=(
                "第一轮不审查业务工具路线，但安全风险、危险 import、禁止路径和恶意 shell 必须阻断。"
            ),
            minimal_edit=(
                "移除危险 import/shell/禁止路径访问；"
                "只保留当前脚本职责所需的安全本地逻辑或受控 helper 调用。"
            ),
        )
    )

    return results


_SCRIPT_CONTENT_REVIEW_CHECK_IDS = {
    "script.raw_source.single_file",
    "script.source.syntax",
    "script.security.dangerous_operations",
}


def _check_script_content_review_contract(
    file_path: str,
    content: str,
    role: str | None = None,
    skill_plan_entry: dict[str, Any] | None = None,
) -> list[ContractCheckResult]:
    """First script phase: deterministic protocol/security review only.

    Runtime truth still comes from the single-script smoke phase, but syntax,
    JSON argv parsing shape, entrypoint shape, and dangerous operations are cheap
    deterministic gates before trial execution.
    """
    return [
        result
        for result in _check_script_file_contract(
            file_path,
            content,
            role=role,
            skill_plan_entry=skill_plan_entry,
        )
        if result.id in _SCRIPT_CONTENT_REVIEW_CHECK_IDS
    ]


def _validate_script_file_source_contract(file_path: str, content: str, role: str | None = None, skill_plan_entry: dict[str, Any] | None = None) -> None:
    # Accept otherwise-valid raw source with a dangling orphan fence marker at
    # the boundary.  Full fenced/bundled responses are still rejected by the
    # lower-level checker unless the sanitize path extracted a single code block.
    candidate = _strip_orphan_trailing_fence(content)
    results = _check_script_file_contract(file_path, candidate, role=role, skill_plan_entry=skill_plan_entry)
    if any(not result.passed for result in results):
        raise ContractValidationError(_format_contract_failures(results).replace("SKILL.md contract", f"{file_path} contract"), results)

def _validate_skill_md_against_existing_files(
    skill_name: str,
    content: str,
    *,
    blueprint_text: str = "",
    require_existing: bool = True,
) -> None:
    skill_name = _validate_skill_name(skill_name)
    skill_root = settings.skills_path / skill_name

    try:
        referenced_paths = sorted(set(_skill_local_paths_in_markdown(content)))
    except Exception as exc:
        raise ValueError(f"SKILL.md 本地路径扫描失败：{exc}") from exc

    ignored_dirs = [
        path for path in referenced_paths if _is_directory_like_skill_path(path)
    ]
    materialized_paths = [
        path for path in referenced_paths if _is_materialized_skill_resource_path(path)
    ]

    if ignored_dirs:
        logger.info(
            "[Creator][skill_md] 忽略目录型路径，不做上传校验 skill=%s paths=%s",
            skill_name,
            ignored_dirs,
        )

    if not require_existing:
        return

    missing: list[str] = []
    for rel_path in materialized_paths:
        abs_path = (skill_root / rel_path).resolve()
        try:
            abs_path.relative_to(skill_root.resolve())
        except ValueError:
            missing.append(rel_path)
            continue
        if not abs_path.exists() or not abs_path.is_file():
            missing.append(rel_path)

    if missing:
        result = ContractCheckResult(
            id="skill_md.resource.exists_on_disk",
            passed=False,
            target="SKILL.md",
            message="SKILL.md 引用了最终打包时仍不存在的本地资源：" + ", ".join(missing),
            expected="最终打包前，SKILL.md 引用的 scripts/references/assets 具体文件必须生成或上传；目录路径不检查。",
            minimal_edit="生成缺失的 scripts/references，上传缺失 assets 文件，或删除 SKILL.md 中对应引用。",
        )
        raise ContractValidationError(
            "SKILL.md contract 未通过：\n" + _format_contract_failures_safe([result]),
            [result],
        )


def _clean_blueprint_for_file_prompt(blueprint_text: str) -> str:
    """Remove Creator UI confirmation text from blueprint context before generation."""
    cleaned_lines: list[str] = []
    in_confirmation_block = False
    for line in (blueprint_text or "").splitlines():
        stripped = line.strip()
        if _CREATOR_FLOW_LEAK_RE.search(stripped):
            in_confirmation_block = True
            continue
        if in_confirmation_block:
            if stripped.startswith("```") or stripped.startswith("- [") or stripped.startswith(">"):
                continue
            if not stripped:
                in_confirmation_block = False
                continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip() or blueprint_text


def _reject_fake_script_implementation(file_path: str, content: str) -> None:
    """Reject placeholder/mock scripts that pretend to implement capabilities."""
    if _SCRIPT_FAKE_IMPLEMENTATION_RE.search(content):
        raise ValueError(
            f"{file_path} 包含占位/模拟/假 API 实现。"
            "Creator 生成的脚本必须具备真实可执行功能；"
            "如需图像或多模态能力，应通过宿主配置的模型/服务完成，不能写 placeholder 文件或假装调用 API。"
        )


def _requires_configured_model_call(*, plan_entry: SkillPlanEntry | None) -> bool:
    """Return whether the current script contract requires host model use.

    Model-call requirements are scoped to this script's SkillPlanEntry.
    Whole-SKILL.md wording about LLM/image models can describe earlier or later
    steps, but must not force deterministic exporter/builder scripts to call a
    model unless their own required_capabilities declare text/image generation.
    """
    if plan_entry is None:
        return False
    return bool({"text_generation", "image_generation"} & set(_effective_required_capabilities_for_script(plan_entry)))


def _script_uses_configured_model(content: str) -> bool:
    """Detect whether script calls the configured host LLM/VL endpoint."""
    return bool(_CONFIGURED_MODEL_CALL_RE.search(content))


def _validate_configured_model_usage_static(*, file_path: str, content: str, skill_md: str, plan_entry: SkillPlanEntry | None = None) -> None:
    """Reject scripts whose own SkillPlanEntry requires host-model behavior but do not call models."""
    if _DIRECT_IMAGE_API_RE.search(content) and "VISION_MODEL" in content:
        raise ValueError(
            f"{file_path} 将 VISION_MODEL 与图片生成接口混用。"
            "生成图片必须使用平台 Stable Diffusion 图片运行时或 IMAGE_MODEL；"
            "VISION_MODEL 只用于看图理解/OCR/多模态问答。"
        )


    if _DATA_URI_RE.search(content):
        raise ValueError(
            f"{file_path} 输出 base64 data URI。"
            "图片结果必须由平台运行时写入 OUTPUT_DIR，并在 stdout JSON 中返回 image_paths。"
        )

    if re.search(r"(?m)^\s*image_path\s*=\s*generate_stable_diffusion_image\s*\(", content):
        raise ValueError(
            f"{file_path} 将 helper 返回 dict 直接赋给 image_path。"
            "图片脚本必须先保存 result = generate_stable_diffusion_image(desc)，"
            "再执行 image_paths.append(result.get(\"image_path\"))。"
        )

    effective_required_capabilities = _effective_required_capabilities_for_script(plan_entry) if plan_entry else []
    if plan_entry and plan_entry.role in {"pdf_builder", "docx_builder", "pptx_builder", "html_asset_builder", "asset_builder"} and not ({"text_generation", "image_generation"} & set(effective_required_capabilities)):
        return
    if not _requires_configured_model_call(plan_entry=plan_entry):
        return
    if _script_uses_configured_model(content):
        return
    raise ValueError(
        f"{file_path} 的当前脚本职责/SkillPlan.required_capabilities 声明需要使用宿主/内置/配置模型，但脚本没有调用这些模型。"
        "脚本不能用固定模板、随机词表或 ASCII 图替代模型能力；"
        "请通过 LLM_BASE_URL + TEXT_MODEL 调用文本模型，需要图像/视觉能力时使用 IMAGE_MODEL/VISION_MODEL。"
    )

def _script_paths_in_shell_fenced_blocks(skill_md: str) -> set[str]:
    """Return scripts/*.py paths that appear inside shell fenced blocks."""
    paths: set[str] = set()

    for info, body in _iter_markdown_fenced_blocks(skill_md):
        if not _is_shell_fence_info(info):
            continue

        for match in re.finditer(
            r"(?<![\w./-])(scripts/[A-Za-z0-9_./-]+\.py)(?![\w./-])",
            body.replace("\\", "/"),
        ):
            paths.add(match.group(1))

    return paths


def _script_paths_outside_shell_fenced_blocks(skill_md: str) -> set[str]:
    """Return scripts/*.py paths mentioned outside shell fenced blocks.

    This is not used to decide whether a script is part of the blueprint.
    It only catches a bad SKILL.md style:
    mentioning scripts/foo.py in prose without an executable ```bash block.
    """
    text = skill_md or ""

    shell_block_bodies: list[str] = []
    for info, body in _iter_markdown_fenced_blocks(text):
        if _is_shell_fence_info(info):
            shell_block_bodies.append(body)

    text_without_shell_blocks = text
    for body in shell_block_bodies:
        text_without_shell_blocks = text_without_shell_blocks.replace(body, "")

    paths: set[str] = set()
    for match in re.finditer(
        r"(?<![\w./-])(scripts/[A-Za-z0-9_./-]+\.py)(?![\w./-])",
        text_without_shell_blocks.replace("\\", "/"),
    ):
        paths.add(match.group(1))

    return paths


def _validate_command_is_single_shell_json_invocation(
    *,
    command: str,
    script_path: str,
    entry: SkillPlanEntry,
    upstream_available_outputs: set[str] | None = None,
) -> list[ContractCheckResult]:
    """Validate one shell fenced command under Creator JSON argv protocol.

    - 阻断：不是单行命令；
    - 阻断：不能解析成 runner 调用 scripts/*.py；
    - 阻断：脚本路径后没有可解析为 object 的 JSON argv。
    """
    results: list[ContractCheckResult] = []
    target = script_path

    raw_command = command or ""
    lines = [line.strip() for line in raw_command.strip().splitlines() if line.strip()]
    one_line = len(lines) == 1

    results.append(ContractCheckResult(
        id="skill_md.command_block.single_command",
        passed=one_line,
        target=target,
        message=(
            f"{script_path} 命令块只包含一条命令。"
            if one_line
            else f"{script_path} 命令块应只包含一条命令，不要在一个 block 里写多条命令或解释。"
        ),
        expected="每个 ```bash fenced block 内只放一条真实 shell 命令。",
        minimal_edit="把解释移出 fenced block；一个 block 只保留一条调用 scripts/*.py 的 shell 命令。",
    ))

    if not one_line:
        return results

    command_line = lines[0]

    try:
        command_sig = _command_signature(command_line, script_path)
    except Exception as exc:
        logger.warning(
            "[Creator][skill_md] command signature parser crashed script=%s command=%s error=%s",
            script_path,
            command_line,
            exc,
        )
        command_sig = None

    parsed_ok = command_sig is not None

    results.append(ContractCheckResult(
        id="skill_md.command_block.signature_parseable",
        passed=parsed_ok,
        target=target,
        message=(
            f"{script_path} 命令块可解析为真实 scripts/*.py shell 调用。"
            if parsed_ok
            else f"{script_path} 命令块无法解析为真实 scripts/*.py shell 调用。"
        ),
        expected=(
            "命令应是一条真实 shell 命令，并直接调用 scripts/*.py，脚本路径后跟 JSON object argv。"
        ),
        minimal_edit=(
            f"改为调用真实脚本的 shell 命令，例如：python {script_path} '<JSON object>'。"
        ),
    ))

    if not command_sig:
        return results

    arg_mode = str(command_sig.get("arg_mode") or "")
    args = list(command_sig.get("args") or [])

    json_arg_ok = arg_mode == "json_arg"

    results.append(ContractCheckResult(
        id="skill_md.command_block.args_parseable",
        passed=json_arg_ok,
        target=target,
        message=(
            f"{script_path} 命令参数形态可接受：{arg_mode or 'unknown'}。"
            if json_arg_ok
            else f"{script_path} 看起来使用 JSON argv，但 JSON 无法解析。"
        ),
        expected=(
            "脚本路径后必须传一个 json.loads 可解析的 JSON object argv。"
        ),
        minimal_edit=(
            "只修当前命令参数。不要固定套用 payload/user_request/fields/options/input_files。"
        ),
        details={
            "arg_mode": arg_mode,
            "args": args,
        },
    ))

    try:
        runtime_matches = _command_runtime_matches(command_line, script_path, entry)
    except Exception as exc:
        runtime_matches = False
        logger.warning(
            "[Creator][skill_md] runtime match check crashed script=%s command=%s error=%s",
            script_path,
            command_line,
            exc,
        )

    if not runtime_matches:
        logger.info(
            "[Creator][skill_md] non-blocking runtime mismatch script=%s inferred_runtime=%s command=%s",
            script_path,
            getattr(entry, "runtime", ""),
            command_line,
        )

    return results


def _check_skill_md_fenced_command_contracts(
    *,
    content: str,
    blueprint_text: str,
    required_script_paths: list[str] | None = None,
) -> list[ContractCheckResult]:
    """Validate fenced command style for SKILL.md.

    这里是格式/可解析性校验，不做蓝图语义判断。
    任何内部异常都转换成 ContractCheckResult，避免直接崩溃。
    """
    results: list[ContractCheckResult] = []

    source_required_paths = required_script_paths
    if source_required_paths is None:
        source_required_paths = [
            path for path in _declared_skill_paths_from_blueprint(blueprint_text)
            if path.startswith("scripts/")
        ]

    required = {
        path.replace("\\", "/").strip()
        for path in (source_required_paths or [])
        if isinstance(path, str) and path.replace("\\", "/").strip().startswith("scripts/")
    }

    mentioned = {
        path
        for path in _skill_local_paths_in_markdown(content)
        if isinstance(path, str) and path.startswith("scripts/")
    }

    scripts_to_check = sorted(required or mentioned)

    entries_by_path: dict[str, SkillPlanEntry] = {}
    try:
        parsed = parse_blueprint([{"role": "assistant", "content": blueprint_text}])
        if parsed.skill_plan:
            entries_by_path = {
                entry.path: entry
                for entry in parsed.skill_plan.files
                if entry.file_type == "script"
            }
    except Exception as exc:
        logger.warning("[Creator][skill_md] failed to parse blueprint SkillPlan for command validation: %s", exc)

    if entries_by_path:
        scripts_to_check = [entry.path for entry in entries_by_path.values() if entry.path in scripts_to_check]

    prior_outputs: set[str] = set()

    for script_path in scripts_to_check:
        try:
            commands = _extract_script_command_templates(content, script_path)
        except Exception as exc:
            results.append(ContractCheckResult(
                id="skill_md.command_block.extract_crashed",
                passed=False,
                target=script_path,
                message=f"{script_path} 命令块提取失败：{exc}",
                expected="能够从 SKILL.md 中提取该脚本对应的标准 ```bash fenced code block。",
                minimal_edit=(
                    f"为 {script_path} 添加独立、无缩进的标准命令块，例如：\n"
                    f"```bash\npython {script_path} '{{\"arg_name\":\"arg_value_or_placeholder\"}}'\n```"
                ),
            ))
            continue

        has_fenced = bool(commands)

        results.append(ContractCheckResult(
            id="skill_md.script_command.exists",
            passed=has_fenced,
            target=script_path,
            message=(
                f"{script_path} 已使用 ```bash fenced code block 表达可执行命令。"
                if has_fenced
                else f"{script_path} 缺少可执行 Markdown 命令块：标准 ```bash fenced code block。"
            ),
            expected=(
                "真实脚本必须用标准 Markdown fenced code block 表示，并传入 JSON object argv。"
            ),
            minimal_edit=(
                f"为 {script_path} 添加独立、无缩进的 ```bash fenced block。"
            ),
        ))

        results.append(ContractCheckResult(
            id="skill_md.command_block.fenced_exists",
            passed=has_fenced,
            target=script_path,
            message=(
                f"{script_path} 已使用 ```bash fenced code block 表达可执行命令。"
                if has_fenced
                else f"{script_path} 缺少可执行 Markdown 命令块：标准 ```bash fenced code block。"
            ),
            expected=(
                "真实脚本必须用标准 Markdown fenced code block 表示，例如：\n"
                f"```bash\npython {script_path} '{{\"arg_name\":\"arg_value_or_placeholder\"}}'\n```"
            ),
            minimal_edit=(
                f"为 {script_path} 添加独立、无缩进的 ```bash fenced block；"
                "不要只在正文中写“调用脚本”。"
            ),
        ))

        if not commands:
            continue

        try:
            entry = entries_by_path.get(script_path) or _skill_plan_entry_for_file(
                file_path=script_path,
                blueprint_text=blueprint_text,
            )
        except Exception as exc:
            logger.warning(
                "[Creator][skill_md] failed to infer SkillPlanEntry for %s: %s",
                script_path,
                exc,
            )
            entry = SkillPlanEntry(
                path=script_path,
                role="generic_script",
                file_type="python",
                purpose="Inferred fallback entry for command validation.",
                runtime="python",
                inputs=[],
                outputs=[],
                dependencies=[],
            )

        for command in commands:
            try:
                results.extend(_validate_command_is_single_shell_json_invocation(
                    command=command,
                    script_path=script_path,
                    entry=entry,
                    upstream_available_outputs=prior_outputs,
                ))
            except Exception as exc:
                logger.exception(
                    "[Creator][skill_md] command validation crashed script=%s command=%s",
                    script_path,
                    command,
                )
                results.append(ContractCheckResult(
                    id="skill_md.command_block.validation_crashed",
                    passed=False,
                    target=script_path,
                    message=f"{script_path} 命令块校验内部异常：{exc}",
                    expected="命令块应能被解析为 runner + scripts 路径 + JSON object argv。",
                    minimal_edit=(
                        f"将命令改为标准形式：\n"
                        f"```bash\npython {script_path} '{{\"arg_name\":\"arg_value_or_placeholder\"}}'\n```"
                    ),
                ))

        prior_outputs.update(entry.outputs or [])

    return results


def _skill_md_body_structure_failures(file_path: str, content: str) -> list[dict[str, Any]]:
    """Hard-check SKILL.md frontmatter/body boundary without title wordlists."""
    text = (content or "").lstrip("\ufeff")
    failures: list[dict[str, Any]] = []

    def failure(check_id: str, message: str, expected: str, minimal_edit: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
        item = {
            "id": check_id,
            "source": "markdown_format",
            "target": file_path,
            "layer": "markdown_format",
            "message": message,
            "expected": expected,
            "minimal_edit": minimal_edit,
        }
        if details:
            item["details"] = details
        return item

    if file_path != "SKILL.md":
        return failures

    if not text.startswith("---"):
        return [failure(
            "markdown.frontmatter.missing",
            "SKILL.md 缺少 YAML frontmatter。",
            "SKILL.md 必须以 YAML frontmatter 开始，并在正文前用单独一行 --- 闭合。",
            "重新生成完整 SKILL.md：frontmatter 只放 metadata，闭合 --- 后写入非空 Markdown 正文。",
        )]

    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return [failure(
            "markdown.frontmatter.boundary_invalid",
            "SKILL.md frontmatter 起始边界异常。",
            "第一行必须是单独的 ---。",
            "修正文件开头 frontmatter 边界，并保留闭合后的正文。",
        )]

    close_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            close_idx = idx
            break
    if close_idx is None:
        return [failure(
            "markdown.frontmatter.unclosed",
            "SKILL.md frontmatter 没有收尾 ---，正文边界无法确定。",
            "frontmatter 必须在正文前用单独一行 --- 闭合。",
            "重新生成完整 SKILL.md，确保 metadata 和正文由闭合 --- 明确分隔。",
        )]

    raw_yaml = "".join(lines[1:close_idx])
    body = "".join(lines[close_idx + 1:])
    try:
        parsed = yaml.safe_load(raw_yaml) or {}
    except Exception as exc:
        return [failure(
            "markdown.frontmatter.invalid_yaml",
            f"SKILL.md frontmatter YAML 无法解析：{type(exc).__name__}: {exc}",
            "frontmatter 必须是合法 YAML object。",
            "只修 frontmatter YAML；正文必须保留在闭合 --- 之后。",
        )]
    if not isinstance(parsed, dict):
        failures.append(failure(
            "markdown.frontmatter.not_object",
            "SKILL.md frontmatter 必须是 YAML object。",
            "frontmatter 只能承载 metadata key/value。",
            "将正文级内容移到闭合 --- 之后，并修正 metadata object。",
        ))

    if not body.strip():
        failures.append(failure(
            "markdown.body.missing",
            "SKILL.md frontmatter 后缺少非空 Markdown 正文。",
            "闭合 --- 之后必须存在真实正文；frontmatter 不能吞掉正文。",
            "重新生成完整 SKILL.md，在闭合 --- 后写入用途、执行步骤、资源和输出说明。",
        ))

    metadata_markdown_lines = [
        idx + 2 for idx, line in enumerate(lines[1:close_idx])
        if re.match(r"^\s{0,3}(#{1,6}\s+|[-*+]\s+|```|~~~|>\s+)", line)
    ]
    if metadata_markdown_lines:
        failures.append(failure(
            "markdown.frontmatter.contains_body_markdown",
            "SKILL.md frontmatter 区域包含正文级 Markdown 结构，疑似正文被 metadata 区吞掉。",
            "frontmatter 只能承载 YAML metadata；标题、列表、引用和 fenced block 必须在闭合 --- 之后。",
            "移动正文 Markdown 到闭合 --- 后，frontmatter 只保留 metadata。",
            {"line_numbers": metadata_markdown_lines[:20]},
        ))

    return failures

_SKILL_FRONTMATTER_ALLOWED_TOP_LEVEL = {"name", "description", "license", "allowed-tools", "metadata"}
_REFERENCE_FRONTMATTER_ALLOWED_TOP_LEVEL = {"title", "description", "source", "license", "metadata"}


def _hard_format_failure(
    *,
    check_id: str,
    file_path: str,
    message: str,
    expected: str,
    minimal_edit: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item = {
        "id": check_id,
        "source": "markdown_format",
        "target": file_path,
        "layer": "markdown_format",
        "severity": "hard_format",
        "repair_strategy": "full_rewrite",
        "model_patch_allowed": False,
        "message": message,
        "expected": expected,
        "minimal_edit": minimal_edit,
    }
    if details:
        item["details"] = details
    return item


def detect_markdown_hard_format_failures(file_path: str, content: str, require_frontmatter: bool) -> list[dict[str, Any]]:
    """Deterministic hard Markdown format gate.

    This gate owns global Markdown structure decisions.  Failures are tagged
    with metadata_region/body_region so repair can rewrite only the failing
    region instead of the whole Markdown file.
    """
    raw = content or ""
    text = raw.lstrip("\ufeff")
    stripped = text.strip()
    failures: list[dict[str, Any]] = []

    if not stripped:
        return [_hard_format_failure(
            check_id="markdown.file.empty",
            file_path=file_path,
            message=f"{file_path} 内容为空。",
            expected="Markdown 文件必须包含合法 frontmatter（如需要）和非空正文。",
            minimal_edit="重写 metadata_region 和 body_region 后合并为完整 Markdown 文件。",
        )]

    if re.fullmatch(r"(```|~~~)[^\n`~]*\n[\s\S]*\n\1\s*", stripped, flags=re.I):
        failures.append(_hard_format_failure(
            check_id="markdown.file.wrapped_in_code_fence",
            file_path=file_path,
            message=f"{file_path} 被整体包裹在 ```markdown/```text fenced block 中。",
            expected="文件内容本身必须是 Markdown，不得整体再套一层 fenced block。",
            minimal_edit="重写 body_region，移除最外层 fenced block。",
            details={"region": "body_region"},
        ))

    lines = text.splitlines(keepends=True)
    had_frontmatter = bool(lines and lines[0].strip() == "---")
    close_idx: int | None = None
    parsed_frontmatter: dict[str, Any] | None = None
    frontmatter_allowed = (
        _SKILL_FRONTMATTER_ALLOWED_TOP_LEVEL
        if file_path == "SKILL.md"
        else _REFERENCE_FRONTMATTER_ALLOWED_TOP_LEVEL
    )

    if require_frontmatter and not had_frontmatter:
        failures.append(_hard_format_failure(
            check_id="markdown.frontmatter.missing",
            file_path=file_path,
            message=f"{file_path} 缺少 YAML frontmatter。",
            expected="文件必须以单独一行 --- 开始，并在正文前用单独一行 --- 闭合。",
            minimal_edit="只重写 metadata_region，补齐合法 frontmatter。",
            details={"region": "metadata_region"},
        ))

    if had_frontmatter:
        for idx in range(1, len(lines)):
            if lines[idx].strip() == "---":
                close_idx = idx
                break
        if close_idx is None:
            failures.append(_hard_format_failure(
                check_id="markdown.frontmatter.unclosed",
                file_path=file_path,
                message=f"{file_path} frontmatter 开始/结束 --- 不成对。",
                expected="YAML frontmatter 必须用单独一行 --- 闭合。",
                minimal_edit="只重写 metadata_region，确保 frontmatter 边界明确闭合。",
                details={"region": "metadata_region"},
            ))
        else:
            raw_yaml = "".join(lines[1:close_idx])
            try:
                parsed = yaml.safe_load(raw_yaml) or {}
                if not isinstance(parsed, dict):
                    failures.append(_hard_format_failure(
                        check_id="markdown.frontmatter.not_object",
                        file_path=file_path,
                        message=f"{file_path} frontmatter 必须是 YAML object。",
                        expected="frontmatter 顶层必须是 key/value object。",
                        minimal_edit="只重写 metadata_region，修正 frontmatter object。",
                        details={"region": "metadata_region"},
                    ))
                else:
                    parsed_frontmatter = parsed
            except Exception as exc:
                failures.append(_hard_format_failure(
                    check_id="markdown.frontmatter.invalid_yaml",
                    file_path=file_path,
                    message=f"{file_path} frontmatter YAML 无法解析：{type(exc).__name__}: {exc}",
                    expected="frontmatter 必须是合法 YAML。",
                    minimal_edit="只重写 metadata_region，修正 YAML 并保留正文。",
                    details={"region": "metadata_region"},
                ))

            if parsed_frontmatter is not None:
                illegal = sorted(str(key) for key in parsed_frontmatter if str(key) not in frontmatter_allowed)
                if illegal:
                    failures.append(_hard_format_failure(
                        check_id="markdown.frontmatter.illegal_top_level_fields",
                        file_path=file_path,
                        message=f"{file_path} frontmatter 顶层字段非法：{', '.join(illegal)}。",
                        expected=f"frontmatter 顶层字段只允许：{', '.join(sorted(frontmatter_allowed))}。",
                        minimal_edit="只重写 metadata_region，移除非法顶层字段。",
                        details={"illegal_fields": illegal, "region": "metadata_region"},
                    ))
                if file_path == "SKILL.md":
                    missing = [key for key in ("name", "description") if not str(parsed_frontmatter.get(key) or "").strip()]
                    if missing:
                        failures.append(_hard_format_failure(
                            check_id="markdown.frontmatter.missing_required_fields",
                            file_path=file_path,
                            message=f"SKILL.md frontmatter 缺少必填字段：{', '.join(missing)}。",
                            expected="SKILL.md frontmatter 必须包含非空 name 和 description。",
                            minimal_edit="只重写 metadata_region，补齐 name/description。",
                            details={"missing_fields": missing, "region": "metadata_region"},
                        ))
                body = "".join(lines[close_idx + 1:])
                if require_frontmatter and not body.strip():
                    failures.append(_hard_format_failure(
                        check_id="markdown.body.missing",
                        file_path=file_path,
                        message=f"{file_path} frontmatter 后缺少非空正文。",
                        expected="frontmatter 闭合后必须有 Markdown 正文。",
                        minimal_edit="只重写 body_region，在 frontmatter 后补齐正文。",
                        details={"region": "body_region"},
                    ))

    fence_stack: list[tuple[str, str, int]] = []
    fence_re = re.compile(r"^\s{0,3}(```|~~~)\s*([A-Za-z0-9_-]*)")
    for line_no, line in enumerate(text.splitlines(), start=1):
        match = fence_re.match(line)
        if not match:
            continue
        marker = match.group(1)
        info = (match.group(2) or "").lower()
        if fence_stack and fence_stack[-1][0] == marker:
            fence_stack.pop()
        else:
            fence_stack.append((marker, info, line_no))
    if fence_stack:
        first_unclosed = fence_stack[-1]
        check_id = "markdown.fences.bash_unclosed" if first_unclosed[1] in {"bash", "sh", "shell"} else "markdown.fences.unclosed"
        failures.append(_hard_format_failure(
            check_id=check_id,
            file_path=file_path,
            message=f"{file_path} 存在未闭合的 {'bash ' if first_unclosed[1] in {'bash', 'sh', 'shell'} else ''}fenced code block。",
            expected="所有 fenced code block 必须成对闭合；bash block 未闭合属于 hard_format。",
            minimal_edit="只重写 body_region，确保 fenced block 开闭结构正确。",
            details={"line": first_unclosed[2], "info": first_unclosed[1], "region": "body_region"},
        ))

    return failures


def validate_no_hard_format_regression(file_path: str, before: str, after: str, *, require_frontmatter: bool) -> None:
    before_failures = detect_markdown_hard_format_failures(file_path, before, require_frontmatter)
    after_failures = detect_markdown_hard_format_failures(file_path, after, require_frontmatter)
    if not before_failures and after_failures:
        raise ValueError(
            "localized patch caused hard Markdown format regression; patch rejected:\n"
            + json.dumps(after_failures, ensure_ascii=False, default=str)
        )


def _basic_markdown_format_failures(file_path: str, content: str, *, require_frontmatter: bool) -> list[dict[str, Any]]:
    """Compatibility wrapper for the deterministic hard format gate.

    只检查最基础格式：
    1. frontmatter 是否存在；
    2. frontmatter 是否有收尾 ---；
    3. frontmatter YAML 是否能解析成 dict；
    4. fenced block 数量是否成对。

    不检查内容责任，不检查蓝图一致性。
    """
    return detect_markdown_hard_format_failures(file_path, content, require_frontmatter)

def _extract_script_command_templates(skill_md: str, script_path: str) -> list[str]:
    """Return shell command templates in SKILL.md that invoke script_path."""
    commands: list[str] = []
    normalized_script_path = script_path.replace("\\", "/")

    for info, body in _iter_markdown_fenced_blocks(skill_md):
        if not _is_shell_fence_info(info):
            continue

        command = body.strip()
        if not command:
            continue

        normalized_command = command.replace("\\", "/")
        if normalized_script_path in normalized_command:
            commands.append(command)

    return commands


def _command_uses_json_argv(command: str) -> bool:
    return "{" in command and "}" in command


def _script_reads_json_argv(content: str, runtime: str = "python") -> bool:
    if runtime == "node":
        return "JSON.parse" in content and "process.argv" in content
    if runtime in {"bash", "shell"}:
        return "$1" in content or "${1" in content or "jq" in content
    return "json.loads" in content and "sys.argv" in content


def _python_required_key_get_default_violations(tree: ast.AST, required_keys: set[str]) -> list[int]:
    """Return lines where code reads a required argv key via .get/default fallback."""
    if not required_keys:
        return []
    lines: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and node.args[0].value in required_keys
            and len(node.args) >= 2
        ):
            lines.append(getattr(node, "lineno", 0))
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            if any(
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr == "get"
                and value.args
                and isinstance(value.args[0], ast.Constant)
                and isinstance(value.args[0].value, str)
                and value.args[0].value in required_keys
                for value in node.values
            ):
                lines.append(getattr(node, "lineno", 0))
    return sorted({line for line in lines if line})


def _literal_string_set(node: ast.AST) -> set[str] | None:
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        values: set[str] = set()
        for item in node.elts:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                values.add(item.value)
            else:
                return None
        return values
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "set" and not node.args:
        return set()
    return None


def _type_name_from_ast(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Tuple):
        names = [_type_name_from_ast(item) for item in node.elts]
        if all(names):
            return "|".join(str(name) for name in names)
    return None


def _literal_expected_types(node: ast.AST) -> dict[str, str] | None:
    if not isinstance(node, ast.Dict):
        return None
    result: dict[str, str] = {}
    for key_node, value_node in zip(node.keys, node.values):
        if not (isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)):
            return None
        type_name = _type_name_from_ast(value_node)
        if not type_name:
            return None
        result[key_node.value] = type_name
    return result


def _literal_dict_string_keys(node: ast.AST) -> set[str] | None:
    if not isinstance(node, ast.Dict):
        return None
    keys: set[str] = set()
    for key_node in node.keys:
        if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
            keys.add(key_node.value)
        else:
            return None
    return keys


def _schema_placeholder_reasons(tree: ast.AST) -> list[str]:
    reasons: list[str] = []
    schema_names = {"allowed_keys", "required_keys", "optional_keys", "defaulted_keys", "default_values", "defaults", "expected_types", "arg_schema", "schema"}
    for node in ast.walk(tree):
        targets: list[ast.AST] = []
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        if value is None:
            continue
        names = [target.id.lower() for target in targets if isinstance(target, ast.Name)]
        if not any(name in schema_names or ("allowed" in name and "key" in name) or ("required" in name and "key" in name) or ("optional" in name and "key" in name) or ("default" in name and "key" in name) or ("expected" in name and "type" in name) for name in names):
            continue
        if isinstance(value, ast.Constant) and value.value is Ellipsis:
            reasons.append("schema assignment uses ellipsis placeholder")
        if isinstance(value, ast.Set) and any(isinstance(item, ast.Constant) and item.value is Ellipsis for item in value.elts):
            reasons.append("schema assignment uses {...} / ellipsis placeholder")
        if isinstance(value, ast.Dict) and any(
            (isinstance(item, ast.Constant) and item.value is Ellipsis)
            for item in [*(value.keys or []), *value.values]
            if item is not None
        ):
            reasons.append("schema dict uses ellipsis placeholder")
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "set" and any(isinstance(arg, ast.Constant) and arg.value is Ellipsis for arg in value.args):
            reasons.append("schema assignment uses set(...) placeholder")
        # Field names and string values are skill-internal business vocabulary;
        # do not reject names such as input_text/example/todo/placeholder here.
    return sorted(set(reasons))


def extract_python_strict_argv_schema(content: str) -> dict[str, Any]:
    """Extract generic strict argv schema declarations from Python source.

    This is intentionally lightweight and business-agnostic. It recognizes
    common constant declarations and simple ARG_SCHEMA/SCHEMA dictionaries only.
    """
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return {"allowed_keys": None, "required_keys": None, "optional_keys": None, "defaulted_keys": None, "expected_types": {}, "placeholder_reasons": ["python syntax invalid"]}

    allowed_keys: set[str] | None = None
    required_keys: set[str] | None = None
    optional_keys: set[str] | None = None
    defaulted_keys: set[str] | None = None
    expected_types: dict[str, str] = {}

    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        if value is None:
            continue
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            name = target.id.lower()
            if "allowed" in name and "key" in name:
                parsed = _literal_string_set(value)
                if parsed is not None:
                    allowed_keys = parsed
            elif "required" in name and "key" in name:
                parsed = _literal_string_set(value)
                if parsed is not None:
                    required_keys = parsed
            elif "optional" in name and "key" in name:
                parsed = _literal_string_set(value)
                if parsed is not None:
                    optional_keys = parsed
            elif "default" in name and "key" in name:
                parsed = _literal_string_set(value)
                if parsed is not None:
                    defaulted_keys = parsed
            elif name in {"default_values", "defaults"}:
                parsed = _literal_dict_string_keys(value)
                if parsed is not None:
                    defaulted_keys = parsed if defaulted_keys is None else defaulted_keys | parsed
            elif "expected" in name and "type" in name:
                parsed_types = _literal_expected_types(value)
                if parsed_types is not None:
                    expected_types.update(parsed_types)
            elif name in {"arg_schema", "schema"} and isinstance(value, ast.Dict):
                for key_node, value_node in zip(value.keys, value.values):
                    if not (isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)):
                        continue
                    key = key_node.value.lower()
                    if key in {"allowed_keys", "allowed"}:
                        parsed = _literal_string_set(value_node)
                        if parsed is not None:
                            allowed_keys = parsed
                    elif key in {"required_keys", "required"}:
                        parsed = _literal_string_set(value_node)
                        if parsed is not None:
                            required_keys = parsed
                    elif key in {"optional_keys", "optional"}:
                        parsed = _literal_string_set(value_node)
                        if parsed is not None:
                            optional_keys = parsed
                    elif key in {"defaulted_keys", "defaulted"}:
                        parsed = _literal_string_set(value_node)
                        if parsed is not None:
                            defaulted_keys = parsed
                    elif key in {"expected_types", "types"}:
                        parsed_types = _literal_expected_types(value_node)
                        if parsed_types is not None:
                            expected_types.update(parsed_types)

    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "strict_json_argv_guard"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Dict)
        ):
            continue
        call_allowed: set[str] = set()
        call_required: set[str] = set()
        call_optional: set[str] = set()
        call_defaulted: set[str] = set()
        call_types: dict[str, str] = {}
        for key_node, rule_node in zip(node.args[1].keys, node.args[1].values):
            if not (isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)):
                continue
            key = key_node.value
            call_allowed.add(key)
            required = True
            if isinstance(rule_node, ast.Dict):
                for rule_key, rule_value in zip(rule_node.keys, rule_node.values):
                    if not (isinstance(rule_key, ast.Constant) and isinstance(rule_key.value, str)):
                        continue
                    if rule_key.value == "required" and isinstance(rule_value, ast.Constant):
                        required = bool(rule_value.value)
                    elif rule_key.value == "default":
                        call_defaulted.add(key)
                    elif rule_key.value == "type":
                        type_name = _type_name_from_ast(rule_value)
                        if type_name:
                            call_types[key] = type_name
            if required:
                call_required.add(key)
            else:
                call_optional.add(key)
        if call_allowed:
            allowed_keys = call_allowed if allowed_keys is None else allowed_keys | call_allowed
        if call_required:
            required_keys = call_required if required_keys is None else required_keys | call_required
        if call_optional:
            optional_keys = call_optional if optional_keys is None else optional_keys | call_optional
        if call_defaulted:
            defaulted_keys = call_defaulted if defaulted_keys is None else defaulted_keys | call_defaulted
        expected_types.update(call_types)

    return {
        "allowed_keys": sorted(allowed_keys) if allowed_keys is not None else None,
        "required_keys": sorted(required_keys) if required_keys is not None else None,
        "optional_keys": sorted(optional_keys) if optional_keys is not None else None,
        "defaulted_keys": sorted(defaulted_keys) if defaulted_keys is not None else None,
        "expected_types": dict(sorted(expected_types.items())),
        "placeholder_reasons": _schema_placeholder_reasons(tree),
    }


def _python_imports_strict_argv_guard(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "backend.services.runtime_tools":
            if any(alias.name == "strict_json_argv_guard" for alias in node.names):
                return True
    return False


def _python_calls_strict_argv_guard(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "strict_json_argv_guard")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "strict_json_argv_guard")
        )
        for node in ast.walk(tree)
    )


def _python_strict_argv_guard_spec_placeholder_reasons(tree: ast.AST) -> list[str]:
    reasons: list[str] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "strict_json_argv_guard"
            and len(node.args) >= 2
        ):
            continue
        spec = node.args[1]
        if isinstance(spec, ast.Dict):
            for key in spec.keys:
                if isinstance(key, ast.Constant) and key.value is Ellipsis:
                    reasons.append("strict_json_argv_guard spec contains ellipsis placeholder")
            if any(isinstance(value, ast.Constant) and value.value is Ellipsis for value in spec.values):
                reasons.append("strict_json_argv_guard spec contains ellipsis placeholder")
        elif not (isinstance(spec, ast.Dict) and not spec.keys):
            if isinstance(spec, ast.Constant) and spec.value is Ellipsis:
                reasons.append("strict_json_argv_guard spec contains ellipsis placeholder")
    return sorted(set(reasons))


def _python_run_reparse_or_payload_bypass(tree: ast.AST) -> list[str]:
    reasons: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "run":
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "loads"
                and isinstance(inner.func.value, ast.Name)
                and inner.func.value.id == "json"
            ):
                reasons.append("run() must not re-parse sys.argv/json argv")
            if isinstance(inner, ast.Name) and inner.id == "payload":
                reasons.append("run() must not use unvalidated payload directly")
    return sorted(set(reasons))


def _python_has_strict_argv_runtime_guard(content: str) -> tuple[bool, list[str]]:
    """Heuristically verify that a Python script owns strict runtime argv schema.

    This intentionally checks generic guard structure only.  It does not impose
    platform field names or compare against SkillPlan/SKILL.md business keys.
    """
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return False, ["python syntax invalid"]

    lowered = content.lower()
    reasons: list[str] = []
    schema = extract_python_strict_argv_schema(content)
    placeholder_reasons = list(schema.get("placeholder_reasons") or [])
    if placeholder_reasons:
        reasons.extend(placeholder_reasons)

    if not _script_reads_json_argv(content, "python"):
        reasons.append("script must parse sys.argv[1] with json.loads")

    imports_guard = _python_imports_strict_argv_guard(tree)
    calls_guard = _python_calls_strict_argv_guard(tree)
    if not imports_guard:
        reasons.append("Python scripts must import strict_json_argv_guard from backend.services.runtime_tools")
    if not calls_guard:
        reasons.append("Python scripts must call strict_json_argv_guard(payload, spec) before core logic")
    reasons.extend(_python_strict_argv_guard_spec_placeholder_reasons(tree))
    reasons.extend(_python_run_reparse_or_payload_bypass(tree))

    if imports_guard and calls_guard:
        return not reasons, reasons

    unknown_guard = (
        (
            ("unknown" in lowered or "extra" in lowered or "unexpected" in lowered)
            or re.search(r"set\s*\(\s*(?:payload|data|argv)\s*\)\s*(?:-|!=)", content)
        )
        and ("raise" in lowered or "sys.exit" in lowered)
    )
    if not unknown_guard:
        reasons.append("script must reject unknown argv keys fail-fast")

    missing_guard = (
        (
            "missing" in lowered
            or "required" in lowered
            or re.search(r"['\"][^'\"]+['\"]\s+not\s+in\s+(?:payload|data|argv)", content)
        )
        and ("raise" in lowered or "sys.exit" in lowered)
    )
    if not missing_guard:
        reasons.append("script must reject missing required keys fail-fast")

    empty_guard = any(token in lowered for token in (" is none", "== \"\"", "== ''", "not value", "len(value) == 0", "empty"))
    if not empty_guard:
        reasons.append("script must reject empty required values")

    type_guard = "isinstance(" in content or "type(" in content
    if not type_guard:
        reasons.append("script must validate required value types")

    has_validated_flow = re.search(r"run\s*\(\s*(validated|args|params|config)\s*\)", content) or "validate_payload(" in content
    if not has_validated_flow:
        reasons.append("run/main should use validated args from parse/validate logic")

    return not reasons, reasons


def _strict_argv_guard_failure_message(file_path: str, content: str, runtime: str) -> str | None:
    if runtime != "python":
        return None
    ok, reasons = _python_has_strict_argv_runtime_guard(content)
    if ok:
        return None
    return (
        f"{file_path} 必须在脚本内部实现 strict JSON argv runtime guard：在核心逻辑前完成等价的 argv 校验，"
        "拒绝 unknown/missing/empty/type 错误，并只把已校验参数交给核心逻辑。\n"
        + "\n".join(f"- {reason}" for reason in reasons)
    )


def _script_uses_input_keys(content: str, keys: list[str]) -> tuple[bool, list[str]]:
    missing = [key for key in keys if key not in content]
    return not missing, missing


def _script_has_main_entry(content: str, runtime: str) -> bool:
    if runtime == "python":
        return "def main" in content and "__main__" in content
    if runtime == "node":
        return "process.argv" in content and "console.log" in content
    if runtime in {"bash", "shell"}:
        return ("$1" in content or "${1" in content) and ("echo" in content or "printf" in content or "print(json.dumps" in content)
    return True


def _validate_script_contract_static(
    *,
    file_path: str,
    content: str,
    skill_md: str,
    skill_plan_entry: dict[str, Any] | SkillPlanEntry | None = None,
) -> None:
    """Validate script source against SKILL.md contract locally.

    Creator 单文件阶段只做“协议 + 运行 + 产物”中的静态协议部分：
    - 如果 SKILL.md 命令传入 JSON argv，脚本必须读取 JSON argv；
    - 不用 fake/mock/template 关键词、工具能力声明、helper 路线或 LLM
      validator 作为 hard gate；
    - 字段级 stdout/artifact 闭环交给单文件 trial run 和最终 E2E。
    """
    explicit_entry = (
        skill_plan_entry.__dict__
        if isinstance(skill_plan_entry, SkillPlanEntry)
        else skill_plan_entry
    )
    plan_entry = (
        _skill_plan_entry_for_file(file_path=file_path, skill_plan_entry=explicit_entry)
        if explicit_entry is not None
        else _skill_plan_entry_for_file(file_path=file_path, blueprint_text=skill_md)
    )
    commands = _extract_script_command_templates(skill_md, file_path)
    if not commands:
        return

    command_results = _check_command_block_contract(file_path, commands, plan_entry)
    failed_command_results = [r for r in command_results if not r.passed]
    if failed_command_results:
        raise ValueError(
            "SKILL.md 命令块不合法，属于 workflow 局部合同问题，不要改脚本字段强制对齐 SkillPlan:\n"
            + _format_contract_checks(failed_command_results, passed=False)
        )

    json_argv_commands = [c for c in commands if _command_uses_json_argv(c)]
    if json_argv_commands and not _script_reads_json_argv(content, plan_entry.runtime):
        raise ValueError(
            f"{file_path} SKILL.md 命令传入 JSON argv，但脚本未按 runtime 读取 JSON argv（例如 Python json.loads(sys.argv[1])）。"
        )

    guard_failure = _strict_argv_guard_failure_message(file_path, content, plan_entry.runtime)
    if json_argv_commands and guard_failure:
        raise ValueError(guard_failure)



def _validate_script_against_existing_skill_contract(skill_name: str, file_path: str, content: str) -> None:
    """Refuse saving scripts that do not match the current SKILL.md contract."""
    if not file_path.startswith("scripts/"):
        return
    skill_md_path = settings.skills_path / skill_name / "SKILL.md"
    if not skill_md_path.is_file():
        return
    skill_md = skill_md_path.read_text(encoding="utf-8")
    _validate_script_contract_static(file_path=file_path, content=content, skill_md=skill_md)



def _validate_generated_file_content(file_path: str, content: str, role: str | None = None, skill_plan_entry: dict[str, Any] | None = None) -> None:
    """Reject content that is clearly not the requested single file."""
    if file_path == "SKILL.md":
        _reject_custom_skill_md_protocol(content)
        return

    if file_path.startswith("scripts/"):
        results = _check_script_content_review_contract(
            file_path,
            content,
            role=role,
            skill_plan_entry=skill_plan_entry,
        )
        if any(not result.passed for result in results):
            raise ContractValidationError(
                _format_contract_failures(results).replace("SKILL.md contract", f"{file_path} contract"),
                results,
            )
        return

    if file_path.startswith("references/"):
        _validate_reference_file_contract(file_path, content)
        return

    if file_path.startswith("assets/"):
        _validate_asset_file_contract(file_path, content)
        return


def validate_file_contract(
    *,
    file_path: str,
    content: str,
    blueprint_text: str = "",
    role: str | None = None,
    skill_plan_entry: dict[str, Any] | None = None,
) -> list[ContractCheckResult]:
    """First-round Creator validator: only check one file's own contract.

    This layer deliberately excludes cross-file placeholder/dataflow closure and
    final platform-output checks. Those belong to ``validate_workflow_e2e``.
    """
    if file_path == "SKILL.md":
        return _check_skill_md_contract(content, blueprint_text or content)
    if file_path.startswith("scripts/"):
        return _check_script_file_contract(file_path, content, role=role, skill_plan_entry=skill_plan_entry)
    if file_path.startswith("references/"):
        purpose = ""
        if isinstance(skill_plan_entry, dict):
            purpose = str(skill_plan_entry.get("purpose") or "")
        return _check_reference_file_contract(file_path, content, purpose=purpose)
    if file_path.startswith("assets/"):
        try:
            _validate_asset_file_contract(file_path, content)
            return []
        except ContractValidationError as exc:
            return list(exc.results)
    return []

from .repair import *  # noqa: F403  # late import for blueprint repair helpers

__all__ = [name for name in globals() if not name.startswith("__")]
