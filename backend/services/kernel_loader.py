from dataclasses import dataclass, field
from pathlib import Path
import re

import yaml

from ..config import settings
from .skill_manager import get_visible_skill_dir


@dataclass
class SkillResource:
    """A bundled resource inside a Skill package."""
    path: str
    kind: str
    title: str = ""

@dataclass
class ChildSkill:
    """A child Skill discovered under the current Skill package.

    只保存子 Skill 元数据，不保存正文。
    """
    ref: str
    name: str
    description: str
    root: Path

@dataclass
class SkillPackage:
    """A selected Skill package.

    分层说明：
    - metadata 阶段：只读取 frontmatter，不读取 SKILL.md 正文。
    - body 阶段：读取完整 SKILL.md。
    - child skill 阶段：先暴露子 Skill metadata，再按需加载子 Skill 正文。
    - resource 阶段：只在运行时明确需要时读取 references/assets/scripts。
    """
    name: str
    description: str
    root: Path
    skill_md_path: Path
    skill_md_text: str = ""
    references: list[SkillResource] = field(default_factory=list)
    assets: list[SkillResource] = field(default_factory=list)
    scripts: list[SkillResource] = field(default_factory=list)
    child_skills: list[ChildSkill] = field(default_factory=list)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

_ALLOWED_RESOURCE_DIRS = {"references", "assets", "scripts"}
_ALLOWED_READ_SUFFIXES = {
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".csv",
    ".jinja",
    ".jinja2",
    ".template",
    ".tmpl",
    ".py",
}


def _parse_simple_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from a SKILL.md string.

    Uses PyYAML for correct multi-line and nested value handling.  Falls back
    to an empty metadata dict on any parse error so that a malformed frontmatter
    never prevents loading the body.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text

    meta_text = match.group(1)
    body = match.group(2)

    try:
        meta = yaml.safe_load(meta_text)
        if not isinstance(meta, dict):
            meta = {}
    except yaml.YAMLError:
        meta = {}

    return meta, body


def _read_frontmatter_only(skill_md_path: Path) -> tuple[dict[str, str], str]:
    """Read only frontmatter from SKILL.md.

    不读取完整正文，避免 metadata 阶段把完整 SKILL.md 加载进模型。
    """
    if not skill_md_path.exists():
        raise FileNotFoundError(f"SKILL.md not found at {skill_md_path}")

    lines: list[str] = []
    delimiter_count = 0

    with skill_md_path.open("r", encoding="utf-8", errors="replace") as file:
        for line in file:
            lines.append(line)
            if line.strip() == "---":
                delimiter_count += 1
                if delimiter_count >= 2:
                    break

    frontmatter_text = "".join(lines)

    if delimiter_count < 2:
        preview = skill_md_path.read_text(encoding="utf-8", errors="replace")[:10000]
        return _parse_simple_frontmatter(preview)

    return _parse_simple_frontmatter(frontmatter_text)


def _extract_link_titles(skill_md_text: str) -> dict[str, str]:
    """Extract Markdown link titles from SKILL.md body."""
    result: dict[str, str] = {}

    for title, path in _MD_LINK_RE.findall(skill_md_text):
        normalized = path.strip().lstrip("./")
        result[normalized] = title.strip()

    return result


def _scan_resource_dir(
    root: Path,
    dirname: str,
    kind: str,
    link_titles: dict[str, str] | None = None,
) -> list[SkillResource]:
    """Scan bundled resources under a Skill directory.

    这里只扫描清单，不读取资源正文。
    """
    link_titles = link_titles or {}
    directory = root / dirname

    if not directory.is_dir():
        return []

    resources: list[SkillResource] = []

    try:
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue

            rel_path = path.relative_to(root).as_posix()
            title = link_titles.get(rel_path, "")
            resources.append(SkillResource(path=rel_path, kind=kind, title=title))
    except OSError:
        return []

    return resources

def _is_skill_dir(path: Path) -> bool:
    """Return whether a directory looks like a Skill package."""
    return path.is_dir() and (path / "SKILL.md").is_file()


def _scan_child_skills(root: Path) -> list[ChildSkill]:
    """Scan child Skill packages without reading their bodies.

    支持两类布局：
    1. 当前 Skill 下的 skills/<child>/SKILL.md
    2. 当前 Skill 下的 references/<child>/SKILL.md

    注意：
    - 这里只读取子 Skill 的 frontmatter。
    - 不读取子 Skill 的 SKILL.md 正文。
    """
    candidates: list[Path] = []

    for dirname in ("skills", "references"):
        base = root / dirname
        if not base.is_dir():
            continue

        try:
            for path in sorted(base.rglob("SKILL.md")):
                parent = path.parent
                if _is_skill_dir(parent):
                    candidates.append(parent)
        except OSError:
            continue

    child_skills: list[ChildSkill] = []
    seen: set[str] = set()

    for child_root in candidates:
        try:
            rel = child_root.relative_to(root).as_posix()
        except ValueError:
            continue

        if rel in seen:
            continue
        seen.add(rel)

        try:
            meta, _body = _read_frontmatter_only(child_root / "SKILL.md")
        except Exception:
            continue

        name = meta.get("name") or child_root.name
        description = meta.get("description") or ""

        child_skills.append(
            ChildSkill(
                ref=rel,
                name=name,
                description=description,
                root=child_root,
            )
        )

    return child_skills

def _load_skill_from_root(root: Path, *, include_body: bool = False) -> SkillPackage:
    """Load a selected Skill package.

    include_body=False:
        只读取当前 Skill frontmatter + 扫描资源清单 + 扫描子 Skill metadata。

    include_body=True:
        读取当前 Skill 完整 SKILL.md 正文 + 扫描资源清单 + 扫描子 Skill metadata。

    注意：
    子 Skill 这里只读取 frontmatter，不读取正文。
    """
    skill_md = root / "SKILL.md"

    if not skill_md.exists():
        raise FileNotFoundError(f"SKILL.md not found at {skill_md}")

    if include_body:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
        meta, _body = _parse_simple_frontmatter(text)
        link_titles = _extract_link_titles(text)
    else:
        meta, _body = _read_frontmatter_only(skill_md)
        text = ""
        link_titles = {}

    name = meta.get("name") or root.name
    description = meta.get("description") or ""

    return SkillPackage(
        name=name,
        description=description,
        root=root,
        skill_md_path=skill_md,
        skill_md_text=text,
        references=_scan_resource_dir(root, "references", "reference", link_titles),
        assets=_scan_resource_dir(root, "assets", "asset", link_titles),
        scripts=_scan_resource_dir(root, "scripts", "script", link_titles),
        child_skills=_scan_child_skills(root),
    )


def _format_resource_list(resources: list[SkillResource]) -> str:
    if not resources:
        return "- 无"

    lines: list[str] = []

    for resource in resources:
        if resource.title:
            lines.append(f"- `{resource.path}`：{resource.title}")
        else:
            lines.append(f"- `{resource.path}`")

    return "\n".join(lines)


def _compose_resource_manifest(skill: SkillPackage) -> str:
    """Build a resource manifest without reading resource bodies."""
    return (
        "## Bundled Resources Manifest\n\n"
        "以下只是当前 Skill 目录下的资源清单，不代表已经读取资源正文。\n"
        "references/assets/scripts 的具体内容只能在运行时确实需要时再读取或执行。\n"
        "如果 references 中包含子 Skill 索引，也只能先作为索引使用；不要默认加载所有子 Skill 正文。\n\n"
        "### references/\n"
        f"{_format_resource_list(skill.references)}\n\n"
        "### assets/\n"
        f"{_format_resource_list(skill.assets)}\n\n"
        "### scripts/\n"
        f"{_format_resource_list(skill.scripts)}"
    )

def _format_child_skill_list(child_skills: list[ChildSkill]) -> str:
    if not child_skills:
        return "- 无"

    lines: list[str] = []

    for child in child_skills:
        description = child.description.strip() or "无 description"
        lines.append(
            f"- ref: `{child.ref}`\n"
            f"  - name: `{child.name}`\n"
            f"  - description: {description}"
        )

    return "\n".join(lines)


def _compose_child_skill_manifest(skill: SkillPackage) -> str:
    """Build child Skill manifest without reading child bodies."""
    return (
        "## Child Skills Manifest\n\n"
        "以下是当前 Skill 下发现的子 Skill 清单。\n"
        "这里只提供子 Skill 的 frontmatter 元数据，不代表已经加载子 Skill 的 SKILL.md 正文。\n"
        "只有当用户请求确实匹配某个子 Skill 时，宿主才会按需加载该子 Skill 正文。\n\n"
        f"{_format_child_skill_list(skill.child_skills)}"
    )

def compose_metadata_prompt(skill: SkillPackage) -> str:
    """Compose metadata-only prompt for the first silent model round.

    第一阶段只用于判断该 Skill 是否需要进入正文执行阶段。
    同时暴露子 Skill metadata，但不读取子 Skill 正文。
    """
    return (
        "你处于 Skill 加载流程的第一阶段：metadata 判断阶段。\n\n"
        "当前只提供 Skill 的 frontmatter 元数据、资源清单和子 Skill 元数据，尚未加载当前 Skill 的 SKILL.md 正文，"
        "也尚未加载任何子 Skill 的 SKILL.md 正文。\n"
        "你需要根据用户请求判断是否应该加载当前 Skill 的完整 SKILL.md 正文。\n\n"
        "请只输出严格 JSON，不要输出解释文本，不要使用 Markdown。\n\n"
        "输出格式：\n"
        '{"need_body": true, "reason": "简短原因"}\n\n'
        "判断规则：\n"
        "- 如果用户请求符合当前 Skill description 中描述的使用场景，need_body=true。\n"
        "- 如果用户请求符合某个子 Skill 的 description，也应该 need_body=true，因为需要进入父 Skill 正文后再按需加载子 Skill。\n"
        "- 如果当前路由已经明确选中了该 Skill，通常 need_body=true。\n"
        "- 如果用户请求明显与当前 Skill 及其子 Skill 都无关，need_body=false。\n"
        "- metadata 阶段不要假装知道 SKILL.md 正文中的细节。\n\n"
        "## Skill Metadata\n"
        f"- name: {skill.name}\n"
        f"- description: {skill.description}\n\n"
        "---\n\n"
        f"{_compose_child_skill_manifest(skill)}\n\n"
        "---\n\n"
        f"{_compose_resource_manifest(skill)}"
    )


def _compose_agent_runtime_contract() -> str:
    """Host runtime guidance injected into body stage."""
    return (
        "## Host Agent Runtime Guidance\n\n"
        "你运行在一个分层 Agent 宿主中，当前不是普通自由问答模式，而是 Skill 执行模式。\n\n"
        "核心原则：\n"
        "1. Loaded SKILL.md 是当前任务的最高执行规范，而不是普通参考资料。\n"
        "2. SKILL.md 可以保持标准 Markdown 写法，不需要包含自定义 action JSON 或 `<skill_action>` 标签。\n"
        "3. SKILL.md 中的 fenced code block 默认只是文档内容、代码内容、示例命令或使用方式，"
        "具体是否执行由宿主 runtime planner 根据语义判断。\n"
        "4. 如果 Loaded SKILL.md 语义上要求外部动作，例如运行脚本、执行命令、读取资源、写文件、创建目录、运行测试或调用工具，"
        "宿主会优先通过结构化 action plan 执行，而不是依赖你在最终回答里自由发挥。\n"
        "5. 你不得假装已经执行脚本、读取资源或生成文件；只有 executor observation 中出现的结果才是真实执行结果。\n\n"
        "动作与回答规则：\n"
        "1. 如果宿主已经执行了 action，你必须基于 observation 生成最终回答。\n"
        "2. 如果宿主没有执行 action，而 SKILL.md 又要求外部动作，你不得编造业务结果。\n"
        "3. 如果 SKILL.md 明确要求直接生成文本结果，或者 runtime planner 判定不需要外部动作，才可以直接回答。\n"
        "4. 如果缺少必要参数，应说明缺少哪些信息，不得保留占位符继续执行。\n\n"
        "资源限制：\n"
        "1. 不要假装已经读取 references/assets/scripts 的正文；这些资源只有在宿主明确读取或执行后才可使用。\n"
        "2. 不要假装已经读取子 Skill 的正文；子 Skill 正文只有在宿主明确加载后才可使用。\n"
        "3. 不要输出自定义 `<skill_action>` 标签。\n"
    )


def _compose_creator_workflow_contract() -> str:
    """Creator workflow contract injected into the kernel creator body prompt."""
    return (
        "## Creator Workflow Contract\n\n"
        "你正在执行 Skill Creator 的多阶段工作流。\n\n"
        "相位自我评估：\n"
        "1. 在每次回复前，先通读对话历史和已加载的 SKILL.md。\n"
        "2. 判断当前最符合哪一个 Phase，并在你的思考中标注当前 Phase。\n"
        "3. 必须以 SKILL.md 中的 Phase 定义与完成标志为准，不得自行跳跃。\n\n"
        "阶段门控：\n"
        "1. 在推进到下一阶段前，必须确认当前阶段的完成标志已满足。\n"
        "2. 如果未满足，继续提问或总结确认，不得提前输出下一阶段内容。\n\n"
        "Phase 1-2 行为约束：\n"
        "1. 只进行需求澄清与蓝图确认，不得输出任何文件写入或命令执行格式。\n"
        "2. 需要更好的问法时，可在资源选择阶段请求加载 `references/interaction-guide.md`。\n\n"
        "Phase 3-5 行为约束：\n"
        "1. 只有在 Phase 2 完成并获得用户确认后，才能进入 Phase 3。\n"
        "2. 进入 Phase 3 后，才可以按 SKILL.md 规定输出“写入文件/执行命令”的动作格式。\n"
        "3. 如果需要命名规范或输出格式示例，可在资源选择阶段请求加载 "
        "`references/best-practices.md` 与 `references/output-patterns.md`。\n"
        "4. 如果需要多步骤流程设计参考，可在资源选择阶段请求加载 "
        "`references/workflows.md`。\n\n"
        "蓝图确认信号：\n"
        "Phase 2 输出完整蓝图与确认问题；用户确认后由后端进入 Phase 3。\n"
        "Phase 1-2 期间禁止输出 phase3_start JSON 标记。\n\n"
        "运行时安全：\n"
        "1. 不要假装已经读取 references/assets/scripts 的正文；这些资源只有在宿主明确加载后才可使用。\n"
        "2. 不要假装已经读取子 Skill 的正文；子 Skill 正文只有在宿主明确加载后才可使用。\n"
        "3. 不要输出自定义 `<skill_action>` 标签。\n"
    )


def _compose_creator_workflow_contract_for_phase(phase: str) -> str:
    """Create phase-specific creator workflow contract.
    
    Progressive disclosure: only show constraints relevant to current phase.
    """
    base_contract = (
        "## Creator Workflow Contract\n\n"
        "你正在执行 Skill Creator 的多阶段工作流。\n\n"
        "相位自我评估：\n"
        "1. 在每次回复前，先通读对话历史和已加载的 SKILL.md。\n"
        "2. 判断当前最符合哪一个 Phase，并在你的思考中标注当前 Phase。\n"
        "3. 必须以 SKILL.md 中的 Phase 定义与完成标志为准，不得自行跳跃。\n\n"
        "阶段门控：\n"
        "1. 在推进到下一阶段前，必须确认当前阶段的完成标志已满足。\n"
        "2. 如果未满足，继续提问或总结确认，不得提前输出下一阶段内容。\n\n"
    )
    
    if phase == "first_time" or phase == "phase1":
        # Phase 1: only show Phase 1 constraints
        return base_contract + (
            "当前阶段：Phase 1（深度需求挖掘）\n\n"
            "==================== Phase 1 执行要求 ====================\n"
            "【重要！Phase 1 必须严格按顺序执行！】\n\n"
            "执行顺序：\n"
            "1. 必须严格按照 1.1 → 1.2 → 1.3 → 1.4 → 1.5 的顺序执行\n"
            "2. 每个步骤只问一个问题，不要在一次回复中问多个问题\n"
            "3. 只有当前步骤获得用户回复后，才能进入下一个步骤\n"
            "4. 不要跳过任何步骤！\n\n"
            "【重要！AskUserQuestion 输出格式要求！】\n\n"
            "必须使用以下格式输出问题，用 ```text 包裹！\n"
            "格式示例：\n"
            "```text\n"
            "问题: \"你希望 智能助手 帮你做什么事情？\"\n"
            "选项:\n"
            "- \"处理文件 (比如 PDF、Excel、图片等)\"\n"
            "- \"帮我写东西 (比如文档、代码、报告)\"\n"
            "- \"连接某个服务 (比如发消息、查数据)\"\n"
            "- \"其他 (我来描述)\"\n"
            "```\n\n"
            "注意事项：\n"
            "1. 必须用 ```text 和 ``` 包裹整个问题块\n"
            "2. 必须使用 \"问题:\" 开头\n"
            "3. 必须使用 \"选项:\" 开头列出选项\n"
            "4. 每个选项用 - 开头\n"
            "5. 问题和选项都要用双引号包裹\n"
            "6. 每次只输出一个 AskUserQuestion 块\n\n"
            "问题格式要求：\n"
            "1. 必须使用 SKILL.md 中给出的 ``` 包裹的问题模板\n"
            "2. 不要自己编问题，直接用 SKILL.md 里的问题\n"
            "3. 每次只输出一个 AskUserQuestion\n\n"
            "Phase 1 行为约束：\n"
            "1. 只进行需求澄清，不得输出任何文件写入或命令执行格式。\n"
            "2. 需要更好的问法时，可在资源选择阶段请求加载 `references/interaction-guide.md`。\n\n"
            "运行时安全：\n"
            "1. 不要假装已经读取 references/assets/scripts 的正文；这些资源只有在宿主明确加载后才可使用。\n"
            "2. 不要假装已经读取子 Skill 的正文；子 Skill 正文只有在宿主明确加载后才可使用。\n"
            "3. 不要输出自定义 `<skill_action>` 标签。\n"
        )
    elif phase == "phase2":
        # Phase 2: show Phase 1-2 constraints
        return base_contract + (
            "当前阶段：Phase 2（技能架构蓝图）\n\n"
            "Phase 1-2 行为约束：\n"
            "1. 只进行需求澄清与蓝图确认，不得输出任何文件写入或命令执行格式。\n"
            "2. 需要更好的问法时，可在资源选择阶段请求加载 `references/interaction-guide.md`。\n\n"
            "蓝图确认规则：\n"
            "1. Phase 2 的输出必须先展示完整蓝图，再询问用户确认。\n"
            "2. Phase 2 期间禁止输出 phase3_start JSON 标记；必须等用户在下一轮确认后由后端进入 Phase 3。\n\n"
            "运行时安全：\n"
            "1. 不要假装已经读取 references/assets/scripts 的正文；这些资源只有在宿主明确加载后才可使用。\n"
            "2. 不要假装已经读取子 Skill 的正文；子 Skill 正文只有在宿主明确加载后才可使用。\n"
            "3. 不要输出自定义 `<skill_action>` 标签。\n"
        )
    elif phase in ["phase3+", "phase3", "phase4", "phase5"]:
        # Phase 3+: show specific execution mode constraints
        return base_contract + (
            "当前阶段：Phase 3+（工程化实现）\n\n"
            "⚠️ 重要：Phase 3+ 是后台执行模式\n\n"
            "Phase 3+ 执行要求：\n"
            "1. 不要输出自然语言给用户看 - 你的输出是给后端执行引擎解析的\n"
            "2. 只输出执行指令，使用 fenced code blocks 格式\n"
            "3. 不要输出 phase3_start / creator_phase 等阶段启动 JSON；后端已经完成阶段切换\n"
            "4. 严格按照 SKILL.md 中的 3.1.1 动作输出格式\n\n"
            "动作输出格式（必须严格遵守）：\n"
            "- 写入文件：代码块前一行写 `写入文件：<path>` 或 `保存到：<path>`，紧跟一个 code block\n"
            "- 运行命令：代码块前一行写 `执行命令：`，code block 中写完整命令\n"
            "- 路径必须包含完整 Skill 根目录，例如 `skills/<skill-name>/SKILL.md`\n"
            "- 一个 code block 只对应一个文件或一条命令\n\n"
            "生成的 Skill.md Markdown 运行说明：\n"
            "- 生成的 SKILL.md 必须保持标准 Markdown，不要加入自定义 Runtime Contract JSON、小型 DSL 或 action 标签。\n"
            "- 如果 Skill 需要运行 scripts/ 下的脚本，SKILL.md 可用普通 ```bash fenced block 给出命令示例，并说明 assistant 在 Sandbox 当轮回复中按示例替换真实参数后输出。\n"
            "- 只写行内脚本路径或‘立即调用脚本’不会触发宿主执行；必须使用标准 ```bash fenced code block 表达可执行命令。\n"
            "- 注意：在说明反例时，不要写出真实形态的 `scripts/*.py`、`references/*.md`、`assets/*` 示例路径，避免它们被误识别为真实 Skill 文件计划。\n"            "- SKILL.md 必须要求 assistant 等待宿主 observation 后再生成最终回答，不得假装执行。\n"
            "- 如果用户要求使用平台内置图像/多模态模型，不要生成外部 API key、关键词数据库或假图片脚本；应说明由宿主配置的模型能力完成相关步骤。模型来源必须区分：文本/翻译/语义改写使用 LLM_BASE_URL + TEXT_MODEL；看图理解/OCR/多模态问答使用 LLM_BASE_URL + VISION_MODEL；生成图片使用 Stable Diffusion 图片运行时 IMAGE_BASE_URL + IMAGE_MODEL（不要用 VISION_MODEL 生成图片）。生成出来的 SKILL.md 不要写中文 topic 翻译细节；图片脚本如需生成图片，可优先调用平台 helper `backend.services.skill_runtime.generate_stable_diffusion_image`，也可自实现；最终以 OUTPUT_DIR 产物和 stdout 字段通过 E2E 为准。确定性脚本必须实现真实算法，禁止固定模板/随机词表/ASCII 图冒充模型生成。\n\n"
            "Phase 3+ 行为约束：\n"
            "- 只有在 Phase 2 完成并获得用户确认后，才能进入 Phase 3\n"
            "- 进入 Phase 3 后，才可以按 SKILL.md 规定输出\"写入文件/执行命令\"的动作格式\n"
            "- 如果需要命名规范或输出格式示例，可在资源选择阶段请求加载 `references/best-practices.md` 与 `references/output-patterns.md`\n"
            "- 如果需要多步骤流程设计参考，可在资源选择阶段请求加载 `references/workflows.md`\n\n"
            "输出示例（严格按照此格式，不包含任何阶段启动 JSON）：\n"
            "执行命令：\n"
            "```bash\n"
            "python ../kernel/scripts/init_skill.py query-system-time --path .\n"
            "```\n"
            "\n"
            "写入文件：skills/query-system-time/SKILL.md\n"
            "```markdown\n"
            "---\n"
            "name: query-system-time\n"
            "description: 查询当前系统时间，精确到秒。当用户提到时间、现在几点、查询时间时使用。\n"
            "---\n"
            "# 查询系统时间\n"
            "\n"
            "## Overview\n"
            "查询当前系统的标准时间格式。\n"
            "```\n"
            "\n"
            "写入文件：skills/query-system-time/scripts/get_time.py\n"
            "```python\n"
            "#!/usr/bin/env python3\n"
            "import datetime\n"
            "\n"
            "def main():\n"
            '    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")\n'
            '    print(f"当前系统时间为: {now}")\n'
            "\n"
            'if __name__ == "__main__":\n'
            "    main()\n"
            "```\n"
            "```\n\n"
            "运行时安全：\n"
            "1. 不要假装已经读取 references/assets/scripts 的正文；这些资源只有在宿主明确加载后才可使用。\n"
            "2. 不要假装已经读取子 Skill 的正文；子 Skill 正文只有在宿主明确加载后才可使用。\n"
            "3. 不要输出自定义 `<skill_action>` 标签。\n"
        )
    else:
        # Unknown phase: default to Phase 1
        return _compose_creator_workflow_contract_for_phase("phase1")


def compose_body_prompt(skill: SkillPackage) -> str:
    """Compose body prompt for the second model round.

    第二阶段用于真正执行当前 Skill。
    子 Skill 仍然只提供 metadata，正文由宿主按需加载。
    """
    return (
        "你处于 Skill 加载流程的第二阶段：SKILL.md 正文执行阶段。\n\n"
        "请严格遵循下面完整 SKILL.md 中定义的流程、步骤、约束和输出要求。\n"
        "不要假装已经读取 references/assets/scripts 的正文；这些资源只有在宿主运行时明确提供后才可使用。\n"
        "不要假装已经读取子 Skill 的正文；子 Skill 正文只有在宿主明确加载后才可使用。\n"
        "不要输出自定义 `<skill_action>` 标签。\n\n"
        f"{_compose_agent_runtime_contract()}\n\n"
        "## Skill Metadata\n"
        f"- name: {skill.name}\n"
        f"- description: {skill.description}\n\n"
        "---\n\n"
        "## Loaded SKILL.md\n\n"
        f"{skill.skill_md_text}\n\n"
        "---\n\n"
        f"{_compose_child_skill_manifest(skill)}\n\n"
        "---\n\n"
        f"{_compose_resource_manifest(skill)}"
    )


def compose_kernel_creator_body_prompt(skill: SkillPackage) -> str:
    """Compose creator-specific body prompt for the kernel Skill Creator."""
    return (
        "你处于 Skill Creator 的 SKILL.md 正文执行阶段。\n\n"
        "阶段自评估指令：\n"
        "1. 先基于对话历史判断当前处于哪个 Phase。\n"
        "2. 核对该 Phase 的完成标志是否满足；未满足就继续提问或总结确认。\n"
        "3. 只有在 Phase 2 被用户确认后，才进入 Phase 3 并执行工程化输出。\n"
        "4. 在 Phase 1-2 期间，不得输出任何文件写入或命令执行格式。\n\n"
        "请严格遵循下面完整 SKILL.md 中定义的流程、步骤、约束和输出要求。\n"
        "不要假装已经读取 references/assets/scripts 的正文；这些资源只有在宿主运行时明确提供后才可使用。\n"
        "不要假装已经读取子 Skill 的正文；子 Skill 正文只有在宿主明确加载后才可使用。\n"
        "不要输出自定义 `<skill_action>` 标签。\n\n"
        f"{_compose_creator_workflow_contract()}\n\n"
        "## Skill Metadata\n"
        f"- name: {skill.name}\n"
        f"- description: {skill.description}\n\n"
        "---\n\n"
        "## Loaded SKILL.md\n\n"
        f"{skill.skill_md_text}\n\n"
        "---\n\n"
        f"{_compose_child_skill_manifest(skill)}\n\n"
        "---\n\n"
        f"{_compose_resource_manifest(skill)}"
    )


def compose_kernel_creator_metadata_prompt(skill: SkillPackage) -> str:
    """Compose creator-specific metadata-only prompt (no full SKILL.md body).
    
    精简版 creator prompt：
    - 只包含元数据和核心流程定义
    - 不加载完整 SKILL.md 正文
    - 适合 Phase 3+ 阶段，或不需要完整指导的场景
    """
    return (
        "你处于 Skill Creator 精简执行模式。\n\n"
        "阶段自评估指令：\n"
        "1. 先基于对话历史判断当前处于哪个 Phase。\n"
        "2. 只有在 Phase 2 被用户确认后，才进入 Phase 3 并执行工程化输出。\n"
        "3. 在 Phase 1-2 期间，不得输出任何文件写入或命令执行格式。\n\n"
        "核心流程：\n"
        "- Phase 1：需求收集 - 收集用户对 Skill 的真实需求\n"
        "- Phase 2：蓝图确认 - 设计并确认 Skill 架构蓝图\n"
        "- Phase 3+：工程化实现 - 执行文件创建和脚本编写\n\n"
        "蓝图确认信号：\n"
        "Phase 2 输出完整蓝图与确认问题；用户确认后由后端进入 Phase 3。\n"
        "Phase 1-2 期间禁止输出 phase3_start JSON 标记。\n\n"
        "请确保：\n"
        "- 严格按 Phase 流程执行\n"
        "- Phase 1-2 只做需求澄清和蓝图确认\n"
        "- Phase 3+ 才输出写入文件或执行命令的格式\n\n"
        f"{_compose_creator_workflow_contract()}\n\n"
        "## Skill Metadata\n"
        f"- name: {skill.name}\n"
        f"- description: {skill.description}\n\n"
        "---\n\n"
        f"{_compose_child_skill_manifest(skill)}\n\n"
        "---\n\n"
        f"{_compose_resource_manifest(skill)}"
    )


def compose_kernel_creator_first_part_prompt(skill: SkillPackage, first_part_content: str) -> str:
    """Compose creator prompt with only the first part of SKILL.md.
    
    初始版 creator prompt：
    - 只包含元数据和 SKILL.md 的第一部分（到第一个 --- 分隔符）
    - 包含启动对话的指令，适合首次进入页面
    - 比完整加载更轻量，同时保留了关键信息
    """
    return (
        "你处于 Skill Creator 初始对话模式。\n\n"
        "请严格按照下面 SKILL.md 中的启动对话要求执行。\n"
        "不要假装已经读取 references/assets/scripts 的正文；这些资源只有在宿主运行时明确提供后才可使用。\n"
        "不要假装已经读取子 Skill 的正文；子 Skill 正文只有在宿主明确加载后才可使用。\n"
        "不要输出自定义 `<skill_action>` 标签。\n\n"
        f"{_compose_creator_workflow_contract()}\n\n"
        "## Skill Metadata\n"
        f"- name: {skill.name}\n"
        f"- description: {skill.description}\n\n"
        "---\n\n"
        "## Loaded SKILL.md (First Part)\n\n"
        f"{first_part_content}\n\n"
        "---\n\n"
        f"{_compose_child_skill_manifest(skill)}\n\n"
        "---\n\n"
        f"{_compose_resource_manifest(skill)}"
    )


def _split_skill_md_into_blocks(skill_root: Path) -> list[str]:
    """Split SKILL.md into blocks separated by --- separators.
    
    Returns a list of blocks, where:
    - Block 0: Frontmatter + intro (to first ---)
    - Block 1: Phase 1
    - Block 2: Phase 2
    - Block 3: Phase 3
    - Block 4: Phase 4
    - Block 5: Phase 5
    - Block 6: Core principles
    """
    skill_md_path = skill_root / "SKILL.md"
    if not skill_md_path.exists():
        raise FileNotFoundError(f"SKILL.md not found at {skill_md_path}")
    
    with open(skill_md_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    
    # Split by --- separators
    parts = content.split("\n---\n")
    
    # The first part includes frontmatter, we need to process it
    blocks = []
    for i, part in enumerate(parts):
        if i == 0:
            # First part: frontmatter + intro
            blocks.append(part.strip() + "\n")
        else:
            # Subsequent parts: add back the separator context
            blocks.append("---\n" + part.strip() + "\n")
    
    return blocks


def _compose_kernel_creator_blocks_prompt(skill: SkillPackage, blocks: list[int], phase: str) -> str:
    """Compose creator prompt with specific blocks from SKILL.md.
    
    Args:
        skill: SkillPackage with metadata
        blocks: List of block indices to include (0 = intro, 1 = Phase1, etc.)
        phase: Current phase for progressive disclosure
    """
    # Load the full content first to extract blocks
    all_blocks = _split_skill_md_into_blocks(settings.kernel_path)
    
    # Build combined content from selected blocks
    selected_content = []
    for block_idx in blocks:
        if block_idx < len(all_blocks):
            selected_content.append(all_blocks[block_idx])
    
    combined_content = "\n".join(selected_content)
    
    # Phase 1/2 特殊处理：在 SKILL.md 前面增加执行提示
    if phase == "first_time" or phase == "phase1":
        phase1_instruction = """
==================== Phase 1 执行指南 ====================

【重要！请务必阅读！】

1. **严格按顺序执行**：必须按照 1.1 → 1.2 → 1.3 → 1.4 → 1.5 的顺序执行
2. **每次只问一个问题**：不要在一次回复中问多个问题
3. **使用指定的问题模板**：必须使用 SKILL.md 中用 ``` 包裹的问题模板
4. **不要跳过步骤**：每个步骤都要执行，不要省略
5. **不要重复开场问题**：如果对话历史中用户已经描述了要创建的 Skill，不要再次询问“你希望 智能助手 帮你做什么事情？”，应继续追问具体需求或生成蓝图。

现在，让我们开始执行 Phase 1 吧！

========================================================

"""
        combined_content = phase1_instruction + combined_content
    elif phase == "phase2":
        phase2_instruction = """
==================== Phase 2 执行指南 ====================

【重要！请务必阅读！】

Phase 2 的任务是：
1. 基于 Phase 1 收集的信息，生成完整的"架构蓝图"
2. 使用 AskUserQuestion 询问用户确认蓝图

【关键要求！】
1. **必须先输出完整蓝图正文**，不要只输出确认问题。
2. **蓝图格式必须严格按照 SKILL.md 中的模板**，包括：
   - 必须包含 `## 📋 Skill 架构蓝图` 标记
   - 必须明确列出 Skill 名称
   - 必须包含 I/O 契约、目录结构、工作流逻辑
   - 必须包含“宿主执行方式”，明确哪些任务直接回答，哪些任务需要输出标准 Markdown fenced block
   - 如果涉及图像/多模态能力，必须明确使用宿主已配置模型，不要虚构 API 密钥、关键词数据库或占位图片脚本
   - 如果涉及需要模型判断的开放式能力，优先设计为模型直接回答；若必须生成 scripts/，脚本必须区分模型来源：文本/语义使用 LLM_BASE_URL + TEXT_MODEL，看图理解使用 LLM_BASE_URL + VISION_MODEL，生成图片可使用 Stable Diffusion 图片运行时 IMAGE_BASE_URL + IMAGE_MODEL，并优先使用平台 helper `backend.services.skill_runtime.generate_stable_diffusion_image`；确定性脚本必须实现真实算法，不得用固定模板/随机词表/ASCII 图冒充模型能力
3. 完整蓝图之后，再输出一个 AskUserQuestion 确认问题。
4. **AskUserQuestion 必须用 ```text 包裹**，严格按照模板格式
5. **AskUserQuestion 选项必须使用以下三项原文**：
   - "对，开始做吧"
   - "大体对，但有些地方要改"
   - "不对，我重新说一下"
6. **不要输出 phase3_start 标记**，必须等用户确认后再说

现在，让我们开始执行 Phase 2 吧！

========================================================

"""
        combined_content = phase2_instruction + combined_content
    
    return (
        "你处于 Skill Creator 模式。\n\n"
        "请严格按照下面 SKILL.md 中的流程和要求执行。\n"
        "不要假装已经读取 references/assets/scripts 的正文；这些资源只有在宿主运行时明确提供后才可使用。\n"
        "不要假装已经读取子 Skill 的正文；子 Skill 正文只有在宿主明确加载后才可使用。\n"
        "不要输出自定义 `<skill_action>` 标签。\n\n"
        f"{_compose_creator_workflow_contract_for_phase(phase)}\n\n"
        "## Skill Metadata\n"
        f"- name: {skill.name}\n"
        f"- description: {skill.description}\n\n"
        "---\n\n"
        "## Loaded SKILL.md\n\n"
        f"{combined_content}\n\n"
        "---\n\n"
        f"{_compose_child_skill_manifest(skill)}\n\n"
        "---\n\n"
        f"{_compose_resource_manifest(skill)}"
    )


def load_kernel_creator_for_phase(phase: str) -> str:
    """Load kernel Skill with appropriate blocks for current phase.

    Progressive disclosure strategy:
    - first_time / phase1: blocks [0, 1, 2] (intro + Phase1)
    - phase2: blocks [0, 1, 2, 3] (intro + Phase1 + Phase2)
    - phase3+: load FULL SKILL.md (need full implementation instructions)
    
    Block mapping from SKILL.md split:
    - Block 0: frontmatter
    - Block 1: intro + SOP overview
    - Block 2: Phase 1 (需求挖掘)
    - Block 3: Phase 2 (蓝图设计)
    - Block 4-...: Phase 3-5 + core principles
    """
    skill = load_kernel_package(include_body=False)

    if phase == "first_time" or phase == "phase1":
        # First time or Phase1: only need intro + Phase1
        return _compose_kernel_creator_blocks_prompt(skill, [0, 1, 2], phase)
    elif phase == "phase2":
        # Phase2: intro + Phase1 + Phase2
        return _compose_kernel_creator_blocks_prompt(skill, [0, 3], phase)
    elif phase in ["phase3+", "phase3"]:
        # Phase3 and beyond: NEED FULL SKILL.md for implementation instructions!
        return _compose_kernel_creator_blocks_prompt(skill, [0, 4], phase)
    elif phase in ["phase4"]:
        # Phase4 and beyond: NEED FULL SKILL.md for implementation instructions!
        return _compose_kernel_creator_blocks_prompt(skill, [0, 5], phase)
    elif phase in ["phase5"]:
        # Phase5 and beyond: NEED FULL SKILL.md for implementation instructions!
        return _compose_kernel_creator_blocks_prompt(skill, [0, 6], phase)

    else:
        # Unknown phase: default to intro + Phase1
        return _compose_kernel_creator_blocks_prompt(skill, [0, 1, 2], phase)


def load_kernel_package(*, include_body: bool = False) -> SkillPackage:
    """Load the fixed kernel Skill package."""
    return _load_skill_from_root(settings.kernel_path, include_body=include_body)


def load_user_skill_package(skill_name: str, *, include_body: bool = False) -> SkillPackage:
    """Load a user-created Skill package by directory name."""
    root = get_visible_skill_dir(skill_name, mode="sandbox")
    return _load_skill_from_root(root, include_body=include_body)


def load_kernel_metadata_prompt() -> str:
    """Load kernel Skill metadata-only prompt."""
    skill = load_kernel_package(include_body=False)
    return compose_metadata_prompt(skill)


def load_skill_metadata_prompt(skill_name: str) -> str:
    """Load selected Skill metadata-only prompt."""
    skill = load_user_skill_package(skill_name, include_body=False)
    return compose_metadata_prompt(skill)


def load_kernel_body_prompt() -> str:
    """Load kernel Skill full SKILL.md body prompt."""
    skill = load_kernel_package(include_body=True)
    return compose_body_prompt(skill)


def load_kernel_creator_body_prompt() -> str:
    """Load kernel Skill full SKILL.md body prompt for creator mode."""
    skill = load_kernel_package(include_body=True)
    return compose_kernel_creator_body_prompt(skill)


def load_kernel_creator_metadata_prompt() -> str:
    """Load kernel Skill metadata-only prompt for creator mode (no full SKILL.md body)."""
    skill = load_kernel_package(include_body=False)
    return compose_kernel_creator_metadata_prompt(skill)


def load_kernel_creator_first_part_prompt() -> str:
    """Load kernel Skill with only the first part of SKILL.md (until first --- separator).
    
    适合首次进入 Creator 页面的场景：
    - 包含元数据和启动对话指令
    - 比完整加载更轻量
    - 只包含 SKILL.md 到第一个 --- 的部分
    """
    skill = load_kernel_package(include_body=False)
    first_part_content = _load_skill_first_part(settings.kernel_path)
    return compose_kernel_creator_first_part_prompt(skill, first_part_content)


def load_skill_body_prompt(skill_name: str) -> str:
    """Load selected Skill full SKILL.md body prompt."""
    skill = load_user_skill_package(skill_name, include_body=True)
    return compose_body_prompt(skill)

def resolve_child_skill_root(parent_skill_name: str, child_ref: str) -> Path:
    """Resolve a child Skill root under a parent Skill.

    child_ref 必须来自 Child Skills Manifest 中的 ref。
    """
    if not child_ref or "\x00" in child_ref:
        raise ValueError("子 Skill 引用非法")

    if child_ref.startswith("/") or "\\" in child_ref:
        raise ValueError("子 Skill 引用不允许使用绝对路径或反斜杠")

    rel = Path(child_ref)

    if any(part in {"", ".."} for part in rel.parts):
        raise ValueError("子 Skill 引用路径越界")

    parent = _select_skill_for_resource_action(parent_skill_name)
    child_root = (parent.root / rel).resolve()
    parent_root = parent.root.resolve()

    try:
        child_root.relative_to(parent_root)
    except ValueError as exc:
        raise ValueError("子 Skill 路径越界") from exc

    if not _is_skill_dir(child_root):
        raise FileNotFoundError(f"子 Skill 不存在: {child_ref}")

    return child_root


def load_child_skill_metadata_prompt(parent_skill_name: str, child_ref: str) -> str:
    """Load child Skill metadata-only prompt."""
    child_root = resolve_child_skill_root(parent_skill_name, child_ref)
    child = _load_skill_from_root(child_root, include_body=False)
    return compose_metadata_prompt(child)


def load_child_skill_body_prompt(parent_skill_name: str, child_ref: str) -> str:
    """Load child Skill full body prompt."""
    child_root = resolve_child_skill_root(parent_skill_name, child_ref)
    child = _load_skill_from_root(child_root, include_body=True)

    return (
        "你现在额外加载了一个子 Skill 的完整正文。\n"
        "该子 Skill 只用于当前用户请求中与其 description 匹配的部分。\n"
        "若父 Skill 与子 Skill 规则冲突，优先遵循更具体的子 Skill；若涉及宿主安全限制，始终遵循宿主安全限制。\n\n"
        f"{compose_body_prompt(child)}"
    )

def resolve_skill_resource(skill: SkillPackage, rel_path: str) -> Path:
    """Resolve and validate a resource path inside the Skill directory."""
    if not rel_path or "\x00" in rel_path:
        raise ValueError("资源路径非法")

    rel = Path(rel_path)

    if rel.is_absolute():
        raise ValueError("不允许读取绝对路径")

    parts = rel.parts

    if not parts:
        raise ValueError("资源路径为空")

    if parts[0] not in _ALLOWED_RESOURCE_DIRS:
        raise ValueError("只允许读取 references/、assets/、scripts/ 下的资源")

    resolved_root = skill.root.resolve()
    resolved_path = (skill.root / rel).resolve()

    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("资源路径越界") from exc

    if not resolved_path.exists() or not resolved_path.is_file():
        raise FileNotFoundError(f"资源不存在: {rel_path}")

    suffix = resolved_path.suffix.lower()

    if suffix not in _ALLOWED_READ_SUFFIXES:
        raise ValueError(f"不允许直接读取该类型资源: {suffix}")

    return resolved_path


def _select_skill_for_resource_action(skill_name: str) -> SkillPackage:
    """Select kernel or user Skill for resource reading."""
    kernel = load_kernel_package(include_body=False)

    if skill_name in {kernel.name, "kernel", "skill-creator"}:
        return load_kernel_package(include_body=False)

    return load_user_skill_package(skill_name, include_body=False)


def read_skill_resource_text(
    skill_name: str,
    rel_path: str,
    *,
    max_chars: int = 20000,
) -> dict:
    """Read a bundled Skill resource safely.

    这里只提供底层能力；是否读取由上层运行时决定。
    """
    skill = _select_skill_for_resource_action(skill_name)
    path = resolve_skill_resource(skill, rel_path)

    text = path.read_text(encoding="utf-8", errors="replace")
    truncated = False

    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True

    return {
        "action": "read_skill_resource",
        "name": skill.name,
        "path": rel_path,
        "success": True,
        "content": text,
        "truncated": truncated,
    }
