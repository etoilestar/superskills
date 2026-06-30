<template>
  <div class="skills-page">
    <div class="header">
      <h2>Skills 库</h2>
      <div class="header-actions">
        <button class="btn-ghost" @click="openAllowlist">治理配置</button>
        <button class="btn-primary" @click="openNew">+ 新建 Skill</button>
        <label class="btn-secondary zip-import-label" title="导入来自 skillsmp 的 .zip 文件">
          📦 导入 ZIP
          <input ref="zipInputRef" type="file" accept=".zip" class="hidden-file-input" @change="onZipChange" />
        </label>
      </div>
    </div>

    <div class="body">
      <!-- Skill list -->
      <div class="list-panel">
        <div v-if="loading" class="muted p16">加载中…</div>
        <div v-else-if="skills.length === 0" class="muted p16">
          还没有 Skill。用 Creator 创建第一个吧！
        </div>
        <div
          v-for="sk in skills"
          :key="sk.name"
          class="skill-item"
          :class="{ active: selected?.name === sk.name }"
          @click="select(sk.name)"
        >
          <div class="sk-name">{{ sk.display_name || sk.name }}</div>
          <div class="sk-meta muted">v{{ sk.version || '0.1.0' }} · {{ sk.scope }} · {{ sk.status }}</div>
          <div class="sk-desc muted">{{ sk.description || '暂无描述' }}</div>
        </div>
      </div>

      <!-- Detail panel -->
      <div class="detail-panel">
        <div v-if="!selected && !editing" class="muted p16">← 选择一个 Skill 查看详情</div>

        <!-- Editor mode -->
        <template v-if="editing">
          <div class="detail-header">
            <input v-model="editName" placeholder="skill-name" style="max-width:220px" />
            <button class="btn-primary" @click="save" :disabled="saving">{{ saving ? '保存中…' : '保存' }}</button>
            <button class="btn-ghost" @click="cancelEdit">取消</button>
          </div>
          <div v-if="editError" class="error px16">{{ editError }}</div>
          <textarea v-model="editContent" class="skill-editor" spellcheck="false" />
        </template>

        <!-- View mode -->
        <template v-else-if="selected">
          <div class="detail-header">
            <span class="detail-title">{{ selected.display_name || selected.name }}</span>
            <button class="btn-ghost" @click="startEdit" :disabled="!selected.editable">编辑</button>
            <button class="btn-danger" @click="confirmDelete" :disabled="!selected.editable">删除</button>
          </div>
          <div class="governance-strip">
            <span class="gov-badge">{{ selected.scope }}</span>
            <span class="gov-badge">v{{ selected.version || '0.1.0' }}</span>
            <span class="gov-badge" :class="`status-${selected.status}`">{{ selected.status }}</span>
            <span class="gov-badge" :class="{ blocked: !selected.can_execute }">
              {{ selected.can_execute ? '可执行' : '不可执行' }}
            </span>
          </div>
          <div class="governance-panel">
            <div class="governance-actions">
              <button class="btn-ghost" @click="applyStatus('request_review')" :disabled="!selected.editable">提交审批</button>
              <button class="btn-ghost" @click="applyStatus('approve')" :disabled="!selected.editable">批准</button>
              <button class="btn-ghost" @click="applyStatus('reject')" :disabled="!selected.editable">驳回</button>
              <button class="btn-ghost" @click="applyStatus('quarantine')" :disabled="!selected.editable">隔离</button>
              <button class="btn-ghost" @click="applyStatus(selected.status === 'disabled' ? 'enable' : 'disable')" :disabled="!selected.editable">
                {{ selected.status === 'disabled' ? '启用' : '禁用' }}
              </button>
              <select v-model="portableStyle" class="folder-select" title="导出方式">
                <option value="inline">内联工具函数（推荐）</option>
                <option value="package">携带 portable runtime 包</option>
              </select>
              <a class="btn-secondary zip-download-link" :href="skillZipExportUrl(selected.name, { portable: true, portableStyle })">⬇ 下载 ZIP</a>
              <button class="btn-ghost" @click="saveZipLocally">保存 ZIP 到服务器</button>
              <label class="btn-secondary zip-import-label" :class="{ disabled: !selected.editable }">
                ⬆ 升级 ZIP
                <input type="file" accept=".zip" class="hidden-file-input" :disabled="!selected.editable" @change="onUpgradeZipChange" />
              </label>
              <select v-model="rollbackVersion" class="folder-select" :disabled="!selected.editable || !versions.length">
                <option value="">回滚版本</option>
                <option v-for="ver in versions" :key="`${ver.version}-${ver.timestamp}`" :value="ver.version">
                  {{ ver.version }}
                </option>
              </select>
              <button class="btn-ghost" @click="doRollback" :disabled="!selected.editable || !rollbackVersion">回滚</button>
            </div>
            <div v-if="zipActionMessage" class="muted zip-action-message">{{ zipActionMessage }}</div>
            <div v-if="zipActionError" class="error zip-action-message">{{ zipActionError }}</div>
            <div class="governance-grid">
              <div class="governance-card">
                <div class="governance-title">治理摘要</div>
                <div class="muted">来源：{{ selected.source?.type || 'directory' }}</div>
                <div class="muted">安装方式：{{ selected.install_type || 'local' }}</div>
                <div class="muted">生效作用域：{{ selected.resolved_scope || selected.scope }}</div>
                <div class="muted">可见作用域：{{ (selected.available_scopes || []).join(', ') || '-' }}</div>
              </div>
              <div class="governance-card">
                <div class="governance-title">版本历史</div>
                <div v-if="versions.length" class="governance-list">
                  <div v-for="versionEntry in lastFiveVersions" :key="`${versionEntry.version}-${versionEntry.timestamp}`">
                    v{{ versionEntry.version }} · {{ versionEntry.source_type || 'unknown' }}
                  </div>
                </div>
                <div v-else class="muted">暂无版本历史</div>
              </div>
              <div class="governance-card">
                <div class="governance-title">最近事件</div>
                <div v-if="events.length" class="governance-list">
                  <div v-for="event in events.slice(0, 5)" :key="event.id">
                    {{ event.type }} · {{ new Date(event.timestamp).toLocaleString() }}
                  </div>
                </div>
                <div v-else class="muted">暂无事件</div>
              </div>
            </div>
          </div>
          <pre class="skill-preview">{{ selected.content }}</pre>

          <!-- Asset files section -->
          <div class="assets-section">
            <div class="assets-header">资产文件</div>

            <!-- File groups -->
            <div v-for="folder in assetFolders" :key="folder" class="asset-group">
              <div class="asset-group-title">{{ folder }}</div>
              <div v-if="assets[folder] && assets[folder].length" class="asset-list">
                <div v-for="fname in assets[folder]" :key="fname" class="asset-row">
                  <span class="asset-name">{{ fname }}</span>
                  <button class="btn-icon-edit" :aria-label="`编辑 ${fname}`" :title="`编辑 ${fname}`" :disabled="!selected?.editable" @click="openAssetEditor(folder, fname)">✎</button>
                  <button class="btn-icon-danger" :aria-label="`删除 ${fname}`" :title="`删除 ${fname}`" :disabled="!selected?.editable" @click="removeAsset(folder, fname)">✕</button>
                </div>
              </div>
              <div v-else class="muted asset-empty">暂无文件</div>
            </div>

            <!-- Upload row -->
            <div class="upload-row">
              <select v-model="uploadFolder" class="folder-select" aria-label="上传目录">
                <option v-for="f in assetFolders" :key="f" :value="f">{{ f }}</option>
              </select>
              <label class="file-input-label">
                <input ref="fileInputRef" type="file" class="file-input" aria-label="选择要上传的文件" @change="onFileChange" />
              </label>
              <button class="btn-primary" :disabled="!uploadFile || uploading || !selected?.editable" @click="doUpload">
                {{ uploading ? '上传中…' : '上传' }}
              </button>
            </div>
            <div v-if="uploadError" class="error px16">{{ uploadError }}</div>
            <div v-if="assetError" class="error px16">{{ assetError }}</div>
          </div>
        </template>
      </div>
    </div>

    <!-- Asset editor modal -->
  <div v-if="assetEditor" class="overlay" @click.self="closeAssetEditor">
    <div class="dialog dialog-editor">
      <div class="dialog-title">{{ assetEditor.folder }}/{{ assetEditor.filename }}</div>
      <div v-if="assetEditor.loading" class="muted p16">加载中…</div>
      <div v-else-if="assetEditor.binary" class="muted p16">该文件为二进制，无法编辑。</div>
      <textarea
        v-else
        v-model="assetEditor.content"
        class="skill-editor asset-edit-textarea"
        spellcheck="false"
      />
      <div v-if="assetEditor.error" class="error px16">{{ assetEditor.error }}</div>
      <div class="dialog-actions">
        <template v-if="!assetEditor.binary">
          <button class="btn-primary" :disabled="assetEditor.saving" @click="saveAssetEdit">
            {{ assetEditor.saving ? '保存中…' : '保存' }}
          </button>
        </template>
        <button class="btn-ghost" @click="closeAssetEditor">{{ assetEditor.binary ? '关闭' : '取消' }}</button>
      </div>
    </div>
  </div>

  <!-- ZIP Import modal -->
  <div v-if="zipImport" class="overlay" @click.self="cancelZipImport">
    <div class="dialog">
      <p style="margin-bottom:8px;font-weight:600;">📦 导入 Skill ZIP</p>
      <p class="muted" style="font-size:13px;margin-bottom:16px;">文件：{{ zipImport.filename }}</p>

      <!-- Conflict confirmation -->
      <template v-if="zipImport.conflict">
        <p style="margin-bottom:16px;">
          技能 <strong>{{ zipImport.conflictName }}</strong> 已存在，是否覆盖？
        </p>
        <div class="dialog-actions">
          <button class="btn-danger" :disabled="zipImport.loading" @click="doImportZip(true)">
            {{ zipImport.loading ? '导入中…' : '覆盖导入' }}
          </button>
          <button class="btn-ghost" @click="cancelZipImport">取消</button>
        </div>
      </template>

      <!-- Normal state -->
      <template v-else>
        <div v-if="zipImport.error" class="error" style="margin-bottom:12px;">{{ zipImport.error }}</div>
        <div class="dialog-actions">
          <button class="btn-primary" :disabled="zipImport.loading" @click="doImportZip(false)">
            {{ zipImport.loading ? '导入中…' : '导入' }}
          </button>
          <button class="btn-ghost" @click="cancelZipImport">取消</button>
        </div>
      </template>
    </div>
  </div>

  <!-- Delete confirm -->
    <div v-if="deleteTarget" class="overlay" @click.self="deleteTarget = null">
      <div class="dialog">
        <p>确认删除 <strong>{{ deleteTarget }}</strong>？此操作不可撤销。</p>
        <div class="dialog-actions">
          <button class="btn-danger" @click="doDelete">删除</button>
          <button class="btn-ghost" @click="deleteTarget = null">取消</button>
        </div>
      </div>
    </div>

    <div v-if="allowlistEditor" class="overlay" @click.self="closeAllowlist">
      <div class="dialog dialog-editor">
        <div class="dialog-title">白名单 / 治理配置</div>
        <textarea v-model="allowlistText" class="skill-editor asset-edit-textarea" spellcheck="false" />
        <div v-if="allowlistError" class="error px16">{{ allowlistError }}</div>
        <div class="dialog-actions">
          <button class="btn-primary" @click="saveAllowlistEditor">保存</button>
          <button class="btn-ghost" @click="closeAllowlist">取消</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, onMounted } from 'vue'
import {
  deleteAsset,
  deleteSkill,
  fetchAllowlist,
  fetchAssetContent,
  fetchSkill,
  fetchSkillAssets,
  fetchSkillEvents,
  fetchSkillVersions,
  fetchSkills,
  importSkillZip,
  rollbackSkillVersion,
  saveAllowlist,
  saveAssetContent,
  saveSkill,
  saveSkillZip,
  skillZipExportUrl,
  updateSkillStatus,
  upgradeSkillZip,
  uploadAsset,
} from '../composables/useSkills.js'

const skills = ref([])
const selected = ref(null)
const loading = ref(true)
const editing = ref(false)
const editName = ref('')
const editContent = ref('')
const editError = ref('')
const saving = ref(false)
const deleteTarget = ref(null)
const zipActionMessage = ref('')
const zipActionError = ref('')
const portableStyle = ref('inline')

// assets
const assetFolders = ['assets', 'references', 'scripts']
const assets = ref({ assets: [], references: [], scripts: [] })
const uploadFolder = ref('assets')
const uploadFile = ref(null)
const uploading = ref(false)
const uploadError = ref('')
const assetError = ref('')
const fileInputRef = ref(null)
const events = ref([])
const versions = ref([])
const rollbackVersion = ref('')
const allowlistEditor = ref(false)
const allowlistText = ref('')
const allowlistError = ref('')
const lastFiveVersions = computed(() => versions.value.slice(-5).reverse())

// asset editor
const assetEditor = ref(null)

async function load() {
  loading.value = true
  skills.value = await fetchSkills('manage', { includeHidden: true })
  loading.value = false
}

async function loadAssets(name) {
  try {
    assets.value = await fetchSkillAssets(name)
  } catch {
    assets.value = { assets: [], references: [], scripts: [] }
  }
}

async function select(name) {
  editing.value = false
  selected.value = await fetchSkill(name, 'manage')
  uploadError.value = ''
  assetError.value = ''
  zipActionMessage.value = ''
  zipActionError.value = ''
  await loadAssets(name)
  await loadGovernance(name)
}

async function loadGovernance(name) {
  const [{ events: nextEvents }, versionInfo] = await Promise.all([
    fetchSkillEvents(name),
    fetchSkillVersions(name),
  ])
  events.value = nextEvents
  versions.value = versionInfo.versions || []
  rollbackVersion.value = ''
}

function openNew() {
  selected.value = null
  editing.value = true
  editName.value = ''
  editContent.value = `---\nname: my-skill\ndescription: Describe what this skill does and when to use it.\n---\n\n# My Skill\n\n`
  editError.value = ''
}

function startEdit() {
  if (!selected.value?.editable) return
  editName.value = selected.value.name
  editContent.value = selected.value.content
  editError.value = ''
  editing.value = true
}

function cancelEdit() {
  editing.value = false
  editError.value = ''
}

async function save() {
  const name = editName.value.trim()
  if (!name) { editError.value = 'Skill 名称不能为空'; return }
  saving.value = true
  editError.value = ''
  try {
    await saveSkill(name, editContent.value)
    await load()
    editing.value = false
    selected.value = await fetchSkill(name, 'manage')
    await loadAssets(name)
    await loadGovernance(name)
  } catch (e) {
    editError.value = e.message
  } finally {
    saving.value = false
  }
}

function confirmDelete() {
  if (!selected.value?.editable) return
  deleteTarget.value = selected.value.name
}

async function doDelete() {
  const name = deleteTarget.value
  deleteTarget.value = null
  await deleteSkill(name)
  selected.value = null
  assets.value = { assets: [], references: [], scripts: [] }
  uploadError.value = ''
  assetError.value = ''
  zipActionMessage.value = ''
  zipActionError.value = ''
  await load()
}

function onFileChange(e) {
  uploadFile.value = e.target.files[0] || null
  uploadError.value = ''
}

async function doUpload() {
  if (!uploadFile.value || !selected.value) return
  uploading.value = true
  uploadError.value = ''
  try {
    await uploadAsset(selected.value.name, uploadFolder.value, uploadFile.value)
    uploadFile.value = null
    if (fileInputRef.value) fileInputRef.value.value = ''
    await loadAssets(selected.value.name)
  } catch (e) {
    uploadError.value = e.message
  } finally {
    uploading.value = false
  }
}

async function removeAsset(folder, filename) {
  if (!selected.value) return
  assetError.value = ''
  try {
    await deleteAsset(selected.value.name, folder, filename)
    await loadAssets(selected.value.name)
  } catch (e) {
    assetError.value = e.message
  }
}

async function openAssetEditor(folder, filename) {
  assetEditor.value = { folder, filename, content: '', loading: true, binary: false, saving: false, error: '' }
  try {
    const text = await fetchAssetContent(selected.value.name, folder, filename)
    assetEditor.value = { folder, filename, content: text, loading: false, binary: false, saving: false, error: '' }
  } catch (e) {
    if (e.status === 415) {
      assetEditor.value = { folder, filename, content: '', loading: false, binary: true, saving: false, error: '' }
    } else {
      assetEditor.value = null
      assetError.value = e.message
    }
  }
}

function closeAssetEditor() {
  assetEditor.value = null
}

async function saveAssetEdit() {
  if (!assetEditor.value || !selected.value) return
  assetEditor.value.saving = true
  assetEditor.value.error = ''
  try {
    await saveAssetContent(selected.value.name, assetEditor.value.folder, assetEditor.value.filename, assetEditor.value.content)
    assetEditor.value = null
    await loadAssets(selected.value.name)
  } catch (e) {
    assetEditor.value.error = e.message
  } finally {
    if (assetEditor.value) assetEditor.value.saving = false
  }
}

async function applyStatus(action) {
  if (!selected.value?.editable) return
  await updateSkillStatus(selected.value.name, action)
  await load()
  await select(selected.value.name)
}

async function saveZipLocally() {
  if (!selected.value) return
  zipActionMessage.value = ''
  zipActionError.value = ''
  try {
    const result = await saveSkillZip(selected.value.name, { portable: true, portableStyle: portableStyle.value, mode: 'manage' })
    zipActionMessage.value = `已保存 ZIP：${result.filename}`
  } catch (e) {
    zipActionError.value = e.message
  }
}

async function onUpgradeZipChange(event) {
  const file = event.target.files[0]
  event.target.value = ''
  if (!file || !selected.value?.editable) return
  await upgradeSkillZip(selected.value.name, file)
  await load()
  await select(selected.value.name)
}

async function doRollback() {
  if (!selected.value?.editable || !rollbackVersion.value) return
  await rollbackSkillVersion(selected.value.name, rollbackVersion.value)
  await load()
  await select(selected.value.name)
}

async function openAllowlist() {
  const payload = await fetchAllowlist()
  allowlistText.value = JSON.stringify(payload, null, 2)
  allowlistError.value = ''
  allowlistEditor.value = true
}

function closeAllowlist() {
  allowlistEditor.value = false
}

async function saveAllowlistEditor() {
  try {
    await saveAllowlist(JSON.parse(allowlistText.value))
    allowlistEditor.value = false
    await load()
  } catch (e) {
    allowlistError.value = e.message
  }
}

// zip import
const zipInputRef = ref(null)
const zipImport = ref(null)  // null | { filename, file, loading, error, conflict, conflictName }

function onZipChange(e) {
  const file = e.target.files[0]
  if (zipInputRef.value) zipInputRef.value.value = ''
  if (!file) return
  zipImport.value = { filename: file.name, file, loading: false, error: '', conflict: false, conflictName: '' }
}

function cancelZipImport() {
  zipImport.value = null
}

async function doImportZip(overwrite) {
  if (!zipImport.value) return
  zipImport.value.loading = true
  zipImport.value.error = ''
  zipImport.value.conflict = false
  try {
    const result = await importSkillZip(zipImport.value.file, overwrite)
    zipImport.value = null
    await load()
    selected.value = await fetchSkill(result.name, 'manage')
    await loadAssets(result.name)
    await loadGovernance(result.name)
  } catch (e) {
    if (e.status === 409) {
      zipImport.value.conflict = true
      zipImport.value.conflictName = e.skillName || ''
      zipImport.value.loading = false
    } else {
      zipImport.value.error = e.message
      zipImport.value.loading = false
    }
  }
}

onMounted(load)
</script>

<style scoped>
.skills-page { display: flex; flex-direction: column; height: 100%; overflow: hidden; }

.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 20px 24px 12px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.header h2 { font-size: 18px; font-weight: 600; }
.header-actions { display: flex; gap: 8px; align-items: center; }

.zip-import-label {
  cursor: pointer;
}
.zip-download-link {
  text-decoration: none;
}
.zip-action-message {
  padding: 0 16px 8px;
}
.zip-import-label.disabled {
  opacity: 0.5;
  pointer-events: none;
}
.hidden-file-input {
  display: none;
}
.btn-secondary {
  background: var(--surface2);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: var(--radius, 8px);
  padding: 8px 16px;
  font: inherit;
  transition: background 0.15s, border-color 0.15s;
  cursor: pointer;
  display: inline-block;
}
.btn-secondary:hover { background: var(--surface); border-color: var(--accent); }

.body { display: flex; flex: 1; overflow: hidden; }

.list-panel {
  width: 240px;
  flex-shrink: 0;
  border-right: 1px solid var(--border);
  overflow-y: auto;
}

.skill-item {
  padding: 14px 16px;
  cursor: pointer;
  border-bottom: 1px solid var(--border);
  transition: background 0.15s;
}
.skill-item:hover { background: var(--surface2); }
.skill-item.active { background: var(--surface2); border-left: 3px solid var(--accent); }

.sk-name { font-weight: 500; margin-bottom: 4px; }
.sk-meta { font-size: 11px; margin-bottom: 4px; }
.sk-desc { font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

.detail-panel { flex: 1; display: flex; flex-direction: column; overflow: hidden; }

.detail-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.detail-title { font-weight: 600; flex: 1; }

.governance-strip {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  padding: 10px 16px 0;
}

.gov-badge {
  padding: 4px 8px;
  border-radius: 999px;
  background: var(--surface2);
  font-size: 12px;
}

.gov-badge.blocked { color: var(--danger, #e55); }
.status-approved { color: var(--accent, #4c8); }
.status-disabled,
.status-rejected,
.status-quarantined { color: var(--danger, #e55); }

.governance-panel {
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.governance-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.governance-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}

.governance-card {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
  background: var(--surface);
}

.governance-title {
  font-weight: 600;
  margin-bottom: 6px;
}

.governance-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
}

.skill-preview {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  font-family: 'Fira Code', 'Cascadia Code', monospace;
  font-size: 13px;
  white-space: pre-wrap;
  word-break: break-word;
  background: var(--bg);
  margin: 0;
  min-height: 0;
}

.skill-editor {
  flex: 1;
  border: none;
  border-radius: 0;
  font-family: 'Fira Code', 'Cascadia Code', monospace;
  font-size: 13px;
  background: var(--bg);
  padding: 16px;
  resize: none;
}

.p16 { padding: 16px; }
.px16 { padding: 4px 16px; }

/* Asset files section */
.assets-section {
  border-top: 1px solid var(--border);
  flex-shrink: 0;
  padding: 12px 16px 16px;
  overflow-y: auto;
  max-height: 300px;
}
.assets-header {
  font-weight: 600;
  font-size: 13px;
  margin-bottom: 10px;
}
.asset-group { margin-bottom: 10px; }
.asset-group-title {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--muted, #888);
  margin-bottom: 4px;
}
.asset-list { display: flex; flex-direction: column; gap: 2px; }
.asset-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 6px;
  border-radius: 4px;
  background: var(--surface2);
}
.asset-name { flex: 1; font-size: 12px; font-family: 'Fira Code', monospace; word-break: break-all; }
.asset-empty { font-size: 12px; padding: 2px 0; }
.btn-icon-danger {
  background: none;
  border: none;
  cursor: pointer;
  color: var(--danger, #e55);
  padding: 0 4px;
  font-size: 11px;
  line-height: 1;
  border-radius: 3px;
  flex-shrink: 0;
}
.btn-icon-danger:hover { background: var(--danger-bg, rgba(220,50,50,0.12)); }
.upload-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  flex-wrap: wrap;
}
.folder-select {
  font-size: 12px;
  padding: 4px 6px;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: var(--surface);
  color: inherit;
}
.file-input { flex: 1; font-size: 12px; min-width: 0; }

.overlay {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.6);
  display: flex; align-items: center; justify-content: center;
  z-index: 100;
}
.dialog {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 24px;
  max-width: 380px;
  width: 90%;
}
.dialog p { margin-bottom: 20px; }
.dialog-actions { display: flex; gap: 10px; margin-top: 12px; }
.dialog-title { font-weight: 600; font-size: 14px; margin-bottom: 12px; font-family: 'Fira Code', monospace; }
.dialog-editor { width: 680px; max-width: 94vw; display: flex; flex-direction: column; }
.asset-edit-textarea { flex: 1; min-height: 340px; resize: vertical; border: 1px solid var(--border); border-radius: 6px; margin-bottom: 4px; }
.btn-icon-edit {
  background: none;
  border: none;
  cursor: pointer;
  color: var(--accent, #4a9);
  padding: 0 4px;
  font-size: 13px;
  line-height: 1;
  border-radius: 3px;
  flex-shrink: 0;
}
.btn-icon-edit:hover { background: var(--surface2); }
</style>
