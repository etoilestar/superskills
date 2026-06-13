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
              <button class="btn-ghost" :disabled="busy || !canRunLiveTest" @click="runLiveTest">测试连接</button>
            </div>
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
              <button class="btn-primary" :disabled="busy || !parsedManifest || !adapterCode" @click="finalizeAuthoring">确认代码 → 生成 snippet</button>
            </div>
          </div>
          <SmartCodeEditor v-model="adapterCode" language="python" fill placeholder="Python adapter code" />
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
          </div>
          <CollapsiblePanel v-model:open="debugOpen" title="调试信息 / Function card preview" description="模型注入时看到的函数卡片预览">
            <pre class="tool-card">{{ cardPreview }}</pre>
          </CollapsiblePanel>
          <div class="step-actions">
            <button class="btn-ghost" @click="activeStep = 'adapter'">返回 Adapter</button>
            <button class="btn-primary" :disabled="!snippetReady" @click="activeStep = 'snippet'">继续确认 Snippet</button>
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
              <button class="btn-primary" :disabled="busy || !lastValidation?.success" @click="registerTool">使用当前 snippet 注册工具</button>
            </div>
          </div>
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
          <label>服务地址 / IP / endpoint<input v-model="configForm.base_url" placeholder="待填写" /></label>
          <label>认证方式<select v-model="configForm.auth_type"><option value="none">none</option><option value="api_key">api_key</option><option value="token">token</option><option value="basic">basic</option><option value="custom">custom</option></select></label>
        </div>
        <div v-if="configForm.auth_type !== 'none'" class="form-row relaxed">
          <label>密钥名称（env）<input v-model="configForm.secret_env" placeholder="XXX_API_KEY" /></label>
          <label>密钥值<input v-model="configForm.secret_value" type="password" autocomplete="off" placeholder="不会写入代码或日志" /></label>
        </div>

        <CollapsiblePanel v-model:open="configAdvancedOpen" title="高级配置（可选）" description="需要时再添加其他字段、sample input 或开启连接测试">
          <div class="extra-fields">
            <div class="extra-field-row header"><span>字段名</span><span>字段值</span><span>敏感</span><span></span></div>
            <div v-for="(field, idx) in configExtraFields" :key="idx" class="extra-field-row">
              <input v-model="field.key" placeholder="tenant_id" />
              <input v-model="field.value" :type="field.sensitive ? 'password' : 'text'" placeholder="值" />
              <label class="inline-check"><input v-model="field.sensitive" type="checkbox" /> 是</label>
              <button class="btn-ghost" type="button" @click="removeExtraField(idx)">删除</button>
            </div>
            <button class="btn-ghost" type="button" @click="addExtraField">+ 添加一项</button>
          </div>
          <label>sample input（可选，简化 JSON）<SmartCodeEditor v-model="sampleInputText" language="json" density="compact" min-height="120px" max-height="220px" /></label>
          <label class="inline-check"><input v-model="allowExternalNetwork" type="checkbox" /> 允许本次连接测试访问外部网络</label>
        </CollapsiblePanel>

        <div v-if="configSaveResult" class="validation ok"><strong>配置已保存</strong><p class="small">env: {{ (configSaveResult.configured_env || []).join(', ') || '无' }} · secrets: {{ (configSaveResult.configured_secrets || []).join(', ') || '无' }}</p></div>
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
import { computed, onMounted, reactive, ref } from 'vue'
import CollapsiblePanel from '../components/CollapsiblePanel.vue'
import SideDrawer from '../components/SideDrawer.vue'
import SmartCodeEditor from '../components/SmartCodeEditor.vue'
import { authorCreatorTool, authorCreatorToolStream, createCreatorToolSnippet, draftCreatorTool, generateCreatorToolCode, listCreatorToolSnippets, listCreatorTools, liveTestCreatorTool, registerCreatorTool, testCreatorToolSnippet, updateCreatorToolSnippet, saveCreatorToolConfig, validateCreatorTool } from '../composables/useCreator.js'

const toolTypes = ['python_helper', 'http_api', 'local_command', 'database_query', 'file_converter', 'document_generator', 'image_generator', 'custom_adapter']
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
const optionalCodeBlock = ref('')
const snippetText = ref('{}')
const clarificationQuestions = ref([])
const clarificationAnswers = ref([])
const planState = ref({})
const configForm = reactive({ base_url: '', method: 'GET', auth_type: 'none', secret_env: '', secret_value: '' })
const configExtraFields = ref([])
const configSaveResult = ref(null)
const liveTestResult = ref(null)
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
const configAdvancedOpen = ref(false)
const registryDrawerOpen = ref(false)
const snippetManagerOpen = ref(false)
const sampleInputText = ref(`{
  "payload": {}
}`)
const lastValidation = ref(null)
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

const form = reactive({ tool_name: '', description: '', tool_type: 'python_helper', input_description: '', output_description: '', needs_secret: false, needs_external_network: false, generates_file: false, high_risk: false })
const parsedManifest = computed(() => { try { return manifestText.value ? JSON.parse(manifestText.value) : null } catch { return null } })
const parsedSample = computed(() => { try { return sampleInputText.value ? JSON.parse(sampleInputText.value) : {} } catch { return {} } })
const configExtra = computed(() => Object.fromEntries(configExtraFields.value.filter(field => field.key).map(field => [field.key.trim(), field.value])))
const parsedConfig = computed(() => ({ base_url: configSaveResult.value?.config_refs?.base_url || configForm.base_url, method: configForm.method, auth_type: configForm.auth_type, secret_env: configForm.secret_env, ...configExtra.value, ...(configSaveResult.value?.config || {}) }))
const requiresConfig = computed(() => planState.value?.requires_config || planState.value?.tool_kind === 'external_api' || planState.value?.requires_external_network || form.needs_external_network)
const canRunLiveTest = computed(() => allowExternalNetwork.value && (configSaveResult.value?.success || planState.value?.ready_for_live_test))
const entrypointConfidenceLabel = computed(() => ({ high: '高置信度', medium: '中等置信度', low: '低置信度' }[planState.value?.suggested_entrypoint?.confidence] || '待确认'))
const canGenerate = computed(() => !busy.value && (planState.value?.ready_for_code_generation || (!requiresConfig.value && parsedManifest.value)) && (!planState.value?.requires_live_test || liveTestResult.value?.success || planState.value?.ready_for_code_generation))
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
async function run(task) { busy.value = true; error.value = ''; try { await task() } catch (e) { error.value = e.message || String(e) } finally { busy.value = false; statusMessage.value = '' } }
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
function applySuggestedEntrypoint(entrypoint = {}) {
  if (!entrypoint || typeof entrypoint !== 'object') return
  if (entrypoint.base_url && !configForm.base_url) configForm.base_url = entrypoint.base_url
  if (entrypoint.method && !configForm.method) configForm.method = entrypoint.method
  if (entrypoint.auth_type && configForm.auth_type === 'none') configForm.auth_type = entrypoint.auth_type
  if (entrypoint.secret_env && !configForm.secret_env) configForm.secret_env = entrypoint.secret_env
}
function filterAnsweredQuestions(questions) {
  const answered = new Set(clarificationQuestions.value.map((question, idx) => (clarificationAnswers.value[idx] ? questionText(question) : '')).filter(Boolean))
  return questions.filter(question => !answered.has(questionText(question)))
}
function configSessionId() { return form.tool_name || planState.value?.operation || form.description || 'default' }
function authorPayload(action) { return { ...payload(), action, code_block: optionalCodeBlock.value || (action === 'generate' ? adapterCode.value : undefined), adapter_code: adapterCode.value, sample_input: parsedSample.value, manifest: parsedManifest.value, validation: lastValidation.value, clarification_answers: clarificationQuestions.value.map((question, idx) => ({ id: question?.id || '', question: questionText(question), answer: clarificationAnswers.value[idx] || '', answer_label: answerLabel(question, clarificationAnswers.value[idx] || '') })).filter(item => item.answer), tool_kind: planState.value?.tool_kind, operation: planState.value?.operation, config: parsedConfig.value, live_test_result: liveTestResult.value, allow_external_network: allowExternalNetwork.value, authoring_context: planState.value?.authoring_context || {} } }
function applyAuthorResult(data) { planState.value = { ...planState.value, ...data }; const nextQuestions = (data.clarification_questions || data.questions || []).slice(0, 3); clarificationQuestions.value = filterAnsweredQuestions(nextQuestions); if (!clarificationQuestions.value.length) clarificationAnswers.value = []; applySuggestedEntrypoint(data.suggested_entrypoint); if (data.config?.base_url && !configForm.base_url) configForm.base_url = data.config.base_url; if (data.manifest) manifestText.value = JSON.stringify(data.manifest || {}, null, 2); if (data.sample_input) sampleInputText.value = JSON.stringify(data.sample_input || {}, null, 2); adapterCode.value = data.adapter_code || adapterCode.value; lastValidation.value = data.validation || lastValidation.value; if (data.live_test_result) liveTestResult.value = data.live_test_result; for (const item of data.authoring_tool_plan || []) logAuthor({ event: 'tool_call_planned', tool: item.tool_name, reason: item.reason }); for (const item of data.authoring_tool_results || []) { logAuthor({ event: 'tool_call_started', tool: item.tool_name }); if (item.requires_input) logAuthor({ event: 'tool_call_requires_input', tool: item.tool_name, schema: item.schema || {} }); logAuthor({ event: 'tool_call_result', tool: item.tool_name, success: Boolean(item.success) }) } snippetText.value = data.snippet ? JSON.stringify(data.snippet, null, 2) : snippetText.value }
function draftManifest() { return run(async () => { const data = await draftCreatorTool(payload()); manifestText.value = JSON.stringify(data.manifest, null, 2); lastValidation.value = null; clarificationQuestions.value = []; activeStep.value = 'planner' }) }
function continuePlanning() { return run(async () => { const data = await authorCreatorTool(authorPayload('configure')); applyAuthorResult(data); activeStep.value = 'planner' }) }
async function saveConfigOnly() {
  configSaveResult.value = await saveCreatorToolConfig({ session_id: configSessionId(), tool_name: form.tool_name, operation: planState.value?.operation || form.description, base_url: configForm.base_url, auth_type: configForm.auth_type, secret_env: configForm.secret_env, secret_value: configForm.secret_value, extra: configExtra.value, additional_fields: configExtraFields.value.filter(field => field.key), sample_input: parsedSample.value, config: parsedConfig.value })
  configForm.secret_value = ''
  const data = await authorCreatorTool({ ...authorPayload('configure'), config: configSaveResult.value.config || parsedConfig.value })
  applyAuthorResult(data)
}
function saveConfigAndContinue() { return run(async () => { await saveConfigOnly(); authConfigOpen.value = false; activeStep.value = 'planner' }) }
function saveConfigAndRunLiveTest() { return run(async () => { await saveConfigOnly(); allowExternalNetwork.value = true; const data = await liveTestCreatorTool({ ...authorPayload('live_test'), allow_external_network: true, config: configSaveResult.value.config || parsedConfig.value }); applyAuthorResult(data); authorLogs.value.push({ time: new Date().toLocaleTimeString(), event: 'live_test_result', success: data.live_test_result?.success }); authConfigOpen.value = false; activeStep.value = 'planner' }) }
function authorDraft() { return run(async () => { const data = await authorCreatorTool(authorPayload('clarify')); authorStage.value = 'clarify'; applyAuthorResult(data); activeStep.value = 'planner' }) }
function generateAdapter() { return run(async () => { adapterCode.value = ''; authorLogs.value = []; streamController.value = new AbortController(); for await (const event of authorCreatorToolStream(authorPayload('generate'), streamController.value.signal)) { logAuthor(event); if (event.event === 'model_delta') adapterCode.value += event.delta || ''; if (event.event === 'final_result') applyAuthorResult(event); } activeStep.value = 'adapter' }) }
function cancelAuthoring() { if (streamController.value) { streamController.value.abort(); streamController.value = null; statusMessage.value = '已取消当前生成' } }
function runLiveTest() { return run(async () => { statusMessage.value = '正在测试连接'; const data = await liveTestCreatorTool(authorPayload('live_test')); applyAuthorResult(data); authorLogs.value.push({ time: new Date().toLocaleTimeString(), event: 'live_test_result', success: data.live_test_result?.success }); activeStep.value = 'planner' }) }
function finalizeAuthoring() { return run(async () => { const data = await authorCreatorTool(authorPayload('finalize')); authorStage.value = 'finalize'; applyAuthorResult(data); if (data.snippet && parsedManifest.value) { const manifest = { ...parsedManifest.value, snippets: [data.snippet] }; manifestText.value = JSON.stringify(manifest, null, 2) } activeStep.value = data.snippet ? 'snippet' : 'validation' }) }
function generateCode() { return run(async () => { const data = await generateCreatorToolCode({ manifest: parsedManifest.value }); adapterCode.value = data.adapter_code; activeStep.value = 'adapter' }) }
function validateTool() { return run(async () => { lastValidation.value = await validateCreatorTool({ manifest: parsedManifest.value, adapter_code: adapterCode.value, sample_input: parsedSample.value, dynamic: true }); activeStep.value = 'validation' }) }
function buildFinalManifestForRegister() { const manifest = { ...(parsedManifest.value || {}) }; const snippet = parseJsonText(snippetText.value); if (snippet && Object.keys(snippet).length) manifest.snippets = [snippet]; return manifest }
function registerTool() { return run(async () => { await registerCreatorTool({ manifest: buildFinalManifestForRegister(), adapter_code: adapterCode.value, sample_input: parsedSample.value, dynamic: true, enable: true }); await loadTools(); registryDrawerOpen.value = true }) }
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
small { display: block; color: var(--text-muted); } .green { color: var(--success); }
@media (max-width: 1100px) { .drawer-snippet-layout { grid-template-columns: 1fr; } }
@media (max-width: 960px) { .tool-workspace { grid-template-columns: 1fr; } .step-sidebar { position: static; flex-direction: row; overflow-x: auto; padding-bottom: 4px; } .step-nav { min-width: 190px; } .workspace-card.editor-card { min-height: 70vh; } .card-heading.with-actions { grid-template-columns: 1fr; } .heading-actions { justify-content: flex-start; } }
@media (max-width: 900px) { .hero { flex-direction: column; } }
@media (max-width: 560px) { .page-scroll { padding: 14px; } .workspace-card { padding: 16px; } .grid.two { grid-template-columns: 1fr; } }
</style>
