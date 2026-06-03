"""Creator router — file-by-file Skill generation endpoints.

These endpoints decouple the file-creation phase from the main
/api/chat/creator conversation endpoint:

- POST /api/creator/analyze-blueprint  — extract file list from blueprint (no LLM)
- POST /api/creator/init-skill          — create Skill directory structure
- POST /api/creator/generate-file       — SSE: stream single-file content from LLM
- POST /api/creator/write-file          — write generated content to disk
- POST /api/creator/validate-skill      — validate SKILL.md format
- POST /api/creator/package-skill       — package Skill directory into .skill archive
"""

import ast
import json
import logging
import re
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..config import settings
from ..services.blueprint_parser import BlueprintPlan, parse_blueprint
from ..services.llm_proxy import complete_chat_once, stream_chat
from ..services.model_router import VALIDATOR_TASK, route_creator_file_model, route_model
from ..services.skill_executor import _build_script_runtime_env, run_action
from .chat_utils import _get_skill_venv_python, _scan_and_install_python_deps

logger = logging.getLogger(__name__)

_SKILL_MD_MARKDOWN_EXECUTION_GUIDE = """

宿主 Markdown 执行说明（写入生成的 SKILL.md 正文时必须保持常见 Markdown 形态）：
- SKILL.md 是普通 Markdown 说明书，只描述做什么、何时使用资源，以及 assistant 在运行时应如何表达动作；不要引入自定义协议章节（例如 `Runtime Contract` JSON）。
- 对纯文本即可完成的任务，明确写“直接回答”，不要要求运行脚本。
- 如果确实需要运行 scripts/ 下的脚本，使用市面常见的 Markdown fenced code block 给出命令示例/模板，例如：
  执行命令：
  ```bash
  python scripts/<script-name> '{"topic":"{{topic}}","keywords":"{{keywords}}"}'
  ```
- 命令示例必须与脚本真实接口一致：脚本读 JSON argv 时，示例就传 JSON；脚本读 stdin 时，正文就说明 stdin 内容。禁止让运行时主模型根据脚本名临时猜 CLI flags。
- 参数映射用普通 Markdown 列表说明，例如 `topic` 从用户输入提取、`keywords` 从用户输入提取、可选参数给出默认值；不要使用单独的 JSON contract。
- 只有 assistant 在 Sandbox 当轮回复中输出的 fenced code block 才会被宿主解析和执行；SKILL.md 中的 block 是运行说明/示例，不会在加载时自动执行。
- 如果需要写文件，用普通 Markdown 说明 assistant 应输出 `写入文件：<path>` 或 `保存到：<path>`，并把完整文件内容放在紧随其后的 fenced code block。
- assistant 不得假装脚本已经执行；必须等待宿主返回 stdout/stderr/observation 后，再基于 observation 生成最终回答。
- 禁止在 SKILL.md 中只写“立即调用 `scripts/...`”这种隐式执行描述；应写成“运行时 assistant 输出以下命令块交由宿主执行”，并给出具体命令示例。
- 如果用户要求使用平台内置模型、图像模型或多模态模型，不要写外部 API key、关键词数据库或假 API；应说明由宿主配置的模型完成相关步骤。任何脚本都必须是有实际功能的实现：要么执行确定性的真实计算/转换/文件处理，要么在需要开放式生成、语义理解、视觉/图像能力时使用宿主已配置的模型能力；模型与认证相关参数由平台运行时注入；生成脚本可按需读取 `IMAGE_MODEL`、`IMAGE_BASE_URL`、`IMAGE_SIZE`、`IMAGE_API_KEY` / `LLM_API_KEY` / `OPENAI_API_KEY` 等环境变量，但不要硬编码这些值，也不需要额外校验它们是否存在。
- 如果需要生成图片，SKILL.md 只描述“使用平台稳定扩散图片生成能力”即可；不要把中文 prompt 翻译、TEXT_MODEL 调用、接口字段解析等平台细节写入创建出来的 Skill 正文。平台运行时会静默完成中文 topic 到英文 Stable Diffusion prompt 的转换。
"""

router = APIRouter(prefix="/api/creator", tags=["creator"])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Directories allowed as parents when writing non-SKILL.md files.
_ALLOWED_FOLDERS: frozenset[str] = frozenset({"scripts", "references", "assets"})

# Trailing conversation turns to include in file-generation prompts.
_MAX_HISTORY_TURNS = 6

# Generated files can repair themselves by sending validator/static/trial-run
# failures back to the same routed model before returning content to the frontend.
_MAX_FILE_REPAIR_ATTEMPTS = 8
_SCRIPT_TRIAL_TIMEOUT_SECONDS = 30

# Human-readable language labels indexed by file extension.
_LANG_LABELS: dict[str, str] = {
    ".py":       "Python",
    ".js":       "JavaScript",
    ".mjs":      "JavaScript",
    ".cjs":      "JavaScript",
    ".ts":       "TypeScript",
    ".sh":       "Bash",
    ".bash":     "Bash",
    ".rb":       "Ruby",
    ".go":       "Go",
    ".md":       "Markdown",
    ".yaml":     "YAML",
    ".yml":      "YAML",
    ".json":     "JSON",
    ".toml":     "TOML",
    ".txt":      "Text",
    ".jinja":    "Jinja2 模板",
    ".jinja2":   "Jinja2 模板",
    ".template": "模板文件",
    ".tmpl":     "模板文件",
}


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class AnalyzeBlueprintRequest(BaseModel):
    messages: list[dict]
    model: Optional[str] = None


class FileSpecOut(BaseModel):
    path: str
    purpose: str
    required: bool
    can_skip: bool


class AnalyzeBlueprintResponse(BaseModel):
    skill_name: str
    files: list[FileSpecOut]
    warnings: list[str]


class InitSkillRequest(BaseModel):
    skill_name: str


class InitSkillResponse(BaseModel):
    success: bool
    path: Optional[str] = None
    message: str


class GenerateFileRequest(BaseModel):
    skill_name: str
    file_path: str
    purpose: str
    blueprint_text: str
    conversation_history: list[dict]
    model: Optional[str] = None


class WriteFileRequest(BaseModel):
    skill_name: str
    file_path: str
    content: str


class WriteFileResponse(BaseModel):
    success: bool
    path: Optional[str] = None
    bytes: int = 0
    message: str


class SkillActionRequest(BaseModel):
    skill_name: str


class SkillActionResponse(BaseModel):
    success: bool
    path: Optional[str] = None
    message: str


class ListFilesRequest(BaseModel):
    skill_name: str


class FileInfo(BaseModel):
    path: str
    is_directory: bool
    size: int = 0


class ListFilesResponse(BaseModel):
    success: bool
    files: list[FileInfo]
    message: str


class InitFromBlueprintRequest(BaseModel):
    skill_name: str
    files: list[FileSpecOut]


class InitFromBlueprintResponse(BaseModel):
    success: bool
    path: Optional[str] = None
    files_created: int = 0
    message: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _validate_file_path(file_path: str) -> None:
    """Raise HTTP 400 if file_path is outside allowed locations."""
    p = Path(file_path)
    if p.is_absolute() or ".." in p.parts:
        raise HTTPException(status_code=400, detail=f"非法文件路径: {file_path}")

    if file_path == "SKILL.md":
        return

    if not p.parts or p.parts[0] not in _ALLOWED_FOLDERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"文件路径 '{file_path}' 不合法。"
                f"只允许 SKILL.md 或 scripts/*、references/*、assets/* 下的文件。"
            ),
        )

    filename = p.name
    if not filename or filename.startswith(".") or "\x00" in filename or len(filename) > 255:
        raise HTTPException(status_code=400, detail=f"文件名非法: {filename!r}")


def _validate_skill_name(skill_name: str) -> str:
    """Strip, validate, and return the skill_name or raise HTTP 400."""
    name = skill_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="skill_name 不能为空。")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise HTTPException(
            status_code=400,
            detail="skill_name 只能包含小写字母、数字和连字符，且必须以字母或数字开头。",
        )
    return name


def _extract_first_fenced_block(content: str) -> str | None:
    """Return the first fenced block body from content, or None."""
    lines = content.splitlines(keepends=True)

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        match = re.match(r"(`{3,}|~{3,})([^\n`]*)\n?$", stripped.rstrip("\n"))
        if not match:
            i += 1
            continue

        fence = match.group(1)
        fence_char = fence[0]
        fence_len = len(fence)
        code_lines: list[str] = []
        i += 1

        while i < len(lines):
            close_line = lines[i]
            close_stripped = close_line.lstrip()
            close_match = re.match(
                rf"{re.escape(fence_char)}{{{fence_len},}}\s*$",
                close_stripped.rstrip("\n"),
            )
            if close_match:
                return "".join(code_lines).strip()
            code_lines.append(close_line)
            i += 1

        return "".join(code_lines).strip()

    return None


def _extract_target_file_from_bundle(content: str, file_path: str) -> str | None:
    """Extract the requested file when a model returns a multi-file bundle."""
    escaped_path = re.escape(file_path)
    heading_re = re.compile(
        rf"(?im)^\s*#{{1,6}}\s*(?:[^\n`]*?)`?{escaped_path}`?\s*$"
    )

    for match in heading_re.finditer(content):
        section = content[match.end():]
        block = _extract_first_fenced_block(section)
        if block is not None:
            return block

    return None


_MULTI_FILE_MARKER_RE = re.compile(
    r"(?im)^\s*#{1,6}\s*(?:[^\n`]*)(?:SKILL\.md|scripts/|references/|assets/)"
)


_SCRIPT_FAKE_IMPLEMENTATION_RE = re.compile(
    r"placeholder|TODO|your_api_key|api\.example\.com|example\.com|模拟|占位|假装|"
    r"实际使用时|实际开发中|仅为演示|演示目的|空的占位图|纯色图片|ASCII插图|ascii_art|fake",
    re.IGNORECASE,
)
_SKILL_CUSTOM_RUNTIME_CONTRACT_RE = re.compile(r"(?im)^\s*#{1,6}\s*Runtime\s+Contract\s*$")
_HOST_MODEL_CAPABILITY_RE = re.compile(
    r"宿主.{0,12}模型|内置.{0,12}模型|配置.{0,12}模型|文本模型|图像模型|视觉模型|"
    r"多模态|大语言模型|LLM|AI生成|模型生成|调用模型|TEXT_MODEL|IMAGE_MODEL|VISION_MODEL",
    re.IGNORECASE,
)
_CONFIGURED_MODEL_CALL_RE = re.compile(
    r"LLM_BASE_URL|TEXT_MODEL|IMAGE_MODEL|VISION_MODEL|/v1/chat/completions|"
    r"chat/completions|complete_chat_once|stream_chat|openai|"
    r"generate_stable_diffusion_image|backend\.services\.skill_runtime",
    re.IGNORECASE,
)
_CREATOR_FLOW_LEAK_RE = re.compile(
    r"点击[‘'\"]?开始创建|开始生成文件|确认项列表|系统将自动创建|自动创建以下文件|"
    r"创建文件面板|文件创建面板|若当前无误|已预置|所有路径与命名与蓝图一致|"
    r"不包含任何隐藏逻辑或隐式执行|输出格式符合 Markdown 标准，支持宿主解析",
    re.IGNORECASE,
)
_SKILL_FILE_PATH_RE = re.compile(r"(?<![\w./-])((?:scripts|references|assets)/[A-Za-z0-9_./-]+|SKILL\.md)(?![\w./-])")

_IMAGE_MODEL_USAGE_RE = re.compile(r"IMAGE_MODEL|IMAGE_BASE_URL|/v1/images/generations|images/generations|generate_stable_diffusion_image", re.IGNORECASE)
_DIRECT_IMAGE_API_RE = re.compile(r"IMAGE_BASE_URL|/v1/images/generations|images/generations", re.IGNORECASE)
_PLATFORM_IMAGE_HELPER_RE = re.compile(r"generate_stable_diffusion_image|backend\.services\.skill_runtime", re.IGNORECASE)
_IMAGE_URL_ONLY_RE = re.compile(r'\[0\]\s*\.get\(\s*[\'"]url[\'"]|\[\s*[\'"]url[\'"]\s*\]', re.IGNORECASE)
_DATA_URI_RE = re.compile(r"data:image/[^;]+;base64", re.IGNORECASE)


def _reject_custom_skill_md_protocol(content: str) -> None:
    """Reject non-standard runtime protocol sections in generated SKILL.md."""
    if _SKILL_CUSTOM_RUNTIME_CONTRACT_RE.search(content):
        raise ValueError(
            "SKILL.md 不应包含自定义 Runtime Contract JSON 协议；"
            "请使用普通 Markdown 说明和 ```bash 命令示例描述运行时动作。"
        )


def _extract_declared_skill_paths(text: str) -> list[str]:
    """Return normalized Skill package file paths mentioned by a blueprint."""
    seen: set[str] = set()
    paths: list[str] = []
    for raw in _SKILL_FILE_PATH_RE.findall(text or ""):
        path = raw.strip().rstrip("`，,。；;:)）]").replace("\\", "/")
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _paths_requiring_skill_md_mentions(blueprint_text: str, *, prefix: str) -> list[str]:
    return [path for path in _extract_declared_skill_paths(blueprint_text) if path.startswith(prefix)]


def _reject_creator_flow_leak(content: str) -> None:
    """Reject Creator UI/workflow text copied into generated SKILL.md."""
    if _CREATOR_FLOW_LEAK_RE.search(content):
        raise ValueError(
            "SKILL.md 包含 Creator 界面流程/确认清单文本（例如“点击开始创建”“确认项列表”“系统将自动创建”），"
            "这是平台创建流程泄露，不属于 Skill 使用说明。请删除这些流程文本，只保留 Skill 的使用说明、资源引用和可执行命令示例。"
        )


def _validate_skill_md_contract(content: str, blueprint_text: str) -> None:
    """Validate generated SKILL.md against blueprint-declared resources."""
    _reject_custom_skill_md_protocol(content)
    _reject_creator_flow_leak(content)

    if not re.match(r"^---\nname: [^\n]+\ndescription: [^\n]+\n---\n", content.strip()):
        raise ValueError(
            "SKILL.md 必须以 YAML frontmatter 开始，且包含 name 和 description："
            "--- / name: ... / description: ... / ---。"
        )

    script_paths = _paths_requiring_skill_md_mentions(blueprint_text, prefix="scripts/")
    for script_path in script_paths:
        commands = _extract_script_command_templates(content, script_path)
        if not commands:
            raise ValueError(
                f"SKILL.md 缺少调用 {script_path} 的可执行 Markdown 命令块。"
                "请在正文中加入 ```bash fenced code block，并在其中给出与脚本接口一致的 python scripts/... 命令示例。"
            )

    reference_paths = _paths_requiring_skill_md_mentions(blueprint_text, prefix="references/")
    for reference_path in reference_paths:
        if reference_path not in content:
            raise ValueError(
                f"SKILL.md 缺少对参考资料 {reference_path} 的引用。"
                "请在“参考资料/资源”小节用普通 Markdown 明确说明何时读取该 reference。"
            )


def _validate_skill_md_against_existing_files(skill_name: str, content: str) -> None:
    """Validate SKILL.md against files already initialized in the Skill dir."""
    skill_dir = settings.skills_path / skill_name
    if not skill_dir.exists():
        return
    declared_paths: list[str] = []
    for folder in ("scripts", "references"):
        folder_path = skill_dir / folder
        if not folder_path.is_dir():
            continue
        for child in sorted(path for path in folder_path.iterdir() if path.is_file()):
            declared_paths.append(f"{folder}/{child.name}")
    if declared_paths:
        _validate_skill_md_contract(content, "\n".join(declared_paths))
    else:
        _reject_creator_flow_leak(content)


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


def _requires_configured_model_call(*, skill_md: str, file_path: str) -> bool:
    """Return whether SKILL.md declares host-model-powered behavior for a script.

    This intentionally keys off model-capability wording (host/built-in/
    configured models, LLM, multimodal, image/vision models) rather than a
    finite list of Skill domains.
    """
    if not skill_md:
        return False
    commands = _extract_script_command_templates(skill_md, file_path)
    if not commands:
        return False
    return bool(_HOST_MODEL_CAPABILITY_RE.search(skill_md))


def _script_uses_configured_model(content: str) -> bool:
    """Detect whether script calls the configured host LLM/VL endpoint."""
    return bool(_CONFIGURED_MODEL_CALL_RE.search(content))


def _validate_configured_model_usage_static(*, file_path: str, content: str, skill_md: str) -> None:
    """Reject scripts that claim host-model behavior but do not call models."""
    if _DIRECT_IMAGE_API_RE.search(content) and "VISION_MODEL" in content:
        raise ValueError(
            f"{file_path} 将 VISION_MODEL 与图片生成接口混用。"
            "生成图片必须使用平台 Stable Diffusion 图片运行时或 IMAGE_MODEL；"
            "VISION_MODEL 只用于看图理解/OCR/多模态问答。"
        )

    if _DIRECT_IMAGE_API_RE.search(content) and not _PLATFORM_IMAGE_HELPER_RE.search(content):
        raise ValueError(
            f"{file_path} 直接调用图片生成接口。"
            "Creator 生成的图片脚本必须调用 backend.services.skill_runtime.generate_stable_diffusion_image，"
            "由平台侧静默完成中文 topic 翻译、Stable Diffusion IMAGE_MODEL 选择、b64_json 解析和图片落盘。"
        )

    if _IMAGE_MODEL_USAGE_RE.search(content) and _IMAGE_URL_ONLY_RE.search(content) and "b64_json" not in content:
        raise ValueError(
            f"{file_path} 假设图片接口只返回 url。"
            "平台图片运行时默认使用 b64_json，并会落盘为文件路径；请调用平台图片运行时 helper。"
        )

    if _DATA_URI_RE.search(content):
        raise ValueError(
            f"{file_path} 输出 base64 data URI。"
            "图片结果必须由平台运行时写入 OUTPUT_DIR，并在 stdout JSON 中返回 image_path。"
        )

    if not _requires_configured_model_call(skill_md=skill_md, file_path=file_path):
        return
    if _script_uses_configured_model(content):
        return
    raise ValueError(
        f"{file_path} 的 SKILL.md 声明需要使用宿主/内置/配置模型，但脚本没有调用这些模型。"
        "脚本不能用固定模板、随机词表或 ASCII 图替代模型能力；"
        "请通过 LLM_BASE_URL + TEXT_MODEL 调用文本模型，需要图像/视觉能力时使用 IMAGE_MODEL/VISION_MODEL，"
        "或者把该 Skill 设计为无需 scripts/ 的模型直接回答。"
    )

def _validate_generated_file_content(file_path: str, content: str) -> None:
    """Reject content that is clearly not the requested single file."""
    if file_path == "SKILL.md":
        _reject_custom_skill_md_protocol(content)
        return

    if file_path.startswith("scripts/"):
        _reject_fake_script_implementation(file_path, content)
        if "```" in content or _MULTI_FILE_MARKER_RE.search(content):
            raise ValueError(
                f"{file_path} 生成内容包含 Markdown 代码块或多文件包，"
                "不是单个脚本源码。请重新生成该文件。"
            )

        if Path(file_path).suffix.lower() == ".py":
            try:
                ast.parse(content)
            except SyntaxError as exc:
                raise ValueError(
                    f"{file_path} 生成内容不是合法 Python 源码: {exc.msg}"
                ) from exc


def _extract_script_command_templates(skill_md: str, script_path: str) -> list[str]:
    """Return shell command templates in SKILL.md that invoke script_path."""
    commands: list[str] = []
    for match in re.finditer(r"```(?:bash|sh|shell)?\s*\n([\s\S]*?)\n```", skill_md, flags=re.IGNORECASE):
        command = match.group(1).strip()
        if script_path in command:
            commands.append(command)
    return commands


def _command_uses_json_argv(command: str) -> bool:
    return "{" in command and "}" in command


def _script_reads_json_argv(content: str) -> bool:
    return "json.loads" in content and "sys.argv" in content


def _validate_script_contract_static(*, file_path: str, content: str, skill_md: str) -> None:
    """Validate script source against existing SKILL.md command examples."""
    _reject_fake_script_implementation(file_path, content)
    _validate_configured_model_usage_static(file_path=file_path, content=content, skill_md=skill_md)
    commands = _extract_script_command_templates(skill_md, file_path)
    if not commands:
        return

    json_argv_commands = [cmd for cmd in commands if _command_uses_json_argv(cmd)]
    if json_argv_commands and not _script_reads_json_argv(content):
        raise ValueError(
            f"{file_path} 的 SKILL.md Markdown 命令示例传入 JSON 参数，但脚本没有读取 sys.argv[1] 并 json.loads 解析；"
            "禁止保存与命令示例不一致的脚本。"
        )

    for cmd in commands:
        for key in re.findall(r"{{\s*([a-zA-Z_][\w-]*)\s*}}", cmd):
            if key not in content:
                raise ValueError(
                    f"{file_path} 的 Markdown 命令示例包含参数 {{{{{key}}}}}，但脚本源码未使用该参数。"
                )


def _validate_script_against_existing_skill_contract(skill_name: str, file_path: str, content: str) -> None:
    """Refuse saving scripts that do not match the current SKILL.md contract."""
    if not file_path.startswith("scripts/"):
        return
    skill_md_path = settings.skills_path / skill_name / "SKILL.md"
    if not skill_md_path.is_file():
        return
    skill_md = skill_md_path.read_text(encoding="utf-8")
    _validate_script_contract_static(file_path=file_path, content=content, skill_md=skill_md)


def _sample_value_for_placeholder(key: str) -> str:
    """Return realistic trial input; image/diffusion prompts are already English."""
    lowered = key.lower()
    if any(token in lowered for token in ("prompt", "diffusion", "image", "picture", "photo", "scene")):
        return "a cinematic watercolor cat under a warm sunset"
    if any(token in lowered for token in ("text", "content", "input", "query")):
        return "测试输入 text sample"
    if any(token in lowered for token in ("topic", "theme", "subject")):
        return "system time"
    return f"sample {key}"


def _render_trial_command_args(command: str, script_path: str) -> list[str] | None:
    """Extract argv for script_path from a SKILL.md command template."""
    rendered = re.sub(
        r"{{\s*([a-zA-Z_][\w-]*)\s*}}",
        lambda m: _sample_value_for_placeholder(m.group(1)),
        command,
    )
    try:
        parts = shlex.split(rendered)
    except ValueError:
        return None

    for idx, part in enumerate(parts):
        normalized = part.replace("\\", "/")
        if normalized == script_path or normalized.endswith("/" + script_path):
            return parts[idx + 1:]
    return None


def _trial_args_for_script(skill_md: str, file_path: str, content: str) -> list[list[str]]:
    commands = _extract_script_command_templates(skill_md, file_path)
    arg_sets = [args for cmd in commands if (args := _render_trial_command_args(cmd, file_path)) is not None]
    if arg_sets:
        return arg_sets
    if _script_reads_json_argv(content):
        return [[json.dumps({
            "prompt": _sample_value_for_placeholder("prompt"),
            "text": _sample_value_for_placeholder("text"),
            "topic": _sample_value_for_placeholder("topic"),
        }, ensure_ascii=False)]]
    return [[]]


def _format_trial_failure(*, args: list[str], returncode: int, stdout: str, stderr: str) -> str:
    return (
        "脚本试运行失败：\n"
        f"argv={args!r}\n"
        f"exit_code={returncode}\n"
        f"stdout={stdout[-4000:]}\n"
        f"stderr={stderr[-4000:]}"
    )


def _trial_run_generated_script(skill_name: str, file_path: str, content: str) -> None:
    """Run a generated Python script before accepting it from Creator.

    Python scripts are executed in a temporary per-skill virtual environment.
    Before each trial run, imports are statically scanned and missing common
    third-party packages are installed into that venv, matching sandbox runtime
    behavior and allowing generation-test-repair-test loops to focus on real
    script defects instead of missing packages.
    """
    if not file_path.startswith("scripts/") or Path(file_path).suffix.lower() != ".py":
        return

    skill_md_path = settings.skills_path / skill_name / "SKILL.md"
    skill_md = skill_md_path.read_text(encoding="utf-8") if skill_md_path.is_file() else ""
    _validate_script_contract_static(file_path=file_path, content=content, skill_md=skill_md)

    with tempfile.TemporaryDirectory(prefix="creator-script-trial-") as tmp:
        skill_dir = Path(tmp) / skill_name
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            skill_md or f"---\nname: {skill_name}\ndescription: trial\n---\n",
            encoding="utf-8",
        )
        script_path = scripts_dir / Path(file_path).name
        script_path.write_text(content, encoding="utf-8")

        try:
            venv_python = _get_skill_venv_python(skill_dir)
            _scan_and_install_python_deps(script_path, venv_python)
        except RuntimeError as exc:
            raise ValueError(f"脚本试运行环境准备失败：{exc}") from exc

        for args in _trial_args_for_script(skill_md, file_path, content):
            try:
                proc = subprocess.run(
                    [str(venv_python), str(script_path), *args],
                    cwd=str(scripts_dir),
                    capture_output=True,
                    text=True,
                    timeout=_SCRIPT_TRIAL_TIMEOUT_SECONDS,
                    env={**_build_script_runtime_env(skill_dir), "SKILL_TRIAL_RUN": "1"},
                )
            except subprocess.TimeoutExpired as exc:
                raise ValueError(f"脚本试运行超时（超过 {_SCRIPT_TRIAL_TIMEOUT_SECONDS} 秒）：argv={args!r}") from exc
            if proc.returncode != 0:
                raise ValueError(_format_trial_failure(
                    args=args,
                    returncode=proc.returncode,
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                ))


async def _repair_generated_file_with_feedback(
    *,
    prompt_messages: list[dict],
    model: str,
    file_path: str,
    previous_content: str,
    validation_error: str,
) -> str:
    """Ask the routed generation model to fix one file using validator feedback.

    The repaired model output is intentionally not validated here. Validation
    happens at the top of the generate-file retry loop so format errors in the
    repaired response consume one retry attempt and can be sent back as feedback.
    Previous content is passed as user-quoted data instead of an assistant turn
    so Markdown-wrapped failures are not reinforced as the desired answer shape.
    """
    is_script = file_path.startswith("scripts/")
    output_contract = (
        "最终只返回完整脚本源码；不要解释，不要 Markdown 代码块，不要多文件包，不要写 `写入文件：...`。"
        if is_script
        else "最终只返回 SKILL.md 文件正文；不要在文件外层套 Markdown 代码块，不要输出 Creator 创建流程、确认清单或 `点击开始创建` 文案。"
    )
    local_edit_scope = (
        "保留其它已经正确的导入、函数、参数解析、stdout JSON 协议和业务逻辑。"
        if is_script
        else "保留已经正确的 frontmatter、章节结构、脚本命令示例和 reference 引用。"
    )
    if is_script:
        extra_rules = (
            "如果错误涉及图片生成：必须调用 `backend.services.skill_runtime.generate_stable_diffusion_image`；"
            "不要直接调用 /v1/images/generations，不要用 VISION_MODEL 生成图片，不要写 placeholder/模拟图片。"
        )
    else:
        extra_rules = (
            "如果蓝图包含 scripts/，必须包含调用对应 scripts/ 路径的 ```bash fenced code block；"
            "如果蓝图包含 references/，必须在正文中明确引用对应 reference 路径；"
            "不得复制 Creator UI 流程、待确认清单、文件创建面板说明或系统自动创建文件提示。"
        )

    repair_messages = [*prompt_messages]
    repair_messages.append({
        "role": "user",
        "content": (
            f"以下是上一次生成但未通过校验的 {file_path} 内容。它可能包含错误示范（例如 Markdown fence 或 Creator 流程泄露），"
            "不要模仿错误格式，只把它当作待编辑草稿：\n"
            "<previous_content>\n"
            f"{previous_content[-16000:]}\n"
            "</previous_content>"
        ),
    })
    repair_messages.append({
        "role": "user",
        "content": (
            f"上一次生成的 {file_path} 没有通过校验模型/静态校验/试运行。"
            "请优先做局部修改：只修改校验意见指出的最小错误片段，"
            f"{local_edit_scope}"
            "修改完成后可以整合输出。"
            f"{output_contract}\n"
            f"{extra_rules}\n\n"
            f"错误信息：\n{validation_error}"
        ),
    })
    return await complete_chat_once(repair_messages, model)


def _parse_validator_json_object(text: str) -> dict | None:
    stripped = (text or "").strip()
    if stripped.startswith("```json"):
        stripped = stripped[7:].strip()
    if stripped.startswith("```"):
        stripped = stripped[3:].strip()
    if stripped.endswith("```"):
        stripped = stripped[:-3].strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def _run_generated_file_validator_round(
    *,
    file_path: str,
    content: str,
    deterministic_error: str,
    requested_model: str,
) -> dict:
    """Ask the validator model for actionable repair feedback for the coder."""
    route = route_model(
        VALIDATOR_TASK,
        requested_model=requested_model,
        reason=f"creator generated file validation: {file_path}",
    )
    messages = [
        {
            "role": "system",
            "content": (
                "你是 Creator 生成文件校验模型，只输出严格 JSON object。"
                "你不改代码，只给 coder 可执行的局部修复意见。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"目标文件：{file_path}\n"
                "后端确定性校验/试运行错误：\n"
                f"{deterministic_error}\n\n"
                "候选内容：\n"
                "```text\n"
                f"{content[-12000:]}\n"
                "```\n\n"
                "请输出 JSON："
                "{\"passed\": false, \"issues\": [\"...\"], "
                "\"repair_instructions\": \"给 coder 的局部修改指令；若有 Markdown fence/多文件包，明确要求只返回目标脚本源码本身，不要 fence/说明/写入文件标签。\"}"
            ),
        },
    ]
    try:
        text = await complete_chat_once(messages, route.model)
    except Exception as exc:  # pragma: no cover - defensive fallback
        logger.warning("Creator file validator failed; using deterministic feedback: %s", exc)
        return {
            "passed": False,
            "issues": [deterministic_error],
            "repair_instructions": deterministic_error,
            "model": route.model,
        }

    data = _parse_validator_json_object(text)
    if not data:
        return {
            "passed": False,
            "issues": [deterministic_error],
            "repair_instructions": deterministic_error,
            "model": route.model,
        }

    issues = data.get("issues") if isinstance(data.get("issues"), list) else []
    instructions = str(data.get("repair_instructions") or data.get("feedback") or deterministic_error)
    return {
        "passed": bool(data.get("passed", data.get("valid", False))) and not issues,
        "issues": [str(item) for item in issues],
        "repair_instructions": instructions,
        "model": route.model,
    }


def _format_file_validator_feedback(deterministic_error: str, validator_report: dict) -> str:
    issues = validator_report.get("issues") or []
    issue_text = "\n".join(f"- {issue}" for issue in issues) if issues else "- （校验模型未返回额外问题）"
    return (
        "后端确定性校验/试运行错误：\n"
        f"{deterministic_error}\n\n"
        f"校验模型：{validator_report.get('model', '')}\n"
        "校验模型问题列表：\n"
        f"{issue_text}\n\n"
        "校验模型给 coder 的修复意见：\n"
        f"{validator_report.get('repair_instructions') or deterministic_error}"
    )


def _is_valid_normalized_script_source(file_path: str, content: str) -> bool:
    """Return whether content is safe to accept as the requested raw script.

    This helper is intentionally narrower than the full script validator: it only
    checks that deterministic Markdown/bundle cleanup produced one raw source
    file.  Full fake-implementation, contract, dependency and trial-run checks
    still happen in the normal validation pipeline.
    """
    stripped = content.strip()
    if not stripped or "```" in stripped or _MULTI_FILE_MARKER_RE.search(stripped):
        return False

    if Path(file_path).suffix.lower() == ".py":
        try:
            ast.parse(stripped)
        except SyntaxError:
            return False

    return True


def _extract_single_wrapping_fence(content: str) -> str | None:
    """Extract a code block only when it wraps the entire model response.

    Models often wrap repaired scripts in ```text fences, sometimes with CRLF
    line endings or a closing fence that is longer than the opener.  This parser
    intentionally accepts only a whole-response fence: any prose before/after the
    block, or any non-fence trailing line, returns None and lets validation reject
    the ambiguous output.
    """
    stripped = content.strip().lstrip("\ufeff")
    lines = stripped.splitlines()
    if len(lines) < 2:
        return None

    opening = lines[0].strip()
    opening_match = re.match(r"^(`{3,}|~{3,})[^`~]*$", opening)
    if not opening_match:
        return None

    fence = opening_match.group(1)
    fence_char = fence[0]
    min_fence_len = len(fence)
    closing = lines[-1].strip()
    if not re.fullmatch(rf"{re.escape(fence_char)}{{{min_fence_len},}}", closing):
        return None

    return "\n".join(lines[1:-1]).strip()


def _normalize_generated_file_content(file_path: str, content: str) -> str:
    """Normalize model output while keeping script extraction conservative."""
    if file_path.startswith("scripts/"):
        stripped = content.strip()

        # Low-risk deterministic recovery for common coder behavior: peel only
        # whole-response fences.  A repeated pass handles responses like
        # ```text wrapping a complete ```python block, but we deliberately do not
        # extract scripts from multi-file bundles or prose because those responses
        # are ambiguous and should be repaired by the model instead.
        normalized = stripped
        for _ in range(3):
            wrapping_fence = _extract_single_wrapping_fence(normalized)
            if wrapping_fence is None:
                break
            normalized = wrapping_fence.strip()
            if _is_valid_normalized_script_source(file_path, normalized):
                return normalized

        return stripped

    extracted = _extract_target_file_from_bundle(content, file_path)
    return _strip_code_fence(extracted if extracted is not None else content)


def _sanitize_generated_file_content(file_path: str, content: str) -> str:
    """Normalize model output into exactly the requested file content."""
    sanitized = _normalize_generated_file_content(file_path, content)
    _validate_generated_file_content(file_path, sanitized)
    return sanitized


def _strip_code_fence(content: str) -> str:
    """Strip wrapping code-fence markers that a model may output despite instructions.

    Handles:
      ```python\\n<code>\\n```
      ```\\n<code>\\n```
      ~~~~\\n<code>\\n~~~~
    """
    stripped = content.strip()
    # Opening fence with optional language tag
    m_open = re.match(r"^(`{3,}|~{3,})[^\n]*\n", stripped)
    if m_open:
        fence_char = stripped[0]
        rest = stripped[m_open.end():]
        close_pat = re.compile(r"\n" + re.escape(fence_char) + r"{3,}\s*$")
        m_close = close_pat.search(rest)
        if m_close:
            return rest[: m_close.start()].strip()
        return rest.strip()
    return stripped


def _build_generate_file_prompt(
    file_path: str,
    skill_name: str,
    purpose: str,
    blueprint_text: str,
    conversation_history: list[dict],
) -> list[dict]:
    """Build a minimal generation prompt for a single Skill file.

    The model is asked to output *only* raw file content — no fences, no JSON,
    no explanations.  This maximises reliability for small or unstable models.
    """
    ext = Path(file_path).suffix.lower()
    lang = _LANG_LABELS.get(ext, "文本")

    clean_blueprint_text = _clean_blueprint_for_file_prompt(blueprint_text)
    declared_paths = _extract_declared_skill_paths(blueprint_text)
    declared_paths_text = "\n".join(f"- {path}" for path in declared_paths) or "- （蓝图未显式列出资源文件）"

    if file_path == "SKILL.md":
        instruction = (
            f'你正在为 Skill 包 "{skill_name}" 生成 SKILL.md 文件。\n\n'
            "要求：\n"
            "1. 只输出 SKILL.md 的文件内容，不要任何解释，不要 Markdown 代码块包裹。\n"
            "2. 文件必须以 YAML frontmatter 开始，格式严格如下（冒号后有一个空格）：\n"
            "---\n"
            f"name: {skill_name}\n"
            "description: <一句话说明本 Skill 的用途>\n"
            "---\n"
            "3. frontmatter 闭合后，输出 Skill 的核心执行说明（普通 Markdown 正文）。\n"
            "4. 执行说明应指导宿主 AI 如何理解用户请求、何时直接回答、何时输出显式可执行 block。\n"
            "5. 如果蓝图包含 scripts/ 资源，SKILL.md 正文必须为每个 scripts/ 路径提供一个可执行的 ```bash fenced code block，命令参数必须与脚本接口一致。\n"
            "6. 如果蓝图包含 references/ 资源，SKILL.md 正文必须在“参考资料/资源”小节明确引用每个 references/ 路径，并说明何时读取。\n"
            "7. 不要在输出内容的外侧套 ``` 代码块，但 SKILL.md 正文内部必须按需包含示例 ```bash fenced code block。\n"
            "8. 禁止只写‘立即调用 `scripts/...`’这种隐式执行描述；必须写明 assistant 应输出可执行 fenced block。\n"
            "9. 禁止复制 Creator 界面流程、确认清单、‘点击开始创建/开始生成’、系统将自动创建文件等平台创建流程文案。\n"
            "10. 以下宿主 Markdown 执行说明是内部写作约束，只能转化为面向使用者的 Skill 说明，不要逐字复制这些约束或标题。\n"
            f"{_SKILL_MD_MARKDOWN_EXECUTION_GUIDE}\n"
            f"蓝图声明的文件路径（必须覆盖对应 scripts/references 要求）：\n{declared_paths_text}\n\n"
            f"以下是已确认的蓝图（已移除 Creator UI 确认文案），你的内容必须与此一致：\n\n{clean_blueprint_text}"
        )
    elif file_path.startswith("scripts/"):
        instruction = (
            f'你正在为 Skill 包 "{skill_name}" 生成 {file_path} 文件。\n\n'
            f"职责说明：{purpose}\n\n"
            "要求：\n"
            f"1. 只输出完整可运行的 {lang} 代码，不要任何说明文字。\n"
            "2. 不要用 ``` 代码块包裹输出内容。\n"
            "3. 脚本的命令行参数、stdin/stdout 接口必须与蓝图和 SKILL.md 里的 Markdown 命令示例一致。\n"
            "4. 如果命令示例传入 JSON 字符串参数，脚本必须读取 sys.argv[1] 并使用 json.loads 解析。\n"
            "5. 必须实际使用用户可变参数生成结果；禁止把示例结果、示例标题、示例图片路径硬编码成固定输出。\n"
            "6. 文本/代码/视觉理解与图片生成的模型来源必须区分：文本语义能力使用 LLM_BASE_URL + TEXT_MODEL；看图/OCR/多模态理解使用 LLM_BASE_URL + VISION_MODEL；生成图片使用平台 Stable Diffusion 图片运行时（IMAGE_BASE_URL + IMAGE_MODEL），不要把 VISION_MODEL 用于图片生成。\n"
            "7. 如果脚本需要生成图片，不要在脚本里写中文 prompt 翻译逻辑，也不要直接调用 /v1/images/generations；必须调用 `from backend.services.skill_runtime import generate_stable_diffusion_image`，把用户 topic 原文传入该 helper。平台会静默完成中文 topic 到英文 Stable Diffusion prompt 的转换、IMAGE_MODEL 选择、b64_json 解析和 OUTPUT_DIR 图片落盘。\n"
            "8. 图片脚本 stdout 必须输出结构化 JSON，并返回 helper 结果里的 image_path；禁止输出 base64 data URI，禁止假设接口只返回 url；可按需读取平台注入的 IMAGE_MODEL / IMAGE_BASE_URL / IMAGE_SIZE / IMAGE_API_KEY 等环境变量，但不要硬编码，也不需要额外校验它们是否存在。\n"
            "9. 如果脚本只做确定性计算、转换、文件处理或格式化，必须实现真实算法并使用用户输入；禁止假 API、placeholder 文件、纯色/空白图片或 ASCII 图冒充输出。\n"
            "10. stdout 应输出结构化 JSON（例如 {\"text\": ..., \"image_path\": ...}），不要混入调试说明。\n"
            "11. 所有导入的第三方库必须真实存在且常见；Creator 保存前会先扫描 Python import 并安装缺失依赖，再按“生成→测试→修复生成→再测试”的闭环试运行；脚本仍必须包含必要的错误处理逻辑（如参数校验、文件不存在提示等）。\n\n"
            f"蓝图声明的文件路径：\n{declared_paths_text}\n\n"
            f"以下是已确认的蓝图：\n\n{clean_blueprint_text}"
        )
    elif file_path.startswith("references/"):
        instruction = (
            f'你正在为 Skill 包 "{skill_name}" 生成 {file_path} 参考资料文件。\n\n'
            f"职责说明：{purpose}\n\n"
            "要求：\n"
            "1. 只输出 Markdown 文档内容，不要额外的说明文字。\n"
            "2. 不要在文档外套 ``` 代码块。\n"
            "3. 内容应是有实际指导价值的参考资料，不是对参考资料的再描述。\n\n"
            f"蓝图声明的文件路径：\n{declared_paths_text}\n\n"
            f"以下是已确认的蓝图（参考资料职责说明见 references/ 部分）：\n\n{clean_blueprint_text}"
        )
    elif file_path.startswith("assets/"):
        instruction = (
            f'你正在为 Skill 包 "{skill_name}" 生成 {file_path} 资源文件。\n\n'
            f"职责说明：{purpose}\n\n"
            "要求：\n"
            f"1. 只输出 {lang} 格式的文件内容，不要任何说明文字。\n"
            "2. 不要用 ``` 代码块包裹输出。\n\n"
            f"蓝图声明的文件路径：\n{declared_paths_text}\n\n"
            f"以下是已确认的蓝图：\n\n{clean_blueprint_text}"
        )
    else:
        instruction = (
            f'你正在为 Skill 包 "{skill_name}" 生成 {file_path} 文件。\n\n'
            f"职责说明：{purpose}\n\n"
            "要求：直接输出文件内容，不要任何解释，不要 Markdown 代码块包裹。\n\n"
            f"蓝图声明的文件路径：\n{declared_paths_text}\n\n"
            f"蓝图：\n\n{clean_blueprint_text}"
        )

    messages: list[dict] = [{"role": "system", "content": instruction}]

    # Include recent user context but skip Creator UI confirmation text. Assistant
    # blueprint confirmations often contain "click Start" operational prose that
    # must never be copied into generated files.
    for msg in conversation_history[-_MAX_HISTORY_TURNS:]:
        if not isinstance(msg, dict) or msg.get("role") not in {"user", "assistant"}:
            continue
        content = str(msg.get("content") or "")
        if msg.get("role") == "assistant" and _CREATOR_FLOW_LEAK_RE.search(content):
            continue
        messages.append({**msg, "content": _clean_blueprint_for_file_prompt(content)})

    return messages


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/analyze-blueprint", response_model=AnalyzeBlueprintResponse)
async def analyze_blueprint(request: AnalyzeBlueprintRequest):
    """Extract a file-creation plan from the conversation blueprint.

    Pure rule-based extraction — no LLM call is made.
    """
    plan: BlueprintPlan = parse_blueprint(request.messages)
    return AnalyzeBlueprintResponse(
        skill_name=plan.skill_name,
        files=[
            FileSpecOut(
                path=f.path,
                purpose=f.purpose,
                required=f.required,
                can_skip=f.can_skip,
            )
            for f in plan.files
        ],
        warnings=plan.warnings,
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


@router.post("/generate-file")
async def generate_file(request: GenerateFileRequest):
    """Stream the generated content for a single Skill file.

    SSE event shapes:
      ``{ "content": "<chunk>" }``  — content chunk
      ``{ "done": true }``           — generation complete
      ``{ "error": "<message>" }``   — generation failed
    """
    _validate_file_path(request.file_path)
    skill_name = _validate_skill_name(request.skill_name)
    route = route_creator_file_model(
        file_path=request.file_path,
        purpose=request.purpose,
        requested_model=request.model,
    )
    model = route.model

    prompt_messages = _build_generate_file_prompt(
        file_path=request.file_path,
        skill_name=skill_name,
        purpose=request.purpose,
        blueprint_text=request.blueprint_text,
        conversation_history=request.conversation_history,
    )

    async def event_stream():
        last_validation_error = ""
        repair_attempts = 0
        try:
            yield _sse({"model_ack": route.ack()})
            ack_payload = {}

            def _capture_ack(payload: dict) -> None:
                ack_payload.update(payload)

            generated_chunks: list[str] = []
            async for chunk in stream_chat(prompt_messages, model, model_ack_callback=_capture_ack):
                if ack_payload:
                    yield _sse({"model_ack": {**route.ack(actual_model=ack_payload.get("actual_model")), "provider": ack_payload}})
                    ack_payload.clear()
                generated_chunks.append(chunk)

            raw_content = "".join(generated_chunks)

            should_repair = request.file_path.startswith("scripts/") or request.file_path == "SKILL.md"
            if should_repair:
                content = raw_content
                last_error = ""
                for attempt in range(_MAX_FILE_REPAIR_ATTEMPTS + 1):
                    try:
                        content = _sanitize_generated_file_content(request.file_path, content)
                        if request.file_path == "SKILL.md":
                            _validate_skill_md_contract(content, request.blueprint_text)
                        else:
                            _trial_run_generated_script(skill_name, request.file_path, content)
                        last_error = ""
                        break
                    except ValueError as validation_exc:
                        deterministic_error = str(validation_exc)
                        validator_report = await _run_generated_file_validator_round(
                            file_path=request.file_path,
                            content=content,
                            deterministic_error=deterministic_error,
                            requested_model=model,
                        )
                        last_error = _format_file_validator_feedback(deterministic_error, validator_report)
                        last_validation_error = last_error
                        if attempt >= _MAX_FILE_REPAIR_ATTEMPTS:
                            repair_attempts = attempt
                            yield _sse({
                                "validation": {
                                    "status": "failed",
                                    "attempt": attempt,
                                    "error": deterministic_error,
                                    "validator": validator_report,
                                }
                            })
                            raise
                        repair_attempts = attempt + 1
                        logger.info(
                            "repairing generated file skill=%s file=%s attempt=%s error=%s validator_issues=%s",
                            skill_name,
                            request.file_path,
                            repair_attempts,
                            deterministic_error,
                            validator_report.get("issues"),
                        )
                        yield _sse({
                            "validation": {
                                "status": "repairing",
                                "attempt": repair_attempts,
                                "error": deterministic_error,
                                "validator": validator_report,
                            }
                        })
                        content = await _repair_generated_file_with_feedback(
                            prompt_messages=prompt_messages,
                            model=model,
                            file_path=request.file_path,
                            previous_content=content,
                            validation_error=last_error,
                        )
            else:
                content = _sanitize_generated_file_content(request.file_path, raw_content)

            yield _sse({"content": content})
            yield _sse({"done": True})
        except Exception as exc:
            logger.exception(
                "generate-file stream error skill=%s file=%s",
                skill_name,
                request.file_path,
            )
            # Return a safe user-facing message; full stack trace is in server logs.
            if last_validation_error:
                yield _sse({
                    "error": (
                        f"文件内容生成失败：已自动修复 {repair_attempts} 次仍未通过。"
                        f"最后错误：{last_validation_error}"
                    )
                })
            else:
                yield _sse({"error": "文件内容生成失败，请重试。详情已记录在服务器日志中。"})
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/write-file", response_model=WriteFileResponse)
async def write_file(request: WriteFileRequest):
    """Write generated content to a Skill file on disk.

    Automatically strips spurious code-fence wrappers the LLM may add.
    """
    _validate_file_path(request.file_path)
    skill_name = _validate_skill_name(request.skill_name)

    try:
        content = _sanitize_generated_file_content(request.file_path, request.content)
        if request.file_path == "SKILL.md":
            _validate_skill_md_against_existing_files(skill_name, content)
        _validate_script_against_existing_skill_contract(skill_name, request.file_path, content)
        _trial_run_generated_script(skill_name, request.file_path, content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if request.file_path == "SKILL.md":
        result = run_action({"action": "write", "name": skill_name, "content": content})
    else:
        p = Path(request.file_path)
        folder = p.parts[0]   # "scripts" / "references" / "assets"
        filename = p.name
        result = run_action(
            {
                "action": "write_file",
                "name": skill_name,
                "folder": folder,
                "filename": filename,
                "content": content,
            }
        )

    return WriteFileResponse(
        success=result["success"],
        path=result.get("path"),
        bytes=len(content.encode("utf-8")) if result["success"] else 0,
        message=result["message"],
    )


@router.post("/validate-skill", response_model=SkillActionResponse)
async def validate_skill(request: SkillActionRequest):
    """Validate SKILL.md format for a Skill package."""
    skill_name = _validate_skill_name(request.skill_name)
    result = run_action({"action": "validate", "name": skill_name})
    if result["success"]:
        skill_dir = settings.skills_path / skill_name
        trial_errors: list[str] = []
        for script_path in sorted((skill_dir / "scripts").glob("*.py")) if (skill_dir / "scripts").is_dir() else []:
            rel_path = f"scripts/{script_path.name}"
            try:
                _trial_run_generated_script(skill_name, rel_path, script_path.read_text(encoding="utf-8"))
            except ValueError as exc:
                trial_errors.append(f"{rel_path}: {exc}")
        if trial_errors:
            return SkillActionResponse(
                success=False,
                path=None,
                message="SKILL.md 格式校验通过，但脚本试运行失败：\n" + "\n\n".join(trial_errors),
            )
    return SkillActionResponse(
        success=result["success"],
        path=result.get("path"),
        message=result["message"],
    )


@router.post("/package-skill", response_model=SkillActionResponse)
async def package_skill(request: SkillActionRequest):
    """Package a Skill directory into a distributable .skill archive."""
    skill_name = _validate_skill_name(request.skill_name)
    result = run_action({"action": "package", "name": skill_name})
    return SkillActionResponse(
        success=result["success"],
        path=result.get("path"),
        message=result["message"],
    )


@router.post("/init-from-blueprint", response_model=InitFromBlueprintResponse)
async def init_from_blueprint(request: InitFromBlueprintRequest):
    """Initialize Skill directory structure from blueprint file list.
    
    Creates empty files based on the blueprint analysis result.
    This ensures the file structure matches exactly what the user confirmed.
    
    Workflow:
    1. Create main skill directory
    2. Create required subdirectories (scripts/, references/, assets/)
    3. Create empty files based on the blueprint file list
    """
    skill_name = _validate_skill_name(request.skill_name)
    skill_root = settings.skill_public_dir / skill_name
    
    try:
        # Create main skill directory
        skill_root.mkdir(parents=True, exist_ok=True)
        
        files_created = 0
        
        for file_spec in request.files:
            file_path = skill_root / file_spec.path
            
            # Ensure parent directory exists
            file_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Create empty file if it doesn't exist
            if not file_path.exists():
                file_path.touch()
                files_created += 1
        
        return InitFromBlueprintResponse(
            success=True,
            path=str(skill_root),
            files_created=files_created,
            message=f"已创建 {files_created} 个文件",
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
