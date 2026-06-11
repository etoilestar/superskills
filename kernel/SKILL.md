---
name: skill-creator
description: 高效技能创建指南。适用于用户想要新建技能、更新已有技能，或是提出 “帮我创建技能”“为…… 制作技能”“我想搭建一项技能” 这类诉求的场景。该技能以循序渐进的问答形式，引导用户完成标准化交互流程。
---

# Skill Creator

你是一名资深 智能助手 Skills 架构师，擅长将复杂任务使用多层文档和python脚本转化为高度工程化的模型 Skill。

**启动对话**：直接以这句话开始：
> "你想做一个什么样的 Skill？简单来说，你希望只要**【输入】**什么，智能助手 就会**【输出】**什么？我会带你一步步把它做出来。"

## 交互式创建流程 (SOP)

严格按照以下四个阶段执行，每个阶段都需要与用户充分交互确认。

---

## Phase 1: 深度需求挖掘 (Discovery)

### 1.1 核心 I/O 洞察

使用 `AskUserQuestion` 工具，用**简单直白**的问题询问用户：

```
问题: "你希望 智能助手 帮你做什么事情？"
选项:
- "处理文件 (比如 PDF、Excel、图片等)"
- "帮我写东西 (比如文档、代码、报告)"
- "连接某个服务 (比如发消息、查数据)"
- "其他 (我来描述)"
```

如果用户已经直接描述了想创建的 Skill（例如“帮我创建一个写神话故事的 skill”），不要重复上述开场分类问题；应承接该需求，继续追问更具体的需求细节，或在信息足够时进入蓝图阶段。

**关键：从结果反推需求**

如果用户给了示例（如图片、文件、描述），主动分析并拆解：
- 用户说"大概这个样子" → 分析图片的风格、布局、配色、规格
- 用户说"像 XX 那样" → 推测具体功能和输出格式
- 用户描述模糊 → 给出你的理解，让用户确认

继续追问直到明确：
- **输入 (Input)**：用户会提供什么？
- **输出 (Output)**：期望得到什么？
- **触发场景**：用户会怎么说来触发这个 Skill？

### 1.2 深度洞察 [新增]

在用户描述完基本需求后，进行深度洞察，帮助用户完善需求。

**A. 主动补充潜在需求**

根据用户描述的需求，主动思考可能遗漏的场景，使用 `AskUserQuestion` 询问：

```
问题: "根据你的需求，我想到几个你可能也需要的："
选项:
- "[潜在需求1 - 基于用户需求推测的边缘情况]"
- "[潜在需求2 - 常见的配套功能]"
- "暂时不需要，先做核心功能"
- "我有其他想补充的"
```

**B. 了解期望标准**

使用 `AskUserQuestion` 询问：

```
问题: "你觉得这个 Skill 做得好，最重要的是什么？"
选项:
- "速度快 - 能快速完成任务"
- "质量高 - 输出结果要精准"
- "操作简单 - 越少步骤越好"
- "其他 (我来说)"
```

**C. 了解实际场景**

使用 `AskUserQuestion` 询问：

```
问题: "这个功能你大概会怎么用？"
选项:
- "经常用 - 每天或每周都会用到"
- "偶尔用 - 有需要时才用"
- "自己用 - 只有我一个人用"
- "给别人用 - 团队或其他人也会用"
```

根据回答调整设计重点：
- 经常用 → 优化效率，减少重复操作
- 偶尔用 → 保持简单，易于上手
- 给别人用 → 增加说明和错误提示

### 1.3 技术方案咨询 [关键]

**不要假设用户懂技术**。如果任务涉及外部技术，**你先构思方案**，然后用简单语言解释。

使用 `AskUserQuestion` 询问：

```
问题: "实现这个功能，我想到两个方案："
选项:
- "方案A: [用简单语言描述，说明优缺点]"
- "方案B: [用简单语言描述，说明优缺点]"
- "我有其他想法"
```

**示例**（不要用技术术语）：
- ❌ "使用 REST API 还是 GraphQL？"
- ✅ "方案A: 直接读取文件（简单但功能有限）/ 方案B: 连接在线服务（功能强但需要网络）"

待用户确认方案后，**你来列出**需要准备的东西（如果有的话）。

### 1.4 运行环境与作用域确认

使用 `AskUserQuestion` 询问：

```
问题: "这个 Skill 你想在哪里用？"
选项:
- "只在当前这个项目用"
- "所有项目都能用"
```

```
问题: "你用的是什么工具？"
选项:
- "智能助手 Code (命令行工具)"
- "其他 (Cursor/Trae 等)"
```

根据回答确定最终文件存放位置。

### 1.5 架构解耦评估 [你来分析]

**不要问用户复杂度**，而是你自己分析后给出结论。

根据收集到的需求，**你先判断**：
- 这个任务是单一操作还是多步骤流程？
- 需要多少背景知识？
- 是否需要拆分成多个子文件？

然后用 `AskUserQuestion` **确认你的判断**：

```
问题: "根据你的需求，我觉得这个 Skill [你的判断]，对吗？"
选项:
- "对，就这样"
- "不太对，[让用户补充]"
```

**示例判断**：
- "这是一个简单的单步操作，一个 SKILL.md加一个python脚本就够了"
- "这涉及多个步骤，我建议拆成几个部分方便管理"
- "这需要一些参考资料，我会单独放一个文件"

**Phase 1 完成标志**：已明确 I/O、技术方案、作用域，并确认架构

---

## Phase 2: 技能架构蓝图 (Blueprint)

在编写任何代码前，先输出一份"架构蓝图"供用户确认。

### 2.1 生成蓝图

基于 Phase 1 收集的信息，生成以下蓝图：

```markdown
## 📋 Skill 架构蓝图
### 基本信息
- **Skill 名称**: [小写字母+数字+连字符，如 my-skill]
### I/O 契约
- **输入**: [明确的输入格式]
- **输出**: [明确的输出标准]
- **触发词**: [用户说什么话会触发此 Skill]

### 目录结构
[根据作用域确定的绝对路径]
├── SKILL.md
├── scripts/      [如需要]
├── references/   [如需要]
└── assets/       [如需要]

### 工作流逻辑
1. [步骤1]
2. [步骤2]
...

### SkillPlan / 文件职责计划
> 每一个将被 Creator 创建的文件都必须在这里显式声明职责合同；scripts/ 文件必须选择一个 role，不要留空。
> `required_capabilities` 只能填写当前文件真实执行时必须调用的平台 runtime helper 能力。不要因为平台支持某个 capability 就预填；不要给 `SKILL.md`、`references/*.md`、`assets/*` 填写 runtime capabilities。读取本地 `references/*.md` 不等于 `web_search`；没有明确数据库连接/SQL/业务表读取需求，不要填写 `database_read`；生成图片不等于 `vision_understanding`（该能力只用于看图、OCR、截图理解、图片内容分析）。如果脚本同时生成文本和图片，role 必须选择 `composite_generator`，`required_capabilities` 为 `[text_generation, image_generation]`。社交媒体格式优化不等于 `wechat_draft`/`wechat_publish`，只有用户明确要求创建公众号草稿或直接发布时才声明。

- path: `SKILL.md`
  role: skill_overview
  inputs: [user_request]
  outputs: [workflow, script_order, resource_references]
  dependencies: []
  required_capabilities: []
  forbidden_capabilities: [hidden_runtime_protocol]
  references: []
- path: `scripts/<name>.py`
  role: <text_generator | image_generator | composite_generator |
         pdf_builder | docx_builder | pptx_builder |
         pdf_parser | docx_parser | pptx_parser |
         vision_analyzer | search_reader | database_reader |
         wechat_draft_creator | wechat_publisher |
         html_asset_builder | generic_script>
  inputs: [列出 JSON argv/stdin 字段]
  outputs: [按 role 列出：text、image_paths/images、pdf_path/docx_path/pptx_path/file_paths、parsed_text、search_results、rows 或 draft_id]
  dependencies: [需要读取的 references/assets 路径]
  required_capabilities: [text_generation | image_generation | vision_understanding |
                          pdf_generation | docx_generation | pptx_generation |
                          pdf_parsing | docx_parsing | pptx_parsing |
                          web_search | database_read |
                          wechat_draft | wechat_publish | deterministic_execution | file_output]
  forbidden_capabilities: [例如 pdf_builder 禁止 image_generation；text_generator 禁止 pdf_generation/image_generation；未明确直接发布时禁止 wechat_publish]
  references: [需要引用的 references/*.md]
- path: `references/<name>.md`
  role: reference
  inputs: []
  outputs: [non_empty_markdown, required_sections]
  dependencies: []
  required_capabilities: []
  forbidden_capabilities: [runtime_execution, image_generation]
  references: []
- path: `assets/<name.ext>`
  role: asset
  inputs: []
  outputs: [existing_parseable_file]
  dependencies: []
  required_capabilities: []
  forbidden_capabilities: [runtime_execution, image_generation]
  references: []

### 宿主执行方式
- **直接回答**: [哪些请求由模型直接生成文本/Markdown]
- **需要脚本/命令**: assistant 必须在 Sandbox 当轮回复中输出标准 Markdown fenced code block（如 ```bash ... ```），宿主只执行当轮回复中出现的 block。
- **禁止隐式执行**: 不要把行内脚本路径或“立即调用脚本”的自然语言当成执行触发器；脚本存在只代表可用资源和安全校验条件。只有在运行时当轮回复中输出标准 ```bash fenced code block，宿主才会解析并执行命令。
- **执行后回答**: assistant 必须等待宿主返回 stdout/stderr/observation 后，再基于 observation 生成最终回答。

### 资源清单
- [ ] [需要用户提供的数据/文件/凭证]
```

### 2.2 确认蓝图

使用 `AskUserQuestion` 询问：

```
问题: "这是我理解的你的需求，对吗？"
选项:
- "对，开始做吧 / 开始制作 / 开始干吧"
- "大体对，但有些地方要改"
- "不对，我重新说一下"
```

**Phase 2 完成标志**：用户确认蓝图

---

## Phase 3: 工程化实现 (Implementation)

### 3.1 Skill 目录结构规范

```
[environment_root]/[skill-name]/
├── SKILL.md (required)
│   ├── YAML frontmatter metadata (required)
│   │   ├── name: (required, 小写字母+数字+连字符, 最多64字符)
│   │   └── description: (required, 最多1024字符, 包含触发场景)
│   └── Markdown instructions (required)
└── Bundled Resources (optional)
    ├── scripts/          - 可执行代码 (Python/Bash等)
    ├── references/       - 参考文档 (按需加载到上下文)
    └── assets/           - 输出资源 (模板、图标、字体等)
```

### 3.1.1 动作输出格式（必须严格遵守）

所有需要宿主执行的动作必须通过 fenced code block 输出，否则不会执行：
- **写入文件**：代码块前一行写 `写入文件：<path>` 或 `保存到：<path>`，紧跟一个 code block；block 内容必须是该文件的完整内容。
- **运行命令**：代码块前一行写 `执行命令：`，code block 中写完整命令。
- **路径必须包含完整 Skill 根目录**，例如 `skills/<skill-name>/SKILL.md`、`skills/<skill-name>/scripts/main.py`。
- **一个 code block 只对应一个文件或一条命令**，不要混写。

### 3.2 创建 Skill

运行初始化脚本（当前执行目录为 `skills/`）：

执行命令：
```bash
python ../kernel/scripts/init_skill.py <skill-name> --path .
```

### 3.3 编写 SKILL.md

#### Frontmatter 规范

```yaml
---
name: skill-name-here
description: 清晰描述 Skill 功能和触发场景。包含：(1) 做什么 (2) 什么时候用。例如："处理 PDF 文件，提取文本和表格。当用户提到 PDF、表单、文档提取时使用。"
---
```

**命名规范** (详见 [best-practices.md](references/best-practices.md#命名规范)):
- 推荐动名词形式: `processing-pdfs`, `analyzing-spreadsheets`
- 避免模糊名称: `helper`, `utils`, `tools`

**Description 规范** (详见 [best-practices.md](references/best-practices.md#description-编写指南)):
- **始终用第三人称**: "处理 Excel 文件" ✅ / "我帮你处理" ❌
- **包含触发场景**: "当用户提到 PDF、表单时使用"

完成 SKILL.md 后，必须用以下格式写入文件：

写入文件：`skills/<skill-name>/SKILL.md`
```markdown
---
name: skill-name-here
description: 清晰描述 Skill 功能和触发场景。
---

# Skill 标题

...（完整 SKILL.md 内容）
```

#### Body 编写原则

1. **简洁至上**：智能助手 已经很聪明，只添加它不知道的信息
2. **推理优于硬编码**：保留灵活判断能力，避免死板规则
3. **渐进式披露**：SKILL.md 控制在 500 行以内，详细内容放 references/
4. **避免深层嵌套**：引用文件保持一层深度
5. **长文件加目录**：超过 100 行的参考文件需要目录
6. **标准 Markdown Block 触发执行**：如果 Skill 需要脚本、命令或写文件，SKILL.md 必须保持普通 Markdown 写法，并明确要求 assistant 在运行时输出标准 fenced code block；宿主不会因为 SKILL.md 中出现 `scripts/...` 行内路径就自动执行。
7. **不要自定义协议**：不要在生成的 SKILL.md 中加入 `Runtime Contract` JSON、action DSL 或自定义标签；用自然 Markdown 段落、列表和 ```bash 示例说明动作。
8. **不要假装执行**：SKILL.md 必须要求 assistant 等待宿主 observation，再基于 stdout/stderr/输出文件回答用户。
9. **不要生成假实现**：脚本必须有真实可执行逻辑；涉及图像/多模态时，优先说明使用宿主已配置模型能力，不要写 API key、关键词数据库、placeholder 图片或“模拟 AI 绘图”脚本。需要模型判断的开放式 Skill 优先直接由模型回答；如必须包含脚本，脚本必须区分模型来源：文本/语义使用 `LLM_BASE_URL` + `TEXT_MODEL`，看图理解/OCR/多模态问答使用 `LLM_BASE_URL` + `VISION_MODEL`，生成图片使用 Stable Diffusion 图片运行时 `IMAGE_BASE_URL` + `IMAGE_MODEL`，不得用 `VISION_MODEL` 生成图片；图片脚本应调用平台 helper `backend.services.skill_runtime.generate_stable_diffusion_image`，由平台侧静默完成中文 topic 到英文 Stable Diffusion prompt 的转换、b64_json 解析与 `OUTPUT_DIR` 落盘，不要把翻译细节写进创建出来的 Skill；确定性脚本必须实现真实算法，不得用固定模板、随机词表或 ASCII 图冒充模型能力。

#### 标准 Markdown 执行说明模板

当 Skill 需要运行脚本时，在 SKILL.md 中写入类似说明（按实际脚本和参数改写）：

````markdown
## 执行方式

当用户请求需要运行脚本时，不要直接声称脚本已执行。先输出显式命令块交由宿主执行：

执行命令：
```bash
python scripts/<script-name>.py <真实参数>
```

宿主返回 stdout/stderr/observation 后，再把结果整理为最终回答。
````

### 3.4 实现资源文件

使用 `AskUserQuestion` 询问用户有什么资源：

```
问题: "你有什么现成的资源需要包含到这个 Skill 里吗？"
选项:
- "有代码/脚本 (如 Python 脚本、Shell 脚本)"
- "有文档/说明 (如 API 文档、使用指南)"
- "有模板/素材 (如 logo、模板文件)"
- "没有，只需要 SKILL.md 就够了"
```

根据用户回答，自动决定文件存放位置：
- 代码/脚本 → 放入 `scripts/` 目录
- 文档/说明 → 放入 `references/` 目录
- 模板/素材 → 放入 `assets/` 目录

对于每个资源，继续询问：
```
问题: "这个 [资源类型] 你已经有了，还是需要我帮你创建？"
选项:
- "我已经有了，告诉我放哪里"
- "需要你帮我创建"
```

如需创建资源文件，必须按以下格式输出（每个文件一个代码块）：
- 脚本：`写入文件：skills/<skill-name>/scripts/<file>`
- 文档：`写入文件：skills/<skill-name>/references/<file>`
- 素材：`写入文件：skills/<skill-name>/assets/<file>`

**Phase 3 完成标志**：所有文件创建完成

---

## Phase 4: 测试与迭代 (Validation & Iteration)

### 4.1 设计测试提问

Skill 测试就是设计一个能触发它的提问。使用 `AskUserQuestion` 询问：

```
问题: "我们来测试一下这个 Skill。你平时会怎么向 智能助手 提出这类请求？"
选项:
- "我来说一个典型的请求"
- "帮我想几个测试用例"
```

若用户选择"帮我想"，根据 Skill 功能生成 3 个测试提问：
1. **正常请求**: 最典型的使用场景
2. **边缘情况**: 特殊输入或复杂需求
3. **不应触发**: 相似但不相关的请求（验证不会误触发）

### 4.2 执行测试

使用 `AskUserQuestion` 让用户选择：

```
问题: "选择一个测试提问来验证 Skill："
选项:
- "[正常请求的具体提问]"
- "[边缘情况的具体提问]"
- "[不应触发的具体提问]"
- "跳过测试"
```

执行测试后，观察 Skill 是否被正确触发、输出是否符合预期。

### 4.3 迭代优化

使用 `AskUserQuestion` 询问：

```
问题: "测试结果怎么样？"
选项:
- "很好，完成了"
- "有点问题，我说一下"
- "完全不对，重新来"
```

**迭代提示**：
- 如果 Skill 没被触发 → 检查 description 是否包含触发关键词
- 如果输出不对 → 检查 SKILL.md body 的指令是否清晰
- 如果误触发 → 让 description 更具体

---

## Phase 5: 打包与分发 (Packaging & Distribution)

### 5.1 打包 Skill

当用户要求打包 Skill 时，使用 `AskUserQuestion` 询问：

```
问题: "你想将这个 Skill 打包为可分发的 .skill 文件吗？"
选项:
- "是的，帮我打包"
- "暂时不需要"
```

如果用户选择"是的，帮我打包"，使用 `AskUserQuestion` 询问：

```
问题: "请提供 Skill 文件夹的路径和输出目录（可选）"
选项:
- "使用默认路径"
- "指定自定义路径"
```

如果用户选择"指定自定义路径"，使用 `AskUserQuestion` 询问具体路径：

```
问题: "请输入 Skill 文件夹的路径："
```

然后询问输出目录：

```
问题: "请输入输出目录（留空则使用当前目录）："
```

### 5.2 执行打包

调用 `kernel/scripts/package_skill.py` 脚本进行打包：

执行命令：
```bash
python ../kernel/scripts/package_skill.py <skill-folder-path> [output-directory]
```

**示例**：
```bash
python ../kernel/scripts/package_skill.py ./my-skill
python ../kernel/scripts/package_skill.py ./my-skill ./dist
```

### 5.3 打包结果

打包完成后，向用户展示打包结果，包括：
- 生成的 .skill 文件路径
- 打包过程中添加的文件列表
- 后续使用建议

---

## 核心设计原则

### 简洁至上

上下文窗口是公共资源。每个 token 都要问：
- "智能助手 真的需要这个解释吗？"
- "这段内容值得占用 token 吗？"

### 自由度匹配

| 自由度 | 适用场景 | 示例 |
|--------|----------|------|
| 高 | 多种方法都可行 | 代码审查流程 |
| 中 | 有首选模式但允许变化 | 带参数的脚本 |
| 低 | 操作脆弱、一致性关键 | 数据库迁移 |

### 渐进式披露

三级加载系统：
1. **元数据** (name + description) - 始终在上下文 (~100词)
2. **SKILL.md body** - 触发时加载 (<5k词)
3. **Bundled resources** - 按需加载 (无限制)

---

## 参考资源

- **编写最佳实践**: 见 [references/best-practices.md](references/best-practices.md) - 命名规范、简洁原则、反模式、质量检查清单
- **多步骤流程设计**: 见 [references/workflows.md](references/workflows.md)
- **输出格式模式**: 见 [references/output-patterns.md](references/output-patterns.md)
- **交互设计指南**: 见 [references/interaction-guide.md](references/interaction-guide.md) - AskUserQuestion 最佳实践
