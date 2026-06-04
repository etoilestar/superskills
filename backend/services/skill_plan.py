"""SkillPlan role/contracts for Creator file generation.

This module provides a small, deterministic planning layer between a parsed
blueprint and file generation.  It intentionally keeps keyword signals as
classification hints only; downstream generation/validation is driven by the
resolved file role rather than by scanning the whole blueprint for domain words.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Literal

FileType = Literal["skill", "script", "reference", "asset", "skill_md"]
Language = Literal["python", "javascript", "bash", "sql", "yaml", "json", "markdown", "html", "css", "text"]
Runtime = Literal["python", "node", "bash", "shell", "generic", "none"]
ScriptRole = Literal["text_generator", "image_generator", "pdf_builder", "generic_script"]
ResourceRole = Literal["skill_overview", "reference", "asset"]
FileRole = ScriptRole | ResourceRole

SCRIPT_ROLES: frozenset[str] = frozenset({
    "text_generator",
    "image_generator",
    "pdf_builder",
    "generic_script",
})
RESOURCE_ROLES: frozenset[str] = frozenset({"skill_overview", "reference", "asset"})


@dataclass(frozen=True)
class RoleClassification:
    """Classifier output for a single file role decision."""

    role: FileRole
    confidence: float
    reason: str
    heuristic_signals: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SkillPlanEntry:
    """Contract for one file that Creator will generate."""

    path: str
    file_type: FileType
    role: FileRole
    purpose: str
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    forbidden_capabilities: list[str] = field(default_factory=list)
    reference_files: list[str] = field(default_factory=list)
    language: Language = "text"
    runtime: Runtime = "none"
    entrypoint: str = ""
    command_template: str = ""
    required: bool = True
    can_skip: bool = False
    confidence: float = 0.0
    reason: str = ""
    heuristic_signals: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SkillPlan:
    """Full Creator plan for a skill package."""

    skill_name: str
    files: list[SkillPlanEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_IMAGE_RE = re.compile(r"图片|图像|绘图|海报|插画|image|photo|poster|illustration|stable\s*diffusion", re.I)
_PDF_RE = re.compile(r"pdf|报告|排版|layout|document|report", re.I)
_TEXT_RE = re.compile(r"文本|文案|故事|谜语|摘要|写作|text|story|riddle|summary|copy", re.I)
_MODEL_RE = re.compile(r"模型|llm|大语言|多模态|vision|image_model|text_model", re.I)


def file_type_for_path(path: str) -> FileType:
    if path == "SKILL.md":
        return "skill_md"
    if path.startswith("scripts/"):
        return "script"
    if path.startswith("references/"):
        return "reference"
    return "asset"


def heuristic_signals_for_file(file_path: str, purpose: str = "", blueprint_summary: str = "") -> list[str]:
    """Return role-classification hints without making the final contract decision."""
    text = f"{file_path}\n{purpose}\n{blueprint_summary}"
    signals: list[str] = []
    if _IMAGE_RE.search(text):
        signals.append("mentions_image")
    if _PDF_RE.search(text):
        signals.append("mentions_pdf")
    if _TEXT_RE.search(text):
        signals.append("mentions_text")
    if _MODEL_RE.search(text):
        signals.append("mentions_model")
    if Path(file_path).suffix.lower() == ".py":
        signals.append("python_script")
    return signals


def language_for_path(path: str) -> Language:
    ext = Path(path).suffix.lower()
    if ext == ".py":
        return "python"
    if ext in {".js", ".mjs", ".cjs", ".ts"}:
        return "javascript"
    if ext in {".sh", ".bash"}:
        return "bash"
    if ext == ".sql":
        return "sql"
    if ext in {".yaml", ".yml"}:
        return "yaml"
    if ext == ".json":
        return "json"
    if ext == ".md":
        return "markdown"
    if ext == ".html":
        return "html"
    if ext == ".css":
        return "css"
    return "text"


def runtime_for_language(language: str, file_type: FileType) -> Runtime:
    if file_type != "script":
        return "none"
    if language == "python":
        return "python"
    if language == "javascript":
        return "node"
    if language == "bash":
        return "bash"
    return "generic"


def command_template_for_entry(path: str, runtime: Runtime, inputs: list[str]) -> str:
    keys = inputs or ["payload"]
    payload = "{" + ",".join(f'"{key}":"{{{{{key}}}}}"' for key in keys) + "}"
    if runtime == "python":
        return f"python {path} '{payload}'"
    if runtime == "node":
        return f"node {path} '{payload}'"
    if runtime == "bash":
        return f"bash {path} '{payload}'"
    if runtime == "shell":
        return f"sh {path} '{payload}'"
    return f"{path} '{payload}'"



_EXPLICIT_ROLE_RE = re.compile(
    r"(?:role|角色|职责)\s*[：:=]\s*(text_generator|image_generator|pdf_builder|generic_script)",
    re.I,
)


def _normalize_role(value: str) -> FileRole | None:
    lowered = (value or "").strip().lower()
    return lowered if lowered in SCRIPT_ROLES or lowered in RESOURCE_ROLES else None  # type: ignore[return-value]




def _segment_for_file(file_path: str, *texts: str) -> str:
    """Return nearby plan text for a file path, stopping before the next file block."""
    next_path_re = r"(?:scripts|references|assets)/[A-Za-z0-9_./-]+|SKILL\.md"
    best = ""
    for text in texts:
        text = text or ""
        for occurrence in re.finditer(re.escape(file_path), text):
            after = text[occurrence.end():]
            next_match = re.search(next_path_re, after)
            segment = after[: next_match.start()] if next_match else after
            # Prefer block-style segments that actually contain contract fields;
            # inline path mentions in section summaries often have no local data.
            if re.search(r"\b(?:role|inputs|outputs|dependencies|required_capabilities|forbidden_capabilities)\b\s*[：:=]", segment, re.I):
                return segment
            if len(segment) > len(best):
                best = segment
    return best or "\n".join(texts)


def _explicit_list_field(field_name: str, *, file_path: str, purpose: str = "", blueprint_summary: str = "") -> list[str] | None:
    """Extract SkillPlan list fields such as inputs/outputs/dependencies.

    Accepted syntaxes include `inputs: topic, prompt`, `inputs=[topic,prompt]`,
    and Chinese full-width separators.  The extraction is deliberately local to
    the file's plan segment so domain prose elsewhere cannot enable contracts.
    """
    segment = _segment_for_file(file_path, purpose, blueprint_summary)
    pattern = re.compile(rf"(?:{re.escape(field_name)}|{re.escape(field_name.replace('_', ' '))})\s*[：:=]\s*\[?([^\]\n;]+)\]?", re.I)
    match = pattern.search(segment)
    if not match:
        return None
    raw = match.group(1)
    values = [item.strip().strip("'\"") for item in re.split(r"[,，、]\s*", raw) if item.strip()]
    cleaned: list[str] = []
    for item in values:
        if field_name == "inputs":
            # Keep only the JSON argv key.  Model/blueprint prose often writes
            # `topic: string`, `tone=humorous`, or `style (default: popular-science)`;
            # those must remain `topic`, `tone`, and `style` instead of being
            # concatenated into invalid keys such as `topicstring`.
            item = re.split(r"\s*(?::|=|（|\(|\s)\s*", item, maxsplit=1)[0]
            item = re.sub(r"[^A-Za-z0-9_-]", "", item)
        else:
            item = re.sub(r"[^A-Za-z0-9_./-]", "", item)
        if item:
            cleaned.append(item)
    return cleaned




def _explicit_scalar_field(field_name: str, *, file_path: str, purpose: str = "", blueprint_summary: str = "") -> str | None:
    """Extract scalar SkillPlan fields such as language/runtime from local plan text."""
    segment = _segment_for_file(file_path, purpose, blueprint_summary)
    pattern = re.compile(rf"(?:{re.escape(field_name)}|{re.escape(field_name.replace('_', ' '))})\s*[：:=]\s*([^\s\n;,]+)", re.I)
    match = pattern.search(segment)
    if not match:
        return None
    value = match.group(1).strip().strip("'\"").lower()
    value = re.sub(r"[^a-z0-9_-]", "", value)
    return value or None

def _explicit_role_from_plan_text(*, file_path: str, purpose: str = "", blueprint_summary: str = "") -> FileRole | None:
    """Extract an explicit role declared by the plan/model, not by domain keywords.

    Supports both compact one-line declarations such as
    ``scripts/a.py role: text_generator`` and SkillPlan blocks where the path
    appears on one line and ``role: ...`` appears in the following indented
    contract lines.
    """
    next_path_re = r"(?:scripts|references|assets)/[A-Za-z0-9_./-]+|SKILL\.md"

    def segment_after_path(text: str) -> str:
        if file_path not in text:
            return text
        after = text.split(file_path, 1)[1]
        next_match = re.search(next_path_re, after)
        if next_match:
            between = after[: next_match.start()]
            # Inline prose may mention a reference/dependency path before the
            # role; block-style SkillPlan keeps role on following indented lines.
            if _EXPLICIT_ROLE_RE.search(between):
                return between
            role_before_next_file = _EXPLICIT_ROLE_RE.search(after)
            if role_before_next_file and role_before_next_file.start() < 500:
                return after[: role_before_next_file.end()]
            return between
        return after

    for text in (purpose, blueprint_summary):
        text = text or ""
        if file_path in text:
            segment = segment_after_path(text)
            match = _EXPLICIT_ROLE_RE.search(segment)
            if match:
                return _normalize_role(match.group(1))
        for line in text.splitlines():
            if file_path not in line and line.strip() != purpose.strip():
                continue
            segment = segment_after_path(line)
            match = _EXPLICIT_ROLE_RE.search(segment)
            if match:
                return _normalize_role(match.group(1))

    match = _EXPLICIT_ROLE_RE.search(purpose or "")
    if match:
        return _normalize_role(match.group(1))
    return None


def file_role_classifier(
    *,
    file_path: str,
    purpose: str = "",
    blueprint_summary: str = "",
    heuristic_signals: list[str] | None = None,
) -> RoleClassification:
    """Classify one file into a Creator role from the plan/model contract.

    Domain keyword/regex matches are collected as heuristic_signals only.  They
    do not directly select skeletons, contracts, capabilities, or specialized
    roles.  Specialized script roles require an explicit plan/model role such
    as ``role: image_generator``.  Ambiguous scripts fall back to conservative
    ``generic_script`` so high-impact capabilities are not enabled by accident.
    """
    file_type = file_type_for_path(file_path)
    signals = list(heuristic_signals or heuristic_signals_for_file(file_path, purpose, blueprint_summary))

    if file_type == "skill_md":
        return RoleClassification("skill_overview", 1.0, "SKILL.md is the process overview file", signals)
    if file_type == "reference":
        return RoleClassification("reference", 1.0, "references/ files contain subtask guidance", signals)
    if file_type == "asset":
        return RoleClassification("asset", 1.0, "assets/ files are static resources or templates", signals)

    explicit_role = _explicit_role_from_plan_text(
        file_path=file_path,
        purpose=purpose,
        blueprint_summary=blueprint_summary,
    )
    if explicit_role in SCRIPT_ROLES:
        return RoleClassification(explicit_role, 0.95, "explicit role declared by SkillPlan/blueprint", signals)

    return RoleClassification(
        "generic_script",
        0.45,
        "no explicit script role in SkillPlan; using conservative generic_script fallback",
        signals,
    )


def default_io_for_role(role: FileRole) -> tuple[list[str], list[str]]:
    if role == "text_generator":
        return ["topic", "prompt", "text"], ["text"]
    if role == "image_generator":
        return ["topic", "prompt", "text"], ["image_paths", "images"]
    if role == "pdf_builder":
        return ["text", "image_paths", "template_path"], ["pdf_path", "file_paths"]
    if role == "reference":
        return [], ["non_empty_markdown", "required_sections"]
    if role == "asset":
        return [], ["existing_parseable_file"]
    if role == "skill_overview":
        return ["user_request"], ["workflow", "script_order", "resource_references"]
    return ["payload"], ["text", "file_paths"]


def capabilities_for_role(role: FileRole) -> tuple[list[str], list[str]]:
    if role == "text_generator":
        return ["text_generation"], ["image_generation", "pdf_generation"]
    if role == "image_generator":
        return ["image_generation"], ["pdf_generation"]
    if role == "pdf_builder":
        return ["pdf_generation", "file_output"], ["image_generation"]
    if role == "reference":
        return ["reference_guidance"], ["runtime_execution", "image_generation"]
    if role == "asset":
        return ["static_resource"], ["runtime_execution", "image_generation"]
    if role == "skill_overview":
        return ["workflow_overview"], ["hidden_runtime_protocol"]
    return ["deterministic_execution"], []


def build_skill_plan_entry(
    *,
    file_path: str,
    purpose: str = "",
    required: bool = True,
    can_skip: bool = False,
    blueprint_summary: str = "",
    reference_files: list[str] | None = None,
) -> SkillPlanEntry:
    classification = file_role_classifier(
        file_path=file_path,
        purpose=purpose,
        blueprint_summary=blueprint_summary,
    )
    default_inputs, default_outputs = default_io_for_role(classification.role)
    inputs = _explicit_list_field("inputs", file_path=file_path, purpose=purpose, blueprint_summary=blueprint_summary) or default_inputs
    outputs = _explicit_list_field("outputs", file_path=file_path, purpose=purpose, blueprint_summary=blueprint_summary) or default_outputs
    dependencies = _explicit_list_field("dependencies", file_path=file_path, purpose=purpose, blueprint_summary=blueprint_summary) or list(reference_files or [])
    default_required_capabilities, default_forbidden_capabilities = capabilities_for_role(classification.role)
    required_capabilities = _explicit_list_field("required_capabilities", file_path=file_path, purpose=purpose, blueprint_summary=blueprint_summary) or default_required_capabilities
    forbidden_capabilities = _explicit_list_field("forbidden_capabilities", file_path=file_path, purpose=purpose, blueprint_summary=blueprint_summary) or default_forbidden_capabilities
    file_type = file_type_for_path(file_path)
    detected_language = language_for_path(file_path)
    explicit_language = _explicit_scalar_field("language", file_path=file_path, purpose=purpose, blueprint_summary=blueprint_summary)
    language = explicit_language if explicit_language in {"python", "javascript", "bash", "sql", "yaml", "json", "markdown", "html", "css", "text"} else detected_language
    detected_runtime = runtime_for_language(language, file_type)
    explicit_runtime = _explicit_scalar_field("runtime", file_path=file_path, purpose=purpose, blueprint_summary=blueprint_summary)
    runtime = explicit_runtime if explicit_runtime in {"python", "node", "bash", "shell", "generic", "none"} else detected_runtime
    return SkillPlanEntry(
        path=file_path,
        file_type=file_type,
        role=classification.role,
        purpose=purpose,
        inputs=inputs,
        outputs=outputs,
        dependencies=dependencies,
        required_capabilities=required_capabilities,
        forbidden_capabilities=forbidden_capabilities,
        reference_files=list(reference_files or []),
        language=language,
        runtime=runtime,
        entrypoint=file_path if file_type == "script" else "",
        command_template=command_template_for_entry(file_path, runtime, inputs) if file_type == "script" else "",
        required=required,
        can_skip=can_skip,
        confidence=classification.confidence,
        reason=classification.reason,
        heuristic_signals=classification.heuristic_signals,
    )


def validate_role(role: str, file_type: FileType) -> bool:
    if file_type == "script":
        return role in SCRIPT_ROLES
    return role in RESOURCE_ROLES
