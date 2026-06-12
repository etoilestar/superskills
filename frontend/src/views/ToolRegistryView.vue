<template>
  <div class="tool-registry page-scroll">
    <header class="hero">
      <div>
        <p class="eyebrow">Creator Tool Registry</p>
        <h1>在线工具制作 / 注册</h1>
        <p class="muted">用自然语言描述工具，生成函数级 manifest，补全 adapter，实现验证后注册到 Creator 工具库。</p>
      </div>
      <button class="btn-ghost" @click="loadTools">刷新工具列表</button>
    </header>

    <section class="grid two">
      <div class="card step-card">
        <div class="step-title"><span>1</span><h2>自然语言描述工具</h2></div>
        <label>工具名称<input v-model="form.tool_name" placeholder="markdown_to_pdf" /></label>
        <label>工具用途描述<textarea v-model="form.description" rows="4" placeholder="我想注册一个工具，用来把 markdown 转成 PDF..." /></label>
        <label>可选：粘贴已有 Python 代码<textarea v-model="optionalCodeBlock" class="code" rows="7" spellcheck="false" placeholder="def save_markdown(payload): ..." /></label>
        <div class="form-row">
          <label>工具类型<select v-model="form.tool_type"><option v-for="type in toolTypes" :key="type" :value="type">{{ type }}</option></select></label>
          <label>允许角色<input v-model="allowedRolesText" placeholder="pdf_builder,document_generator" /></label>
        </div>
        <label>输入描述<textarea v-model="form.input_description" rows="2" /></label>
        <label>输出描述<textarea v-model="form.output_description" rows="2" /></label>
        <div class="checks">
          <label><input v-model="form.needs_secret" type="checkbox" /> 需要密钥</label>
          <label><input v-model="form.needs_external_network" type="checkbox" /> 需要外部网络</label>
          <label><input v-model="form.generates_file" type="checkbox" /> 生成文件</label>
          <label><input v-model="form.high_risk" type="checkbox" /> 高风险</label>
        </div>
        <div class="actions">
          <button class="btn-primary" :disabled="busy" @click="authorDraft">智能生成工具草稿</button>
          <button class="btn-ghost" :disabled="busy" @click="draftManifest">旧版规则草稿</button>
        </div>
      </div>

      <div class="card step-card">
        <div class="step-title"><span>2</span><h2>Planner 结果 / Manifest 草稿</h2></div>
        <div v-if="clarificationQuestions.length" class="validation bad">
          <strong>需要补充信息</strong>
          <ul><li v-for="question in clarificationQuestions" :key="question">{{ question }}</li></ul>
        </div>
        <textarea v-model="manifestText" class="code" rows="23" spellcheck="false" />
      </div>
    </section>

    <section class="grid two">
      <div class="card step-card">
        <div class="step-title"><span>3</span><h2>工具实现</h2></div>
        <p class="muted small">统一 author 链路会根据可选代码块自动生成或规范化 adapter。上线前必须先确认代码，再生成 snippet。</p>
        <div class="actions">
          <button class="btn-ghost" :disabled="busy || !parsedManifest" @click="generateCode">旧版生成实现</button>
          <button class="btn-primary" :disabled="busy || !parsedManifest || !adapterCode" @click="finalizeAuthoring">确认代码 → 生成 snippet</button>
        </div>
        <textarea v-model="adapterCode" class="code" rows="22" spellcheck="false" placeholder="Python adapter code" />
      </div>

      <div class="card step-card">
        <div class="step-title"><span>4</span><h2>验证 / 注册 / 工具卡片预览</h2></div>
        <label>Sample input<textarea v-model="sampleInputText" class="code" rows="7" spellcheck="false" /></label>
        <div class="actions">
          <button class="btn-primary" :disabled="busy || !parsedManifest" @click="validateTool">运行验证</button>
          <button class="btn-primary" :disabled="busy || !lastValidation?.success" @click="registerTool">确认 snippet 并注册</button>
        </div>
        <div v-if="lastValidation" class="validation" :class="lastValidation.success ? 'ok' : 'bad'">
          <strong>{{ lastValidation.success ? '验证通过' : '验证失败' }}</strong>
          <ul><li v-for="err in lastValidation.errors" :key="err">{{ err }}</li></ul>
          <p v-for="warn in lastValidation.warnings" :key="warn" class="warn">{{ warn }}</p>
        </div>
        <label>生成的 Snippet<textarea v-model="snippetText" class="code" rows="10" spellcheck="false" placeholder="确认代码后生成 snippet" /></label>
        <pre class="tool-card">{{ cardPreview }}</pre>
      </div>
    </section>

    <section class="card">
      <div class="step-title"><span>5</span><h2>已启用 / 已注册工具</h2></div>
      <div class="tools-table">
        <div class="row head"><span>名称</span><span>策略</span><span>状态</span><span>函数卡</span></div>
        <div v-for="tool in tools" :key="tool.name" class="row">
          <span><strong>{{ tool.name }}</strong><small>{{ tool.display_name }}</small></span>
          <span>{{ tool.usage_policy }}</span>
          <span :class="tool.creator_available ? 'green' : 'muted'">{{ tool.creator_available ? 'enabled' : 'disabled' }}</span>
          <span>{{ (tool.functions || []).map(fn => fn.function_name).join(', ') || tool.helper_imports?.join(', ') }}</span>
        </div>
      </div>
    </section>

    <section class="grid two">
      <div class="card step-card">
        <div class="step-title"><span>6</span><h2>Tool Usage Snippets</h2></div>
        <p class="muted small">编辑模型会看到的 import、最小调用、返回规则和反例；可预览 Creator 注入格式并做静态 smoke test。</p>
        <div class="form-row">
          <label>选择工具<select v-model="selectedToolName" @change="loadSnippets"><option value="">选择工具</option><option v-for="tool in tools" :key="tool.name" :value="tool.name">{{ tool.name }}</option></select></label>
          <label>Snippet 类型<select v-model="snippetForm.kind"><option v-for="kind in snippetKinds" :key="kind" :value="kind">{{ kind }}</option></select></label>
        </div>
        <div class="form-row">
          <label>ID<input v-model="snippetForm.id" placeholder="create_pdf.minimal_text_pdf" /></label>
          <label>标题<input v-model="snippetForm.title" placeholder="Create a simple PDF" /></label>
        </div>
        <label>适用 roles（逗号分隔）<input v-model="snippetRolesText" placeholder="pdf_builder,document_generator" /></label>
        <label>适用 capabilities（逗号分隔）<input v-model="snippetCapabilitiesText" placeholder="pdf_generation" /></label>
        <label>failure layers（逗号分隔）<input v-model="snippetFailuresText" placeholder="final_platform_output_value_invalid,artifact_missing" /></label>
        <label>描述<textarea v-model="snippetForm.description" rows="2" /></label>
        <label>正确调用代码<textarea v-model="snippetForm.code" class="code" rows="10" spellcheck="false" /></label>
        <div class="form-row">
          <label>期望输入 shape(JSON)<textarea v-model="snippetInputShapeText" class="code" rows="5" spellcheck="false" /></label>
          <label>期望输出 shape(JSON)<textarea v-model="snippetOutputShapeText" class="code" rows="5" spellcheck="false" /></label>
        </div>
        <label>return rule<textarea v-model="snippetForm.return_rule" rows="2" /></label>
        <label>anti patterns（每行一条）<textarea v-model="snippetAntiPatternsText" rows="4" /></label>
        <div class="form-row">
          <label>usage policy<select v-model="snippetForm.usage_policy"><option>helper_preferred</option><option>helper_required</option><option>self_implementation_allowed</option></select></label>
          <label>priority<input v-model.number="snippetForm.priority" type="number" /></label>
        </div>
        <div class="actions">
          <button class="btn-primary" :disabled="busy || !selectedToolName" @click="saveSnippet">新增 / 保存 snippet</button>
          <button class="btn-ghost" :disabled="busy || !selectedToolName || !snippetForm.id" @click="runSnippetSmokeTest">运行 smoke test</button>
        </div>
        <p v-if="snippetTestResult" class="small" :class="snippetTestResult.success ? 'green' : 'error'">Smoke test: {{ snippetTestResult.success ? 'passed' : 'failed' }} {{ snippetTestResult.message || '' }}</p>
      </div>

      <div class="card step-card">
        <div class="step-title"><span>7</span><h2>Creator Snippet 预览</h2></div>
        <div class="snippets-list">
          <button v-for="snippet in snippets" :key="snippet.id" class="snippet-item" @click="editSnippet(snippet)">
            <strong>{{ snippet.id }}</strong><small>{{ snippet.kind }} · priority {{ snippet.priority }}</small>
          </button>
        </div>
        <pre class="tool-card">{{ snippetPreview }}</pre>
      </div>
    </section>

    <p v-if="error" class="error">{{ error }}</p>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { authorCreatorTool, createCreatorToolSnippet, draftCreatorTool, generateCreatorToolCode, listCreatorToolSnippets, listCreatorTools, registerCreatorTool, testCreatorToolSnippet, updateCreatorToolSnippet, validateCreatorTool } from '../composables/useCreator.js'

const toolTypes = ['python_helper', 'http_api', 'local_command', 'database_query', 'file_converter', 'document_generator', 'image_generator', 'custom_adapter']
const snippetKinds = ['minimal_usage', 'multi_input_usage', 'file_output_usage', 'batch_usage', 'error_repair_usage', 'anti_pattern', 'trial_run_usage']
const busy = ref(false)
const error = ref('')
const tools = ref([])
const allowedRolesText = ref('')
const manifestText = ref('')
const adapterCode = ref('')
const optionalCodeBlock = ref('')
const snippetText = ref('{}')
const clarificationQuestions = ref([])
const authorStage = ref('draft')
const sampleInputText = ref('{\n  "payload": {}\n}')
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

const form = reactive({ tool_name: '', description: '', tool_type: 'python_helper', input_description: '', output_description: '', needs_secret: false, needs_external_network: false, generates_file: false, high_risk: false })
const parsedManifest = computed(() => { try { return manifestText.value ? JSON.parse(manifestText.value) : null } catch { return null } })
const parsedSample = computed(() => { try { return sampleInputText.value ? JSON.parse(sampleInputText.value) : {} } catch { return {} } })
const cardPreview = computed(() => (lastValidation.value?.tool_card_preview || []).join('\n\n---\n\n') || '验证后展示 Creator prompt 注入的 function card。')
const snippetPreview = computed(() => snippets.value.map(snippet => snippet.formatted || '').join('\n\n---\n\n') || '选择工具后展示 Creator 会看到的 Tool Snippet。')

async function run(task) { busy.value = true; error.value = ''; try { await task() } catch (e) { error.value = e.message || String(e) } finally { busy.value = false } }
async function loadTools() { const data = await listCreatorTools(); tools.value = data.tools || [] }
function payload() { return { ...form, allowed_roles: allowedRolesText.value.split(',').map(s => s.trim()).filter(Boolean) } }
function authorPayload(stage) { return { ...payload(), stage, code_block: optionalCodeBlock.value || (stage === 'draft' ? adapterCode.value : undefined), adapter_code: adapterCode.value, sample_input: parsedSample.value, manifest: parsedManifest.value, validation: lastValidation.value } }
function draftManifest() { return run(async () => { const data = await draftCreatorTool(payload()); manifestText.value = JSON.stringify(data.manifest, null, 2); lastValidation.value = null; clarificationQuestions.value = [] }) }
function authorDraft() { return run(async () => { const data = await authorCreatorTool(authorPayload('draft')); authorStage.value = 'draft'; clarificationQuestions.value = data.questions || []; manifestText.value = JSON.stringify(data.manifest || {}, null, 2); sampleInputText.value = JSON.stringify(data.sample_input || {}, null, 2); adapterCode.value = data.adapter_code || adapterCode.value; lastValidation.value = data.validation || null; snippetText.value = data.snippet ? JSON.stringify(data.snippet, null, 2) : snippetText.value }) }
function finalizeAuthoring() { return run(async () => { const data = await authorCreatorTool(authorPayload('finalize')); authorStage.value = 'finalize'; lastValidation.value = data.validation || lastValidation.value; snippetText.value = data.snippet ? JSON.stringify(data.snippet, null, 2) : snippetText.value; if (data.snippet && parsedManifest.value) { const manifest = { ...parsedManifest.value, snippets: [data.snippet] }; manifestText.value = JSON.stringify(manifest, null, 2) } }) }
function generateCode() { return run(async () => { const data = await generateCreatorToolCode({ manifest: parsedManifest.value }); adapterCode.value = data.adapter_code }) }
function validateTool() { return run(async () => { lastValidation.value = await validateCreatorTool({ manifest: parsedManifest.value, adapter_code: adapterCode.value, sample_input: parsedSample.value, dynamic: true }) }) }
function registerTool() { return run(async () => { await registerCreatorTool({ manifest: parsedManifest.value, adapter_code: adapterCode.value, sample_input: parsedSample.value, dynamic: true, enable: true }); await loadTools() }) }
function parseJsonText(text) { try { return text ? JSON.parse(text) : {} } catch { return {} } }
function splitLines(text) { return text.split('\n').map(s => s.trim()).filter(Boolean) }
function splitCsv(text) { return text.split(',').map(s => s.trim()).filter(Boolean) }
function buildSnippetPayload() { return { ...snippetForm, applies_to: { roles: splitCsv(snippetRolesText.value), capabilities: splitCsv(snippetCapabilitiesText.value), failure_layers: splitCsv(snippetFailuresText.value) }, expected_input_shape: parseJsonText(snippetInputShapeText.value), expected_output_shape: parseJsonText(snippetOutputShapeText.value), anti_patterns: splitLines(snippetAntiPatternsText.value), requires: splitCsv(snippetCapabilitiesText.value) } }
function loadSnippets() { return run(async () => { if (!selectedToolName.value) { snippets.value = []; return } const data = await listCreatorToolSnippets(selectedToolName.value); snippets.value = data.snippets || []; snippetTestResult.value = null }) }
function editSnippet(snippet) { snippetForm.id = snippet.id; snippetForm.title = snippet.title; snippetForm.kind = snippet.kind || 'minimal_usage'; snippetForm.description = snippet.description || ''; snippetForm.code = snippet.code || ''; snippetForm.return_rule = snippet.return_rule || ''; snippetForm.usage_policy = snippet.usage_policy || 'helper_preferred'; snippetForm.priority = snippet.priority || 0; snippetRolesText.value = (snippet.applies_to?.roles || []).join(','); snippetCapabilitiesText.value = (snippet.applies_to?.capabilities || snippet.requires || []).join(','); snippetFailuresText.value = (snippet.applies_to?.failure_layers || []).join(','); snippetInputShapeText.value = JSON.stringify(snippet.expected_input_shape || {}, null, 2); snippetOutputShapeText.value = JSON.stringify(snippet.expected_output_shape || {}, null, 2); snippetAntiPatternsText.value = (snippet.anti_patterns || []).join('\n'); snippetTestResult.value = null }
function saveSnippet() { return run(async () => { const payload = buildSnippetPayload(); const exists = snippets.value.some(item => item.id === payload.id); if (exists) await updateCreatorToolSnippet(selectedToolName.value, payload.id, payload); else await createCreatorToolSnippet(selectedToolName.value, payload); await loadSnippets() }) }
function runSnippetSmokeTest() { return run(async () => { if (!snippets.value.some(item => item.id === snippetForm.id)) await saveSnippet(); snippetTestResult.value = await testCreatorToolSnippet(selectedToolName.value, snippetForm.id) }) }

onMounted(loadTools)
</script>

<style scoped>
.page-scroll { overflow: auto; padding: 28px; display: flex; flex-direction: column; gap: 18px; }
.hero, .card { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 20px; }
.hero { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; }
.eyebrow { color: var(--accent); text-transform: uppercase; letter-spacing: .08em; font-size: 12px; }
h1 { font-size: 26px; margin: 2px 0 6px; } h2 { font-size: 16px; }
.grid { display: grid; gap: 18px; } .grid.two { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.step-card { display: flex; flex-direction: column; gap: 12px; }
.step-title { display: flex; align-items: center; gap: 10px; }
.step-title span { width: 28px; height: 28px; border-radius: 50%; background: var(--accent); display: grid; place-items: center; font-weight: 700; }
.form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
label { display: flex; flex-direction: column; gap: 6px; color: var(--text-muted); }
.checks { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; }
.checks label { flex-direction: row; align-items: center; } .checks input { width: auto; }
.code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
.actions { display: flex; gap: 10px; } .small { font-size: 12px; }
.validation { padding: 10px; border-radius: var(--radius); border: 1px solid var(--border); }
.validation.ok { border-color: var(--success); } .validation.bad { border-color: var(--danger); }
.warn { color: #f6c177; }
.tool-card { white-space: pre-wrap; overflow: auto; max-height: 360px; background: var(--surface2); border: 1px solid var(--border); border-radius: var(--radius); padding: 12px; }
.tools-table, .snippets-list { display: grid; gap: 6px; } .snippet-item { text-align: left; background: var(--surface2); border: 1px solid var(--border); border-radius: var(--radius); padding: 10px; color: var(--text); cursor: pointer; } .row { display: grid; grid-template-columns: 1.1fr .8fr .6fr 1.4fr; gap: 12px; padding: 10px; border-bottom: 1px solid var(--border); }
.row.head { color: var(--text-muted); font-size: 12px; text-transform: uppercase; } small { display: block; color: var(--text-muted); } .green { color: var(--success); }
@media (max-width: 1100px) { .grid.two { grid-template-columns: 1fr; } }
</style>
