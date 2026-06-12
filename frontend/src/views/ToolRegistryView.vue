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
        <button class="btn-primary" :disabled="busy" @click="draftManifest">生成工具定义</button>
      </div>

      <div class="card step-card">
        <div class="step-title"><span>2</span><h2>规划模型总结 / Manifest 草稿</h2></div>
        <textarea v-model="manifestText" class="code" rows="23" spellcheck="false" />
      </div>
    </section>

    <section class="grid two">
      <div class="card step-card">
        <div class="step-title"><span>3</span><h2>工具实现</h2></div>
        <p class="muted small">可点击生成 adapter，也可以直接粘贴 Python helper 代码。上线前必须验证。</p>
        <button class="btn-ghost" :disabled="busy || !parsedManifest" @click="generateCode">生成实现代码</button>
        <textarea v-model="adapterCode" class="code" rows="22" spellcheck="false" placeholder="Python adapter code" />
      </div>

      <div class="card step-card">
        <div class="step-title"><span>4</span><h2>验证 / 注册 / 工具卡片预览</h2></div>
        <label>Sample input<textarea v-model="sampleInputText" class="code" rows="7" spellcheck="false" /></label>
        <div class="actions">
          <button class="btn-primary" :disabled="busy || !parsedManifest" @click="validateTool">运行验证</button>
          <button class="btn-primary" :disabled="busy || !lastValidation?.success" @click="registerTool">注册工具</button>
        </div>
        <div v-if="lastValidation" class="validation" :class="lastValidation.success ? 'ok' : 'bad'">
          <strong>{{ lastValidation.success ? '验证通过' : '验证失败' }}</strong>
          <ul><li v-for="err in lastValidation.errors" :key="err">{{ err }}</li></ul>
          <p v-for="warn in lastValidation.warnings" :key="warn" class="warn">{{ warn }}</p>
        </div>
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

    <p v-if="error" class="error">{{ error }}</p>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { draftCreatorTool, generateCreatorToolCode, listCreatorTools, registerCreatorTool, validateCreatorTool } from '../composables/useCreator.js'

const toolTypes = ['python_helper', 'http_api', 'local_command', 'database_query', 'file_converter', 'document_generator', 'image_generator', 'custom_adapter']
const busy = ref(false)
const error = ref('')
const tools = ref([])
const allowedRolesText = ref('')
const manifestText = ref('')
const adapterCode = ref('')
const sampleInputText = ref('{\n  "payload": {}\n}')
const lastValidation = ref(null)

const form = reactive({ tool_name: '', description: '', tool_type: 'python_helper', input_description: '', output_description: '', needs_secret: false, needs_external_network: false, generates_file: false, high_risk: false })
const parsedManifest = computed(() => { try { return manifestText.value ? JSON.parse(manifestText.value) : null } catch { return null } })
const parsedSample = computed(() => { try { return sampleInputText.value ? JSON.parse(sampleInputText.value) : {} } catch { return {} } })
const cardPreview = computed(() => (lastValidation.value?.tool_card_preview || []).join('\n\n---\n\n') || '验证后展示 Creator prompt 注入的 function card。')

async function run(task) { busy.value = true; error.value = ''; try { await task() } catch (e) { error.value = e.message || String(e) } finally { busy.value = false } }
async function loadTools() { const data = await listCreatorTools(); tools.value = data.tools || [] }
function payload() { return { ...form, allowed_roles: allowedRolesText.value.split(',').map(s => s.trim()).filter(Boolean) } }
function draftManifest() { return run(async () => { const data = await draftCreatorTool(payload()); manifestText.value = JSON.stringify(data.manifest, null, 2); lastValidation.value = null }) }
function generateCode() { return run(async () => { const data = await generateCreatorToolCode({ manifest: parsedManifest.value }); adapterCode.value = data.adapter_code }) }
function validateTool() { return run(async () => { lastValidation.value = await validateCreatorTool({ manifest: parsedManifest.value, adapter_code: adapterCode.value, sample_input: parsedSample.value, dynamic: true }) }) }
function registerTool() { return run(async () => { await registerCreatorTool({ manifest: parsedManifest.value, adapter_code: adapterCode.value, sample_input: parsedSample.value, dynamic: true, enable: true }); await loadTools() }) }

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
.tools-table { display: grid; gap: 6px; } .row { display: grid; grid-template-columns: 1.1fr .8fr .6fr 1.4fr; gap: 12px; padding: 10px; border-bottom: 1px solid var(--border); }
.row.head { color: var(--text-muted); font-size: 12px; text-transform: uppercase; } small { display: block; color: var(--text-muted); } .green { color: var(--success); }
@media (max-width: 1100px) { .grid.two { grid-template-columns: 1fr; } }
</style>
