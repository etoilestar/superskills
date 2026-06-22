"""Blueprint parser — pure-Python extraction of file specs from a confirmed Skill blueprint.

No LLM calls are made here.  All regex failures degrade gracefully to
sensible defaults, and any uncertainty is captured in BlueprintPlan.warnings
so the frontend can surface it to the user.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from .skill_plan import RESOURCE_ROLES, SCRIPT_ROLES, SkillPlan, SkillPlanEntry, build_skill_plan_entry, is_business_capability, is_runtime_artifact_semantic, dependency_is_output_semantic, normalize_skill_plan, validate_file_plan_semantics, skill_plan_field_declaration_warnings

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class FileSpec:
    """A single file to be generated for a Skill package."""

    path: str          # relative to skill root: "SKILL.md" / "scripts/main.py"
    purpose: str       # human-readable description used as LLM prompt context
    required: bool = True
    can_skip: bool = False
    asset_source: str = ""  # explicit platform source for assets: user_upload/bundled/none


@dataclass
class BlueprintPlan:
    """Parsed creation plan extracted from a confirmed Skill blueprint."""

    skill_name: str
    files: list[FileSpec] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skill_plan: SkillPlan | None = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BLUEPRINT_MARKER = "📋 Skill 架构蓝图"

# "- **Skill 名称**: foo-bar" or "- **Skill 名称**: foo-bar"
_SKILL_NAME_RE = re.compile(
    r"-\s+\*\*Skill\s+名称\*\*[：:]\s*([^\n]+)",
    re.IGNORECASE,
)

# Inline backtick references: `scripts/main.py` or `references/guide.md`
_SKILL_PATH_INLINE_RE = re.compile(
    r"`((?:scripts|references|assets)/[^`\s]+)`"
)

# Tree structure patterns: 
# - ├── scripts/main.py 或 └── scripts/main.py (第一层级)
# - │   ├── scripts/main.py 或 │   └── scripts/main.py (第二层级)
# - /path/to/scripts/main.py (完整路径)
_TREE_FILE_RE = re.compile(
    r"(?:[│ ]{2,})?[├└]──\s*((?:/?[\w./-]+/)?(?:scripts|references|assets)/[^\s#]+)"
)

# "主入口脚本：scripts/xxx.py" or "主入口脚本: `scripts/xxx.py`"
_ENTRY_SCRIPT_RE = re.compile(
    r"主入口脚本[：:]\s*(`?)([^\n`]+)\1",
    re.IGNORECASE,
)

# "完整运行命令：python scripts/xxx.py ..."
_RUN_COMMAND_RE = re.compile(
    r"完整运行命令[：:]\s*([^\n]+)",
    re.IGNORECASE,
)

# Section lines in the blueprint for scripts / references / assets.
# These match lines like "- scripts/：..." or "- scripts/: 是否创建；..."
_SECTION_SCRIPTS_RE = re.compile(
    r"-\s+scripts/[：:]\s*([^\n]+(?:\n(?!\s*-).*)*)",
    re.IGNORECASE,
)
_SECTION_REFERENCES_RE = re.compile(
    r"-\s+references/[：:]\s*([^\n]+(?:\n(?!\s*-).*)*)",
    re.IGNORECASE,
)
_SECTION_ASSETS_RE = re.compile(
    r"-\s+assets/[：:]\s*([^\n]+(?:\n(?!\s*-).*)*)",
    re.IGNORECASE,
)

# Phrases that indicate a section is not needed
_SKIP_PHRASES: tuple[str, ...] = (
    "无需创建",
    "无需",
    "不需要",
    "暂无",
    "none",
    "n/a",
)

# Maximum allowed length (chars) for a normalised Skill name.
_MAX_SKILL_NAME_LENGTH = 64

class BlueprintShapeError(ValueError):
    """Raised when a confirmed Creator blueprint violates platform shape."""


_BLUEPRINT_UI_STOP_RE = re.compile(
    r"(?m)^\s*(?:AskUserQuestion|确认问题|用户确认|请选择|选项|按钮状态|创建进度|文件生成进度)\b|^\s*```text\s*$",
    re.I,
)


def clean_blueprint_body_text(blueprint_text: str) -> str:
    """Return only the Skill blueprint body, excluding Creator confirmation UI.

    The backend parser receives a business blueprint contract. Confirmation
    questions, option labels, button states, and progress text belong to the
    outer Creator UI/state envelope and must not become blueprint prose.
    """
    text = (blueprint_text or "").strip()
    if not text:
        return ""
    marker = re.search(r"(?m)^\s*##?\s*📋\s*Skill\s+架构蓝图\s*$|📋\s*Skill\s+架构蓝图", text)
    if marker:
        text = text[marker.start():]
    stop = _BLUEPRINT_UI_STOP_RE.search(text)
    if stop:
        text = text[: stop.start()]
    return text.strip()


def _format_capability_list(values: list[str]) -> str:
    return "[" + ", ".join(values) + "]"


def repair_blueprint_business_layers(blueprint_text: str) -> tuple[str, list[str]]:
    """Locally repair layer-mixed SkillPlan capability fields.

    This handles deterministic structure mistakes before strict validation:
    platform protocol/safety names are removed from business capability fields
    instead of causing an immediate 400. Platform-owned constraints remain
    Creator/Kernel state and are not migrated into the business SkillPlan.
    """
    text = clean_blueprint_body_text(blueprint_text)
    warnings: list[str] = []
    repaired_lines: list[str] = []
    current_path = ""

    for line in text.splitlines():
        path_match = re.match(r"^(\s*-\s*path\s*:\s*)`?([^`\n]+?)`?\s*$", line)
        if path_match:
            current_path = path_match.group(2).strip().strip("`'\"，,。.;；").replace("\\", "/")
            repaired_lines.append(line)
            continue

        field_match = re.match(r"^(\s*)(required_capabilities|business_forbidden_capabilities|forbidden_capabilities)(\s*:\s*)(.*)$", line)
        if not field_match:
            repaired_lines.append(line)
            continue

        indent, field_name, sep, raw_value = field_match.groups()
        values = _list_field_from_block(f"{field_name}: {raw_value}", field_name)
        if field_name == "required_capabilities":
            kept = [cap for cap in values if is_business_capability(cap)]
            removed = [cap for cap in values if not is_business_capability(cap)]
            if removed:
                warnings.append(
                    f"已从 {current_path or 'SkillPlan'} required_capabilities 移除平台协议/资源能力：{', '.join(removed)}。"
                )
            repaired_lines.append(f"{indent}{field_name}{sep}{_format_capability_list(kept)}")
            continue

        kept = [cap for cap in values if is_business_capability(cap)]
        removed = [cap for cap in values if not is_business_capability(cap)]
        if removed:
            warnings.append(
                f"已从 {current_path or 'SkillPlan'} business_forbidden_capabilities 移除平台安全/协议约束：{', '.join(removed)}。"
            )
        repaired_lines.append(f"{indent}business_forbidden_capabilities{sep}{_format_capability_list(kept)}")

    return "\n".join(repaired_lines).strip(), warnings


def _has_required_blueprint_marker(blueprint_text: str) -> bool:
    return bool(re.search(r"(?m)^\s*##\s+📋\s*Skill\s+架构蓝图\s*$", blueprint_text or ""))


def _section_exists(blueprint_text: str, title: str) -> bool:
    return bool(re.search(rf"(?m)^\s*###\s+{re.escape(title)}\s*$", blueprint_text or ""))


def _scalar_field_from_block(block: str, field: str) -> str:
    match = re.search(rf"(?m)^\s*{re.escape(field)}\s*:\s*(.+?)\s*$", block or "")
    if not match:
        return ""
    return match.group(1).strip().strip("`'\"，,。.;；")


def _list_field_from_block(block: str, field: str) -> list[str]:
    raw = _scalar_field_from_block(block, field)
    if not raw:
        return []
    if raw.startswith("[") and "]" in raw:
        raw = raw[1:raw.find("]")]
    values: list[str] = []
    for item in re.split(r"[,，、]\s*", raw):
        value = item.strip().strip("`'\"，,。.;；")
        if value and value not in values:
            values.append(value)
    return values


def _normalized_asset_source_from_block(block: str) -> str:
    source = _scalar_field_from_block(block, "source") or _scalar_field_from_block(block, "asset_source")
    source = source.strip().lower().replace("-", "_")
    aliases = {
        "user_upload": "user_upload",
        "upload": "user_upload",
        "uploaded": "user_upload",
        "bundled": "bundled",
        "static": "bundled",
        "none": "none",
        "no_assets": "none",
    }
    return aliases.get(source, source)


def _asset_block_mentions_runtime_artifact(block: str) -> bool:
    return bool(re.search(r"运行时产物|运行时生成|脚本生成|最终产物|最终生成|runtime\s+artifact|generated\s+artifact", block or "", re.I))


def _extract_skillplan_blocks(blueprint_text: str) -> dict[str, str]:
    """Return SkillPlan YAML-ish blocks keyed by path without reading business text."""
    blocks: dict[str, str] = {}
    text = blueprint_text or ""
    pattern = re.compile(r"(?ms)^\s*-\s*path\s*:\s*`?([^`\n]+)`?\s*\n(.*?)(?=^\s*-\s*path\s*:|^\s*###\s+|\Z)")
    for match in pattern.finditer(text):
        path = match.group(1).strip().strip("`'\"，,。.;；")
        block = match.group(0)
        if path:
            blocks[path.replace("\\", "/")] = block
    return blocks


def _paths_declared_in_shape(blueprint_text: str) -> set[str]:
    paths = {"SKILL.md"} if "SKILL.md" in (blueprint_text or "") else set()
    for prefix in ("scripts", "references", "assets"):
        paths.update(path for path in _extract_inline_paths(blueprint_text or "", prefix) if not _contains_path_wildcard(path))
    for match in _TREE_FILE_RE.finditer(blueprint_text or ""):
        tree_path = match.group(1).strip().strip("`'\"，,。.;；")
        for prefix in ("scripts/", "references/", "assets/"):
            idx = tree_path.find(prefix)
            if idx >= 0:
                candidate = tree_path[idx:]
                if not _contains_path_wildcard(candidate):
                    paths.add(candidate)
                break
    return {path.replace("\\", "/") for path in paths}


def validate_blueprint_shape_for_creator(blueprint_text: str) -> None:
    """Validate Creator platform blueprint shape, not business semantics.

    This keeps strict product-package structure while avoiding role/capability
    inference from business words.
    """
    text = blueprint_text or ""
    issues: list[str] = []
    warnings: list[str] = []

    if not _has_required_blueprint_marker(text):
        if "✅ Skill 架构蓝图" in text or "Skill 架构蓝图" in text:
            issues.append("蓝图 marker 必须是固定标题 `## 📋 Skill 架构蓝图`，不能替换为 ✅ 或其它标题。")
        else:
            issues.append("缺少固定蓝图标题 `## 📋 Skill 架构蓝图`。")

    if not _section_exists(text, "目录结构"):
        issues.append("缺少硬协议章节 `### 目录结构`。")
    if "SKILL.md" not in text:
        issues.append("目录结构必须列出 `SKILL.md`。")
    if "references/" not in text and not re.search(r"references[^\n]*(无需创建|无需|不需要)|(?:无需创建|无需|不需要)[^\n]*references", text, re.I):
        issues.append("目录结构必须列出具体 references/*，或明确写明 references/ 无需创建。")
    if "assets/" not in text and not re.search(r"assets[^\n]*(无需创建|无需|不需要)|(?:无需创建|无需|不需要)[^\n]*assets", text, re.I):
        issues.append("目录结构必须列出具体 assets/*，或明确写明 assets/ 无需创建。")
    if not _section_exists(text, "SkillPlan / 文件职责计划"):
        issues.append("缺少硬协议章节 `### SkillPlan / 文件职责计划`。")
    if not _section_exists(text, "宿主执行方式"):
        issues.append("缺少硬协议章节 `### 宿主执行方式`。")

    path_blocks = _extract_skillplan_blocks(text)
    if "SKILL.md" not in path_blocks:
        issues.append("SkillPlan / 文件职责计划必须包含 `SKILL.md` 文件计划。")

    declared_paths = _paths_declared_in_shape(text)
    script_paths = {path for path in declared_paths if path.startswith("scripts/")}
    reference_paths = {path for path in declared_paths if path.startswith("references/")}
    asset_paths = {path for path in declared_paths if path.startswith("assets/")}

    if re.search(r"(?m)^\s*-\s*scripts/\s*[：:]", text) and not script_paths and "无需" not in text:
        issues.append("如需要 scripts/，目录结构或 SkillPlan 必须列出具体 `scripts/*.py` 路径。")

    required_fields = [
        "inputs",
        "outputs",
        "dependencies",
        "references",
    ]
    valid_roles = set(SCRIPT_ROLES) | set(RESOURCE_ROLES)
    for path, block in path_blocks.items():
        missing = [field for field in required_fields if not re.search(rf"(?m)^\s*{field}\s*:", block)]
        if missing:
            issues.append(f"{path} 文件计划缺少字段：{', '.join(missing)}。")

        role = _scalar_field_from_block(block, "role")
        if role and role not in valid_roles:
            warnings.append(f"{path} role `{role}` 已降级为 component_hint；不会用于 hard fail、能力限制或工具选择。")

        file_kind = (_scalar_field_from_block(block, "file_kind") or "").strip().lower()
        if file_kind and file_kind not in {"script", "skill_doc", "reference", "asset", "config"}:
            issues.append(f"{path} file_kind `{file_kind}` 不在平台文件类型枚举内。")
        elif not file_kind:
            warnings.append(f"{path} 未声明 file_kind；系统将按路径归一化推断。")

        required_caps = _list_field_from_block(block, "required_capabilities")
        if required_caps:
            warnings.append(f"{path} required_capabilities 已降级为 hint；最终能力由 normalized plan 的 required_tool_slots 推断。")

        forbidden_caps = _list_field_from_block(block, "business_forbidden_capabilities") or _list_field_from_block(block, "forbidden_capabilities")
        if forbidden_caps:
            warnings.append(f"{path} forbidden_capabilities 属于业务层遗留字段，已移出 hard validation；安全约束由平台安全层处理。")

    for path in sorted(script_paths | reference_paths | asset_paths | {"SKILL.md"}):
        if path not in path_blocks:
            issues.append(f"目录结构声明了 `{path}`，但 SkillPlan / 文件职责计划缺少对应 path。")

    for path in sorted(reference_paths):
        block = path_blocks.get(path, "")
        if block and not re.search(r"(?m)^\s*role\s*:\s*reference\s*$", block):
            warnings.append(f"reference 文件计划 `{path}` 的 role 已降级为 component_hint；file_kind/path 决定生成方式。")

    for path in sorted(asset_paths):
        block = path_blocks.get(path, "")
        source = _normalized_asset_source_from_block(block)
        if source not in {"user_upload", "bundled"}:
            issues.append(
                f"asset 文件计划 `{path}` 必须显式声明 source=user_upload 或 source=bundled；"
                "assets 只能是用户上传或预置静态资源。"
            )
        if _asset_block_mentions_runtime_artifact(block) or is_runtime_artifact_semantic(path, ""):
            issues.append(
                f"运行时产物 `{path}` 不能列入 assets/ 或 Creator 文件计划；"
                "如为运行时生成文件，应放到脚本 outputs/stdout JSON；如需上传请声明 source=user_upload，内置静态资源声明 source=bundled。"
            )

    if script_paths and "```bash" not in text and "需要脚本/命令" not in text:
        issues.append("需要脚本时，蓝图必须包含宿主执行方式说明，要求最终 SKILL.md 使用标准 ```bash fenced code block。")

    if issues:
        raise BlueprintShapeError("Creator 蓝图格式不符合平台硬协议：\n" + "\n".join(f"- {issue}" for issue in issues))


# Valid extensions per directory
_SCRIPT_EXTENSIONS: frozenset[str] = frozenset(
    {".py", ".js", ".ts", ".sh", ".bash", ".rb", ".mjs", ".cjs"}
)

_PLACEHOLDER_SCRIPT_STEMS: frozenset[str] = frozenset({
    "foo",
    "bar",
    "baz",
    "demo",
    "example",
    "sample",
    "test",
    "tmp",
    "temp",
    "placeholder",
})


def _script_path_has_concrete_contract(path: str, blueprint_text: str, purpose: str = "") -> bool:
    """Return True if a suspicious script path has enough local evidence.

    We only allow placeholder-like names when the blueprint explicitly gives a
    concrete contract near that path, for example role/inputs/outputs/capabilities
    or a real workflow command using it.  This avoids prompt-leaked examples such
    as `scripts/foo.py` entering the generation queue.
    """
    text = blueprint_text or ""
    # Strong evidence: the path appears near explicit contract fields.
    for occurrence in re.finditer(re.escape(path), text):
        start = max(0, occurrence.start() - 500)
        end = min(len(text), occurrence.end() + 900)
        nearby = text[start:end]
        if re.search(
            r"\b(role|inputs|outputs|dependencies|required_capabilities|optional_capabilities|allowed_capabilities|forbidden_capabilities)\b\s*[：:=]",
            nearby,
            re.IGNORECASE,
        ):
            return True
    # A real command line with JSON argv is concrete evidence.
    if re.search(
        rf"(?:python|python3|node|bash|sh)\s+{re.escape(path)}\s+['\"]?\{{",
        text,
        re.IGNORECASE,
    ):
        return True

    return False


def _is_probable_prompt_leaked_script(path: str, *, purpose: str = "", blueprint_text: str = "") -> bool:
    """Detect placeholder scripts likely leaked from prompt examples.

    This is intentionally conservative:
    - Only applies to scripts/*
    - Only applies to generic placeholder-like file names
    - Does not block the file if the blueprint gives a concrete local contract
    """
    normalized = path.strip().replace("\\", "/")
    if not normalized.startswith("scripts/"):
        return False

    file_name = Path(normalized).name
    stem = Path(file_name).stem.lower()
    suffix = Path(file_name).suffix.lower()

    if suffix not in _SCRIPT_EXTENSIONS:
        return False

    if stem not in _PLACEHOLDER_SCRIPT_STEMS:
        return False

    return not _script_path_has_concrete_contract(
        normalized,
        blueprint_text=blueprint_text,
        purpose=purpose,
    )

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _should_skip(text: str) -> bool:
    """Return True when the section description says no files are needed."""
    stripped = text.strip().lower()
    # Exact "无" or starts with any skip phrase
    if stripped == "无":
        return True
    return any(stripped.startswith(phrase.lower()) for phrase in _SKIP_PHRASES)


def _contains_path_wildcard(path: str) -> bool:
    return any(ch in path for ch in "*?[]{}")


def _extract_inline_paths(text: str, prefix: str) -> list[str]:
    """Return all backtick-wrapped paths matching `prefix/...` found in text."""
    found: list[str] = []
    for m in _SKILL_PATH_INLINE_RE.finditer(text):
        p = m.group(1).strip()
        if p.startswith(prefix + "/") and p not in found:
            found.append(p)
    return found


# ---------------------------------------------------------------------------
# Public parsing functions
# ---------------------------------------------------------------------------


def extract_blueprint_text(messages: list[dict]) -> str | None:
    """Return the last assistant message containing the blueprint marker, or None."""
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            content = msg.get("content") or ""
            if _BLUEPRINT_MARKER in content:
                return clean_blueprint_body_text(content)
    return None


def parse_skill_name(blueprint_text: str) -> str | None:
    """Extract and normalise the skill name from the blueprint.

    Returns a kebab-case identifier (lowercase letters, digits, hyphens),
    or None if the name cannot be reliably determined.
    """
    m = _SKILL_NAME_RE.search(blueprint_text)
    if not m:
        return None

    raw = m.group(1).strip()
    # Take only the first token (guard against trailing comments/parens)
    raw = raw.split()[0] if raw else ""

    # Normalise: lowercase, replace spaces/underscores with hyphens, keep alnum+hyphen
    normalised = re.sub(
        r"[^a-z0-9-]",
        "",
        raw.lower().replace(" ", "-").replace("_", "-"),
    )
    normalised = re.sub(r"-{2,}", "-", normalised).strip("-")

    if not normalised or len(normalised) > _MAX_SKILL_NAME_LENGTH:
        return None

    return normalised


def parse_files_from_blueprint(blueprint_text: str) -> tuple[list[FileSpec], list[str]]:
    """Extract the list of files to generate from the blueprint body.

    Returns (files, warnings).  All paths are relative to the skill root.

    Important guard:
    placeholder-like scripts such as scripts/foo.py are ignored unless the
    confirmed blueprint gives them a concrete local contract.  This prevents
    prompt-leaked example paths from entering the Creator generation queue.
    """
    files: list[FileSpec] = []
    warnings: list[str] = []
    seen: set[str] = set()

    def _add(
        path: str,
        purpose: str,
        *,
        required: bool = True,
        can_skip: bool = False,
        asset_source: str = "",
    ) -> None:
        path = path.strip().replace("\\", "/").strip("`'\"，,。.;；")
        if not path:
            return

        if _contains_path_wildcard(path):
            warning = f"忽略通配符文件路径 {path}；Creator 只能逐个生成具体文件，请在蓝图中展开为具体文件名。"
            if warning not in warnings:
                warnings.append(warning)
            return

        if _is_probable_prompt_leaked_script(path, purpose=purpose, blueprint_text=blueprint_text):
            warning = (
                f"已忽略疑似提示词示例/泄露脚本 {path}；"
                "该文件名像占位示例，且蓝图附近没有明确 role/inputs/outputs/capabilities。"
                "如果确实需要该脚本，请在蓝图中明确它的职责、输入、输出和能力后重新添加。"
            )
            if warning not in warnings:
                warnings.append(warning)
            return

        if not (path.startswith("assets/") and asset_source in {"user_upload", "bundled"}) and is_runtime_artifact_semantic(path, purpose):
            warning = f"已忽略运行时产物文件计划项 {path}；脚本生成的结果只能声明在 outputs/stdout metadata 中，不能作为 Creator 待创建文件。"
            if warning not in warnings:
                warnings.append(warning)
            return

        if path in seen:
            return

        seen.add(path)
        files.append(
            FileSpec(path=path, purpose=purpose, required=required, can_skip=can_skip, asset_source=asset_source)
        )

    # 1. SkillPlan / 文件职责计划 is the primary source of file contracts.
    # Directory tree paths are only cross-check/display hints and must not
    # overwrite explicit role/capability/input/output contracts.
    path_blocks = _extract_skillplan_blocks(blueprint_text)
    ordered_paths = sorted(
        path_blocks,
        key=lambda p: (
            0 if p == "SKILL.md" else 1 if p.startswith("scripts/") else 2 if p.startswith("references/") else 3,
            p,
        ),
    )
    for path in ordered_paths:
        source = _normalized_asset_source_from_block(path_blocks[path]) if path.startswith("assets/") else ""
        _add(
            path,
            path_blocks[path],
            required=not path.startswith(("references/", "assets/")) or source == "user_upload",
            can_skip=path.startswith(("references/", "assets/")) and source != "user_upload",
            asset_source=source,
        )

    # 1b. SKILL.md is always required for non-strict/legacy blueprints that do
    # not yet contain an explicit SkillPlan file item.
    _add("SKILL.md", "Skill 核心说明文件，包含 YAML frontmatter 和执行规范")

    # ------------------------------------------------------------------
    # 2. scripts/ section
    # ------------------------------------------------------------------
    scripts_desc = ""
    m_scripts = _SECTION_SCRIPTS_RE.search(blueprint_text)
    if m_scripts:
        scripts_desc = m_scripts.group(1).strip()

    # 3. Entry-point line, higher priority than section scan
    m_entry = _ENTRY_SCRIPT_RE.search(blueprint_text)
    if m_entry:
        raw_entry = m_entry.group(2).strip().split()[0]
        raw_entry = raw_entry.strip("`'\"，,。.;；")
        if raw_entry and not _should_skip(raw_entry):
            if not raw_entry.startswith("scripts/"):
                raw_entry = "scripts/" + Path(raw_entry).name
            _add(raw_entry, scripts_desc or "Skill 主执行脚本")

    # 4. Inline backtick paths anywhere in the blueprint
    for path in _extract_inline_paths(blueprint_text, "scripts"):
        _add(path, scripts_desc or "Skill 执行脚本")

    # 4b. Tree structure paths
    for m_tree in _TREE_FILE_RE.finditer(blueprint_text):
        tree_path = m_tree.group(1).strip().strip("`'\"，,。.;；")
        for prefix in ("scripts/", "references/", "assets/"):
            idx = tree_path.find(prefix)
            if idx >= 0:
                tree_path = tree_path[idx:]
                break
        if tree_path.startswith("scripts/"):
            _add(tree_path, scripts_desc or "Skill 执行脚本（从目录结构提取）")

    # 5. Bare paths inside scripts section description
    if scripts_desc and not _should_skip(scripts_desc):
        for m_bare in re.finditer(r"scripts/(\S+\.\w+)", scripts_desc):
            _add("scripts/" + m_bare.group(1), scripts_desc)

    # 6. Infer default when section says files are needed but none identified
    if scripts_desc and not _should_skip(scripts_desc):
        has_script = any(f.path.startswith("scripts/") for f in files)
        if not has_script:
            default = "scripts/main.py"
            m_cmd = _RUN_COMMAND_RE.search(blueprint_text)
            if m_cmd:
                for token in m_cmd.group(1).split():
                    token = token.strip("`'\"，,。.;；").lstrip("./")
                    if token.startswith("scripts/") and Path(token).suffix in _SCRIPT_EXTENSIONS:
                        if not _is_probable_prompt_leaked_script(
                            token,
                            purpose=scripts_desc,
                            blueprint_text=blueprint_text,
                        ):
                            default = token
                            break
            _add(default, scripts_desc or "Skill 主执行脚本")
            warnings.append(
                f"脚本文件名未在蓝图中明确指定，已默认为 {default}，请在面板中确认或修改。"
            )

    # 7. Run-command line as additional source for script paths
    m_cmd = _RUN_COMMAND_RE.search(blueprint_text)
    if m_cmd:
        cmd_desc = scripts_desc or "Skill 主执行脚本（从运行命令推断）"
        for token in m_cmd.group(1).split():
            token = token.strip("`'\"，,。.;；").lstrip("./")
            if token.startswith("scripts/") and Path(token).suffix in _SCRIPT_EXTENSIONS:
                _add(token, cmd_desc)

    # ------------------------------------------------------------------
    # 8. references/ section
    # ------------------------------------------------------------------
    m_refs = _SECTION_REFERENCES_RE.search(blueprint_text)
    refs_desc = m_refs.group(1).strip() if m_refs else ""
    if refs_desc and not _should_skip(refs_desc):
        ref_files = _extract_inline_paths(blueprint_text, "references")
        for path in ref_files:
            _add(path, refs_desc, required=False, can_skip=True)

        for m_tree in _TREE_FILE_RE.finditer(blueprint_text):
            tree_path = m_tree.group(1).strip().strip("`'\"，,。.;；")
            for prefix in ("scripts/", "references/", "assets/"):
                idx = tree_path.find(prefix)
                if idx >= 0:
                    tree_path = tree_path[idx:]
                    break
            if tree_path.startswith("references/"):
                _add(tree_path, refs_desc + "（从目录结构提取）", required=False, can_skip=True)

        for m_bare in re.finditer(r"references/(\S+\.\w+)", refs_desc):
            _add("references/" + m_bare.group(1), refs_desc, required=False, can_skip=True)

        if not any(f.path.startswith("references/") for f in files):
            default_ref = "references/guide.md"
            _add(default_ref, refs_desc, required=False, can_skip=True)
            warnings.append(
                f"参考资料文件名未在蓝图中明确指定，已默认为 {default_ref}，请在面板中确认或修改。"
            )

    # ------------------------------------------------------------------
    # 9. assets/ section
    # ------------------------------------------------------------------
    m_assets = _SECTION_ASSETS_RE.search(blueprint_text)
    assets_desc = m_assets.group(1).strip() if m_assets else ""
    if assets_desc and not _should_skip(assets_desc):
        for path in _extract_inline_paths(blueprint_text, "assets"):
            _add(path, assets_desc, required=False, can_skip=True, asset_source="bundled")
        for m_bare in re.finditer(r"assets/(\S+\.\w+)", assets_desc):
            _add("assets/" + m_bare.group(1), assets_desc, required=False, can_skip=True, asset_source="bundled")

    generation_order = {"references": 0, "scripts": 1, "bundled_assets": 2, "user_upload_assets": 3, "skill_md": 4}

    def _generation_sort_key(file: FileSpec) -> tuple[int, str]:
        path = file.path.replace("\\", "/")
        if path.startswith("references/"):
            bucket = "references"
        elif path.startswith("scripts/"):
            bucket = "scripts"
        elif path.startswith("assets/") and file.asset_source == "user_upload":
            bucket = "user_upload_assets"
        elif path.startswith("assets/"):
            bucket = "bundled_assets"
        elif path == "SKILL.md":
            bucket = "skill_md"
        else:
            bucket = "bundled_assets"
        return (generation_order[bucket], path)

    files.sort(key=_generation_sort_key)
    return files, warnings


def build_skill_plan_from_files(
    *,
    skill_name: str,
    files: list[FileSpec],
    warnings: list[str] | None = None,
    blueprint_text: str = "",
) -> SkillPlan:
    """Build the role/contract plan used by Creator generation and validation.

    This is a second guard against prompt-leaked placeholder scripts.  If a
    placeholder-like script still has no concrete contract after role resolution,
    remove it from the plan instead of merely marking it low confidence.
    """
    reference_files = [file.path for file in files if file.path.startswith("references/")]
    entries: list[SkillPlanEntry] = []
    plan_warnings = list(warnings or [])

    for dep_match in re.finditer(r"dependencies\s*[：:=]\s*\[?([^\]\n;]+)\]?", blueprint_text or "", re.I):
        for raw_dep in re.split(r"[,，、]\s*", dep_match.group(1)):
            dep = raw_dep.strip().strip("'\"")
            if dependency_is_output_semantic(dep):
                warning = f"已从 dependencies 移除输出/动态路径 {dep}；dependencies 只能表示输入依赖。"
                if warning not in plan_warnings:
                    plan_warnings.append(warning)

    for file in files:
        if _is_probable_prompt_leaked_script(
            file.path,
            purpose=file.purpose,
            blueprint_text=blueprint_text,
        ):
            plan_warnings.append(
                f"已从 SkillPlan 中移除疑似提示词示例/泄露脚本 {file.path}；"
                "如确实需要，请明确 role/inputs/outputs/capabilities 后手动添加。"
            )
            continue

        for warning in skill_plan_field_declaration_warnings(
            file_path=file.path,
            purpose=file.purpose,
            blueprint_summary=blueprint_text,
        ):
            if warning not in plan_warnings:
                plan_warnings.append(warning)

        refs_for_file = reference_files if file.path == "SKILL.md" or file.path.startswith("scripts/") else []
        entry = build_skill_plan_entry(
            file_path=file.path,
            purpose=file.purpose,
            required=file.required,
            can_skip=file.can_skip,
            blueprint_summary=blueprint_text,
            reference_files=refs_for_file,
        )

        # If role classification still says low-confidence generic_script and the
        # filename is placeholder-like, remove it instead of entering repair loops.
        if (
            file.path.startswith("scripts/")
            and entry.role == "generic_script"
            and entry.confidence < 0.7
            and Path(file.path).stem.lower() in _PLACEHOLDER_SCRIPT_STEMS
        ):
            plan_warnings.append(
                f"已从 SkillPlan 中移除 {file.path}：该脚本为低置信 generic_script，"
                "且文件名像示例占位符。请先确认职责、输入、输出和能力后再添加。"
            )
            continue

        if entry.raw_capability_hints:
            warning = f"{file.path} required_capabilities 已降级为 hint，不参与 hard validation、tool_slot 推断或脚本合同。"
            if warning not in plan_warnings:
                plan_warnings.append(warning)

        if entry.raw_capability_hints:
            plan_warnings.append(
                f"{file.path} required_capabilities 已作为 raw_capability_hints 保留；不会参与 hard validation、tool slot 推断或脚本生成合同。"
            )

        entries.append(entry)

        if file.path.startswith("scripts/") and entry.confidence < 0.7:
            plan_warnings.append(
                f"{file.path} 未声明明确 role，已使用保守 generic_script；"
                "不会自动启用图片生成/PDF 生成等高影响能力。"
            )

    normalized = normalize_skill_plan(SkillPlan(skill_name=skill_name, files=entries, warnings=plan_warnings))
    semantic_issues = validate_file_plan_semantics(normalized)
    # SkillPlan static I/O consumption is an internal workflow dataflow hint, not
    # a blueprint-stage user-visible warning.  First-round Creator validation
    # only reports platform/file-boundary issues; real script-to-script field
    # availability is checked by second-round E2E execution.
    return SkillPlan(skill_name=normalized.skill_name, files=normalized.files, warnings=[*normalized.warnings, *semantic_issues])


def parse_blueprint(messages: list[dict], *, strict: bool = False) -> BlueprintPlan:
    """Parse a Skill blueprint from the conversation message history.

    Returns a BlueprintPlan with a best-effort file list and any warnings.
    If no blueprint is found, returns a minimal plan containing only SKILL.md.
    """
    blueprint_text = extract_blueprint_text(messages)
    if not blueprint_text:
        if strict:
            combined = "\n\n".join(str(message.get("content") or "") for message in messages if isinstance(message, dict))
            if "✅ Skill 架构蓝图" in combined or "Skill 架构蓝图" in combined:
                raise BlueprintShapeError("蓝图 marker 必须是固定标题 `## 📋 Skill 架构蓝图`，不能替换为 ✅ 或其它标题。")
            raise BlueprintShapeError("未找到固定蓝图标题 `## 📋 Skill 架构蓝图`，确认创建阶段不能降级为最小 SKILL.md。")
        files = [FileSpec(path="SKILL.md", purpose="Skill 核心说明文件", required=True)]
        warnings = ["未在对话历史中找到蓝图，将创建最小 Skill 包（仅 SKILL.md）。"]
        return BlueprintPlan(
            skill_name="new-skill",
            files=files,
            warnings=warnings,
            skill_plan=build_skill_plan_from_files(
                skill_name="new-skill", files=files, warnings=warnings, blueprint_text=""
            ),
        )

    warnings: list[str] = []
    if strict:
        blueprint_text, repair_warnings = repair_blueprint_business_layers(blueprint_text)
        warnings.extend(repair_warnings)
        validate_blueprint_shape_for_creator(blueprint_text)
    else:
        blueprint_text = clean_blueprint_body_text(blueprint_text)

    skill_name = parse_skill_name(blueprint_text)
    if skill_name is None:
        skill_name = "new-skill"
        warnings.append(
            "未能从蓝图中解析出合法 Skill 名称，已默认为 'new-skill'，请在面板中修改。"
        )

    files, file_warnings = parse_files_from_blueprint(blueprint_text)
    warnings.extend(file_warnings)

    skill_plan = build_skill_plan_from_files(
        skill_name=skill_name, files=files, warnings=warnings, blueprint_text=blueprint_text
    )
    return BlueprintPlan(
        skill_name=skill_name,
        files=files,
        warnings=skill_plan.warnings,
        skill_plan=skill_plan,
    )
