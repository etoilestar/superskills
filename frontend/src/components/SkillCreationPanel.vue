<template>
  <div class="skill-creation-panel">
    <!-- Header -->
    <div class="panel-header">
      <span class="panel-icon">🛠</span>
      <span class="panel-title">
        创建 Skill：
        <input
          v-if="editingName"
          v-model="localSkillName"
          class="name-input"
          @blur="commitName"
          @keyup.enter="commitName"
          ref="nameInputRef"
          :class="{ error: nameError }"
        />
        <span v-else class="skill-name-display" @click="startEditName" title="点击修改名称">
          {{ localSkillName }}
          <span class="edit-hint">✏️</span>
        </span>
      </span>
      <span v-if="nameError" class="name-error">{{ nameError }}</span>
    </div>

    <!-- Progress bar -->
    <div class="progress-bar-wrap">
      <div
        class="progress-bar-fill"
        :style="{ width: progressPercent + '%' }"
      />
      <span class="progress-label">{{ doneCount }}/{{ localFiles.length }} 文件已完成</span>
    </div>

    <div class="generation-stages" aria-label="Skill 生成阶段">
      <div v-for="item in generationStages" :key="item.label" :class="item.status">
        <span>{{ statusIcon(item.status) }}</span><strong>{{ item.label }}</strong>
      </div>
    </div>

    <details class="file-tree-panel">
      <summary>文件结构与摘要 <span>{{ localFiles.length }} 项</span></summary>
      <div v-for="file in localFiles" :key="`tree-${file.path}`" class="tree-row">
        <code>{{ treePrefix(file.path) }}{{ file.path }}</code>
        <span>{{ file.purpose || file.role || '待生成' }}</span>
      </div>
    </details>

    <!-- Warnings from blueprint parser -->
    <div v-if="visibleWarnings.length" class="warnings">
      <div v-for="(w, i) in visibleWarnings" :key="i" class="warning-item">⚠️ {{ warningMessage(w) }}</div>
    </div>

    <div v-if="hasCreationBlockers" class="warnings">
      <div
        v-for="(blocker, i) in props.creationBlockers"
        :key="i"
        class="warning-item"
      >
        ❌ {{ blocker.message || blocker.type }}
      </div>
    </div>

    <!-- File list -->
    <ul class="file-list">
      <li
        v-for="(file, idx) in localFiles"
        :key="file.path"
        class="file-item"
        :class="file.status"
      >
        <!-- Status icon -->
        <span class="file-icon">{{ statusIcon(file.status) }}</span>

        <!-- Path & meta -->
        <span class="file-path">{{ file.path }}</span>
        <span v-if="file.role" class="role-badge" :class="{ warning: file.low_confidence }">
          {{ file.role }}<span v-if="file.low_confidence"> · low confidence</span>
        </span>
        <span v-if="file.status === 'done'" class="file-meta">
          已写入 ({{ formatBytes(file.bytesWritten) }})
        </span>
        <span v-else-if="file.status === 'generating'" class="file-meta generating">
          {{ file.repairMessage || '生成中…' }}
        </span>
        <span v-else-if="file.status === 'writing'" class="file-meta">写入中…</span>
        <span v-else-if="file.status === 'error'" class="file-meta error">{{ file.error }}</span>
        <span v-else-if="file.can_skip && file.status === 'pending'" class="file-meta muted">
          可跳过
        </span>

        <!-- Action buttons -->
        <span class="file-actions">
          <!-- 具体 assets 文件 / asset_requirement：必须由用户上传 -->
          <template v-if="isAssetFile(file)">
              <input
                type="file"
                class="btn-small"
                @change="event => handleAssetUpload(file, event)"
              />

              <span v-if="file.uploaded" class="text-green-600">已上传</span>
              <span v-if="file.error" class="text-red-600">{{ file.error }}</span>

              <button
                v-if="canSkipFile(file)"
                class="btn-small btn-skip"
                @click="skipFile(idx)"
              >
                跳过
              </button>

              <button
                v-if="canRemoveFile(file)"
                class="btn-small btn-remove"
                @click="removeFile(idx)"
              >
                移除
              </button>
          </template>

          <template v-else-if="looksLikeDirectoryPath(file.path)">
              <span class="file-meta muted">目录路径，无需上传</span>

              <button
                v-if="canRemoveFile(file)"
                class="btn-small btn-remove"
                @click="removeFile(idx)"
              >
                移除
              </button>
            </template>

          <!-- 其他文件保持生成/写入按钮 -->
          <template v-else>
            <button
              v-if="file.status === 'preview'"
              class="btn-small btn-write"
              @click="writeOneFile(idx)"
            >
              写入
            </button>
            <button
              v-if="file.status === 'error' || file.status === 'preview'"
              class="btn-small btn-retry"
              @click="generateOneFile(idx)"
            >
              重新生成
            </button>
            <button
              v-if="file.status === 'done'"
              class="btn-small btn-preview"
              @click="togglePreview(idx)"
            >
              {{ file.showPreview ? '收起' : '预览' }}
            </button>
            <button
              v-if="canSkipFile(file)"
              class="btn-small btn-skip"
              @click="skipFile(idx)"
            >
              跳过
            </button>
            <button
              v-if="canRemoveFile(file)"
              class="btn-small btn-remove"
              @click="removeFile(idx)"
            >
              移除
            </button>
          </template>
        </span>
        <!-- Inline editor for preview/error states -->
        <div v-if="file.status === 'preview' || (file.status === 'error' && file.generatedContent)"
          class="file-editor-wrap"
        >
          <textarea
            v-model="file.generatedContent"
            class="file-editor"
            rows="12"
            :placeholder="'在此编辑 ' + file.path + ' 的内容…'"
          />
        </div>

        <!-- Collapsible preview for done files -->
        <div v-if="file.status === 'done' && file.showPreview" class="file-preview-wrap">
          <pre class="file-preview">{{ file.generatedContent }}</pre>
        </div>
      </li>

      <!-- Add file row -->
      <li class="file-item add-file-row">
        <button class="btn-add-file" @click="addFilePrompt = true">＋ 添加文件</button>
        <div v-if="addFilePrompt" class="add-file-form">
          <input
            v-model="newFilePath"
            class="add-file-input"
            placeholder="如：scripts/helper.py"
            @keyup.enter="addFile"
          />
          <input
            v-model="newFilePurpose"
            class="add-file-input"
            placeholder="职责说明（可选）"
            @keyup.enter="addFile"
          />
          <button class="btn-small btn-write" @click="addFile">确认添加</button>
          <button class="btn-small" @click="addFilePrompt = false">取消</button>
        </div>
      </li>
    </ul>

    <!-- Main action buttons -->
    <div class="panel-actions">
      <button
        v-if="phase === 'idle'"
        class="btn-primary"
        @click="startCreation"
        :disabled="localFiles.length === 0 || hasCreationBlockers"
      >
        开始创建
      </button>
      <button
        v-if="phase === 'running'"
        class="btn-secondary"
        @click="pauseCreation"
      >
        暂停
      </button>
      <button
        v-if="phase === 'paused'"
        class="btn-primary"
        @click="resumeCreation"
      >
        继续
      </button>
      <button
        v-if="phase === 'running' || phase === 'paused'"
        class="btn-secondary"
        @click="skipOptionalFiles"
      >
        跳过所有可选文件
      </button>
    </div>

    <!-- Post-creation status -->
    <div
      v-if="phase === 'validating' || phase === 'packaging' || phase === 'complete' || phase === 'failed'"
      class="post-status"
    >
      <section class="runtime-panel">
        <div class="runtime-heading">
          <div><small>E2E RUNTIME</small><h3>{{ runtimeTitle }}</h3></div>
          <span class="runtime-badge" :class="runtimeStatus">{{ runtimeStatusText }}</span>
        </div>
        <div v-if="currentRuntimeStep" class="runtime-current">
          <small>当前唯一运行步骤</small>
          <strong>Step {{ currentRuntimeStep.index + 1 }}/{{ runtimeSteps.length || 1 }} · {{ currentRuntimeStep.script }}</strong>
          <p v-if="currentRuntimeStep.input"><b>输入：</b>{{ currentRuntimeStep.input }}</p>
          <p v-if="currentRuntimeStep.output"><b>输出：</b>{{ currentRuntimeStep.output }}</p>
          <p v-if="currentRuntimeStep.duration"><b>耗时：</b>{{ currentRuntimeStep.duration }}</p>
          <span>● running</span>
        </div>
        <div v-if="runtimeActivityRows.length" class="runtime-live-feed" aria-live="polite">
          <h4>实时执行记录</h4>
          <article v-for="row in runtimeActivityRows" :key="row.key" :class="row.status">
            <span>{{ row.status === 'failed' ? '×' : (row.status === 'success' ? '✓' : '●') }}</span>
            <div><strong>{{ row.title }}</strong><p v-if="row.detail">{{ row.detail }}</p></div>
          </article>
        </div>
        <div v-if="historyRuntimeSteps.length" class="runtime-steps">
          <h4>历史步骤</h4>
          <article v-for="step in historyRuntimeSteps" :key="`${step.id}-${step.index}`" :class="step.status">
            <span class="step-number">{{ step.index + 1 }}</span>
            <div>
              <strong>Step {{ step.index + 1 }}/{{ runtimeSteps.length }} · {{ step.script }}</strong>
              <p v-if="step.input">输入：{{ step.input }}</p>
              <p v-if="step.output">输出：{{ step.output }}</p>
              <p v-if="step.files">文件变化：{{ step.files }}</p>
              <span>{{ statusText(step.status) }}</span>
            </div>
          </article>
        </div>
        <p v-if="!runtimeActivityRows.length && !runtimeSteps.length" class="runtime-waiting">
          {{ runtimeStatus === 'running' ? 'Runtime 请求已发出，正在等待第一个执行事件…' : '本次响应没有提供可展示的步骤记录。' }}
        </p>

        <div v-if="friendlyFailure" class="friendly-error">
          <strong>运行失败</strong>
          <p v-if="friendlyFailure.step"><b>步骤：</b>{{ friendlyFailure.step }}</p>
          <p><b>原因：</b>{{ friendlyFailure.reason }}</p>
          <p v-if="friendlyFailure.impact"><b>影响：</b>{{ friendlyFailure.impact }}</p>
          <p><b>修复：</b>{{ repairSummaries.length ? '正在尝试自动修复' : '等待自动修复建议' }}</p>
        </div>
        <div v-if="repairSummaries.length" class="repair-summary">
          <h4>系统自动修复</h4>
          <div v-for="(repair, index) in repairSummaries" :key="index">
            <span>{{ repair.status === 'success' ? '✓' : '●' }}</span>
            <div><strong>{{ repair.title }}</strong><p>{{ repair.detail }}</p></div>
          </div>
        </div>
        <details v-if="diagnosticRows.length || technicalDetails" class="technical-details">
          <summary>展开查看详细错误</summary>
          <dl v-if="diagnosticRows.length" class="diagnostic-metadata">
            <template v-for="row in diagnosticRows" :key="row.label">
              <dt>{{ row.label }}</dt><dd>{{ row.value }}</dd>
            </template>
          </dl>
          <pre v-if="technicalDetails">{{ technicalDetails }}</pre>
        </details>
      </section>
      <div
        class="post-item"
        :class="{ success: validateResult?.success, fail: validateResult && !validateResult.success }"
      >
        <span>
          {{
            validateResult
              ? (validateResult.success ? '✅ 严格端到端校验通过' : '❌ 严格端到端校验失败')
              : '⏳ 严格端到端校验中…'
          }}
        </span>
      </div>

      <div
        v-if="packageResult !== null"
        class="post-item"
        :class="{ success: packageResult?.success, fail: packageResult && !packageResult.success }"
      >
        <span>{{ packageResult.success ? '✅ 打包完成' : '❌ 打包失败' }}</span>
        <span v-if="packageResult?.message" class="post-detail">{{ conciseText(packageResult.message) }}</span>
      </div>
    </div>

    <!-- Complete actions -->
    <div v-if="phase === 'complete'" class="complete-actions">
      <button class="btn-primary" @click="openInSandbox">在沙盒中打开</button>
      <a
        v-if="packageResult?.path"
        :href="packageDownloadUrl"
        class="btn-secondary"
        download
      >
        下载 .skill 包
      </a>
    </div>

    <div v-if="phase === 'failed'" class="complete-actions">
      <button class="btn-secondary" @click="phase = 'idle'">返回修改后重试</button>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, nextTick } from 'vue'
import {
  initSkill,
  generateFileStream,
  writeFile,
  validateSkillStream,
  packageSkill,
  uploadAssetAPI,
} from '../composables/useCreator.js'

// ---------------------------------------------------------------------------
// Props & emits
// ---------------------------------------------------------------------------

const props = defineProps({
  skillName: { type: String, required: true },
  sourceSkillName: { type: String, default: '' },
  files: { type: Array, required: true },    // [{ path, purpose, required, can_skip }]
  blueprintText: { type: String, default: '' },
  conversationHistory: { type: Array, default: () => [] },
  model: { type: String, default: null },
  warnings: { type: Array, default: () => [] },
  assetRequirements: { type: Array, default: () => [] },
  finalOutputs: { type: Array, default: () => [] },
  requirementGraph: { type: Object, default: null },
  workflowAllocationSummary: { type: String, default: '' },
  toolRequirements: { type: Array, default: () => [] },
  creationBlockers: { type: Array, default: () => [] },
  confirmedUploadedAssets: { type: Array, default: () => [] },
})

const emit = defineEmits(['creation-complete', 'creation-error', 'execution-event'])


function emitExecutionEvent({ phase, label, detail = '', content = [], filePath = '', payload = {} } = {}) {
  emit('execution-event', {
    phase,
    label,
    detail,
    content: Array.isArray(content) ? content.filter(item => typeof item === 'string') : (typeof content === 'string' ? content : ''),
    filePath,
    payload,
  })
}

// ---------------------------------------------------------------------------
// Reactive state
// ---------------------------------------------------------------------------

// File status: 'pending' | 'generating' | 'preview' | 'writing' | 'done' | 'skipped' | 'error' | 'needs_repair'
function normalizeCreationFilePath(path) {
  return String(path || '')
    .trim()
    .replace(/\\/g, '/')
    .replace(/^\.\/+/, '')
}

function mergeCreationFilesByPath(files) {
  const byPath = new Map()
  for (const raw of files || []) {
    if (!raw || typeof raw !== 'object') continue
    const path = normalizeCreationFilePath(raw.path)
    if (!path) continue
    const previous = byPath.get(path)
    if (!previous) {
      byPath.set(path, { ...raw, path })
      continue
    }
    byPath.set(path, {
      ...previous,
      ...raw,
      path,
      purpose: previous.purpose || raw.purpose || '',
      required: Boolean(previous.required || raw.required),
      uploaded_provided: Boolean(previous.uploaded_provided || raw.uploaded_provided),
      asset_requirement: Boolean(previous.asset_requirement || raw.asset_requirement),
      asset_source: raw.asset_source || previous.asset_source || '',
    })
  }
  return [...byPath.values()]
}

const localFiles = ref(
  mergeCreationFilesByPath([
    ...props.files,
    ...(props.confirmedUploadedAssets || []).map(asset => ({
      path: asset.asset_target_path,
      generation_order: 3,
      purpose: `用户已确认上传素材：${asset.original_name || asset.name || asset.asset_target_path}`,
      required: true,
      can_skip: false,
      file_type: 'asset',
      file_kind: 'asset',
      role: 'asset',
      asset_source: 'user_upload',
      uploaded_provided: true,
    })),
    ...props.assetRequirements.map((requirement, index) => ({
      path: normalizeAssetRequirementPath(requirement, index),
      generation_order: Number.isFinite(requirement.generation_order) ? requirement.generation_order : 3,
      purpose: requirement.description || '需要用户上传素材',
      required: requirement.required !== false,
      can_skip: requirement.required === false,
      file_type: 'asset',
      file_kind: 'asset',
      role: 'asset',
      component_hint: 'asset_requirement',
      inputs: [],
      outputs: [],
      dependencies: [],
      side_effects: [],
      required_tool_slots: [],
      implementation_strategy: [{ strategy: 'require_user_asset', reason: requirement.description || '用户上传素材' }],
      selected_tools: [],
      runtime_contract: {},
      artifact_contract: {},
      required_capabilities: [],
      raw_capability_hints: [],
      forbidden_capabilities: [],
      asset_source: 'user_upload',
      asset_requirement: true,
    })),
  ])
    .filter(f => f.asset_requirement || isMaterializedSkillFilePath(f.path))
    .sort((a, b) => (Number(a.generation_order ?? 99) - Number(b.generation_order ?? 99)) || String(a.path || '').localeCompare(String(b.path || '')))
    .map(f => ({
      ...f,
      role: f.role || (
        f.path === 'SKILL.md'
          ? 'skill_overview'
          : f.path?.startsWith('references/')
            ? 'reference'
            : f.path?.startsWith('assets/')
              ? 'asset'
              : f.path?.startsWith('scripts/')
                ? 'generic_script'
                : null
      ),
      pendingLabel: f.path === 'SKILL.md' ? '待生成' : '',
      generatedContent: '',
      bytesWritten: 0,
      error: '',
      showPreview: false,
      repairMessage: '',
      uploaded: Boolean(f.uploaded_provided),
      status: f.uploaded_provided ? 'done' : (f.path === 'SKILL.md' ? 'pending' : 'pending'),
    }))
)

const localSkillName = ref(props.skillName)
const editingName = ref(false)
const nameError = ref('')
const nameInputRef = ref(null)

const hasCreationBlockers = computed(() =>
  (props.creationBlockers || []).some(item => item.blocking !== false)
)

const visibleWarnings = computed(() => {
  const seen = new Set()
  return (props.warnings || []).filter((warning) => {
    if (!warning || typeof warning !== 'object' || warning.severity !== 'user_warning') return false
    const message = warningMessage(warning)
    if (!message) return false
    const key = warning && typeof warning === 'object'
      ? [warning.source, warning.path, warning.field, warning.code].filter(Boolean).join(':')
      : message
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
})

function warningMessage(warning) {
  return warning && typeof warning === 'object'
    ? String(warning.message || warning.code || '')
    : String(warning || '')
}

const phase = ref('idle')   // idle | running | paused | validating | packaging | complete | failed
const paused = ref(false)

const validateResult = ref(null)
const liveRuntimeEvents = ref([])
const packageResult = ref(null)

const addFilePrompt = ref(false)
const newFilePath = ref('')
const newFilePurpose = ref('')

// ---------------------------------------------------------------------------
// Computed
// ---------------------------------------------------------------------------

const doneCount = computed(
  () => localFiles.value.filter(f => f.status === 'done').length
)

const progressPercent = computed(() => {
  const total = localFiles.value.filter(f => f.status !== 'skipped').length
  if (!total) return 0
  return Math.round((doneCount.value / total) * 100)
})

const generationStages = computed(() => {
  const files = localFiles.value
  const statusFor = predicate => {
    const group = files.filter(predicate)
    if (!group.length) return 'success'
    if (group.some(file => file.status === 'error')) return 'failed'
    if (group.every(file => ['done', 'skipped'].includes(file.status))) return 'success'
    if (group.some(file => ['generating', 'writing', 'preview'].includes(file.status))) return 'running'
    return phase.value === 'running' ? 'running' : 'pending'
  }
  return [
    { label: '创建目录', status: skillInitialized.value ? 'success' : (phase.value === 'running' ? 'running' : 'pending') },
    { label: '生成 SKILL.md', status: statusFor(file => file.path === 'SKILL.md') },
    { label: '生成脚本文件', status: statusFor(file => file.path.startsWith('scripts/')) },
    { label: '生成依赖配置', status: statusFor(file => /(^|\/)(requirements|pyproject|package|environment)/i.test(file.path)) },
  ]
})

const runtimeEvents = computed(() => liveRuntimeEvents.value.length
  ? liveRuntimeEvents.value
  : (Array.isArray(validateResult.value?.runtime_trace)
      ? validateResult.value.runtime_trace
      : (Array.isArray(validateResult.value?.repair_events) ? validateResult.value.repair_events : [])))

const runtimeActivityRows = computed(() => runtimeEvents.value.map((event, index) => {
  const rawStatus = String(event?.status || event?.rerun_status || event?.patch_status || '').toLowerCase()
  const failed = rawStatus.includes('fail') || rawStatus.includes('error') || Boolean(event?.failure_summary || event?.error)
  const success = ['success', 'passed', 'complete', 'completed', 'accepted', 'applied'].some(value => rawStatus.includes(value))
  return {
    key: `${event?.event || event?.phase || event?.type || 'runtime'}-${index}`,
    title: conciseText(event?.message || event?.event || event?.phase || event?.type || event?.step_id || 'Runtime 更新'),
    detail: conciseText(event?.target_file || event?.script_path || event?.failure_summary || event?.output_summary || ''),
    status: failed ? 'failed' : (success ? 'success' : 'running'),
  }
}).slice(-20))

const runtimeSteps = computed(() => {
  const steps = new Map()
  runtimeEvents.value
    .filter(event => event && (event.current_step || event.step_index || event.step_id || event.script_path || event.target_file))
    .forEach((event, eventIndex) => {
      const id = event.step_id || event.current_step || event.step_index || `${event.target_file || event.script_path}-${eventIndex}`
      const previous = steps.get(id) || { id, index: steps.size }
      steps.set(id, {
        ...previous,
        script: event.script_path || event.target_file || previous.script || `执行步骤 ${id}`,
        input: summaryValue(event.input_summary || event.inputs || event.rendered_payload_preview || event.rendered_payload_summary) || previous.input,
        output: summaryValue(event.output_summary || event.outputs || event.artifact) || previous.output,
        files: summaryValue(event.file_changes || event.changed_files || event.invalidated_checkpoints) || previous.files,
        duration: summaryValue(event.duration || event.duration_ms || event.elapsed_ms) || previous.duration,
        status: normalizeRuntimeStatus(event.status || event.rerun_status || event.patch_status),
      })
    })
  return [...steps.values()]
})
const currentRuntimeStep = computed(() => runtimeSteps.value.find(step => step.status === 'running') || (runtimeStatus.value === 'running' ? runtimeSteps.value.at(-1) : null))
const historyRuntimeSteps = computed(() => runtimeSteps.value.filter(step => step !== currentRuntimeStep.value))

const runtimeStatus = computed(() => phase.value === 'validating' ? 'running' : (validateResult.value?.success ? 'success' : (validateResult.value ? 'failed' : 'pending')))
const runtimeStatusText = computed(() => statusText(runtimeStatus.value))
const runtimeTitle = computed(() => runtimeStatus.value === 'running' ? 'Runtime 执行中' : (runtimeStatus.value === 'success' ? 'Runtime 验证完成' : (runtimeStatus.value === 'failed' ? 'Runtime 验证未通过' : '等待 Runtime')))
const friendlyFailure = computed(() => {
  if (runtimeStatus.value !== 'failed') return null
  const event = [...runtimeEvents.value].reverse().find(item => item?.failure_summary || item?.reason || item?.message)
  return {
    step: event?.target_file || event?.step_id || event?.failed_step || '',
    reason: conciseText(event?.failure_summary || event?.reason || validateResult.value?.message || '执行结果未满足验证要求'),
    impact: event?.target_file ? `${event.target_file} 对应步骤无法继续` : (event?.step_id ? `${event.step_id} 步骤无法继续` : ''),
  }
})
const repairSummaries = computed(() => runtimeEvents.value
  .filter(event => event && (event.patch_status || event.repair_result || event.resume_from_step || /repair/i.test(String(event.phase || event.type || ''))))
  .map(event => ({
    title: event.target_file ? `修复 ${event.target_file}` : '调整执行约束与文件',
    detail: conciseText(event.repair_result || event.failure_summary || event.next_target || (event.resume_from_step ? `从第 ${event.resume_from_step} 步继续验证` : '已提交修复并重新验证')),
    status: ['success', 'accepted', 'applied', 'passed'].includes(String(event.patch_status || event.status || '').toLowerCase()) ? 'success' : 'running',
  })).slice(0, 8))
const diagnosticRows = computed(() => {
  if (runtimeStatus.value !== 'failed') return []
  const failure = [...runtimeEvents.value].reverse().find(event => event && (
    event.failure_code || event.error_code || event.failure_layer || event.target_file ||
    event.failed_step || event.step_id || event.current_step || event.step_index
  )) || {}
  return [
    ['错误代码', failure.failure_code || failure.error_code || validateResult.value?.failure_code],
    ['失败层级', failure.failure_layer || validateResult.value?.failure_layer],
    ['目标文件', failure.target_file || validateResult.value?.target_file],
    ['失败步骤', failure.failed_step || failure.step_id || failure.current_step || failure.step_index || validateResult.value?.failed_step],
    ['修复阶段', failure.phase || failure.type],
  ].filter(([, value]) => value !== undefined && value !== null && String(value).trim()).map(([label, value]) => ({ label, value: summaryValue(value) }))
})
const technicalDetails = computed(() => {
  if (runtimeStatus.value !== 'failed') return ''
  const details = runtimeEvents.value.flatMap(event => [
    event?.failure_summary,
    event?.trace_summary,
    event?.stdout_summary,
    event?.stderr_summary,
    event?.diff_excerpt,
    event?.rejection_reason,
    event?.parser_error,
    event?.last_output_excerpt,
  ]).filter(Boolean)
  return details.join('\n\n') || String(validateResult.value?.message || packageResult.value?.message || '')
})

const packageDownloadUrl = computed(() => {
  if (!packageResult.value?.path) return '#'
  const name = localSkillName.value
  return `/api/skills/${name}/files/dist/${name}.skill`
})

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function statusIcon(status) {
  const icons = {
    pending:    '⬜',
    generating: '⟳',
    preview:    '👁',
    writing:    '✏️',
    done:       '✅',
    skipped:    '⏭',
    error:      '❌',
    success:    '✓',
    failed:     '×',
    running:    '●',
  }
  return icons[status] ?? '⬜'
}

function statusText(status) { return ({ pending: '等待中', running: '运行中', success: '成功', failed: '失败' })[status] || '等待中' }
function normalizeRuntimeStatus(status) {
  const value = String(status || '').toLowerCase()
  if (['success', 'passed', 'complete', 'completed', 'accepted', 'applied'].includes(value)) return 'success'
  if (['failed', 'error', 'rejected', 'parse_failed', 'hard_format_failed'].includes(value)) return 'failed'
  if (['running', 'started', 'retrying', 'pending'].includes(value)) return value === 'pending' ? 'pending' : 'running'
  return 'pending'
}
function summaryValue(value) {
  if (Array.isArray(value)) return value.map(item => typeof item === 'object' ? (item.path || item.name || item.id) : item).filter(Boolean).slice(0, 5).join('、')
  if (value && typeof value === 'object') return Object.entries(value).slice(0, 5).map(([key, item]) => `${key}: ${typeof item === 'object' ? '[结构化数据]' : item}`).join('；')
  return conciseText(value)
}
function conciseText(value) {
  const text = String(value || '').replace(/Traceback[\s\S]*/i, '').replace(/\s+/g, ' ').trim()
  return text.length > 240 ? `${text.slice(0, 240)}…` : text
}
function treePrefix(path) { return path.includes('/') ? '├── ' : '└── ' }

function formatBytes(n) {
  if (!n) return '0 B'
  if (n < 1024) return `${n} B`
  return `${(n / 1024).toFixed(1)} KB`
}

function normalizeSkillPath(path) {
  return String(path || '').replace(/\\/g, '/').trim()
}

function isMarkdownSkillFile(path) {
  const normalized = normalizeSkillPath(path)
  return normalized === 'SKILL.md' || normalized.startsWith('references/') || /\.md(?:own)?$/i.test(normalized)
}

function isEditableMarkdownWarning(file, payload = {}) {
  if (!isMarkdownSkillFile(file?.path)) return false
  return payload.editable === true && payload.disabled === false
}

function pathBasename(path) {
  const normalized = normalizeSkillPath(path).replace(/\/$/, '')
  if (!normalized) return ''
  return normalized.split('/').pop() || ''
}

function hasFileExtension(path) {
  const name = pathBasename(path)
  if (!name) return false

  const dotIndex = name.lastIndexOf('.')
  return dotIndex > 0 && dotIndex < name.length - 1
}

function looksLikeDirectoryPath(path) {
  const normalized = normalizeSkillPath(path)
  if (!normalized) return false

  if (normalized.endsWith('/')) return true

  if (
    normalized.startsWith('scripts/') ||
    normalized.startsWith('references/') ||
    normalized.startsWith('assets/')
  ) {
    return !hasFileExtension(normalized)
  }

  return false
}

function isMaterializedSkillFilePath(path) {
  const normalized = normalizeSkillPath(path)

  if (!normalized) return false
  if (normalized === 'SKILL.md') return true

  return (
    (
      normalized.startsWith('scripts/') ||
      normalized.startsWith('references/') ||
      normalized.startsWith('assets/')
    ) &&
    hasFileExtension(normalized)
  )
}

function normalizeAssetRequirementPath(requirement, index) {
  const path = normalizeSkillPath(requirement?.path)
  if (path && path.startsWith('assets/') && hasFileExtension(path)) return path
  return `assets/__upload_required_${index + 1}__`
}

function isAssetFile(file) {
  const path = normalizeSkillPath(file?.path)

  if (!path.startsWith('assets/')) return false

  // assets/** 在 Creator 中默认是素材文件。
  // 只要是具体 assets 文件或 asset_requirement，都应走上传流程，
  // 不要求后端必须把 asset_source 标成 user_upload。
  //
  // 这样可以避免后端旧解析把 asset_source 误写成 bundled/空字符串后，
  // 前端不显示上传按钮的问题。
  return hasFileExtension(path) || Boolean(file?.asset_requirement)
}

function isReferenceFile(file) {
  const path = normalizeSkillPath(file?.path)
  return path.startsWith('references/') && hasFileExtension(path)
}

function canSkipFile(file) {
  return (
    file.can_skip &&
    file.status !== 'done' &&
    file.status !== 'skipped' &&
    file.status !== 'writing'
  )
}

function canRemoveFile(file) {
  return (
    file.path !== 'SKILL.md' &&
    file.status !== 'generating' &&
    file.status !== 'writing'
  )
}

// ---------------------------------------------------------------------------
// Name editing
// ---------------------------------------------------------------------------

function startEditName() {
  editingName.value = true
  nextTick(() => nameInputRef.value?.focus())
}

function commitName() {
  const raw = localSkillName.value.trim()
  if (!raw) {
    nameError.value = '名称不能为空'
    return
  }
  if (!/^[a-z0-9][a-z0-9-]*$/.test(raw)) {
    nameError.value = '只能包含小写字母、数字和连字符'
    return
  }
  nameError.value = ''
  localSkillName.value = raw
  editingName.value = false
}

// ---------------------------------------------------------------------------
// File management
// ---------------------------------------------------------------------------

function addFile() {
  const path = newFilePath.value.trim()
  if (!path) return

  const allowed = ['SKILL.md', 'scripts/', 'references/', 'assets/']
  if (!allowed.some(p => path === p || path.startsWith(p))) {
    alert('路径必须是 SKILL.md 或 scripts/*、references/*、assets/* 下的文件')
    return
  }

  if (looksLikeDirectoryPath(path)) {
    alert('目录路径不需要加入待生成列表；请只添加具体文件，例如 assets/template.pdf')
    return
  }

  if (localFiles.value.some(f => f.path === path)) {
    alert('该文件路径已存在')
    return
  }

  const isSkillMd = path === 'SKILL.md'
  const isScript = path.startsWith('scripts/')
  const isReference = path.startsWith('references/')
  const isAsset = path.startsWith('assets/')

  localFiles.value.push({
    path,
    purpose: newFilePurpose.value.trim() || (
      isSkillMd
        ? 'Skill 核心说明文件'
        : isAsset
          ? '用户上传素材文件'
          : `${path} 的职责待确认；请补充 role/inputs/outputs/capabilities 后再生成`
    ),
    required: isSkillMd || isAsset,
    can_skip: !isSkillMd && !isAsset,
    file_type: isSkillMd ? 'skill' : path.split('/')[0]?.replace(/s$/, '') || null,
    file_kind: isSkillMd
      ? 'skill_doc'
      : (
          isScript
            ? 'script'
            : (
                isReference
                  ? 'reference'
                  : (
                      isAsset
                        ? 'asset'
                        : 'config'
                    )
              )
        ),
    role: isSkillMd
      ? 'skill_overview'
      : (
          isReference
            ? 'reference'
            : (
                isAsset
                  ? 'asset'
                  : 'generic_script'
              )
        ),
    component_hint: isSkillMd
      ? 'skill_overview'
      : (
          isScript
            ? 'generic_script'
            : (
                isAsset
                  ? 'asset_requirement'
                  : ''
              )
        ),
    inputs: [],
    outputs: [],
    dependencies: [],
    side_effects: [],
    required_tool_slots: [],
    implementation_strategy: isScript
      ? [{ strategy: 'local_code', reason: 'Manually added script defaults to local code until normalized.' }]
      : (
          isAsset
            ? [{ strategy: 'require_user_asset', reason: 'assets 素材必须由用户上传，不能由模型生成。' }]
            : []
        ),
    selected_tools: [],
    runtime_contract: {},
    artifact_contract: {},
    required_capabilities: [],
    raw_capability_hints: [],
    forbidden_capabilities: [],
    reference_files: [],
    references: [],
    low_confidence: isScript,
    asset_source: isAsset ? 'user_upload' : '',
    asset_requirement: isAsset,
    status: 'pending',
    generatedContent: '',
    bytesWritten: 0,
    error: '',
    showPreview: false,
    repairMessage: '',
    uploaded: false,
  })

  newFilePath.value = ''
  newFilePurpose.value = ''
  addFilePrompt.value = false
}

function skipFile(idx) {
  localFiles.value[idx].status = 'skipped'
}

function removeFile(idx) {
  const file = localFiles.value[idx]
  if (!file || file.path === 'SKILL.md') return

  const hasContent =
    file.status === 'done' ||
    file.status === 'preview' ||
    Boolean(file.generatedContent?.trim())

  if (hasContent) {
    const ok = window.confirm(`确认从待生成列表移除 ${file.path}？\n已写入磁盘的文件不会在这里删除。`)
    if (!ok) return
  }

  localFiles.value.splice(idx, 1)
}

function togglePreview(idx) {
  localFiles.value[idx].showPreview = !localFiles.value[idx].showPreview
}


async function handleAssetUpload(fileItem, event) {
  const selected = event.target.files?.[0]
  if (!selected) return

  fileItem.status = 'writing'
  fileItem.error = ''

  try {
    const uploadPath = fileItem.asset_requirement && !hasFileExtension(fileItem.path)
      ? `assets/${selected.name}`
      : fileItem.path
    const result = await uploadAssetAPI({
      skillName: localSkillName.value,
      filePath: uploadPath,
      file: selected,
    })
    fileItem.path = uploadPath
    fileItem.asset_requirement = false
    fileItem.status = 'done'
    fileItem.uploaded = true
    fileItem.bytesWritten = result.size
  } catch (err) {
    fileItem.status = 'error'
    fileItem.error = err?.message || String(err)
  } finally {
    event.target.value = ''
  }
}

// ---------------------------------------------------------------------------
// Per-file generation & writing
// ---------------------------------------------------------------------------

async function generateOneFile(idx) {
  const file = localFiles.value[idx]
  file.status = 'generating'
  emitExecutionEvent({ phase: 'file_generation_start', label: '文件开始生成', detail: file.path, filePath: file.path, content: [file.purpose || '生成文件内容'] })
  file.error = ''
  file.generatedContent = ''
  file.repairMessage = ''

  try {
    let generationSucceeded = false
    for await (const chunk of generateFileStream({
      skillName: localSkillName.value,
      filePath: file.path,
      purpose: file.purpose,
      blueprintText: props.blueprintText,
      conversationHistory: props.conversationHistory,
      model: props.model,
      role: file.role || null,
      skillPlanEntry: file,
      requirementGraph: props.requirementGraph,
      workflowAllocationSummary: props.workflowAllocationSummary || '',
      finalOutputs: props.finalOutputs || [],
    })) {
      if (chunk?.fileDone) {
        if (typeof chunk.content === 'string' && chunk.content) {
          file.generatedContent = chunk.content
        }
        if (chunk.success === true && chunk.status === 'success') {
          generationSucceeded = true
          break
        }
        if (chunk.needsRepair || chunk.success === false || chunk.status === 'needs_repair' || chunk.validationStatus === 'needs_repair') {
          file.status = 'needs_repair'
          file.showPreview = true
          file.error = chunk.error || '生成结果需要人工修复'
          file.repairMessage = `${chunk.errorType || chunk.status || 'needs_repair'}：${chunk.error || '生成结果需要人工修复'}`
          return
        }
        file.status = 'error'
        file.error = chunk.error || '生成流结束但未返回成功终态'
        return
      } else if (chunk?.done) {
        // SSE stream close/done only means the stream ended. It is not generation success.
        break
      } else if (chunk?.validation) {
        const validationStatus = chunk.validation.status
        const statusText = validationStatus === 'failed'
          ? '自动修复失败'
          : validationStatus === 'format_full_rewrite'
            ? '格式整文件重写中'
            : validationStatus === 'format_full_rewrite_failed'
              ? '格式整文件重写失败'
              : validationStatus === 'parse_failed'
                ? '补丁解析失败'
                : validationStatus === 'localized_patch_failed'
                  ? '局部补丁失败'
                  : '自动修复中'
        file.repairMessage = `${statusText}（第 ${chunk.validation.attempt} 次）：${chunk.validation.error || ''}`
        emitExecutionEvent({ phase: 'file_validation_feedback', label: '收到校验反馈', detail: file.repairMessage, filePath: file.path, content: [statusText] })
        if (validationStatus && validationStatus !== 'failed') {
          emitExecutionEvent({ phase: 'file_local_repair_start', label: '开始局部修复', detail: file.path, filePath: file.path, content: [statusText] })
        }
      } else if (chunk?.error) {
        if (typeof chunk.content === 'string' && chunk.content.trim()) {
          file.generatedContent = chunk.content
        }
        if (isEditableMarkdownWarning(file, chunk)) {
          file.status = 'preview'
          file.showPreview = true
          file.repairMessage = `${chunk.errorType || 'md_content_repair_warning'}：${chunk.error || 'Markdown 仍需手动微调'}`
          return
        }
        throw new Error(chunk.error)
      } else if (typeof chunk === 'string') {
        file.generatedContent += chunk
      } else if (typeof chunk?.content === 'string') {
        file.generatedContent += chunk.content
      } else if (typeof chunk?.delta === 'string') {
        file.generatedContent += chunk.delta
      }
    }

    if (!generationSucceeded) {
      throw new Error('生成流已结束，但后端未返回显式成功终态')
    }

    if (!file.generatedContent.trim()) {
      throw new Error('模型未返回任何内容，请重试或手动填写')
    }

    file.repairMessage = ''
    file.status = 'preview'
  } catch (err) {
    file.status = 'error'
    const failedChecks = Array.isArray(err?.detail?.failed_checks)
      ? err.detail.failed_checks.map(item => `${item.id || 'check'}: ${item.message || ''}`).join('\n')
      : ''
    file.error = failedChecks || err.message || String(err)

    if (isMarkdownSkillFile(file.path) && file.generatedContent?.trim()) {
      file.showPreview = true
      file.repairMessage = 'Markdown 校验/修复未完全通过，但草稿可编辑。可在下方手动微调后点击“写入”或“重新生成”。'
    }
  }
}

async function writeOneFile(idx) {
  const file = localFiles.value[idx]
  file.status = 'writing'
  try {
    const result = await writeFile(
      localSkillName.value,
      file.path,
      file.generatedContent,
      file.role || null,
      file
    )
    if (!result.success) throw new Error(result.message)
    file.status = 'done'
    file.bytesWritten = result.bytes || 0
    emitExecutionEvent({ phase: 'file_write_complete', label: '文件写入完成', detail: `${file.path}（${file.bytesWritten} bytes）`, filePath: file.path, content: [file.path] })
  } catch (err) {
    file.status = 'error'
    file.error = err.message
  }
}

// ---------------------------------------------------------------------------
// Full creation flow
// ---------------------------------------------------------------------------

const currentIndex = ref(0)
const skillInitialized = ref(false)

async function ensureSkillInitialized() {
  if (skillInitialized.value) return
  const r = await initSkill(localSkillName.value, {
    confirmedUploadedAssets: props.confirmedUploadedAssets || [],
    sourceSkillName: props.sourceSkillName,
    baselineFiles: localFiles.value.map(file => file.path),
  })
  if (!r.success) throw new Error(r.message)
  skillInitialized.value = true
}

async function startCreation() {
  if (nameError.value) return
  currentIndex.value = 0
  emitExecutionEvent({ phase: 'file_generation_start', label: '文件开始生成', detail: `准备生成 ${localFiles.value.length} 个文件`, content: localFiles.value.map(file => file.path) })
  validateResult.value = null
  packageResult.value = null
  paused.value = false

  await runCreationFromCurrentIndex()
}

async function resumeCreation() {
  paused.value = false
  if (phase.value === 'paused') {
    await runCreationFromCurrentIndex()
  }
}

async function runCreationFromCurrentIndex() {
  phase.value = 'running'

  try {
    await ensureSkillInitialized()
  } catch (err) {
    emit('creation-error', err.message)
    phase.value = 'idle'
    return
  }

  for (; currentIndex.value < localFiles.value.length; currentIndex.value++) {
    if (paused.value) {
      phase.value = 'paused'
      return
    }

    const idx = currentIndex.value
    const file = localFiles.value[idx]

    if (file.status === 'skipped' || file.status === 'done') continue

    if (file.status === 'preview' && file.generatedContent?.trim()) {
      await writeOneFile(idx)

      if (localFiles.value[idx].status === 'error' || localFiles.value[idx].status === 'needs_repair') {
        if (file.path === 'SKILL.md') {
          file.showPreview = true
          file.repairMessage = '写入时校验仍未通过，请继续手动微调或重新生成。'
          phase.value = 'paused'
          return
        }

        if (file.required) {
          phase.value = 'paused'
          return
        }

        localFiles.value[idx].status = 'skipped'
        continue
      }

      continue
    }

    if (isAssetFile(file)) {
      if (file.status === 'done' || file.uploaded) continue
      file.status = 'error'
      file.error = 'assets 素材文件必须先上传，不能由模型生成'
      phase.value = 'paused'
      return
    }

    await generateOneFile(idx)

    if (localFiles.value[idx].status === 'error' || localFiles.value[idx].status === 'needs_repair') {
      if (file.path === 'SKILL.md' && file.generatedContent?.trim()) {
        file.showPreview = true
        file.repairMessage = 'SKILL.md 二次校验失败，可手动微调后点击“写入”或“重新生成”。'
        phase.value = 'paused'
        return
      }

      if (file.required) {
        phase.value = 'paused'
        return
      }

      localFiles.value[idx].status = 'skipped'
      continue
    }

    await writeOneFile(idx)

    if (localFiles.value[idx].status === 'error') {
      if (file.required) {
        phase.value = 'paused'
        return
      }
      localFiles.value[idx].status = 'skipped'
      continue
    }
  }

  await runPostValidationAndPackaging()
}

async function runPostValidationAndPackaging() {
  phase.value = 'validating'
  liveRuntimeEvents.value = []
  validateResult.value = null
  emitExecutionEvent({ phase: 'e2e_start', label: 'E2E 开始', detail: '开始严格端到端校验', content: [] })
  packageResult.value = null

  try {
    validateResult.value = await validateSkillStream(localSkillName.value, {
      model: props.model,
      autoRepair: true,
      maxE2ERepairAttempts: 10,
      onEvent(event) {
        if (event.event === 'e2e_input_ready') {
          validateResult.value = {
            ...(validateResult.value || {}),
            e2e_review_sample: event.e2e_review_sample,
          }
          emitExecutionEvent({
            phase: 'e2e_input_ready',
            label: 'E2E 输入已构造',
            detail: 'Runtime 待测输入已冻结，可供审阅',
            payload: { e2e_review_sample: event.e2e_review_sample },
          })
        }
        if (event.event === 'e2e_runtime_event' && event.runtime_event) {
          liveRuntimeEvents.value.push(event.runtime_event)
          emitExecutionEvent({
            phase: 'e2e_runtime_event',
            label: event.runtime_event.event || 'Runtime 步骤更新',
            detail: event.runtime_event.target_file || event.runtime_event.script_path || '',
            payload: { runtime_event: event.runtime_event },
          })
        }
      },
    })
  } catch (err) {
    validateResult.value = {
      success: false,
      path: null,
      message: err.message || '严格端到端校验请求失败',
    }
  }

  if (!validateResult.value?.success) {
    emitExecutionEvent({ phase: 'e2e_failed', label: 'E2E 失败', detail: validateResult.value?.message || '严格端到端校验失败', content: [], payload: validateResult.value || {} })
    phase.value = 'failed'
    emit('creation-error', validateResult.value?.message || '严格端到端校验失败，已停止打包。')
    return
  }

  emitExecutionEvent({ phase: 'e2e_success', label: 'E2E 成功', detail: '严格端到端校验通过', content: [], payload: validateResult.value || {} })

  phase.value = 'packaging'
  emitExecutionEvent({ phase: 'package_start', label: '开始打包', detail: localSkillName.value, content: [] })
  try {
    packageResult.value = await packageSkill(localSkillName.value, {
      model: props.model,
      validateBeforePackage: true,
    })
  } catch (err) {
    packageResult.value = {
      success: false,
      message: err.message || '打包请求失败',
      path: null,
    }
  }

  if (!packageResult.value?.success) {
    emitExecutionEvent({ phase: 'package_failed', label: '打包结果', detail: packageResult.value?.message || '打包失败', content: [] })
    phase.value = 'failed'
    emit('creation-error', packageResult.value?.message || '打包失败')
    return
  }

  emitExecutionEvent({ phase: 'package_complete', label: '打包完成', detail: packageResult.value?.path || '打包完成', content: [] })

  phase.value = 'complete'
  emit('creation-complete', {
    skillName: localSkillName.value,
    validateResult: validateResult.value,
    packageResult: packageResult.value,
  })
}

function pauseCreation() {
  paused.value = true
}

function skipOptionalFiles() {
  localFiles.value.forEach(f => {
    if (f.can_skip && f.status === 'pending') {
      f.status = 'skipped'
    }
  })
}

function openInSandbox() {
  window.location.href = `/sandbox/${localSkillName.value}`
}
</script>

<style scoped>
.skill-creation-panel {
  border: 1px solid #3a3a3a;
  border-radius: 8px;
  padding: 16px;
  background: #1e1e1e;
  color: #d4d4d4;
  font-size: 14px;
  margin-top: 16px;
}

.panel-header {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 15px;
  font-weight: 600;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.panel-icon { font-size: 18px; }

.skill-name-display {
  cursor: pointer;
  color: #80c8ff;
  text-decoration: underline dotted;
}
.edit-hint { font-size: 12px; margin-left: 4px; opacity: 0.6; }

.name-input {
  background: #2a2a2a;
  border: 1px solid #555;
  border-radius: 4px;
  color: #d4d4d4;
  padding: 2px 6px;
  font-size: 14px;
  width: 180px;
}
.name-input.error { border-color: #e05c5c; }
.name-error { color: #e05c5c; font-size: 12px; }

/* Progress */
.progress-bar-wrap {
  background: #2a2a2a;
  border-radius: 4px;
  height: 20px;
  position: relative;
  margin-bottom: 10px;
  overflow: hidden;
}
.progress-bar-fill {
  background: #3a7bd5;
  height: 100%;
  transition: width 0.3s ease;
}
.progress-label {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  text-align: center;
  line-height: 20px;
  font-size: 12px;
  color: #fff;
  pointer-events: none;
}
.generation-stages { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; margin: 12px 0; }
.generation-stages > div { display: flex; align-items: center; gap: 6px; padding: 8px; border: 1px solid #3b4654; border-radius: 8px; color: #94a3b8; font-size: 12px; }
.generation-stages .running { border-color: #3b82f6; color: #93c5fd; }.generation-stages .success { border-color: #166534; color: #86efac; }.generation-stages .failed { border-color: #991b1b; color: #fca5a5; }
.file-tree-panel { margin-bottom: 12px; padding: 10px; border: 1px solid #374151; border-radius: 10px; background: #191f27; }
.file-tree-panel summary { cursor: pointer; font-weight: 700; }.file-tree-panel summary span { color: #94a3b8; font-size: 11px; }
.tree-row { display: grid; grid-template-columns: minmax(180px, .8fr) 1fr; gap: 12px; padding: 7px 3px; border-bottom: 1px solid #29313d; }.tree-row:last-child { border-bottom: 0; }.tree-row code { color: #93c5fd; }.tree-row span { color: #94a3b8; font-size: 12px; }
.runtime-panel { margin-bottom: 12px; padding: 16px; border: 1px solid #334155; border-radius: 12px; background: #151b24; }
.runtime-heading { display: flex; align-items: center; justify-content: space-between; }.runtime-heading small { color: #60a5fa; font-size: 9px; font-weight: 800; letter-spacing: .16em; }.runtime-heading h3 { margin: 3px 0 0; font-size: 16px; }
.runtime-badge { padding: 4px 9px; border-radius: 999px; background: #334155; font-size: 11px; }.runtime-badge.running { background: #1e3a8a; color: #bfdbfe; }.runtime-badge.success { background: #14532d; color: #bbf7d0; }.runtime-badge.failed { background: #7f1d1d; color: #fecaca; }
.runtime-current { display: grid; gap: 6px; margin-top: 14px; padding: 14px; border: 1px solid #2563eb; border-radius: 10px; background: #17233a; }.runtime-current small { color: #93c5fd; font-weight: 700; }.runtime-current strong { font-family: monospace; }.runtime-current p { margin: 0; color: #cbd5e1; font-size: 11px; }.runtime-current > span { color: #60a5fa; font-size: 11px; }
.runtime-live-feed { display: grid; gap: 7px; margin-top: 14px; }
.runtime-live-feed h4 { margin: 0; color: #94a3b8; font-size: 11px; text-transform: uppercase; }
.runtime-live-feed article { display: flex; gap: 9px; padding: 9px 10px; border-radius: 7px; background: #202936; }
.runtime-live-feed article > span { color: #60a5fa; }
.runtime-live-feed article.success > span { color: #22c55e; }
.runtime-live-feed article.failed > span { color: #ef4444; }
.runtime-live-feed strong { font-size: 11px; }
.runtime-live-feed p { margin: 3px 0 0; color: #94a3b8; font-size: 10px; }
.runtime-steps > h4 { margin: 4px 0; color: #94a3b8; font-size: 11px; text-transform: uppercase; }
.runtime-steps { display: grid; gap: 8px; margin-top: 14px; }.runtime-steps article { display: flex; gap: 10px; padding: 10px; border-left: 3px solid #475569; border-radius: 6px; background: #202936; }.runtime-steps article.success { border-color: #22c55e; }.runtime-steps article.failed { border-color: #ef4444; }.runtime-steps article.running { border-color: #3b82f6; }.step-number { display: grid; flex: 0 0 24px; height: 24px; place-items: center; border-radius: 50%; background: #334155; font-size: 11px; }.runtime-steps strong { font-family: monospace; font-size: 12px; }.runtime-steps p { margin: 5px 0; color: #aab6c5; font-size: 11px; }.runtime-steps article > div > span { color: #94a3b8; font-size: 11px; }
.runtime-waiting { padding: 14px 0 2px; color: #94a3b8; }.friendly-error { margin-top: 14px; padding: 12px; border: 1px solid #7f1d1d; border-radius: 8px; background: #2a171b; }.friendly-error > strong { color: #fca5a5; }.friendly-error p { margin: 7px 0 0; line-height: 1.55; }
.repair-summary { margin-top: 14px; }.repair-summary h4 { margin: 0 0 8px; }.repair-summary > div { display: flex; gap: 8px; padding: 8px; border-radius: 6px; background: #1e293b; }.repair-summary p { margin: 3px 0 0; color: #aab6c5; font-size: 11px; }.technical-details { margin-top: 12px; color: #94a3b8; }.technical-details summary { cursor: pointer; }.technical-details pre { max-height: 240px; overflow: auto; white-space: pre-wrap; color: #aab6c5; font-size: 11px; }
.diagnostic-metadata { display: grid; grid-template-columns: max-content 1fr; gap: 6px 12px; margin: 12px 0; padding: 10px; border-radius: 6px; background: #111827; font-size: 11px; }.diagnostic-metadata dt { color: #94a3b8; }.diagnostic-metadata dd { min-width: 0; margin: 0; overflow-wrap: anywhere; color: #e2e8f0; font-family: monospace; }
@media (max-width: 760px) { .generation-stages { grid-template-columns: 1fr 1fr; }.tree-row { grid-template-columns: 1fr; gap: 3px; } }

/* Warnings */
.warnings { margin-bottom: 10px; }
.warning-item {
  background: #3b2e00;
  border-radius: 4px;
  padding: 4px 8px;
  margin-bottom: 4px;
  font-size: 12px;
  color: #f0c060;
}

/* File list */
.file-list {
  list-style: none;
  margin: 0 0 12px 0;
  padding: 0;
}

.file-item {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  border-radius: 4px;
  margin-bottom: 4px;
  background: #252525;
}
.file-item.done     { background: #1a2a1a; }
.file-item.error    { background: #2a1a1a; }
.file-item.skipped  { opacity: 0.4; }

.file-icon { font-size: 14px; flex-shrink: 0; }
.file-path { font-family: monospace; flex: 1; }
.role-badge {
  padding: 1px 6px;
  border-radius: 999px;
  border: 1px solid #3f5f7f;
  color: #9fd0ff;
  background: #1d2a36;
  font-size: 11px;
  white-space: nowrap;
}
.role-badge.warning {
  border-color: #7a5a24;
  color: #f0c060;
  background: #332817;
}

.file-meta { font-size: 12px; color: #888; }
.file-meta.error { color: #e05c5c; }
.file-meta.generating { color: #80c8ff; }
.file-meta.muted { color: #666; }

.file-actions { display: flex; gap: 4px; }

/* Editor */
.file-editor-wrap { width: 100%; margin-top: 6px; }
.file-editor {
  width: 100%;
  box-sizing: border-box;
  background: #1a1a1a;
  border: 1px solid #444;
  border-radius: 4px;
  color: #d4d4d4;
  font-family: monospace;
  font-size: 12px;
  padding: 6px;
  resize: vertical;
}

/* Preview */
.file-preview-wrap { width: 100%; margin-top: 6px; }
.file-preview {
  background: #1a1a1a;
  border: 1px solid #333;
  border-radius: 4px;
  padding: 8px;
  font-size: 12px;
  white-space: pre-wrap;
  overflow-x: auto;
  max-height: 200px;
  overflow-y: auto;
}

/* Add-file row */
.add-file-row { background: transparent; justify-content: flex-start; }
.add-file-form {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  width: 100%;
  margin-top: 4px;
}
.add-file-input {
  background: #2a2a2a;
  border: 1px solid #555;
  border-radius: 4px;
  color: #d4d4d4;
  padding: 3px 8px;
  font-size: 13px;
  flex: 1;
  min-width: 140px;
}

/* Buttons */
.btn-primary, .btn-secondary {
  padding: 6px 14px;
  border-radius: 5px;
  border: none;
  cursor: pointer;
  font-size: 13px;
}
.btn-primary  { background: #3a7bd5; color: #fff; }
.btn-primary:hover  { background: #2e68c0; }
.btn-primary:disabled { background: #444; cursor: not-allowed; }
.btn-secondary { background: #3a3a3a; color: #d4d4d4; }
.btn-secondary:hover { background: #4a4a4a; }

.btn-small {
  padding: 2px 8px;
  border-radius: 3px;
  border: 1px solid #555;
  background: #2a2a2a;
  color: #bbb;
  cursor: pointer;
  font-size: 12px;
}
.btn-small:hover  { background: #3a3a3a; }
.btn-write        { border-color: #3a7bd5; color: #80c8ff; }
.btn-retry        { border-color: #c07030; color: #f0c060; }
.btn-skip         { border-color: #666; }
.btn-preview      { border-color: #666; }
.btn-add-file     { background: transparent; border: 1px dashed #555; color: #888; cursor: pointer; padding: 4px 10px; border-radius: 4px; font-size: 13px; }
.btn-add-file:hover { color: #d4d4d4; border-color: #888; }

/* Panel actions */
.panel-actions {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}

/* Post-creation status */
.post-status {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.post-item {
  padding: 4px 10px;
  border-radius: 4px;
  font-size: 13px;
  background: #252525;
}
.post-item.success { background: #1a2a1a; }
.post-item.fail    { background: #2a1a1a; }
.post-detail { display: block; font-size: 11px; color: #e05c5c; margin-top: 2px; }

/* Complete actions */
.complete-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.complete-actions a {
  display: inline-block;
  padding: 6px 14px;
  border-radius: 5px;
  background: #3a3a3a;
  color: #d4d4d4;
  text-decoration: none;
  font-size: 13px;
}
.complete-actions a:hover { background: #4a4a4a; }
</style>
