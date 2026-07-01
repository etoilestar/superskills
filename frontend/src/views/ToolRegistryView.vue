<template>
  <div class="tool-registry page-scroll">
    <header class="hero">
      <div>
        <p class="eyebrow">Creator Tool Registry</p>
        <h1>在线工具制作 / 注册</h1>
        <p class="muted">主工作台只负责制作新工具；已注册工具和 Snippet 管理放到右侧抽屉。</p>
      </div>
      <div class="hero-actions">
        <button class="btn-ghost" @click="loadTools">刷新</button>
        <button class="btn-primary" @click="registryDrawerOpen = true">管理已注册工具</button>
      </div>
    </header>

    <div class="tool-workspace">
      <aside class="step-sidebar" aria-label="Tool authoring steps">
        <button v-for="step in steps" :key="step.key" type="button" class="step-nav" :class="{ active: activeStep === step.key }" @click="activeStep = step.key">
          <span class="step-index">{{ step.index }}</span>
          <span class="step-meta"><strong>{{ step.title }}</strong><small>{{ step.description }}</small></span>
          <span v-if="stepStatus(step.key)" class="status-dot" :class="stepStatus(step.key)"></span>
        </button>
      </aside>

      <main class="step-main">
        <section v-show="activeStep === 'input'" class="workspace-card compact-card">
          <div class="card-heading">
            <p class="eyebrow">Step 1</p>
            <h2>描述工具能力</h2>
            <p class="muted small">一句话描述即可；已有代码时展开下方可选区域粘贴。</p>
          </div>

          <SmartCodeEditor v-model="form.description" language="markdown" density="compact" :toolbar="false" min-height="120px" max-height="220px" placeholder="例如：帮我做一个把 markdown 保存为 PDF 并返回路径的小工具" />

          <CollapsiblePanel v-model:open="codeInputOpen" title="可选：已有 Python 代码" description="已有实现时再展开粘贴；planner 会优先从代码推断 IO">
            <SmartCodeEditor v-model="optionalCodeBlock" language="python" min-height="260px" max-height="520px" placeholder="def save_markdown(payload): ..." />
          </CollapsiblePanel>

          <CollapsiblePanel v-model:open="advancedOpen" title="专家模式（可选）" description="通常不用填写；IO 会由 planner、live test 和 code_model 推断" :badge="advancedBadge">
            <div class="form-row relaxed">
              <label>工具名称<input v-model="form.tool_name" placeholder="可选，如 markdown_to_pdf" /></label>
              <label>工具类型<select v-model="form.tool_type"><option v-for="type in toolTypes" :key="type" :value="type">{{ type }}</option></select></label>
              <label>允许角色<input v-model="allowedRolesText" placeholder="可选，逗号分隔" /></label>
            </div>
            <div class="checks">
              <label><input v-model="form.needs_secret" type="checkbox" /> 需要密钥</label>
              <label><input v-model="form.needs_external_network" type="checkbox" /> 需要外部网络</label>
              <label><input v-model="form.generates_file" type="checkbox" /> 生成文件</label>
              <label><input v-model="form.high_risk" type="checkbox" /> 高风险</label>
            </div>
            <CollapsiblePanel v-model:open="expertIoOpen" title="专家 IO 提示（默认无需填写）" description="普通流程会自动推断输入输出，最终在 Snippet 阶段确认">
              <div class="form-row relaxed">
                <label>输入描述<SmartCodeEditor v-model="form.input_description" language="text" density="compact" :toolbar="false" min-height="80px" max-height="160px" /></label>
                <label>输出描述<SmartCodeEditor v-model="form.output_description" language="text" density="compact" :toolbar="false" min-height="80px" max-height="160px" /></label>
              </div>
            </CollapsiblePanel>
          </CollapsiblePanel>

          <div class="step-actions">
            <button class="btn-ghost" :disabled="busy" @click="draftManifest">旧版规则草稿</button>
            <button class="btn-primary" :disabled="busy" @click="authorDraft">开始通用 Authoring 流程</button>
          </div>
        </section>

        <section v-show="activeStep === 'planner'" class="workspace-card editor-card">
          <div class="card-heading with-actions">
            <div>
              <p class="eyebrow">Step 2</p>
              <h2>Planner / Manifest</h2>
              <p class="muted small">检查 planner 推断的 manifest、输入输出 schema 和安全声明。</p>
            </div>
            <div class="heading-actions">
              <button class="btn-ghost" @click="activeStep = 'input'">返回需求</button>
              <button class="btn-primary" :disabled="!canGenerate" @click="generateAdapter">生成工具代码</button>
            </div>
          </div>
          <div v-if="statusMessage" class="compact-preview">{{ statusMessage }}</div>
          <div v-if="clarificationQuestions.length" class="validation bad clarify-card">
            <strong>还需要确认：</strong>
            <div v-for="(question, idx) in clarificationQuestions" :key="question" class="clarify-question">
              <p>{{ idx + 1 }}. {{ questionText(question) }}</p>
              <div v-if="questionOptions(question).length" class="choice-row">
                <button v-for="option in questionOptions(question)" :key="optionValue(option)" type="button" class="btn-ghost" :class="{ selected: clarificationAnswers[idx] === optionValue(option) }" @click="clarificationAnswers[idx] = optionValue(option)">{{ optionLabel(option) }}</button>
              </div>
              <input v-else v-model="clarificationAnswers[idx]" placeholder="一句话补充即可" />
            </div>
            <div class="actions"><button class="btn-primary" :disabled="busy" @click="continuePlanning">继续规划</button></div>
          </div>

          <div v-if="requiresConfig && !clarificationQuestions.length" class="auth-summary compact-preview">
            <div>
              <strong>需要授权配置</strong>
              <p class="muted small">Planner 已预填连接入口（{{ entrypointConfidenceLabel }}）。请打开配置确认地址并输入密钥值。</p>
              <small>服务地址：{{ configForm.base_url || '待填写' }} · 认证方式：{{ configForm.auth_type || 'none' }} · 密钥名：{{ configForm.secret_env || '无' }}</small>
            </div>
            <div class="actions">
              <button class="btn-primary" @click="authConfigOpen = true">打开配置</button>
              <button class="btn-ghost" :disabled="busy" @click="runLiveTest">测试连接</button>
            </div>
          </div>

          <div
              v-if="!clarificationQuestions.length"
              class="manual-auth-panel compact-preview"
              :class="{ 'manual-active': manualAuthOverride.mode !== 'auto' }"
          >
              <div>
                <strong>人工认证判定</strong>
                <p class="muted small">
                  当前模式：{{ manualAuthModeLabel }}。用于修正 planner / 系统误判，只影响认证 gate，不绕过代码安全校验。
                </p>
                <small v-if="manualAuthOverride.reason">
                  原因：{{ manualAuthOverride.reason }}
                </small>
              </div>

              <div class="manual-auth-actions">
                <button
                  class="btn-primary"
                  type="button"
                  :disabled="busy"
                  @click="forceRequireAuth"
                >
                  + 手动添加认证配置
                </button>

                <button
                  class="btn-ghost"
                  type="button"
                  :disabled="busy"
                  @click="forceNoAuth"
                >
                  - 手动删除认证要求
                </button>

                <button
                  v-if="manualAuthOverride.mode !== 'auto'"
                  class="btn-ghost"
                  type="button"
                  :disabled="busy"
                  @click="clearManualAuthOverride"
                >
                  恢复自动判断
                </button>
              </div>
          </div>

          <div v-if="liveTestRunning || liveTestResult || liveTestError" ref="liveTestResultRef" class="validation live-test-card" :class="liveTestResult?.success ? 'ok' : liveTestError || liveTestResult ? 'bad' : ''">
            <div class="live-test-heading">
              <strong>{{ liveTestRunning ? '正在测试连接...' : liveTestResult?.success ? 'live_test 成功' : 'live_test 失败' }}</strong>
              <small v-if="liveTestHistory.length">历史 {{ liveTestHistory.length }} 次</small>
            </div>
            <p v-if="liveTestError" class="error small">{{ liveTestError }}</p>
            <div v-if="liveTestResult" class="live-test-grid">
              <span><strong>status</strong>{{ liveTestResult.status || (liveTestResult.success ? 'ok' : 'failed') }}</span>
              <span v-if="liveTestResult.status_code"><strong>status_code</strong>{{ liveTestResult.status_code }}</span>
              <span v-if="liveTestResult.content_type"><strong>content_type</strong>{{ liveTestResult.content_type }}</span>
              <span v-if="liveTestResult.request_preview?.header_keys?.length"><strong>header_keys</strong>{{ liveTestResult.request_preview.header_keys.join(', ') }}</span>
              <span v-if="liveTestResult.missing_env?.length"><strong>missing_env</strong>{{ liveTestResult.missing_env.join(', ') }}</span>
            </div>
            <div v-if="liveTestResult?.errors?.length" class="live-test-section"><strong>errors</strong><pre class="tool-card compact-json">{{ stringifyPretty(liveTestResult.errors) }}</pre></div>
            <div v-if="liveTestResult?.request_preview" class="live-test-section"><strong>request_preview</strong><pre class="tool-card compact-json">{{ stringifyPretty(liveTestResult.request_preview) }}</pre></div>
            <div v-if="liveTestResult?.normalized_preview" class="live-test-section"><strong>normalized_preview</strong><pre class="tool-card compact-json">{{ stringifyPretty(liveTestResult.normalized_preview) }}</pre></div>
            <div v-else-if="liveTestResult?.preview" class="live-test-section"><strong>preview</strong><pre class="tool-card compact-json">{{ stringifyPretty(liveTestResult.preview) }}</pre></div>
          </div>

          <div v-if="liveTestResult" class="validation" :class="liveTestResult.success ? 'ok' : 'bad'">
            <strong>{{ liveTestResult.success ? 'live_test 成功' : 'live_test 失败' }}</strong>
            <pre class="tool-card">{{ JSON.stringify(liveTestResult.normalized_preview || liveTestResult.preview || liveTestResult.errors, null, 2) }}</pre>
          </div>

          <div class="stream-status">
            <div><strong>生成进度 / 日志</strong><small>长耗时步骤会持续写入事件。</small></div>
            <button v-if="busy" class="btn-ghost" @click="cancelAuthoring">取消当前生成</button>
          </div>
          <pre v-if="authorLogs.length" class="tool-card log-panel">{{ authorLogs.map(item => `${item.time} ${item.event || ''} ${item.step || ''} ${item.message || item.summary || ''}`).join('\n') }}</pre>
          <SmartCodeEditor v-model="manifestText" language="json" fill placeholder="Planner 生成的 manifest JSON" />
        </section>

        <section v-show="activeStep === 'adapter'" class="workspace-card editor-card">
          <div class="card-heading with-actions">
            <div>
              <p class="eyebrow">Step 3</p>
              <h2>Adapter 实现</h2>
              <p class="muted small">确认或编辑最终 Python adapter。Finalize 会重新跑 dynamic trial，通过后才生成 snippet。</p>
            </div>
            <div class="heading-actions">
              <button class="btn-ghost" :disabled="busy || !canGenerate" @click="generateAdapter">重新生成实现</button>
              <button
                  class="btn-primary"
                  :disabled="busy || !parsedManifest || !effectiveRuntimeCode"
                  @click="activeStep = 'validation'"
              >
                  确认代码 → 进入试运行
              </button>
            </div>
          </div>
          <div class="adapter-edit-layout">
              <div class="pane model-code-pane">
                <div class="section-title row-title">
                  <div>
                    <h3>对外发布函数核心实现</h3>
                    <small>这里只展示工具对外暴露的核心函数实现；内部 run / runner / 临时环境在下方折叠框查看。</small>
                  </div>
                </div>

                <SmartCodeEditor
                  v-model="adapterCode"
                  language="python"
                  density="compact"
                  min-height="420px"
                  max-height="680px"
                  placeholder="def your_tool_name(payload: dict, config: dict | None = None) -> dict:"
                />
              </div>

              <CollapsiblePanel
                v-if="runtimeCode"
                title="完整内部实现代码（注册 / 验证使用）"
                description="包含 run(payload, config=None)、内部 helper 和完整可执行实现；默认折叠，不作为主框展示。"
              >
                <SmartCodeEditor
                  v-model="runtimeCode"
                  language="python"
                  density="compact"
                  min-height="360px"
                  max-height="680px"
                />
              </CollapsiblePanel>

              <CollapsiblePanel
                v-for="panel in visibleDebugPanels"
                :key="panel.id"
                :title="panel.title"
                :description="panel.description"
              >
                <SmartCodeEditor
                  v-if="panel.language === 'python' || panel.language === 'code'"
                  :model-value="String(panel.content || '')"
                  language="python"
                  density="compact"
                  :toolbar="false"
                  min-height="240px"
                  max-height="520px"
                  readonly
                />

                <SmartCodeEditor
                  v-else-if="panel.language === 'json'"
                  :model-value="stringifyPretty(panel.content)"
                  language="json"
                  density="compact"
                  :toolbar="false"
                  min-height="220px"
                  max-height="420px"
                  readonly
                />

                <pre v-else class="tool-card adapter-fixed-code">{{ stringifyPretty(panel.content) }}</pre>
              </CollapsiblePanel>
          </div>
        </section>

        <section v-show="activeStep === 'validation'" class="workspace-card compact-card">
          <div class="card-heading">
            <p class="eyebrow">Step 4</p>
            <h2>验证</h2>
            <p class="muted small">上方准备 sample input，下方查看验证摘要；调试信息默认折叠。</p>
          </div>
          <div class="split-layout">
            <div class="pane">
              <h3>Sample Input</h3>
              <SmartCodeEditor v-model="sampleInputText" language="json" density="compact" min-height="180px" max-height="320px" />
              <p v-if="sampleInputParseWarning" class="warn small">
                {{ sampleInputParseWarning }}
              </p>
              <div class="actions"><button class="btn-primary" :disabled="busy || !parsedManifest" @click="validateTool">运行验证</button></div>
            </div>
            <div class="pane">
              <h3>验证结果</h3>
              <div v-if="lastValidation" class="validation" :class="lastValidation.success ? 'ok' : 'bad'">
                <strong>{{ lastValidation.success ? '验证通过' : '验证失败' }}</strong>
                <ul><li v-for="err in lastValidation.errors" :key="err">{{ err }}</li></ul>
                <p v-for="warn in lastValidation.warnings" :key="warn" class="warn">{{ warn }}</p>
              </div>
              <p v-else class="muted small">还没有运行验证。</p>
              <div v-if="snippetReady" class="compact-preview">Snippet 已生成，请到「5 Snippet」确认后注册。</div>
            </div>
            <div class="pane feedback-pane">
              <h3>人工反馈修改</h3>
              <p class="muted small">
                试运行后，把不符合预期的地方写在这里。系统会把当前代码、manifest、sample input、验证结果和你的反馈一起交给 code_model 修改。
              </p>

              <SmartCodeEditor
                v-model="humanFeedbackText"
                language="markdown"
                density="compact"
                :toolbar="false"
                min-height="140px"
                max-height="260px"
                placeholder="例如：抓取结果包含导航栏和页脚，希望只保留正文；output_format=markdown 时应该返回 markdown；失败时不要抛异常，返回 success=false 和 error。"
              />

              <div class="actions">
                <button
                  class="btn-primary"
                  :disabled="busy || reviseRunning || !humanFeedbackText.trim() || !effectiveRuntimeCode"
                  @click="reviseWithFeedback"
                >
                  根据反馈修改代码
                </button>
              </div>

              <div v-if="revisionHistory.length" class="revision-history">
                <strong>修改历史</strong>
                <details
                  v-for="(item, idx) in revisionHistory"
                  :key="item.time"
                  class="adapter-fixed-block"
                >
                  <summary>
                    <strong>第 {{ revisionHistory.length - idx }} 次反馈</strong>
                    <small>{{ item.time }}</small>
                  </summary>
                  <pre class="tool-card">{{ item.feedback }}</pre>
                </details>
              </div>
            </div>
          </div>
          <CollapsiblePanel v-model:open="debugOpen" title="调试信息 / 调用链 / Function card preview" description="包含临时环境、完整运行代码、runner 调用、stdout/stderr 和模型注入预览">
              <div v-if="visibleDebugPanels.length" class="debug-panel-list">
                <CollapsiblePanel
                  v-for="panel in visibleDebugPanels"
                  :key="`validation_${panel.id}`"
                  :title="panel.title"
                  :description="panel.description"
                >
                  <SmartCodeEditor
                    v-if="panel.language === 'python' || panel.language === 'code'"
                    :model-value="String(panel.content || '')"
                    language="python"
                    density="compact"
                    :toolbar="false"
                    min-height="220px"
                    max-height="480px"
                    readonly
                  />

                  <SmartCodeEditor
                    v-else-if="panel.language === 'json'"
                    :model-value="stringifyPretty(panel.content)"
                    language="json"
                    density="compact"
                    :toolbar="false"
                    min-height="220px"
                    max-height="420px"
                    readonly
                  />

                  <pre v-else class="tool-card">{{ stringifyPretty(panel.content) }}</pre>
                </CollapsiblePanel>
              </div>

              <pre class="tool-card">{{ cardPreview }}</pre>
          </CollapsiblePanel>
          <div class="step-actions">
            <button class="btn-ghost" @click="activeStep = 'adapter'">返回 Adapter</button>
            <button
              class="btn-primary"
              :disabled="!canFinalizeAuthoring"
              @click="finalizeAuthoring"
            >
              试运行满意 → 生成 Snippet
            </button>
          </div>
        </section>

        <section v-show="activeStep === 'snippet'" class="workspace-card editor-card">
          <div class="card-heading with-actions">
            <div>
              <p class="eyebrow">Step 5</p>
              <h2>Snippet 确认</h2>
              <p class="muted small">这是唯一注册用 snippet 编辑入口；注册前会从当前内容覆盖 manifest.snippets。</p>
            </div>
            <div class="heading-actions">
              <button class="btn-ghost" @click="activeStep = 'validation'">返回验证</button>
              <button
                  class="btn-primary"
                  :disabled="!canRegister"
                  @click="registerTool"
                >
                  使用当前 snippet 注册工具
              </button>
            </div>
          </div>
          <p v-if="registerBlockingReason" class="error small">
            {{ registerBlockingReason }}
          </p>
          <SmartCodeEditor v-model="snippetText" language="json" fill placeholder="确认代码后生成 snippet" />
        </section>
      </main>
    </div>

    <SideDrawer v-model:open="registryDrawerOpen" title="已注册工具 / Snippets" description="查看工具列表，并管理已注册工具的额外 Tool Usage Snippets" eyebrow="Registry">
      <section class="drawer-section">
        <div class="drawer-heading">
          <h3>已注册工具</h3>
          <button class="btn-ghost" @click="loadTools">刷新</button>
        </div>
        <div class="tool-list">
          <article v-for="tool in tools" :key="tool.name" class="tool-list-item">
            <div class="tool-list-main"><strong>{{ tool.name }}</strong><small>{{ tool.display_name }}</small></div>
            <div class="tool-list-meta">
              <span class="pill">{{ tool.usage_policy }}</span>
              <span class="pill" :class="tool.creator_available ? 'ok' : 'muted'">{{ tool.creator_available ? 'enabled' : 'disabled' }}</span>
            </div>
            <small class="tool-functions">{{ (tool.functions || []).map(fn => fn.function_name).join(', ') || tool.helper_imports?.join(', ') }}</small>
          </article>
        </div>
      </section>

      <CollapsiblePanel v-model:open="snippetManagerOpen" title="Snippet Manager" description="管理模型会看到的 import、最小调用、返回规则和反例" :badge="snippets.length">
        <div class="drawer-snippet-layout">
          <div class="step-card">
            <div class="form-row relaxed">
              <label>选择工具<select v-model="selectedToolName" @change="loadSnippets"><option value="">选择工具</option><option v-for="tool in tools" :key="tool.name" :value="tool.name">{{ tool.name }}</option></select></label>
              <label>Snippet 类型<select v-model="snippetForm.kind"><option v-for="kind in snippetKinds" :key="kind" :value="kind">{{ kind }}</option></select></label>
            </div>
            <div class="form-row relaxed">
              <label>ID<input v-model="snippetForm.id" placeholder="create_pdf.minimal_text_pdf" /></label>
              <label>标题<input v-model="snippetForm.title" placeholder="Create a simple PDF" /></label>
            </div>
            <label>适用 roles（逗号分隔）<input v-model="snippetRolesText" placeholder="pdf_builder,document_generator" /></label>
            <label>适用 capabilities（逗号分隔）<input v-model="snippetCapabilitiesText" placeholder="pdf_generation" /></label>
            <label>failure layers（逗号分隔）<input v-model="snippetFailuresText" placeholder="final_platform_output_value_invalid,artifact_missing" /></label>
            <label>描述<SmartCodeEditor v-model="snippetForm.description" language="markdown" density="compact" :toolbar="false" min-height="80px" /></label>
            <label>正确调用代码<SmartCodeEditor v-model="snippetForm.code" language="python" density="compact" min-height="180px" max-height="320px" /></label>
            <div class="form-row relaxed">
              <label>期望输入 shape(JSON)<SmartCodeEditor v-model="snippetInputShapeText" language="json" density="compact" min-height="140px" max-height="260px" /></label>
              <label>期望输出 shape(JSON)<SmartCodeEditor v-model="snippetOutputShapeText" language="json" density="compact" min-height="140px" max-height="260px" /></label>
            </div>
            <label>return rule<SmartCodeEditor v-model="snippetForm.return_rule" language="text" density="compact" :toolbar="false" min-height="80px" /></label>
            <label>anti patterns（每行一条）<SmartCodeEditor v-model="snippetAntiPatternsText" language="text" density="compact" :toolbar="false" min-height="100px" /></label>
            <div class="form-row relaxed">
              <label>usage policy<select v-model="snippetForm.usage_policy"><option>helper_preferred</option><option>helper_required</option><option>self_implementation_allowed</option></select></label>
              <label>priority<input v-model.number="snippetForm.priority" type="number" /></label>
            </div>
            <div class="actions">
              <button class="btn-primary" :disabled="busy || !selectedToolName" @click="saveSnippet">新增 / 保存 snippet</button>
              <button class="btn-ghost" :disabled="busy || !selectedToolName || !snippetForm.id" @click="runSnippetSmokeTest">运行 smoke test</button>
            </div>
            <p v-if="snippetTestResult" class="small" :class="snippetTestResult.success ? 'green' : 'error'">Smoke test: {{ snippetTestResult.success ? 'passed' : 'failed' }} {{ snippetTestResult.message || '' }}</p>
          </div>

          <div class="step-card">
            <div class="snippets-list">
              <button v-for="snippet in snippets" :key="snippet.id" class="snippet-item" @click="editSnippet(snippet)">
                <strong>{{ snippet.id }}</strong><small>{{ snippet.kind }} · priority {{ snippet.priority }}</small>
              </button>
            </div>
            <pre class="tool-card">{{ snippetPreview }}</pre>
          </div>
        </div>
      </CollapsiblePanel>
    </SideDrawer>

    <div v-if="authConfigOpen" class="modal-backdrop" @click.self="authConfigOpen = false">
      <section class="auth-modal">
        <div class="card-heading with-actions">
          <div>
            <p class="eyebrow">Authorization</p>
            <h2>连接配置</h2>
            <p class="muted small">模型已尽量预填 entrypoint；你只需确认或修改，并输入密钥值。</p>
          </div>
          <button class="btn-ghost" @click="authConfigOpen = false">关闭</button>
        </div>

        <div class="form-row relaxed">
          <label>
            服务地址 / IP / endpoint
            <input v-model="configForm.base_url" placeholder="模型预填或手动填写" />
          </label>

          <label>
            请求方式
            <select v-model="configForm.method">
              <option value="GET">GET</option>
              <option value="POST">POST</option>
              <option value="PUT">PUT</option>
              <option value="PATCH">PATCH</option>
              <option value="DELETE">DELETE</option>
            </select>
          </label>

          <label>
            认证方式
            <select v-model="configForm.auth_type">
              <option value="none">none</option>
              <option value="api_key">api_key</option>
              <option value="token">token</option>
              <option value="basic">basic</option>
              <option value="custom">custom</option>
              <option value="unknown">unknown</option>
              <option value="oauth2">oauth2</option>
            </select>
          </label>
        </div>
        <div v-if="configForm.auth_type !== 'none'" class="form-row relaxed">
          <label>密钥名称（env）<input v-model="configForm.secret_env" placeholder="XXX_API_KEY" /></label>
          <label>密钥值<input v-model="configForm.secret_value" type="password" autocomplete="off" placeholder="不会写入代码或日志" /></label>
        </div>
        <div v-if="configForm.auth_type === 'api_key'" class="form-row relaxed">
          <label>密钥放置位置
            <select v-model="configForm.auth_placement">
              <option value="header">Header</option>
              <option value="query">Query 参数</option>
              <option value="bearer">Authorization Bearer</option>
            </select>
          </label>
          <label v-if="configForm.auth_placement === 'header'">Header 名称<input v-model="configForm.auth_header_name" placeholder="X-API-KEY" /></label>
          <label v-if="configForm.auth_placement === 'query'">Query 参数名<input v-model="configForm.auth_query_param" placeholder="api_key" /></label>
        </div>
        <div v-if="dynamicConfigFields.length" class="dynamic-config-block">
          <div class="section-title">
            <strong>额外授权配置</strong>
            <small>由 planner 根据当前工具需求生成；不需要的字段可以留空。</small>
          </div>

          <div class="form-row relaxed">
            <label v-for="field in dynamicConfigFields" :key="field.name">
              {{ field.label || field.name }}

              <select
                v-if="field.type === 'select'"
                v-model="dynamicConfig[field.name]"
              >
                <option value="">请选择</option>
                <option
                  v-for="option in field.options || []"
                  :key="option.value || option.label || option"
                  :value="option.value || option.label || option"
                >
                  {{ option.label || option.value || option }}
                </option>
              </select>

              <input
                v-else
                v-model="dynamicConfig[field.name]"
                :type="field.secret || field.type === 'password' ? 'password' : 'text'"
                :placeholder="field.placeholder || ''"
                autocomplete="off"
              />

              <small v-if="field.description">{{ field.description }}</small>
            </label>
          </div>
        </div>

        <section class="auth-extra-panel">
          <div class="section-title">
            <strong>测试输入 / 高级配置</strong>
            <small>连接测试需要 query/q 等测试参数；其他特殊字段也可以在这里追加。</small>
          </div>

          <label>
            sample input（测试输入，JSON）
            <SmartCodeEditor
              v-model="sampleInputText"
              language="json"
              density="compact"
              min-height="120px"
              max-height="220px"
              placeholder='{"query":"测试内容"}'
            />
          </label>

          <label class="inline-check">
            <input v-model="allowExternalNetwork" type="checkbox" />
            允许本次连接测试访问外部网络
          </label>

          <div class="extra-fields">
            <div class="extra-field-row header">
              <span>字段名</span>
              <span>字段值</span>
              <span>敏感</span>
              <span></span>
            </div>

            <div
              v-for="(field, idx) in configExtraFields"
              :key="idx"
              class="extra-field-row"
            >
              <input v-model="field.key" placeholder="tenant_id / region / custom_header" />
              <input
                v-model="field.value"
                :type="field.sensitive ? 'password' : 'text'"
                placeholder="值"
              />
              <label class="inline-check">
                <input v-model="field.sensitive" type="checkbox" />
                是
              </label>
              <button class="btn-ghost" type="button" @click="removeExtraField(idx)">
                删除
              </button>
            </div>

            <button class="btn-ghost" type="button" @click="addExtraField">
              + 添加一项
            </button>
          </div>
        </section>

        <div v-if="configSaveResult" class="validation ok"><strong>配置已保存</strong><p class="small">env: {{ (configSaveResult.configured_env || []).join(', ') || '无' }} · secrets: {{ (configSaveResult.configured_secrets || []).join(', ') || '无' }}</p></div>
        <div v-if="liveTestRunning || liveTestResult || liveTestError" class="validation live-test-card" :class="liveTestResult?.success ? 'ok' : liveTestError || liveTestResult ? 'bad' : ''">
          <strong>{{ liveTestRunning ? '正在测试连接...' : liveTestResult?.success ? 'live_test 成功' : 'live_test 失败' }}</strong>
          <p v-if="liveTestError" class="error small">{{ liveTestError }}</p>
          <div v-if="liveTestResult?.request_preview" class="live-test-section"><strong>request_preview</strong><pre class="tool-card compact-json">{{ stringifyPretty(liveTestResult.request_preview) }}</pre></div>
          <div v-if="liveTestResult?.normalized_preview" class="live-test-section"><strong>normalized_preview</strong><pre class="tool-card compact-json">{{ stringifyPretty(liveTestResult.normalized_preview) }}</pre></div>
          <div v-if="liveTestResult?.errors?.length" class="live-test-section"><strong>errors</strong><pre class="tool-card compact-json">{{ stringifyPretty(liveTestResult.errors) }}</pre></div>
        </div>
        <div class="actions">
          <button class="btn-ghost" :disabled="busy" @click="saveConfigAndContinue">保存配置</button>
          <button class="btn-primary" :disabled="busy" @click="saveConfigAndRunLiveTest">保存并测试连接</button>
        </div>
      </section>
    </div>

    <p v-if="error" class="error">{{ error }}</p>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import CollapsiblePanel from '../components/CollapsiblePanel.vue'
import SideDrawer from '../components/SideDrawer.vue'
import SmartCodeEditor from '../components/SmartCodeEditor.vue'
import { authorCreatorTool, authorCreatorToolStream, createCreatorToolSnippet, draftCreatorTool, generateCreatorToolCode, listCreatorToolSnippets, listCreatorTools, liveTestCreatorTool, registerCreatorTool, testCreatorToolSnippet, updateCreatorToolSnippet, saveCreatorToolConfig, validateCreatorTool } from '../composables/useCreator.js'

const toolTypes = [
  'python_helper',
  'web_fetch',
  'http_api',
  'local_command',
  'database_query',
  'file_converter',
  'document_generator',
  'image_generator',
  'custom_adapter'
]
const snippetKinds = ['minimal_usage', 'multi_input_usage', 'file_output_usage', 'batch_usage', 'error_repair_usage', 'anti_pattern', 'trial_run_usage']
const steps = [
  { key: 'input', index: 1, title: '需求 / 代码', description: '描述能力' },
  { key: 'planner', index: 2, title: '澄清 / 配置', description: '补齐定义' },
  { key: 'adapter', index: 3, title: 'Adapter', description: '确认代码' },
  { key: 'validation', index: 4, title: '验证', description: '试运行' },
  { key: 'snippet', index: 5, title: 'Snippet', description: '确认用法' },
]
const busy = ref(false)
const error = ref('')
const tools = ref([])
const allowedRolesText = ref('')
const manifestText = ref('')
const adapterCode = ref('')
const runtimeCode = ref('')
const debugSections = ref([])
const toolContract = ref(null)
const optionalCodeBlock = ref('')
const snippetText = ref('{}')
const clarificationQuestions = ref([])
const clarificationAnswers = ref([])
const planState = ref({})
const manualAuthOverride = reactive({
  mode: 'auto', // auto | force_required | force_no_auth
  reason: '',
  source: 'user',
  updated_at: ''
})
const configForm = reactive({
  base_url: '',
  method: 'GET',
  auth_type: 'none',
  secret_env: '',
  secret_value: '',
  auth_placement: 'header',
  auth_header_name: 'X-API-KEY',
  auth_query_param: 'api_key'
})

const adapterSections = computed(() => {
  const code = adapterCode.value || ''
  const start = '# === MODEL_INTERNAL_CODE_START ==='
  const end = '# === MODEL_INTERNAL_CODE_END ==='

  if (!code.includes(start) || !code.includes(end)) {
    return {
      hasSections: false,
      wrapperBefore: code,
      internalCode: '',
      wrapperAfter: ''
    }
  }

  const [wrapperBefore, rest] = code.split(start)
  const [internalCode, wrapperAfter] = rest.split(end)

  return {
    hasSections: true,
    wrapperBefore: `${wrapperBefore}${start}`,
    internalCode: internalCode.trim(),
    wrapperAfter: `${end}${wrapperAfter}`
  }
})

const editableInternalCode = ref('')

function refreshEditableInternalCode() {
  editableInternalCode.value = adapterSections.value.internalCode || ''
}

function applyInternalCodeEdit() {
  const code = adapterCode.value || ''
  const start = '# === MODEL_INTERNAL_CODE_START ==='
  const end = '# === MODEL_INTERNAL_CODE_END ==='

  if (!code.includes(start) || !code.includes(end)) return

  const [before, rest] = code.split(start)
  const [, after] = rest.split(end)

  adapterCode.value = `${before}${start}\n${editableInternalCode.value.trim()}\n${end}${after}`
}

const dynamicConfig = reactive({})
const configExtraFields = ref([])
const configSaveResult = ref(null)
const liveTestRunning = ref(false)
const liveTestResult = ref(null)
const liveTestError = ref('')
const liveTestHistory = ref([])
const liveTestResultRef = ref(null)
const allowExternalNetwork = ref(false)
const statusMessage = ref('')
const authorLogs = ref([])
const streamController = ref(null)
const authorStage = ref('draft')
const activeStep = ref('input')
const codeInputOpen = ref(false)
const advancedOpen = ref(false)
const expertIoOpen = ref(false)
const debugOpen = ref(false)
const authConfigOpen = ref(false)
const registryDrawerOpen = ref(false)
const snippetManagerOpen = ref(false)
const sampleInputText = ref(`{
  "query": "测试内容"
}`)
const lastValidation = ref(null)
const humanFeedbackText = ref('')
const reviseRunning = ref(false)
const revisionHistory = ref([])
const selectedToolName = ref('')
const snippets = ref([])
const snippetRolesText = ref('')
const snippetCapabilitiesText = ref('')
const snippetFailuresText = ref('')
const snippetInputShapeText = ref('{}')
const snippetOutputShapeText = ref('{}')
const snippetAntiPatternsText = ref('')
const snippetTestResult = ref(null)
const snippetForm = reactive({ id: '', title: '', kind: 'minimal_usage', description: '', code: '', return_rule: '', usage_policy: 'helper_preferred', priority: 80 })
const expandedPanels = reactive({ input: true, planner: false, adapter: false, validation: false, snippet: false, registeredTools: false, snippetManager: false })

const form = reactive({
  tool_name: '',
  description: '',
  tool_type: 'python_helper',
  wrapper_family: 'auto',
  input_description: '',
  output_description: '',
  needs_secret: false,
  needs_external_network: false,
  generates_file: false,
  high_risk: false
})

function looksLikeFullRuntime(code = '') {
  return /\bdef\s+run\s*\(/.test(String(code || ''))
}

const effectiveRuntimeCode = computed(() => {
  return looksLikeFullRuntime(runtimeCode.value)
    ? runtimeCode.value
    : ''
})

const visibleDebugPanels = computed(() =>
  mergeDebugSections(
    debugSections.value,
    lastValidation.value?.debug_sections,
    lastValidation.value?.collapsible_blocks,
    lastValidation.value?.dynamic_trial?.debug_sections,
    lastValidation.value?.dynamic_trial?.collapsible_blocks
  )
)
const parsedManifest = computed(() => { try { return manifestText.value ? JSON.parse(manifestText.value) : null } catch { return null } })
const sampleInputParseWarning = computed(() => {
  const text = String(sampleInputText.value || '').trim()
  if (!text) return ''

  try {
    JSON.parse(text)
    return ''
  } catch (err) {
    return `Sample Input 不是严格 JSON，试运行将使用空样例并交给后端按 schema 自动补全：${err.message || err}`
  }
})
const parsedSample = computed(() => {
  const text = String(sampleInputText.value || '').trim()
  if (!text) return {}

  try {
    const value = JSON.parse(text)
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {}
  } catch {
    return {}
  }
})
const configExtra = computed(() =>
  Object.fromEntries(
    configExtraFields.value
      .filter(field => field.key)
      .map(field => [field.key.trim(), field.value])
  )
)

const defaultConfigFieldNames = new Set([
  'base_url',
  'endpoint',
  'url',
  'method',
  'auth_type',
  'secret_env',
  'secret_value',
  'auth_placement',
  'auth_header_name',
  'auth_query_param',
  'sample_input'
])

const dynamicConfigFields = computed(() => {
  const schema = planState.value?.config_form_schema || {}
  const rawFields = Array.isArray(schema.fields)
    ? schema.fields
    : Array.isArray(schema)
      ? schema
      : []

  return rawFields
    .map(field => (typeof field === 'string' ? { name: field, label: field } : field))
    .filter(field => field?.name && !defaultConfigFieldNames.has(field.name))
})

function isPlainObject(value) {
  return value && typeof value === 'object' && !Array.isArray(value)
}

function hasObjectContent(value) {
  return isPlainObject(value) && Object.keys(value).length > 0
}

function normalizeRequestMethod(method) {
  return String(method || 'GET').trim().toUpperCase()
}

function shouldUseJsonBody(method) {
  return ['POST', 'PUT', 'PATCH'].includes(normalizeRequestMethod(method))
}

function shouldUseQueryParams(method) {
  return ['GET', 'DELETE'].includes(normalizeRequestMethod(method))
}

function buildRequestTemplateFromSample(method, sampleInput) {
  if (!hasObjectContent(sampleInput)) return {}

  if (shouldUseJsonBody(method)) {
    return {
      json_body_template: sampleInput,
      headers_template: {
        'Content-Type': 'application/json'
      }
    }
  }

  if (shouldUseQueryParams(method)) {
    return {
      query_template: sampleInput
    }
  }

  return {}
}

function compactObject(obj) {
  return Object.fromEntries(
    Object.entries(obj || {}).filter(([, value]) => {
      if (value === undefined || value === null || value === '') return false
      if (isPlainObject(value) && !Object.keys(value).length) return false
      return true
    })
  )
}

function buildCurrentUiConfig() {
  const method = normalizeRequestMethod(configForm.method)
  const sampleTemplateConfig = buildRequestTemplateFromSample(method, parsedSample.value)

  const headersTemplate = {
    ...(sampleTemplateConfig.headers_template || {}),
    ...(dynamicConfig.headers_template || {}),
    ...(configExtra.value.headers_template || {})
  }

  return compactObject({
    base_url: configForm.base_url,
    method,
    auth_type: configForm.auth_type,
    secret_env: configForm.secret_env,
    auth_placement: configForm.auth_placement,
    auth_header_name: configForm.auth_header_name,
    auth_query_param: configForm.auth_query_param,

    ...dynamicConfig,
    ...configExtra.value,

    json_body_template:
      dynamicConfig.json_body_template ||
      configExtra.value.json_body_template ||
      sampleTemplateConfig.json_body_template,

    query_template:
      dynamicConfig.query_template ||
      configExtra.value.query_template ||
      sampleTemplateConfig.query_template,

    headers_template: headersTemplate
  })
}
function canonicalAuthoringConfig() {
  const saved = configSaveResult.value?.config
  if (saved && typeof saved === 'object' && Object.keys(saved).length) {
    return saved
  }
  return buildCurrentUiConfig()
}
const parsedConfig = computed(() => buildCurrentUiConfig())
const effectiveWrapperFamily = computed(() => {
  const raw =
    planState.value?.wrapper_family ||
    form.wrapper_family ||
    planState.value?.tool_kind ||
    form.tool_type ||
    'auto'

  if (raw === 'external_api' || raw === 'http_api') return 'http_api'
  if (raw === 'web_fetch') return 'web_fetch'
  if (raw === 'python_helper' || raw === 'data_transform' || raw === 'local_helper') return 'python_compute'
  if (raw === 'file_generator') return 'file_converter'
  return raw
})

function stripUnconfirmedDefaultAuthConfig(config = {}) {
  const cfg = { ...(config || {}) }

  const authType = String(cfg.auth_type || '').trim().toLowerCase()

  // auth_type=none 是 UI 默认值，不代表用户确认无需认证。
  if (['', 'none', 'no_auth', 'anonymous', 'public', 'noauth'].includes(authType)) {
    delete cfg.auth_type

    if (String(cfg.auth_placement || '').toLowerCase() === 'header') {
      delete cfg.auth_placement
    }

    if (String(cfg.auth_header_name || '').toUpperCase() === 'X-API-KEY') {
      delete cfg.auth_header_name
    }

    if (String(cfg.auth_query_param || '').toLowerCase() === 'api_key') {
      delete cfg.auth_query_param
    }
  }

  // method=GET 也是 UI 默认值；没有 base_url 时不要发给 planner。
  if (!cfg.base_url && String(cfg.method || '').toUpperCase() === 'GET') {
    delete cfg.method
  }

  return Object.fromEntries(
    Object.entries(cfg).filter(([, value]) => {
      if (value === undefined || value === null || value === '') return false
      if (Array.isArray(value) && !value.length) return false
      if (isPlainObject(value) && !Object.keys(value).length) return false
      return true
    })
  )
}

function hasExplicitAuthConfig() {
  const authType = String(configForm.auth_type || '').trim().toLowerCase()

  if (authType && !['none', 'no_auth', 'anonymous', 'public', 'noauth'].includes(authType)) {
    return true
  }

  return Boolean(configForm.secret_env || configForm.secret_value)
}

function configForAuthorAction(action) {
  const config = canonicalAuthoringConfig()

  // configure / live_test / finalize / revise 可以带完整 config。
  if (!['clarify', 'generate'].includes(action)) {
    return config
  }

  // 保存过配置，说明用户确认过，可以发送。
  if (configSaveResult.value?.success) {
    return config
  }

  // 人工强制认证/无认证，也属于用户确认。
  if (manualAuthOverride.mode !== 'auto') {
    return config
  }

  // 用户显式选择了 api_key/token/basic 等，才发送。
  if (hasExplicitAuthConfig()) {
    return config
  }

  // 否则只发非默认字段，不发 auth_type=none。
  const cleaned = stripUnconfirmedDefaultAuthConfig(config)
  return Object.keys(cleaned).length ? cleaned : undefined
}

const isHttpApiFamily = computed(() => effectiveWrapperFamily.value === 'http_api')

const canRunLiveTest = computed(() => allowExternalNetwork.value && (configSaveResult.value?.success || planState.value?.ready_for_live_test))
const entrypointConfidenceLabel = computed(() => ({ high: '高置信度', medium: '中等置信度', low: '低置信度' }[planState.value?.suggested_entrypoint?.confidence] || '待确认'))
const liveTestPassed = computed(() => Boolean(liveTestResult.value?.success))

const hasEnoughPlanForGeneration = computed(() =>
  Boolean(
    parsedManifest.value ||
    planState.value?.manifest ||
    planState.value?.operation ||
    form.description
  )
)

const configGatePassed = computed(() => {
  if (!requiresConfig.value) return true
  if (planState.value?.ready_for_code_generation) return true
  if (liveTestPassed.value) return true
  return false
})

function normalizeAuthGate(rawGate = {}) {
  const status = String(rawGate.status || 'none').trim() || 'none'
  const blockingStatuses = ['needs_config', 'needs_live_test', 'needs_review', 'blocked']

  const canRegister =
    rawGate.can_register !== undefined
      ? Boolean(rawGate.can_register)
      : !blockingStatuses.includes(status)

  return {
    status,
    block_code_generation: Boolean(rawGate.block_code_generation),
    block_registration:
      rawGate.block_registration !== undefined
        ? Boolean(rawGate.block_registration)
        : !canRegister,
    can_register: canRegister,
    reasons: Array.isArray(rawGate.reasons)
      ? rawGate.reasons.filter(Boolean)
      : rawGate.reason
        ? [String(rawGate.reason)]
        : []
  }
}

const authGate = computed(() => {
  const validationGate = lastValidation.value?.auth_gate
  if (validationGate && typeof validationGate === 'object') {
    return normalizeAuthGate(validationGate)
  }

  if (manualAuthOverride.mode === 'force_required') {
    return normalizeAuthGate({
      status: 'needs_config',
      block_code_generation: false,
      block_registration: true,
      reasons: [
        manualAuthOverride.reason || '用户手动要求该工具必须配置认证'
      ]
    })
  }

  if (manualAuthOverride.mode === 'force_no_auth') {
    return normalizeAuthGate({
      status: 'none',
      block_code_generation: false,
      block_registration: false,
      reasons: [
        manualAuthOverride.reason || '用户手动确认该工具无需认证'
      ]
    })
  }

  const backendGate = planState.value?.auth_gate
  if (backendGate && typeof backendGate === 'object') {
    return normalizeAuthGate(backendGate)
  }

  const decision =
    planState.value?.auth_decision ||
    parsedManifest.value?.auth_decision ||
    {}

  const required = String(
    decision.required ||
    (planState.value?.requires_config ? 'yes' : 'no')
  ).trim().toLowerCase()

  if (liveTestResult.value?.success) {
    return normalizeAuthGate({
      status: 'none',
      block_code_generation: false,
      block_registration: false,
      reasons: ['live_test 已通过']
    })
  }

  if (required === 'yes') {
    return normalizeAuthGate({
      status: 'needs_config',
      block_code_generation: false,
      block_registration: true,
      reasons: decision.evidence || decision.reasons || ['Planner 判定需要认证或配置']
    })
  }

  if (required === 'unknown') {
    return normalizeAuthGate({
      status: 'needs_review',
      block_code_generation: false,
      block_registration: true,
      reasons: decision.evidence || decision.reasons || ['Planner 无法确定是否需要认证']
    })
  }

  return normalizeAuthGate({
    status: 'none',
    block_code_generation: false,
    block_registration: false,
    reasons: []
  })
})

const requiresConfig = computed(() =>
  ['needs_config', 'needs_live_test', 'needs_review', 'blocked'].includes(authGate.value.status)
)


const canGenerate = computed(() =>
  !busy.value &&
  !clarificationQuestions.value.length &&
  hasEnoughPlanForGeneration.value &&
  !authGate.value.block_code_generation
)

const canFinalizeAuthoring = computed(() =>
  Boolean(lastValidation.value?.success) &&
  !busy.value &&
  Boolean(effectiveRuntimeCode.value) &&
  Boolean(parsedManifest.value)
)

const registerBlockingReason = computed(() => {
  if (busy.value) return '当前仍有任务运行中。'
  if (!lastValidation.value?.success) return '工具还没有通过验证。'
  if (!parsedManifest.value) return 'manifest 为空或 JSON 格式错误。'
  if (!effectiveRuntimeCode.value) return '缺少完整 runtime_code，不能注册。'
  if (!snippetReady.value) return 'Snippet 还没有生成或不是有效 JSON。'
  if (authGate.value.block_registration) {
    return authGate.value.reasons?.join('；') || '认证/配置 gate 未通过，不能注册。'
  }
  return ''
})

const canRegister = computed(() => !registerBlockingReason.value)
const manualAuthModeLabel = computed(() => {
  if (manualAuthOverride.mode === 'force_required') return '人工要求认证'
  if (manualAuthOverride.mode === 'force_no_auth') return '人工确认无需认证'
  return '自动判断'
})
const cardPreview = computed(() => (lastValidation.value?.tool_card_preview || []).join('\n\n---\n\n') || '验证后展示 Creator prompt 注入的 function card。')
const snippetPreview = computed(() => snippets.value.map(snippet => snippet.formatted || '').join('\n\n---\n\n') || '选择工具后展示 Creator 会看到的 Tool Snippet。')
const snippetReady = computed(() => snippetText.value && snippetText.value.trim() !== '{}')
const advancedBadge = computed(() => [form.tool_name && 'name', allowedRolesText.value && 'roles', form.needs_secret && 'secret', form.needs_external_network && 'network', form.generates_file && 'file', form.high_risk && 'risk'].filter(Boolean).join(' · '))

function stepStatus(key) {
  if (key === 'planner' && (parsedManifest.value || clarificationQuestions.value.length || requiresConfig.value)) return clarificationQuestions.value.length ? 'bad' : 'ok'
  if (key === 'adapter' && adapterCode.value) return 'ok'
  if (key === 'validation' && lastValidation.value) return lastValidation.value.success ? 'ok' : 'bad'
  if (key === 'snippet' && snippetReady.value) return 'ok'
  return ''
}
async function run(task) {
  busy.value = true
  error.value = ''

  try {
    await task()
  } catch (e) {
    error.value = e.message || String(e)
  } finally {
    busy.value = false
    // 不要在 finally 里清空 statusMessage，否则 revise 成功/失败提示会一闪而过。
    // statusMessage.value = ''
  }
}
function logAuthor(event) { const helperMessages = { tool_call_planned: `正在分析需要辅助工具：${event.tool || ''} ${event.reason || ''}`, tool_call_started: `正在调用${event.tool || '辅助工具'}...`, tool_call_requires_input: `等待用户填写${event.tool || '辅助工具'}配置...`, tool_call_result: `${event.tool || '辅助工具'}${event.success ? '完成，继续生成 adapter...' : '需要补充信息或执行失败'}` }; const message = event.message || helperMessages[event.event] || ''; authorLogs.value.push({ time: new Date().toLocaleTimeString(), ...event, message }); if (authorLogs.value.length > 80) authorLogs.value.shift(); if (message) statusMessage.value = message }
async function loadTools() { const data = await listCreatorTools(); tools.value = data.tools || [] }
function payload() { return { ...form, allowed_roles: allowedRolesText.value.split(',').map(s => s.trim()).filter(Boolean) } }
function questionText(question) { return typeof question === 'string' ? question : (question?.question || question?.text || '') }
function questionOptions(question) { return Array.isArray(question?.options) ? question.options : [] }
function optionLabel(option) { return typeof option === 'string' ? option : (option?.label || option?.text || option?.value || '') }
function optionValue(option) { return typeof option === 'string' ? option : (option?.value || option?.label || option?.text || '') }
function answerLabel(question, answer) { const option = questionOptions(question).find(item => optionValue(item) === answer); return option ? optionLabel(option) : answer }
function addExtraField() { configExtraFields.value.push({ key: '', value: '', sensitive: false }) }
function removeExtraField(idx) { configExtraFields.value.splice(idx, 1) }
function normalizeSuggestedAuthType(value) {
  const text = String(value || '').trim().toLowerCase()

  if (!text || ['none', 'no_auth', 'anonymous', 'public', 'noauth'].includes(text)) {
    return 'none'
  }

  if (['api_key', 'apikey', 'key'].includes(text)) return 'api_key'
  if (['bearer', 'token', 'oauth_bearer'].includes(text)) return 'token'
  if (text === 'basic') return 'basic'
  if (['oauth', 'oauth2'].includes(text)) return 'oauth2'
  if (text === 'custom') return 'custom'

  // header/query/bearer 是 placement，不是 auth_type。
  if (['header', 'query'].includes(text)) return 'custom'

  return 'unknown'
}

function authTypeNeedsSecret(authType) {
  const text = String(authType || '').trim().toLowerCase()
  return !['none', 'no_auth', 'anonymous', 'public', 'unknown', ''].includes(text)
}

function writeConfigIfUseful(key, value, { overwriteDefault = false, defaultValue = '' } = {}) {
  if (value === undefined || value === null || value === '') return

  const current = configForm[key]
  const shouldWrite =
    current === undefined ||
    current === null ||
    current === '' ||
    (overwriteDefault && String(current) === String(defaultValue))

  if (shouldWrite) {
    configForm[key] = value
  }
}

function applyAuthDecision(decision = {}) {
  if (!decision || typeof decision !== 'object') return
  if (liveTestResult.value?.success) return

  const schemes = Array.isArray(decision.security_schemes)
    ? decision.security_schemes
    : Array.isArray(decision.securitySchemes)
      ? decision.securitySchemes
      : []

  const scheme = schemes.find(item => {
    const type = String(item?.type || item?.scheme || '').trim().toLowerCase()
    return type && !['none', 'noauth', 'no_auth', 'anonymous', 'public'].includes(type)
  })

  if (!scheme) {
    if (decision.required === 'unknown' && configForm.auth_type === 'none') {
      configForm.auth_type = 'unknown'
    }
    return
  }

  const schemeType = normalizeSuggestedAuthType(scheme.type || scheme.scheme)

  if (schemeType !== 'unknown') {
    configForm.auth_type = schemeType
  } else if (configForm.auth_type === 'none') {
    configForm.auth_type = 'unknown'
  }

  writeConfigIfUseful('secret_env', scheme.env || scheme.secret_env || scheme.secretEnv)

  const placement = scheme.placement || scheme.in || scheme.auth_placement
  if (placement) {
    writeConfigIfUseful('auth_placement', placement, {
      overwriteDefault: true,
      defaultValue: 'header'
    })
  }

  if (scheme.header_name || scheme.headerName) {
    writeConfigIfUseful('auth_header_name', scheme.header_name || scheme.headerName, {
      overwriteDefault: true,
      defaultValue: 'X-API-KEY'
    })
  }

  if (scheme.query_param || scheme.queryParam || scheme.name) {
    writeConfigIfUseful('auth_query_param', scheme.query_param || scheme.queryParam || scheme.name, {
      overwriteDefault: true,
      defaultValue: 'api_key'
    })
  }

  if (schemeType === 'token') {
    configForm.auth_placement = 'bearer'
    if (!configForm.auth_header_name || configForm.auth_header_name === 'X-API-KEY') {
      configForm.auth_header_name = 'Authorization'
    }
  }

  if (schemeType === 'basic') {
    configForm.auth_placement = 'header'
    configForm.auth_header_name = 'Authorization'
  }
}

const AUTH_AUTHORING_TOOL_NAMES = new Set([
  'authoring_config_collector',
  'authoring_live_test'
])

function manualAuthOverridePayload() {
  return {
    mode: manualAuthOverride.mode,
    reason: manualAuthOverride.reason,
    source: manualAuthOverride.source,
    updated_at: manualAuthOverride.updated_at
  }
}

function removeAuthAuthoringTools(items = []) {
  return (items || []).filter(item => {
    const toolName = String(item?.tool_name || item?.name || '').trim()
    return !AUTH_AUTHORING_TOOL_NAMES.has(toolName)
  })
}

function applyManualAuthOverrideToPlan() {
  if (manualAuthOverride.mode === 'auto') return

  if (manualAuthOverride.mode === 'force_required') {
    planState.value = {
      ...planState.value,
      requires_config: true,
      requires_authorization: true,
      requires_secret: true,
      ready_for_code_generation: false,
      auth_decision: {
        required: 'yes',
        confidence: 1,
        reason: manualAuthOverride.reason || '用户手动要求认证',
        evidence: ['manual_override'],
        security_schemes: []
      },
      auth_gate: {
        status: 'needs_config',
        block_code_generation: false,
        block_registration: true,
        reasons: [manualAuthOverride.reason || '用户手动要求该工具必须配置认证']
      }
    }
    return
  }

  if (manualAuthOverride.mode === 'force_no_auth') {
    planState.value = {
      ...planState.value,
      requires_config: false,
      requires_authorization: false,
      requires_secret: false,
      requires_authoring_tools: false,
      ready_for_code_generation: Boolean(
        planState.value?.manifest ||
        parsedManifest.value ||
        planState.value?.operation ||
        form.description
      ),
      authoring_tool_plan: removeAuthAuthoringTools(planState.value?.authoring_tool_plan || []),
      auth_decision: {
        required: 'no',
        confidence: 1,
        reason: manualAuthOverride.reason || '用户手动确认无需认证',
        evidence: ['manual_override'],
        security_schemes: [{ type: 'none' }]
      },
      auth_gate: {
        status: 'none',
        block_code_generation: false,
        block_registration: false,
        reasons: [manualAuthOverride.reason || '用户手动确认该工具无需认证']
      }
    }
  }
}

function setManualAuthOverride(mode, reason) {
  manualAuthOverride.mode = mode
  manualAuthOverride.reason = reason
  manualAuthOverride.source = 'user'
  manualAuthOverride.updated_at = new Date().toISOString()
  applyManualAuthOverrideToPlan()
}

function forceRequireAuth() {
  setManualAuthOverride('force_required', '用户手动添加认证配置')
  if (configForm.auth_type === 'none' || configForm.auth_type === 'unknown') {
    configForm.auth_type = 'api_key'
  }
  authConfigOpen.value = true
  activeStep.value = 'planner'
}

function forceNoAuth() {
  setManualAuthOverride('force_no_auth', '用户手动删除认证要求')
  configForm.auth_type = 'none'
  configForm.secret_env = ''
  configForm.secret_value = ''
  configForm.auth_placement = 'header'
  liveTestError.value = ''
  activeStep.value = 'planner'
}

function clearManualAuthOverride() {
  manualAuthOverride.mode = 'auto'
  manualAuthOverride.reason = ''
  manualAuthOverride.source = 'user'
  manualAuthOverride.updated_at = new Date().toISOString()

  planState.value = {
    ...planState.value,
    auth_override: manualAuthOverridePayload()
  }
}

function applySuggestedEntrypoint(entrypoint = {}) {
  if (!entrypoint || typeof entrypoint !== 'object') return

  const lockedByLiveTest = Boolean(liveTestResult.value?.success)
  if (lockedByLiveTest) return

  writeConfigIfUseful('base_url', entrypoint.base_url || entrypoint.endpoint || entrypoint.url)

  const suggestedMethod = normalizeRequestMethod(entrypoint.method)
  const currentMethod = normalizeRequestMethod(configForm.method)
  const methodLooksDefault = !configForm.method || currentMethod === 'GET'

  if (entrypoint.method && methodLooksDefault && !configSaveResult.value?.success) {
    configForm.method = suggestedMethod
  }

  if (entrypoint.auth_type && configForm.auth_type === 'none') {
    configForm.auth_type = normalizeSuggestedAuthType(entrypoint.auth_type)
  }

  writeConfigIfUseful('secret_env', entrypoint.secret_env || entrypoint.secretEnv)

  if (entrypoint.auth_placement) {
    writeConfigIfUseful('auth_placement', entrypoint.auth_placement, {
      overwriteDefault: true,
      defaultValue: 'header'
    })
  }

  if (entrypoint.auth_header_name) {
    writeConfigIfUseful('auth_header_name', entrypoint.auth_header_name, {
      overwriteDefault: true,
      defaultValue: 'X-API-KEY'
    })
  }

  if (entrypoint.auth_query_param) {
    writeConfigIfUseful('auth_query_param', entrypoint.auth_query_param, {
      overwriteDefault: true,
      defaultValue: 'api_key'
    })
  }

  if (entrypoint.auth && typeof entrypoint.auth === 'object') {
    applyAuthDecision({
      required: entrypoint.auth.type && entrypoint.auth.type !== 'none' ? 'yes' : 'no',
      security_schemes: [entrypoint.auth]
    })
  }

  const extra = entrypoint.extra || entrypoint.config || {}
  Object.entries(extra).forEach(([key, value]) => {
    if (!defaultConfigFieldNames.has(key) && dynamicConfig[key] === undefined) {
      dynamicConfig[key] = value
    }
  })

  dynamicConfigFields.value.forEach(field => {
    if (field.default !== undefined && dynamicConfig[field.name] === undefined) {
      dynamicConfig[field.name] = field.default
    }
  })
}

function filterAnsweredQuestions(questions) {
  const answered = new Set(clarificationQuestions.value.map((question, idx) => (clarificationAnswers.value[idx] ? questionText(question) : '')).filter(Boolean))
  return questions.filter(question => !answered.has(questionText(question)))
}
function configSessionId() { return form.tool_name || planState.value?.operation || form.description || 'default' }

function authorPayload(action) {
  const runtime = looksLikeFullRuntime(runtimeCode.value)
    ? runtimeCode.value
    : ''

  const isGenerateLike = ['generate', 'clarify'].includes(action)

  return {
    ...payload(),

    action,

    reference_code: optionalCodeBlock.value || undefined,
    reference_snippet: optionalCodeBlock.value || undefined,

    code_block: isGenerateLike
      ? (optionalCodeBlock.value || undefined)
      : undefined,

    // 主窗格展示代码
    adapter_code: adapterCode.value,
    display_code: adapterCode.value,
    public_api_code: adapterCode.value,

    // 唯一完整执行代码来源
    runtime_code: runtime,
    full_adapter_code: runtime,
    internal_code: runtime,
    script_code: runtime,

    sample_input: parsedSample.value,
    manifest: parsedManifest.value,
    validation: lastValidation.value,

    human_feedback: humanFeedbackText.value,
    review_feedback: humanFeedbackText.value,
    feedback: humanFeedbackText.value,

    trial_run_result: lastValidation.value,
    debug_sections: debugSections.value,
    tool_contract: toolContract.value,

    clarification_answers: clarificationQuestions.value
      .map((question, idx) => ({
        id: question?.id || '',
        question: questionText(question),
        answer: clarificationAnswers.value[idx] || '',
        answer_label: answerLabel(question, clarificationAnswers.value[idx] || '')
      }))
      .filter(item => item.answer),

    tool_kind: planState.value?.tool_kind,
    operation: planState.value?.operation,
    config: configForAuthorAction(action),
    live_test_result: liveTestResult.value,
    allow_external_network: allowExternalNetwork.value,
    authoring_context: planState.value?.authoring_context || {},
    auth_override: manualAuthOverridePayload()
  }
}

function extractLiveTestResult(data) {
  if (data?.live_test_result) return data.live_test_result
  if (data?.authoring_context?.live_test_result) return data.authoring_context.live_test_result
  for (const item of data?.authoring_tool_results || []) {
    if (item?.live_test_result) return item.live_test_result
  }
  return null
}
function rememberLiveTestResult(result) {
  if (!result) return
  liveTestResult.value = result
  liveTestHistory.value.unshift({ time: new Date().toISOString(), result })
  if (liveTestHistory.value.length > 10) liveTestHistory.value.pop()
}
function normalizeManualAuthOverride(override = {}) {
  if (!override || typeof override !== 'object') {
    return null
  }

  const aliases = {
    required: 'force_required',
    force_auth: 'force_required',
    add: 'force_required',
    add_auth: 'force_required',
    no_auth: 'force_no_auth',
    remove: 'force_no_auth',
    remove_auth: 'force_no_auth',
    none: 'force_no_auth'
  }

  let mode = String(override.mode || 'auto').trim().toLowerCase()
  mode = aliases[mode] || mode

  if (!['auto', 'force_required', 'force_no_auth'].includes(mode)) {
    mode = 'auto'
  }

  return {
    mode,
    reason: String(override.reason || ''),
    source: String(override.source || 'user'),
    updated_at: String(override.updated_at || '')
  }
}

function applyManualAuthOverrideFromData(data = {}) {
  const override =
    data?.auth_override ||
    data?.config?.auth_override ||
    data?.manifest?.auth_override ||
    data?.authoring_context?.auth_override

  const normalized = normalizeManualAuthOverride(override)
  if (!normalized) return

  manualAuthOverride.mode = normalized.mode
  manualAuthOverride.reason = normalized.reason
  manualAuthOverride.source = normalized.source
  manualAuthOverride.updated_at = normalized.updated_at

  if (normalized.mode !== 'auto') {
    applyManualAuthOverrideToPlan()
  }
}
function stringifyPretty(value) { return typeof value === 'string' ? value : JSON.stringify(value, null, 2) }
function normalizeDebugSections(items = []) {
  const raw = Array.isArray(items) ? items : []
  return raw
    .map((item, index) => {
      if (!item) return null

      if (typeof item === 'string') {
        return {
          id: `debug_${index}`,
          title: `调试信息 ${index + 1}`,
          description: '',
          language: 'text',
          content: item
        }
      }

      return {
        id: item.id || item.key || `debug_${index}`,
        title: item.title || item.name || `调试信息 ${index + 1}`,
        description: item.description || item.summary || '',
        language: item.language || item.type || 'text',
        content:
          item.content !== undefined
            ? item.content
            : item.code !== undefined
              ? item.code
              : item.data !== undefined
                ? item.data
                : item
      }
    })
    .filter(Boolean)
}

function mergeDebugSections(...groups) {
  const merged = []
  const seen = new Set()

  for (const group of groups) {
    for (const item of normalizeDebugSections(group)) {
      const key = `${item.id}:${item.title}`
      if (seen.has(key)) continue
      seen.add(key)
      merged.push(item)
    }
  }

  return merged
}

function extractRuntimeCode(data = {}) {
  const candidates = [
    data.runtime_code,
    data.full_adapter_code,
    data.internal_code,
    data.script_code
  ]

  for (const item of candidates) {
    const code = String(item || '').trim()
    if (looksLikeFullRuntime(code)) {
      return code
    }
  }

  return ''
}

function extractDisplayCode(data = {}) {
  return (
    data.display_code ||
    data.public_api_code ||
    data.adapter_code ||
    data.core_display_code ||
    ''
  )
}

function extractDebugSections(data = {}) {
  return mergeDebugSections(
    data.debug_sections,
    data.collapsible_blocks,
    data.validation?.debug_sections,
    data.validation?.collapsible_blocks,
    data.validation?.dynamic_trial?.debug_sections,
    data.validation?.dynamic_trial?.collapsible_blocks
  )
}
function scrollToLiveTestResult() { nextTick(() => liveTestResultRef.value?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })) }

function applyAuthorResult(data = {}) {
  applyManualAuthOverrideFromData(data)

  planState.value = {
    ...planState.value,
    ...data,
    auth_override: manualAuthOverridePayload()
  }

  const liveResult = extractLiveTestResult(data)
  rememberLiveTestResult(liveResult)

  const nextQuestions = (data.clarification_questions || data.questions || []).slice(0, 3)
  clarificationQuestions.value = filterAnsweredQuestions(nextQuestions)

  if (!clarificationQuestions.value.length) {
    clarificationAnswers.value = []
  }

  const lockedByConfigOrLiveTest =
    Boolean(configSaveResult.value?.success) ||
    Boolean(liveTestResult.value?.success)

  if (!lockedByConfigOrLiveTest && manualAuthOverride.mode === 'auto') {
    applyAuthDecision(
      data.auth_decision ||
      data.manifest?.auth_decision ||
      data.security_decision ||
      {}
    )

    applySuggestedEntrypoint(data.suggested_entrypoint || {})
  }

  if (manualAuthOverride.mode !== 'auto') {
    applyManualAuthOverrideToPlan()
  }

  if (data.config?.base_url && !configForm.base_url) {
    configForm.base_url = data.config.base_url
  }

  if (data.manifest) {
    const manifest = {
      ...(data.manifest || {}),
      auth_override: manualAuthOverridePayload()
    }

    if (planState.value?.auth_decision) {
      manifest.auth_decision = planState.value.auth_decision
    }

    if (authGate.value) {
      manifest.auth_gate = authGate.value
    }

    manifestText.value = JSON.stringify(manifest, null, 2)
  }

  if (data.sample_input) {
    sampleInputText.value = JSON.stringify(data.sample_input || {}, null, 2)
  }

  // 代码协议：
  // display 只进主窗格；
  // runtime 只来自后端正式 runtime 字段；
  // 不从 debug/validation 里兜底捞代码。
  const display = extractDisplayCode(data)
  const runtime = extractRuntimeCode(data)

  if (display) {
    adapterCode.value = display
  }

  if (runtime) {
    runtimeCode.value = runtime
  }

  const hasRuntimeField =
    Boolean(data.runtime_code) ||
    Boolean(data.full_adapter_code) ||
    Boolean(data.internal_code) ||
    Boolean(data.script_code)

  if (hasRuntimeField && !runtime) {
    error.value = '后端返回了 runtime 字段，但内容不包含 def run(...)，生成阶段协议不完整。'
  }

  const sections = extractDebugSections(data)
  if (sections.length) {
    debugSections.value = mergeDebugSections(debugSections.value, sections)
  }

  if (data.tool_contract) {
    toolContract.value = data.tool_contract
  }

  if (data.validation) {
    lastValidation.value = data.validation
  }

  for (const item of data.authoring_tool_plan || []) {
    logAuthor({
      event: 'tool_call_planned',
      tool: item.tool_name,
      reason: item.reason
    })
  }

  for (const item of data.authoring_tool_results || []) {
    logAuthor({
      event: 'tool_call_started',
      tool: item.tool_name
    })

    if (item.requires_input) {
      logAuthor({
        event: 'tool_call_requires_input',
        tool: item.tool_name,
        schema: item.schema || {}
      })
    }

    logAuthor({
      event: 'tool_call_result',
      tool: item.tool_name,
      success: Boolean(item.success)
    })
  }

  if (data.snippet) {
    snippetText.value = JSON.stringify(data.snippet, null, 2)
  }

  if (display || runtime) {
    nextTick(refreshEditableInternalCode)
  }
}

function draftManifest() { return run(async () => { const data = await draftCreatorTool(payload()); manifestText.value = JSON.stringify(data.manifest, null, 2); lastValidation.value = null; clarificationQuestions.value = []; activeStep.value = 'planner' }) }
function continuePlanning() { return run(async () => { const data = await authorCreatorTool(authorPayload('configure')); applyAuthorResult(data); activeStep.value = 'planner' }) }
async function saveConfigOnly({ configureAfterSave = true } = {}) {
  const uiConfig = {
    ...buildCurrentUiConfig(),
    auth_override: manualAuthOverridePayload()
  }

  configSaveResult.value = await saveCreatorToolConfig({
    session_id: configSessionId(),
    tool_name: form.tool_name,
    operation: planState.value?.operation || form.description,
    base_url: configForm.base_url,
    auth_type: configForm.auth_type,
    secret_env: configForm.secret_env,
    secret_value: configForm.secret_value,
    auth_placement: configForm.auth_placement,
    auth_header_name: configForm.auth_header_name,
    auth_query_param: configForm.auth_query_param,
    extra: configExtra.value,
    additional_fields: configExtraFields.value.filter(field => field.key),
    sample_input: parsedSample.value,
    config: uiConfig,
    auth_override: manualAuthOverridePayload()
  })

  applyManualAuthOverrideFromData(configSaveResult.value)

  configForm.secret_value = ''

  if (configureAfterSave) {
    const data = await authorCreatorTool({
      ...authorPayload('configure'),
      config: canonicalAuthoringConfig(),
      auth_override: manualAuthOverridePayload()
    })

    applyAuthorResult(data)
  }
}
function saveConfigAndContinue() { return run(async () => { await saveConfigOnly(); authConfigOpen.value = false; activeStep.value = 'planner' }) }
async function performLiveTest({ saveFirst = false } = {}) {
  liveTestRunning.value = true
  liveTestError.value = ''

  try {
    if (saveFirst || !configSaveResult.value?.success) {
      await saveConfigOnly({ configureAfterSave: false })
    }

    allowExternalNetwork.value = true

    const liveConfig = canonicalAuthoringConfig()

    const data = await liveTestCreatorTool({
      ...authorPayload('live_test'),
      allow_external_network: true,
      sample_input: parsedSample.value,
      config: liveConfig
    })

    applyAuthorResult(data)

    const result = extractLiveTestResult(data)
    if (!result) {
      liveTestError.value = 'live_test 未返回结果。'
    } else if (result.success) {
      liveTestResult.value = result
      clarificationQuestions.value = []
      clarificationAnswers.value = []
      planState.value = {
        ...planState.value,
        needs_clarification: false,
        questions: [],
        clarification_questions: [],
        requires_authoring_tools: false,
        authoring_tool_plan: [],
        ready_for_live_test: true,
        ready_for_code_generation: true
      }
    }

    authorLogs.value.push({
      time: new Date().toLocaleTimeString(),
      event: 'live_test_result',
      success: result?.success
    })

    activeStep.value = 'planner'
    scrollToLiveTestResult()
  } catch (e) {
    liveTestError.value = e.message || String(e)
    activeStep.value = 'planner'
    scrollToLiveTestResult()
  } finally {
    liveTestRunning.value = false
  }
}
function saveConfigAndRunLiveTest() { return run(async () => { await performLiveTest({ saveFirst: true }); authConfigOpen.value = false; activeStep.value = 'planner'; scrollToLiveTestResult() }) }
function authorDraft() { return run(async () => { const data = await authorCreatorTool(authorPayload('clarify')); authorStage.value = 'clarify'; applyAuthorResult(data); activeStep.value = 'planner' }) }
function generateAdapter() {
  return run(async () => {
    const previousDisplayCode = adapterCode.value
    const previousRuntimeCode = runtimeCode.value

    adapterCode.value = ''
    runtimeCode.value = ''
    editableInternalCode.value = ''
    debugSections.value = []
    toolContract.value = null
    authorLogs.value = []
    lastValidation.value = null
    streamController.value = new AbortController()

    let finalResult = null
    let receivedDisplayFromStructuredField = false

    for await (const event of authorCreatorToolStream(authorPayload('generate'), streamController.value.signal)) {
      logAuthor(event)

      if (event.event === 'error') {
        throw new Error(event.message || '生成 adapter 失败')
      }

      if (event.event === 'model_delta') {
        const runtime =
          event.runtime_code ||
          event.full_adapter_code ||
          event.internal_code ||
          event.script_code ||
          ''

        const display =
          event.display_code ||
          event.public_api_code ||
          event.adapter_code ||
          ''

        const delta = event.delta || ''

        if (display) {
          adapterCode.value = display
          receivedDisplayFromStructuredField = true
        } else if (delta && !receivedDisplayFromStructuredField) {
          adapterCode.value += delta
        }

        if (runtime && looksLikeFullRuntime(runtime)) {
          runtimeCode.value = runtime
        } else if (!runtimeCode.value && looksLikeFullRuntime(delta)) {
          // 兼容旧 stream：delta 本身是完整 runtime。
          runtimeCode.value = delta
        }
      }

      if (event.event === 'debug_trace') {
        debugSections.value = mergeDebugSections(
          debugSections.value,
          event.debug_sections,
          event.collapsible_blocks
        )

        const debugRuntime = extractRuntimeCode(event)
        if (debugRuntime && !runtimeCode.value) {
          runtimeCode.value = debugRuntime
        }
      }

      if (event.event === 'temporary_environment') {
        debugSections.value = mergeDebugSections(debugSections.value, [
          {
            id: 'temporary_environment',
            title: '临时环境创建',
            description: '后端用于试运行工具的临时目录、依赖目录和环境变量',
            language: 'json',
            content: event.temporary_environment || event
          }
        ])
      }

      if (event.event === 'tool_contract') {
        toolContract.value = event.tool_contract || null
      }

      if (event.event === 'validation') {
        lastValidation.value = {
          ...(lastValidation.value || {}),
          success: Boolean(event.success),
          errors: event.errors || [],
          warnings: event.warnings || [],
          debug_sections: event.debug_sections || [],
          collapsible_blocks: event.collapsible_blocks || [],
          dynamic_trial: event.validation?.dynamic_trial || lastValidation.value?.dynamic_trial
        }

        debugSections.value = mergeDebugSections(
          debugSections.value,
          event.debug_sections,
          event.collapsible_blocks,
          event.validation?.debug_sections,
          event.validation?.collapsible_blocks
        )

        const validationRuntime = extractRuntimeCode(event.validation || event)
        if (validationRuntime && !runtimeCode.value) {
          runtimeCode.value = validationRuntime
        }
      }

      if (event.event === 'final_result') {
        finalResult = event
        applyAuthorResult(event)

        const finalRuntime = extractRuntimeCode(event)
        if (finalRuntime) {
          runtimeCode.value = finalRuntime
        }
      }
    }

    const runtime = effectiveRuntimeCode.value

    if (adapterCode.value || runtime) {
      if (runtime) {
        runtimeCode.value = runtime
      }

      refreshEditableInternalCode()
      activeStep.value = 'adapter'
      return
    }

    if (finalResult?.needs_clarification || finalResult?.questions?.length || finalResult?.clarification_questions?.length) {
      statusMessage.value = '还需要补充信息，暂未生成代码。'
      activeStep.value = 'planner'
      return
    }

    if (finalResult?.requires_config || finalResult?.requires_authoring_tools || finalResult?.authoring_tool_plan?.length) {
      statusMessage.value = '还需要完成配置或 live_test，暂未生成代码。'
      activeStep.value = 'planner'
      return
    }

    if (finalResult?.validation && finalResult.validation.success === false) {
      statusMessage.value = '生成失败或验证失败，未得到可展示的 adapter 代码。'
      activeStep.value = 'validation'
      return
    }

    adapterCode.value = previousDisplayCode || ''
    runtimeCode.value = previousRuntimeCode || ''

    if (adapterCode.value || runtimeCode.value) {
      refreshEditableInternalCode()
      activeStep.value = 'adapter'
    } else {
      statusMessage.value = '后端未返回可展示代码，请查看生成日志。'
      activeStep.value = 'planner'
    }
  })
}
function cancelAuthoring() { if (streamController.value) { streamController.value.abort(); streamController.value = null; statusMessage.value = '已取消当前生成' } }
function runLiveTest() {
  return run(async () => {
    if (!configForm.base_url) {
      authConfigOpen.value = true
      return
    }

    if (configForm.auth_type === 'unknown') {
      authConfigOpen.value = true
      return
    }

    if (authTypeNeedsSecret(configForm.auth_type) && !configForm.secret_env) {
      authConfigOpen.value = true
      return
    }

    await performLiveTest({ saveFirst: true })
  })
}
function finalizeAuthoring() {
  return run(async () => {
    const runtime = effectiveRuntimeCode.value

    if (!runtime) {
      error.value = '当前没有完整运行代码，不能生成 snippet。'
      activeStep.value = 'adapter'
      return
    }

    if (humanFeedbackText.value.trim()) {
      statusMessage.value = '已忽略未提交的反馈，按当前验证通过版本生成 Snippet。'
    }

    const data = await authorCreatorTool({
      ...authorPayload('finalize'),

      action: 'finalize',
      manifest: parsedManifest.value,
      sample_input: parsedSample.value,

      adapter_code: adapterCode.value,
      display_code: adapterCode.value,
      public_api_code: adapterCode.value,

      runtime_code: runtime,
      full_adapter_code: runtime,
      internal_code: runtime,
      script_code: runtime,

      validation: lastValidation.value
    })

    authorStage.value = 'finalize'
    applyAuthorResult(data)

    if (data.snippet && parsedManifest.value) {
      const manifest = {
        ...parsedManifest.value,
        snippets: [data.snippet]
      }
      manifestText.value = JSON.stringify(manifest, null, 2)
    }

    activeStep.value = data.snippet ? 'snippet' : 'validation'
  })
}
function generateCode() { return run(async () => { const data = await generateCreatorToolCode({ manifest: parsedManifest.value }); adapterCode.value = data.adapter_code; activeStep.value = 'adapter' }) }
function normalizeCodeForCompare(code = '') {
  return String(code || '')
    .replace(/\r\n/g, '\n')
    .replace(/[ \t]+$/gm, '')
    .trim()
}

function reviseWithFeedback() {
  return run(async () => {
    if (!humanFeedbackText.value.trim()) {
      error.value = '请先填写人工反馈。'
      return
    }

    if (!looksLikeFullRuntime(runtimeCode.value)) {
      error.value = '当前 runtimeCode 为空或不包含 def run(...)。这是生成阶段协议断了，不应该进入反馈修改。请重新生成实现或检查后端 generate 是否返回 runtime_code。'
      activeStep.value = 'adapter'
      return
    }

    reviseRunning.value = true
    statusMessage.value = '正在根据反馈修改代码...'

    try {
      const feedback = humanFeedbackText.value
      const previousDisplay = adapterCode.value
      const previousRuntime = runtimeCode.value
      const previousValidation = lastValidation.value

      const data = await authorCreatorTool({
        ...authorPayload('revise'),

        action: 'revise',

        adapter_code: previousDisplay,
        display_code: previousDisplay,
        public_api_code: previousDisplay,

        runtime_code: previousRuntime,
        full_adapter_code: previousRuntime,
        internal_code: previousRuntime,
        script_code: previousRuntime,

        manifest: parsedManifest.value,
        sample_input: parsedSample.value,
        validation: previousValidation,

        human_feedback: feedback,
        review_feedback: feedback,
        feedback
      })

      revisionHistory.value.unshift({
        time: new Date().toISOString(),
        feedback,
        validation: previousValidation,
        result: data
      })

      const nextRuntime = extractRuntimeCode(data)
      const nextDisplay = extractDisplayCode(data)

      if (!nextRuntime) {
        runtimeCode.value = previousRuntime
        adapterCode.value = previousDisplay
        lastValidation.value = data.validation || previousValidation

        error.value =
          data.validation?.errors?.join('；') ||
          data.warnings?.join('；') ||
          '后端 revise 没有返回有效 runtime_code，这是后端 revise 协议问题。'

        statusMessage.value = '反馈修改失败，已保留上一版代码。'
        activeStep.value = 'validation'
        return
      }

      runtimeCode.value = nextRuntime

      if (nextDisplay) {
        adapterCode.value = nextDisplay
      }

      if (data.validation) {
        lastValidation.value = data.validation
      }

      if (data.snippet) {
        snippetText.value = JSON.stringify(data.snippet, null, 2)
      }

      humanFeedbackText.value = ''
      statusMessage.value = '已根据反馈修改代码。'
      activeStep.value = 'adapter'
      nextTick(refreshEditableInternalCode)
    } finally {
      reviseRunning.value = false
    }
  })
}

function validateTool() {
  return run(async () => {
    const runtime = effectiveRuntimeCode.value

    if (!runtime) {
      error.value = '当前没有完整运行代码，请先生成或返回 Adapter 检查完整内部实现代码。'
      activeStep.value = 'adapter'
      return
    }

    lastValidation.value = await validateCreatorTool({
      manifest: parsedManifest.value,

      adapter_code: runtime,
      runtime_code: runtime,
      full_adapter_code: runtime,
      internal_code: runtime,
      script_code: runtime,

      display_code: adapterCode.value,
      public_api_code: adapterCode.value,

      sample_input: parsedSample.value,
      dynamic: true,
      allow_external_network: allowExternalNetwork.value || Boolean(liveTestResult.value?.success),
      real_run: allowExternalNetwork.value || Boolean(liveTestResult.value?.success)
    })

    debugSections.value = mergeDebugSections(
      debugSections.value,
      lastValidation.value?.debug_sections,
      lastValidation.value?.collapsible_blocks,
      lastValidation.value?.dynamic_trial?.debug_sections,
      lastValidation.value?.dynamic_trial?.collapsible_blocks
    )

    activeStep.value = 'validation'
  })
}

function buildFinalManifestForRegister() {
  const manifest = { ...(parsedManifest.value || {}) }

  const snippet = parseJsonText(snippetText.value)
  if (snippet && Object.keys(snippet).length) {
    manifest.snippets = [snippet]
  }

  if (planState.value?.auth_decision) {
    manifest.auth_decision = planState.value.auth_decision
  }

  manifest.auth_gate = authGate.value
  manifest.auth_override = manualAuthOverridePayload()

  return manifest
}
function registerTool() {
  return run(async () => {
    const runtime = effectiveRuntimeCode.value

    if (!canRegister.value) {
      error.value = registerBlockingReason.value || '当前工具不能注册。'
      return
    }

    if (!runtime) {
      error.value = '当前没有完整运行代码，不能注册。'
      activeStep.value = 'adapter'
      return
    }

    const result = await registerCreatorTool({
      manifest: buildFinalManifestForRegister(),

      adapter_code: runtime,
      runtime_code: runtime,
      full_adapter_code: runtime,
      internal_code: runtime,
      script_code: runtime,

      display_code: adapterCode.value,
      public_api_code: adapterCode.value,

      sample_input: parsedSample.value,
      dynamic: true,
      allow_external_network: allowExternalNetwork.value || Boolean(liveTestResult.value?.success),
      real_run: allowExternalNetwork.value || Boolean(liveTestResult.value?.success),
      enable: true
    })

    await loadTools()
    statusMessage.value = `工具 ${result.tool?.name || parsedManifest.value?.tool_name || parsedManifest.value?.name || 'custom_tool'} 已注册并启用。`
    registryDrawerOpen.value = true
  })
}
function parseJsonText(text) { try { return text ? JSON.parse(text) : {} } catch { return {} } }
function splitLines(text) { return text.split('\n').map(s => s.trim()).filter(Boolean) }
function splitCsv(text) { return text.split(',').map(s => s.trim()).filter(Boolean) }
function buildSnippetPayload() { return { ...snippetForm, applies_to: { roles: splitCsv(snippetRolesText.value), capabilities: splitCsv(snippetCapabilitiesText.value), failure_layers: splitCsv(snippetFailuresText.value) }, expected_input_shape: parseJsonText(snippetInputShapeText.value), expected_output_shape: parseJsonText(snippetOutputShapeText.value), anti_patterns: splitLines(snippetAntiPatternsText.value), requires: splitCsv(snippetCapabilitiesText.value) } }
function loadSnippets() { return run(async () => { if (!selectedToolName.value) { snippets.value = []; return } const data = await listCreatorToolSnippets(selectedToolName.value); snippets.value = data.snippets || []; snippetTestResult.value = null; snippetManagerOpen.value = true }) }
function editSnippet(snippet) { snippetForm.id = snippet.id; snippetForm.title = snippet.title; snippetForm.kind = snippet.kind || 'minimal_usage'; snippetForm.description = snippet.description || ''; snippetForm.code = snippet.code || ''; snippetForm.return_rule = snippet.return_rule || ''; snippetForm.usage_policy = snippet.usage_policy || 'helper_preferred'; snippetForm.priority = snippet.priority || 0; snippetRolesText.value = (snippet.applies_to?.roles || []).join(','); snippetCapabilitiesText.value = (snippet.applies_to?.capabilities || snippet.requires || []).join(','); snippetFailuresText.value = (snippet.applies_to?.failure_layers || []).join(','); snippetInputShapeText.value = JSON.stringify(snippet.expected_input_shape || {}, null, 2); snippetOutputShapeText.value = JSON.stringify(snippet.expected_output_shape || {}, null, 2); snippetAntiPatternsText.value = (snippet.anti_patterns || []).join('\n'); snippetTestResult.value = null; registryDrawerOpen.value = true; snippetManagerOpen.value = true }
function saveSnippet() { return run(async () => { const payload = buildSnippetPayload(); const exists = snippets.value.some(item => item.id === payload.id); if (exists) await updateCreatorToolSnippet(selectedToolName.value, payload.id, payload); else await createCreatorToolSnippet(selectedToolName.value, payload); await loadSnippets() }) }
function runSnippetSmokeTest() { return run(async () => { if (!snippets.value.some(item => item.id === snippetForm.id)) await saveSnippet(); snippetTestResult.value = await testCreatorToolSnippet(selectedToolName.value, snippetForm.id) }) }

onMounted(loadTools)
</script>

<style scoped>
.page-scroll { overflow: auto; padding: 24px; display: flex; flex-direction: column; gap: 20px; }
.tool-registry { max-width: 1440px; margin: 0 auto; }
.hero, .card, .workspace-card { background: var(--surface); border: 1px solid var(--border); border-radius: 16px; }
.hero { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; padding: 18px 22px; }
.hero-actions, .heading-actions { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; justify-content: flex-end; }
.eyebrow { color: var(--accent); text-transform: uppercase; letter-spacing: .08em; font-size: 12px; margin: 0 0 4px; }
h1 { font-size: 26px; margin: 2px 0 6px; } h2 { font-size: 20px; margin: 0 0 4px; } h3 { margin: 0; font-size: 15px; }
.tool-workspace { display: grid; grid-template-columns: 220px minmax(0, 1fr); gap: 24px; align-items: start; }
.step-sidebar { position: sticky; top: 16px; display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.step-nav { display: grid; grid-template-columns: 28px minmax(0, 1fr) auto; gap: 9px; align-items: center; text-align: left; border: 1px solid var(--border); background: var(--surface); color: var(--text); border-radius: 14px; padding: 10px; cursor: pointer; }
.step-nav.active { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 12%, var(--surface)); }
.step-index { width: 26px; height: 26px; border-radius: 50%; background: var(--surface2); display: grid; place-items: center; font-weight: 800; }
.step-meta { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.step-meta strong { font-size: 14px; } .step-meta small { display: none; color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.step-nav.active .step-meta small { display: block; }
.status-dot { width: 9px; height: 9px; border-radius: 50%; background: var(--text-muted); }
.status-dot.ok { background: var(--success); } .status-dot.bad { background: var(--danger); }
.step-main { min-width: 0; }
.workspace-card { padding: 24px; display: flex; flex-direction: column; gap: 20px; min-height: auto; }
.workspace-card.editor-card { min-height: calc(100vh - 180px); }
.workspace-card.compact-card { max-width: 980px; }
.card-heading { display: flex; flex-direction: column; gap: 2px; }
.card-heading.with-actions { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 16px; align-items: start; }
.editor-card .smart-editor { flex: 1; }
.editor-card :deep(.editor-shell) { min-height: 0; }
.split-layout { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 18px; align-items: start; }
.pane { border: 1px solid var(--border); background: var(--surface2); border-radius: 14px; padding: 14px; display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.drawer-section { display: flex; flex-direction: column; gap: 12px; }
.drawer-heading { display: flex; justify-content: space-between; gap: 12px; align-items: center; }
.drawer-snippet-layout { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(260px, .8fr); gap: 18px; align-items: start; }
.grid { display: grid; gap: 20px; } .grid.two { grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); }
.step-card { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.form-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px; align-items: start; }
.form-row.relaxed { gap: 18px; }
label { display: flex; flex-direction: column; gap: 8px; color: var(--text-muted); min-width: 0; }
.checks { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; }
.checks label, .inline-check { flex-direction: row; align-items: center; } .checks input, .inline-check input { width: auto; } textarea { min-height: 76px; border-radius: 10px; border: 1px solid var(--border); background: var(--surface2); color: var(--text); padding: 10px; }
.actions, .step-actions { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; justify-content: flex-end; }
.step-actions { margin: 8px 0 0; padding: 0; background: transparent; border: 0; position: static; }
.small { font-size: 12px; }
.validation, .compact-preview { padding: 12px; border-radius: var(--radius); border: 1px solid var(--border); }
.validation.ok, .compact-preview { border-color: var(--success); } .validation.bad { border-color: var(--danger); }
.live-test-card { display: grid; gap: 12px; }
.live-test-heading { display: flex; justify-content: space-between; gap: 12px; align-items: center; }
.live-test-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
.live-test-grid span { display: grid; gap: 3px; min-width: 0; word-break: break-word; color: var(--text); }
.live-test-grid span strong, .live-test-section strong { color: var(--text-muted); font-size: 12px; }
.live-test-section { display: grid; gap: 6px; }
.compact-json { max-height: 220px; margin: 0; }
.warn { color: #f6c177; }
.choice-row { display: flex; flex-wrap: wrap; gap: 8px; } .btn-ghost.selected { border-color: var(--accent); color: var(--accent); } .auth-panel input[type="password"], .auth-modal input[type="password"] { letter-spacing: .08em; }
.auth-summary { display: flex; justify-content: space-between; gap: 16px; align-items: center; }
.modal-backdrop { position: fixed; inset: 0; z-index: 50; background: rgba(0, 0, 0, .48); display: grid; place-items: center; padding: 24px; }
.auth-modal { width: min(760px, 100%); max-height: min(86vh, 860px); overflow: auto; background: var(--surface); border: 1px solid var(--border); border-radius: 18px; padding: 22px; display: flex; flex-direction: column; gap: 18px; box-shadow: 0 22px 70px rgba(0, 0, 0, .35); }
.extra-fields { display: grid; gap: 10px; } .extra-field-row { display: grid; grid-template-columns: minmax(120px, 1fr) minmax(160px, 1.4fr) auto auto; gap: 8px; align-items: center; } .extra-field-row.header { color: var(--text-muted); font-size: 12px; }
.tool-card { white-space: pre-wrap; overflow: auto; max-height: 360px; background: var(--surface2); border: 1px solid var(--border); border-radius: var(--radius); padding: 12px; } .log-panel { max-height: 180px; } .stream-status { display: flex; justify-content: space-between; gap: 12px; align-items: center; } .clarify-card { display: grid; gap: 12px; }
.tool-list, .snippets-list { display: grid; gap: 12px; }
.tool-list-item { border: 1px solid var(--border); border-radius: 14px; padding: 14px; background: var(--surface2); display: grid; gap: 8px; }
.tool-list-main { display: grid; gap: 2px; }
.tool-list-meta { display: flex; flex-wrap: wrap; gap: 8px; }
.tool-functions { word-break: break-word; }
.pill { border: 1px solid var(--border); border-radius: 999px; padding: 3px 8px; font-size: 12px; color: var(--text-muted); }
.pill.ok { color: var(--success); border-color: var(--success); }
.snippet-item { text-align: left; background: var(--surface2); border: 1px solid var(--border); border-radius: var(--radius); padding: 10px; color: var(--text); cursor: pointer; }
.dynamic-config-block {
  border: 1px dashed var(--border);
  border-radius: 14px;
  padding: 12px;
  background: var(--surface2);
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.section-title {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.section-title small {
  color: var(--text-muted);
}

.auth-extra-panel {
  border: 1px solid var(--border);
  border-radius: 14px;
  background: var(--surface2);
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.auth-extra-panel .section-title {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.auth-extra-panel .section-title small {
  color: var(--text-muted);
}

.adapter-edit-layout {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}

.model-code-pane {
  width: 100%;
  min-width: 0;
}

.model-code-pane :deep(.editor-shell) {
  min-height: 420px;
}

.row-title {
  display: flex;
  flex-direction: row;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.adapter-fixed-block {
  border: 1px solid var(--border);
  background: var(--surface2);
  border-radius: 14px;
  overflow: hidden;
}

.adapter-fixed-block summary {
  cursor: pointer;
  padding: 12px 14px;
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: center;
  list-style: none;
}

.adapter-fixed-block summary::-webkit-details-marker {
  display: none;
}

.manual-auth-panel {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: center;
}

.manual-auth-panel.manual-active {
  border-color: var(--accent);
}

.manual-auth-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: flex-end;
}
.adapter-fixed-block summary::before {
  content: '▶';
  color: var(--text-muted);
  font-size: 12px;
  transition: transform .15s ease;
}

.adapter-fixed-block[open] summary::before {
  transform: rotate(90deg);
}

.adapter-fixed-block summary strong {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.adapter-fixed-block summary small {
  margin-left: auto;
  color: var(--text-muted);
}

.adapter-fixed-code {
  margin: 0 12px 12px;
  max-height: 320px;
}

.manual-auth-panel {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: center;
}

.manual-auth-panel.manual-active {
  border-color: var(--accent);
}

.debug-panel-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.adapter-edit-layout .collapsible-panel {
  width: 100%;
}

.manual-auth-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: flex-end;
}

.feedback-pane {
  border-color: color-mix(in srgb, var(--accent) 30%, var(--border));
}

.revision-history {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
small { display: block; color: var(--text-muted); } .green { color: var(--success); }
@media (max-width: 1100px) { .drawer-snippet-layout { grid-template-columns: 1fr; } }
@media (max-width: 960px) { .tool-workspace { grid-template-columns: 1fr; } .step-sidebar { position: static; flex-direction: row; overflow-x: auto; padding-bottom: 4px; } .step-nav { min-width: 190px; } .workspace-card.editor-card { min-height: 70vh; } .card-heading.with-actions { grid-template-columns: 1fr; } .heading-actions { justify-content: flex-start; } }
@media (max-width: 900px) { .hero { flex-direction: column; } }
@media (max-width: 560px) { .page-scroll { padding: 14px; } .workspace-card { padding: 16px; } .grid.two { grid-template-columns: 1fr; } }
</style>
