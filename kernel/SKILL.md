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

## Phase 1: 需求快速解析 (Prepare)

Creator 前半段应尽量短：用户已经给出需求、选择已有 Skill 或上传文件时，不要重新问开场分类问题，也不要按固定问卷逐项追问。

### 1.1 一次性抽取需求

直接从用户当前输入、对话历史、已有 Skill 上下文和上传文件信息中抽取：
- **目标**：要创建/修改的 Skill 解决什么问题。
- **输入**：用户运行 Skill 时会提供什么文本、文件、参数或素材。
- **输出**：Skill 最终要返回什么文本、结构化数据或 `OUTPUT_DIR` 文件产物。
- **工作流**：需要直接回答、调用脚本、读取 references/assets、调用已注册工具，还是组合执行。
- **素材需求**：哪些静态素材必须来自用户上传或已有 bundled assets；不得让模型生成 `assets/**`。
- **外部依赖**：网络、模型、平台工具、API、数据库、第三方命令等真实依赖。
- **约束**：格式、语言、质量标准、安全边界、运行环境、最终输出协议。

能合理默认的内容不要问用户，例如：
- 默认在当前 Creator 沙盒 Skill 目录创建。
- 默认按任务复杂度和可执行责任边界规划文件集。文件数量不是优化目标：既不追求脚本尽可能少，也不追求模块尽可能多。简单、单一且高度耦合的责任可以使用 1 个脚本；存在多个清晰 producer/consumer、能力、产物或独立验证边界的中等和复杂任务通常使用 2～3 个脚本；只有存在更多真实独立责任时才继续增加脚本。
- 默认优先可执行、可验证、可通过 E2E 的实现。
- 默认 `assets/**` 只接收用户上传或 bundled 静态文件，运行时生成产物写入 `OUTPUT_DIR` 并通过 stdout JSON 返回。

### 1.2 澄清与 Readiness Principle

需求在模型无需虚构重要用户意图，就能构造一个连贯、可执行、可验证的 Blueprint 时即为 ready。

缺失事实只有在不同合理答案会实质改变用户可见能力、必要资源边界或执行契约时才构成阻塞。如果缺失选择只影响实现细节，且存在安全、可逆的默认值，应采用默认值并继续。

模型每轮静默判断：

1. 是否仍有重要用户意图未知；
2. 继续执行是否必须虚构该意图。

只有两者均为 yes 时才返回 `needs_clarification`，且只询问一个最阻塞的业务事实。否则返回 `ready`。输入、输出、资源和外部依赖是需要结合完整上下文考虑的事实，不是固定触发器；不得使用缺字段计数、固定维度列表或问题模板库控制状态。

不得要求用户批准 Blueprint、Interface Plan、Graph、endpoint mapping 或其他 Creator 内部表示来推进流水线。已经确认的事实不得重复询问。

### 1.3 安全默认值

可以安全默认的实现选择包括 Creator 沙盒位置、按真实责任边界规划文件、优先可执行与可验证的实现，以及将运行时产物写入 `OUTPUT_DIR` 并通过 stdout JSON 返回。默认值不得覆盖用户已经确认的业务事实。

### 脚本数量、上传文件和工具选择规则

#### FunctionItem Input Contract

Every declared FunctionItem input represents one distinct receiving slot.
Inputs are conjunctive by default: when two inputs both have
`runtime_source_required=true`, runtime must be able to supply both. Do not
declare aliases, alternative names, fallback forms, or two expressions of one
runtime value as separate required inputs. Choose one canonical logical input.
Declare several required inputs only for genuinely distinct runtime values.

Do not emit placeholder resources, example assets, example references, example
tools, or example files as actual Blueprint content. Instruction examples are
explanatory only. Keep an empty resource category empty, or omit it only when
the protocol permits omission.

- 文件数量只在蓝图阶段确定；蓝图通过后，不再新增、删除、拆分或合并脚本文件。

- 脚本数量必须由任务复杂度和真实责任边界决定。脚本更少和脚本更多都不是优化目标。

- 简单任务如果只有一个核心责任闭包，局部步骤高度耦合并共享主要输入输出边界，可以使用 1 个脚本。

- 中等或复杂任务如果存在多个清晰独立责任闭包，通常规划 2～3 个脚本。2～3 是常见结果，不是固定数量或硬上限。

- 只有存在更多真实、独立、可执行且可验证的责任边界时，才继续增加脚本数量。如果 2～3 个职责清晰的脚本已经能够完整表达工作流，不要继续细碎拆分。

- 在确定 scripts/* 文件前，先识别工作流中的可执行责任。判断每项责任消费什么输入或前序结果、产生什么后续可消费结果、执行什么核心业务动作、需要什么能力、是否形成 producer/consumer handoff，以及是否具有独立验证和局部修复价值。

- 多项责任只有在执行逻辑高度耦合、共享同一核心输入输出边界，并且合并后仍能形成单一清晰文件职责时，才合并到同一个脚本。

- 存在明确前序结果被后续责任消费、不同核心能力边界、独立 artifact 生产或构建责任、独立可验证阶段或独立失败修复边界时，应认真考虑形成独立脚本职责。

- 不要为了减少文件数量，把多个独立业务责任压缩进一个 composite_generator 或 generic_script。

- 不按自然语言步骤数量机械拆脚本。纯局部字段适配、数据整理、格式转换和只服务于当前责任的 deterministic helper 逻辑可以保留在所属脚本内部。

- 每个 script 必须具有一个清晰主要业务职责。purpose 应说明当前文件消费什么语义输入、真正执行什么核心动作、交付什么业务结果。

- 多脚本之间不得重复拥有同一个核心业务责任。上游已经负责产生某项业务结果时，下游应消费该结果完成自己的职责，不应再次实现上游核心动作。

- 上游 outputs 与下游 inputs 应在语义上可追踪，但字段名不要求逐字一致；真实运行映射由第二轮 E2E 验证和修复。

- 当前 script 内部产生且只在当前 script 内部消费的中间值，不得提升为该 script 的 required external input。

- 当前平台没有显式 loop/map/foreach 节点；批量处理、逐项处理、顺序映射或局部聚合由拥有该业务责任的脚本内部承担。内部循环本身不构成拆脚本理由，也不改变 script boundary IO cardinality。

- `uploaded_files` 是 Creator 创建阶段上下文文件，不等于 Skill assets。上传文件必须先判断是参考文件、运行时输入文件，还是静态 assets 候选。没有用户明确确认，不得写入 `assets/**`。

- Blueprint 只声明脚本所需的抽象能力、必须由运行时工具实现的能力约束，以及用户明确指定且不可替换的外部依赖。普通场景不得提前绑定 `selected_tools` 或具体 helper；最终绑定归后续 Tool Planner。reference 文件和 asset 文件不得声明运行时工具能力。

- 上传图片需要理解内容时，应使用 `vision_understanding`，不要要求用户手动描述图片，不要把图像理解误当成图像生成，不要把上传图片默认加入 assets。
### 1.4 澄清原则

决定当前缺失信息是否会阻止构造一个有效、可执行、可验证的 Blueprint。只有缺失的业务事实确实阻止该构造时，才向用户提问。

可以通过安全、可逆默认值解决的实现选择，不是澄清阻塞项。每次只询问一个最阻塞、尚未解决的业务事实。不得再次询问对话中已经确认的信息。

不得仅为了推进流水线而要求用户批准内部 Blueprint、Interface Plan、Graph、endpoint mapping 或其他 Creator 内部表示。固定问题模板只能作为措辞示例，绝不能形成“缺失维度 → 固定触发问题”的决策表；是否需要询问必须根据完整上下文进行语义判断。

### 2.0 status=ready 的必要条件

1. `internal_blueprint_text` 已生成。
2. `internal_blueprint_text` 能被 `analyze_blueprint(strict=True)` 解析。
3. SkillPlan 中每个 path 都是具体文件路径。
4. 目录结构和 SkillPlan 文件计划不冲突。
5. assets 只表示 Creator 创建阶段的静态素材，不表示运行时用户输入。
6. 运行时产物只出现在脚本 outputs/stdout JSON/file_outputs 中。
7. `review_summary.files_to_create_or_update` 能由最终 `plan.files` 回填。
8. `review_summary.assets_to_upload` 只包含 Creator 静态素材上传需求，不包含运行时输入文件。
9. 如果上一轮用户选择“有，我补充说明”，但尚未给出补充内容，不得 ready。

### 2.1 生成内部蓝图

内部蓝图必须保持以下 Creator 平台硬协议，以便后端复用 `analyze_blueprint` 生成 creation plan：

```markdown
## 📋 Skill 架构蓝图
### 基本信息
- **Skill 名称**: [小写字母+数字+连字符，如 my-skill]
### I/O 契约
- **输入**: [明确的输入格式]
- **输出**: [明确的输出标准]
- **触发词**: [用户说什么话会触发此 Skill]

### 目录结构
[当前 Skill 根目录]
├── SKILL.md
├── scripts/      [如需要]
├── references/   [如需要]
└── assets/       [仅静态素材需要时才出现]

### 工作流逻辑
1. [步骤1]
2. [步骤2]
...

### SkillPlan / 文件职责计划
> 每一个将被 Creator 创建的文件都必须在这里显式声明职责合同；scripts/ 文件必须选择一个 role，不要留空。
> `required_capabilities` / `forbidden_capabilities` 必须由模型基于当前文件的真实运行需求显式声明；不要因为相邻概念、全局描述、文件名或业务描述自动扩展能力。后端只校验显式能力是否在当前 role 边界内，不会用业务词补 capability；资源文件（`SKILL.md`、`references/*.md`、`assets/*`）不声明 runtime capabilities。helper_required 能力必须调用平台 runtime helper；helper_preferred/self_implementation_allowed 能力可按 Tool Registry 指引使用 helper 或自实现。
> 文件计划只包含 Creator 需要创建或上传的源文件：`SKILL.md`、`scripts/*`、`references/*`、`assets/*` 静态素材。目录结构只展示目录级结构，不要列具体 `scripts/*`、`references/*`、`assets/*` 文件名；具体文件只能出现在 SkillPlan / 文件职责计划中。如果目录结构中列出了具体文件，则必须与 SkillPlan path 完全一致，否则视为 invalid blueprint。`assets/*` 必须显式声明 `source: user_upload` 或 `source: bundled`；如不需要 assets，不要输出任何 assets path。用户运行 Skill 时上传或粘贴的输入文件不属于 Creator assets，应写入 I/O 契约和脚本 inputs。脚本运行后生成的 PDF/DOCX/PPTX/图片/JSON/中间文件/最终结果不得写入目录结构或 `assets/*` 文件计划；它们只能写在对应脚本的 `outputs`、stdout JSON schema、`file_paths` / `file_outputs` 中。`dependencies` 只能表示运行前要读取的输入依赖，不得填写输出目录、最终产物目录、动态文件名或脚本运行后才生成的文件。最终文件产物应由脚本运行时写入 `OUTPUT_DIR` 并通过 stdout JSON 返回路径。
> `inputs` / `outputs` 必须是确定字段名列表，不要写候选字段、别名字段或组合表达；若存在多种可能，请先选定一个字段名。蓝图第一轮只检查文件边界、role/capability、安全边界、命令块基础格式和 JSON argv 可解析性，不在蓝图阶段要求脚本 output 必须被后续 input 静态同名消费；内部字段流转由第二轮 E2E 真实执行验证。
> 下方 `scripts/<name>.py` 只表示一个 script responsibility entry 的字段结构，不表示默认只创建一个脚本。必须先根据任务复杂度和责任边界确定 script topology，再为每个独立脚本职责重复声明完整 entry。简单任务可以只有 1 个 script；中等或复杂任务通常形成 2～3 个清晰责任 script；不得为了套用单个示例而把多个独立责任压缩进一个文件，也不得为了模块化而机械拆分。

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
  inputs: [列出确定的 JSON argv/stdin 字段；不要使用候选/别名/组合写法]
  outputs: [列出确定的 stdout JSON 字段；文件产物路径只在这里表达，运行时中间数据和最终产物不要列入资源清单]
  dependencies: [需要读取的 references/assets 静态输入路径；不要写 outputs/、OUTPUT_DIR、最终产物或动态文件名]
  required_capabilities: [text_generation | image_generation | vision_understanding |
                          pdf_generation | docx_generation | pptx_generation |
                          pdf_parsing | docx_parsing | pptx_parsing |
                          web_search | database_read |
                          wechat_draft | wechat_publish | deterministic_execution | file_output]
  forbidden_capabilities: [列出当前 role 边界内必须禁止的 runtime capability；不要用业务描述自动补充]
  references: [需要引用的 references/*.md]
- path: `references/<name>.md`
  role: reference
  inputs: []
  outputs: [reference_metadata, reference_body]
  dependencies: []
  required_capabilities: []
  forbidden_capabilities: [runtime_execution, image_generation]
  references: []
只有确实需要 Creator 静态素材时，才添加 assets 文件计划。assets 文件计划必须满足：

- path 必须是具体文件路径，例如 `assets/template.docx`。
- path 不能是 `assets/`、`assets/<name.ext>`、`assets/*`、`assets/{name}.ext` 或任何动态占位符。
- source 必须是 `user_upload` 或 `bundled`。
- inputs / outputs / dependencies 必须为空。
- required_capabilities 必须为空。
- forbidden_capabilities 至少包含 `runtime_execution`。
- 运行时用户输入文件不属于 assets。
- 运行时生成产物不属于 assets。
- 如果不需要静态素材，不要输出任何 assets path。

### 宿主执行方式
- **直接回答**: [哪些请求由模型直接生成文本/Markdown]
- **需要脚本/命令**: assistant 必须在 Sandbox 当轮回复中输出标准 Markdown fenced code block（如 ```bash ... ```），宿主只执行当轮回复中出现的 block。脚本命令必须使用标准 JSON object argv，不要生成位置参数命令说明；JSON argv 值中不得写 `{{input_files[0]}}`、`{{references/...}}`、`{{assets/...}}` 等复杂模板表达式。运行时输入文件使用 `__RUNTIME_INPUT_FILE__` / `__RUNTIME_INPUT_FILE_0__` 等安全占位符；reference/assets 文件使用普通相对路径字符串（如 `references/parse_rules.md`）；模型名称使用 `TEXT_MODEL`。每个核心命令附近必须写 **argv JSON contract**，声明每个 argv 字段的 type、source、placeholder/path、required。
- **禁止隐式执行**: 不要把行内脚本路径或“立即调用脚本”的自然语言当成执行触发器；脚本存在只代表可用资源和安全校验条件。只有在运行时当轮回复中输出标准 ```bash fenced code block，宿主才会解析并执行命令。
- **执行后回答**: assistant 必须等待宿主返回 stdout/stderr/observation 后，再基于 observation 生成最终回答。最终 SKILL.md 只描述运行时触发、命令、observation 消费和结果返回，不得包含“输出蓝图等待确认”“用户确认后开始创建文件”等 Creator 创建阶段动作。

### 资源清单
- [ ] [仅列静态 references 和 Creator 创建阶段静态 assets；不要列运行时输入、运行时中间数据或最终生成产物]
```

### 2.2 用户侧展示

不要要求用户阅读完整蓝图，也不要把固定确认语作为进入生成的条件。用户侧只展示“创建要点摘要”，包括：
- 目标
- 输入
- 输出
- 工作流步骤
- 将创建/更新的文件
- 需要上传的素材（`review_summary.assets_to_upload` 只表示 Creator 创建阶段必须上传的静态 assets；运行 Skill 时用户上传的输入文件不得写入；如果没有 Creator 静态素材上传需求，必须为空数组）

用户可以直接点击“开始生成”，也可以输入修改意见重新准备 plan。

**Phase 2 完成标志**：已生成可被 `analyze_blueprint` 解析的内部蓝图和用户可读创建要点摘要。

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
    └── assets/           - 静态上传/预置素材 (模板、图标、字体等；不放运行时输出)
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
9. **不要生成假实现**：脚本必须有真实可执行逻辑；模型、网络、外部副作用、文件产物等能力只能在当前脚本 SkillPlan 显式声明且 Tool Registry 允许时使用。具体 helper、环境变量、返回结构以 Tool Registry snippets/function cards 为准；不得用固定模板、随机词表、placeholder、mock API 或空文件冒充真实能力。

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
- 素材：`assets/` 仅接收用户已有/上传的静态素材；不要让模型创建运行时生成结果或最终产物到 assets。

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

## Creator Tool Pool and Runtime Helper Gate

- Creator maintains a Skill-level tool pool for generated scripts; blueprint planning may declare capabilities/tool slots, but must not declare concrete runtime helper names.
- Runtime helper names are selected by the backend registry and tool gate, then bound per script file in the current file tool binding.
- Generated scripts may import only helpers listed in the current file binding's `allowed_helper_imports`; `backend.services.runtime_tools` is not an open namespace.
- If a script needs a capability outside the current binding, the model must request a tool-pool addition; the request must pass the backend gate before code imports the helper.
- Gate-denied tools/helpers must be treated as unavailable and must not be reintroduced by generation, repair, or E2E.
- Do not invent runtime helper names such as `read_pdf_text`, `read_xlsx_text`, `read_txt_text`, or `read_excel_text`.
- Multi-format text ingestion should prefer the gated `read_file_text` helper when it is present in the current file binding.
- Reference and asset files are resources only; they must not declare or use runtime tools.
