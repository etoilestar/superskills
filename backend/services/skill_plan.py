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
ScriptRole = Literal["text_generator", "image_generator", "composite_generator", "pdf_builder", "docx_builder", "pptx_builder", "html_asset_builder", "asset_builder", "generic_script"]
ResourceRole = Literal["skill_overview", "reference", "asset"]
FileRole = ScriptRole | ResourceRole

SCRIPT_ROLES: frozenset[str] = frozenset({
    "text_generator",
    "image_generator",
    "pdf_builder",
    "docx_builder",
    "pptx_builder",
    "html_asset_builder",
    "asset_builder",
    "composite_generator",
    "generic_script",
})
RESOURCE_ROLES: frozenset[str] = frozenset({"skill_overview", "reference", "asset"})
_CREATOR_INTERNAL_REFERENCE_PATHS: tuple[str, ...] = (
    "kernel/references/best-practices.md",
    "kernel/references/workflows.md",
    "kernel/references/output-patterns.md",
)


def _is_skill_local_reference(path: str) -> bool:
    return path.startswith(("references/", "assets/", "scripts/"))


def _is_creator_internal_reference(path: str) -> bool:
    return path.startswith("kernel/references/")


def _dedupe_paths(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in paths:
        path = str(raw).strip().replace("\\", "/")
        if not path or path in seen:
            continue
        seen.add(path)
        result.append(path)
    return result


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
    optional_capabilities: list[str] = field(default_factory=list)
    allowed_capabilities: list[str] = field(default_factory=list)
    forbidden_capabilities: list[str] = field(default_factory=list)
    reference_files: list[str] = field(default_factory=list)
    skill_local_references: list[str] = field(default_factory=list)
    creator_internal_references: list[str] = field(default_factory=list)
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
_TEXT_RE = re.compile(r"文本|文案|故事|童话|剧本|谜语|摘要|写作|text|story|tale|fairy|riddle|summary|copy", re.I)
_MODEL_RE = re.compile(r"模型|llm|大语言|多模态|vision|image_model|text_model", re.I)
_IMAGE_SCRIPT_NAME_RE = re.compile(r"(?:^|[_/-])(images|imgs|render|illustration|poster|picture|photo|visuals)(?:[_.-]|$)|配图|插画|海报|图片", re.I)
_PDF_SCRIPT_NAME_RE = re.compile(r"(?:^|[_/-])(?:build|export|create|make|render|combine|merge)?_?pdf(?:[_.-]|$)|(?:^|[_/-])(?:pdf_builder|build_pdf|export_pdf|combine_to_pdf|merge_to_pdf)(?:[_.-]|$)|合并.*pdf|pdf.*合并", re.I)
_CUSTOM_CHARACTER_RE = re.compile(r"custom_character|character|角色|主角", re.I)


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
    r"(?:role|角色|职责)\s*[：:=]\s*(text_generator|image_generator|composite_generator|pdf_builder|docx_builder|pptx_builder|html_asset_builder|asset_builder|generic_script)",
    re.I,
)


def _normalize_role(value: str) -> FileRole | None:
    lowered = (value or "").strip().lower()
    return lowered if lowered in SCRIPT_ROLES or lowered in RESOURCE_ROLES else None  # type: ignore[return-value]




def _segment_for_file(file_path: str, *texts: str) -> str:
    """Return nearby plan text for a file path, stopping before the next file block.

    Capability fields must be scoped to the concrete file block.  Earlier
    versions fell back to the whole blueprint when a field was not found near
    ``file_path``; that allowed global model notes in SKILL.md to leak into
    deterministic builders such as ``scripts/build_pdf.py``.  The fallback is
    now limited to the explicit per-file ``purpose`` text passed by the caller
    (the first argument), never to the full blueprint/SKILL.md summary.
    """
    next_path_re = r"(?m)^\s*(?:(?:[-*]\s*)?(?:(?:scripts|references|assets)/[A-Za-z0-9_./-]+|SKILL\.md)|`{3,}|~{3,}|#{1,6}\s+)"
    best = ""
    saw_path = False
    for text in texts:
        text = text or ""
        for occurrence in re.finditer(re.escape(file_path), text):
            saw_path = True
            after = text[occurrence.end():]
            next_match = re.search(next_path_re, after)
            segment = after[: next_match.start()] if next_match else after
            # Prefer block-style segments that actually contain contract fields;
            # inline path mentions in section summaries often have no local data.
            if re.search(r"\b(?:role|inputs|outputs|dependencies|required_capabilities|optional_capabilities|allowed_capabilities|forbidden_capabilities)\b\s*[：:=]", segment, re.I):
                return segment
            if len(segment) > len(best):
                best = segment
    if saw_path:
        return best
    # The first text is the caller-provided purpose, which is already a local
    # per-file description.  Do not mine the full blueprint/SKILL.md for fields
    # when it does not contain this file path.
    return texts[0] if texts else ""


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
    raw = re.split(r"\s+(?:role|inputs|outputs|dependencies|required_capabilities|optional_capabilities|allowed_capabilities|forbidden_capabilities|language|runtime)\s*[：:=]", raw, maxsplit=1, flags=re.I)[0]
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
    next_path_re = r"(?m)^\s*(?:(?:[-*]\s*)?(?:(?:scripts|references|assets)/[A-Za-z0-9_./-]+|SKILL\.md)|`{3,}|~{3,}|#{1,6}\s+)"

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



def _should_promote_pdf_builder_role(file_path: str, purpose: str = "", blueprint_summary: str = "") -> bool:
    """Return True for deterministic scripts whose local responsibility is PDF output.

    PDF merge/export scripts are file builders, not model generators.  The path
    must identify a PDF builder/exporter (for example build_pdf.py,
    export_pdf.py, or combine_to_pdf.py); global SKILL.md model prose is ignored.
    """
    if file_type_for_path(file_path) != "script":
        return False
    if not _PDF_SCRIPT_NAME_RE.search(file_path):
        return False
    local_segment = _segment_for_file(file_path, purpose, blueprint_summary)
    local_text = f"{file_path}\n{local_segment or purpose}"
    if _IMAGE_SCRIPT_NAME_RE.search(file_path):
        return False
    return bool(_PDF_RE.search(local_text) or _PDF_SCRIPT_NAME_RE.search(file_path))


def _should_promote_image_script_role(file_path: str, purpose: str = "", blueprint_summary: str = "") -> bool:
    """Return True for scripts whose local contract clearly names image generation.

    Generic prose such as "this skill generates images and PDFs" remains
    conservative.  Promotion requires both image-generation wording and an
    image-oriented script path/name so `scripts/main.py` in an ambiguous
    composite blueprint still falls back to `generic_script` unless role is
    explicitly declared.
    """
    if file_type_for_path(file_path) != "script":
        return False
    text = f"{file_path}\n{purpose}\n{blueprint_summary}"
    if not _IMAGE_RE.search(text):
        return False
    return bool(_IMAGE_SCRIPT_NAME_RE.search(file_path))


def _augment_inputs_for_role(role: FileRole, inputs: list[str], *, purpose: str = "", blueprint_summary: str = "") -> list[str]:
    augmented = list(inputs)
    text = f"{purpose}\n{blueprint_summary}"
    if role in {"image_generator", "composite_generator"} and _CUSTOM_CHARACTER_RE.search(text) and "custom_character" not in augmented:
        augmented.append("custom_character")
    return augmented

def file_role_classifier(
    *,
    file_path: str,
    purpose: str = "",
    blueprint_summary: str = "",
    heuristic_signals: list[str] | None = None,
) -> RoleClassification:
    """Classify one file into a Creator role from the plan/model contract.

    Domain keyword/regex matches are collected as heuristic_signals only.  They
    do not directly select model capabilities.  Model-generating roles require
    an explicit plan/model role such as ``role: image_generator``; deterministic
    PDF exporter paths such as ``build_pdf.py``/``combine_to_pdf.py`` can be
    inferred as ``pdf_builder`` because that role only grants file-output
    capabilities.  Ambiguous scripts fall back to conservative
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

    if _should_promote_pdf_builder_role(file_path, purpose, blueprint_summary):
        if "inferred_pdf_builder_role" not in signals:
            signals.append("inferred_pdf_builder_role")
        return RoleClassification(
            "pdf_builder",
            0.82,
            "pdf-oriented script path/local responsibility builds or combines PDF files",
            signals,
        )

    if _should_promote_image_script_role(file_path, purpose, blueprint_summary):
        if "inferred_image_script_role" not in signals:
            signals.append("inferred_image_script_role")
        if _TEXT_RE.search(f"{file_path}\n{purpose}\n{blueprint_summary}"):
            if "inferred_composite_script_role" not in signals:
                signals.append("inferred_composite_script_role")
            return RoleClassification(
                "composite_generator",
                0.84,
                "image-oriented script also requires text_generation capability",
                signals,
            )
        return RoleClassification(
            "image_generator",
            0.82,
            "image-oriented script path/purpose requires image_generation capability",
            signals,
        )

    return RoleClassification(
        "generic_script",
        0.45,
        "no explicit script role in SkillPlan; using conservative generic_script fallback",
        signals,
    )


def default_io_for_role(role: FileRole) -> tuple[list[str], list[str]]:
    """Return permissive blueprint hints for a role.

    These defaults are intentionally not a runtime contract.  Runtime dataflow is
    determined by the concrete SKILL.md command placeholders and each script's
    JSON stdout, so business Skills can choose domain-specific field names.
    """
    if role in SCRIPT_ROLES:
        return ["payload"], []
    if role == "reference":
        return [], ["non_empty_markdown", "required_sections"]
    if role == "asset":
        return [], ["existing_parseable_file"]
    if role == "skill_overview":
        return ["user_request"], ["workflow", "script_order", "resource_references"]
    return ["payload"], []


def capabilities_for_role(role: FileRole) -> tuple[list[str], list[str]]:
    if role == "text_generator":
        return ["text_generation"], ["image_generation", "pdf_generation"]
    if role == "image_generator":
        return ["image_generation"], ["text_generation", "pdf_generation"]
    if role == "composite_generator":
        return ["text_generation", "image_generation"], ["pdf_generation"]
    if role == "pdf_builder":
        return ["pdf_generation", "file_output"], []
    if role == "docx_builder":
        return ["docx_generation", "file_output"], []
    if role == "pptx_builder":
        return ["pptx_generation", "file_output"], []
    if role == "html_asset_builder":
        return ["html_asset_generation", "file_output"], []
    if role == "asset_builder":
        return ["asset_generation", "file_output"], []
    if role == "reference":
        return ["reference_guidance"], ["runtime_execution", "image_generation"]
    if role == "asset":
        return ["static_resource"], ["runtime_execution", "image_generation"]
    if role == "skill_overview":
        return ["workflow_overview"], ["hidden_runtime_protocol"]
    return ["deterministic_execution"], ["text_generation", "image_generation", "pdf_generation"]


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
    file_type = file_type_for_path(file_path)
    explicit_required_capabilities = _explicit_list_field("required_capabilities", file_path=file_path, purpose=purpose, blueprint_summary=blueprint_summary)
    explicit_optional_capabilities = _explicit_list_field("optional_capabilities", file_path=file_path, purpose=purpose, blueprint_summary=blueprint_summary)
    explicit_allowed_capabilities = _explicit_list_field("allowed_capabilities", file_path=file_path, purpose=purpose, blueprint_summary=blueprint_summary)
    role = classification.role
    role_reason = classification.reason
    if (
        file_type == "script"
        and explicit_required_capabilities
        and {"text_generation", "image_generation"}.issubset(set(explicit_required_capabilities))
        and role not in {"pdf_builder", "docx_builder", "pptx_builder", "html_asset_builder", "asset_builder"}
    ):
        role = "composite_generator"
        role_reason = "normalized text_generation + image_generation capabilities to composite_generator"
    explicit_inputs = _explicit_list_field("inputs", file_path=file_path, purpose=purpose, blueprint_summary=blueprint_summary)
    explicit_outputs = _explicit_list_field("outputs", file_path=file_path, purpose=purpose, blueprint_summary=blueprint_summary)
    default_inputs, default_outputs = default_io_for_role(role)
    inputs = explicit_inputs or default_inputs
    inputs = _augment_inputs_for_role(role, inputs, purpose=purpose, blueprint_summary=blueprint_summary)
    outputs = explicit_outputs or default_outputs
    all_reference_files = _dedupe_paths(list(reference_files or []))
    skill_local_references = [ref for ref in all_reference_files if _is_skill_local_reference(ref)]
    creator_internal_references = [ref for ref in all_reference_files if _is_creator_internal_reference(ref)]
    for ref in _CREATOR_INTERNAL_REFERENCE_PATHS:
        if ref not in creator_internal_references:
            creator_internal_references.append(ref)
    explicit_dependencies = _explicit_list_field("dependencies", file_path=file_path, purpose=purpose, blueprint_summary=blueprint_summary)
    dependencies = _dedupe_paths([ref for ref in (explicit_dependencies or skill_local_references) if _is_skill_local_reference(ref)])
    default_required_capabilities, default_forbidden_capabilities = capabilities_for_role(role)
    required_capabilities = explicit_required_capabilities or default_required_capabilities
    if (
        file_type == "script"
        and {"text_generation", "image_generation"}.issubset(set(required_capabilities))
        and role not in {"pdf_builder", "docx_builder", "pptx_builder", "html_asset_builder", "asset_builder"}
    ):
        role = "composite_generator"
        default_required_capabilities, default_forbidden_capabilities = capabilities_for_role(role)
        if not explicit_required_capabilities:
            required_capabilities = default_required_capabilities
        inputs = explicit_inputs or default_io_for_role(role)[0]
        inputs = _augment_inputs_for_role(role, inputs, purpose=purpose, blueprint_summary=blueprint_summary)
        outputs = explicit_outputs or default_io_for_role(role)[1]
        role_reason = "normalized text_generation + image_generation capabilities to composite_generator"
    optional_capabilities = explicit_optional_capabilities or []
    allowed_capabilities = explicit_allowed_capabilities or []
    forbidden_capabilities = _explicit_list_field("forbidden_capabilities", file_path=file_path, purpose=purpose, blueprint_summary=blueprint_summary) or default_forbidden_capabilities
    forbidden_capabilities = [capability for capability in forbidden_capabilities if capability not in required_capabilities]
    if role in {"pdf_builder", "docx_builder", "pptx_builder", "html_asset_builder"}:
        # Document exporters are optional by default so text/image generation can
        # be run on demand without forcing export.  An explicit blueprint/UI
        # required=true still overrides this default by passing required=True
        # with purpose text that says the user requested one-step export.
        if not re.search(r"一步|一次性|直接导出|必须导出|必需导出|one[- ]?step|single[- ]?step|required", purpose or "", re.I):
            required = False
            can_skip = True
    detected_language = language_for_path(file_path)
    explicit_language = _explicit_scalar_field("language", file_path=file_path, purpose=purpose, blueprint_summary=blueprint_summary)
    language = explicit_language if explicit_language in {"python", "javascript", "bash", "sql", "yaml", "json", "markdown", "html", "css", "text"} else detected_language
    detected_runtime = runtime_for_language(language, file_type)
    explicit_runtime = _explicit_scalar_field("runtime", file_path=file_path, purpose=purpose, blueprint_summary=blueprint_summary)
    runtime = explicit_runtime if explicit_runtime in {"python", "node", "bash", "shell", "generic", "none"} else detected_runtime
    return SkillPlanEntry(
        path=file_path,
        file_type=file_type,
        role=role,
        purpose=purpose,
        inputs=inputs,
        outputs=outputs,
        dependencies=dependencies,
        required_capabilities=required_capabilities,
        optional_capabilities=optional_capabilities,
        allowed_capabilities=allowed_capabilities,
        forbidden_capabilities=forbidden_capabilities,
        # Public/final SKILL.md references are skill-local only.  Creator
        # kernel references remain separate internal context and must never be
        # merged into reference_files/skill_local_references.
        reference_files=skill_local_references,
        skill_local_references=skill_local_references,
        creator_internal_references=creator_internal_references,
        language=language,
        runtime=runtime,
        entrypoint=file_path if file_type == "script" else "",
        command_template=command_template_for_entry(file_path, runtime, inputs) if file_type == "script" else "",
        required=required,
        can_skip=can_skip,
        confidence=classification.confidence,
        reason=role_reason,
        heuristic_signals=classification.heuristic_signals,
    )


def validate_role(role: str, file_type: FileType) -> bool:
    if file_type == "script":
        return role in SCRIPT_ROLES
    return role in RESOURCE_ROLES
