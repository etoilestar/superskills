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

    <!-- Warnings from blueprint parser -->
    <div v-if="visibleWarnings.length" class="warnings">
      <div v-for="(w, i) in visibleWarnings" :key="i" class="warning-item">⚠️ {{ warningMessage(w) }}</div>
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
        :disabled="localFiles.length === 0"
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
        <pre v-if="validateResult?.message" class="post-detail">{{ validateResult.message }}</pre>
        <div v-if="validateResult?.repair_events?.length" class="post-detail repair-events">
          <div v-for="(event, idx) in validateResult.repair_events" :key="idx" class="repair-event">
            <strong>{{ event.phase || 'e2e_repair' }}</strong>
            <span v-if="event.e2e_session_id"> · session {{ event.e2e_session_id }}</span>
            <span v-if="event.target_file"> · {{ event.target_file }}</span>
            <span v-if="event.resume_from_step"> · 从第 {{ event.resume_from_step }} 步继续</span>
            <span v-if="Array.isArray(event.reused_checkpoints)"> · 复用 checkpoint: {{ event.reused_checkpoints.join(', ') || '无' }}</span>
            <span v-if="Array.isArray(event.invalidated_checkpoints)"> · 失效 checkpoint: {{ event.invalidated_checkpoints.join(', ') || '无' }}</span>
            <span v-if="event.reused_venv !== undefined"> · {{ event.reused_venv ? '复用 venv' : '准备 venv' }}</span>
            <span v-if="event.patch_status"> · patch {{ event.patch_status }}</span>
            <span v-if="event.rerun_status"> · rerun {{ event.rerun_status }}</span>
            <pre v-if="event.diff_excerpt" class="post-detail">{{ event.diff_excerpt }}</pre>
          </div>
        </div>
      </div>

      <div
        v-if="packageResult !== null"
        class="post-item"
        :class="{ success: packageResult?.success, fail: packageResult && !packageResult.success }"
      >
        <span>{{ packageResult.success ? '✅ 打包完成' : '❌ 打包失败' }}</span>
        <pre v-if="packageResult?.message" class="post-detail">{{ packageResult.message }}</pre>
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
  finalizeSkillMd,
  writeFile,
  validateSkill,
  packageSkill,
  uploadAssetAPI,
} from '../composables/useCreator.js'

// ---------------------------------------------------------------------------
// Props & emits
// ---------------------------------------------------------------------------

const props = defineProps({
  skillName: { type: String, required: true },
  files: { type: Array, required: true },    // [{ path, purpose, required, can_skip }]
  blueprintText: { type: String, default: '' },
  conversationHistory: { type: Array, default: () => [] },
  model: { type: String, default: null },
  warnings: { type: Array, default: () => [] },
  assetRequirements: { type: Array, default: () => [] },
  finalOutputs: { type: Array, default: () => [] },
})

const emit = defineEmits(['creation-complete', 'creation-error'])

// ---------------------------------------------------------------------------
// Reactive state
// ---------------------------------------------------------------------------

// File status: 'pending' | 'generating' | 'preview' | 'writing' | 'done' | 'skipped' | 'error'
const localFiles = ref(
  [
    ...props.files,
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
  ]
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
      status: f.path === 'SKILL.md' ? 'pending' : 'pending',
      pendingLabel: f.path === 'SKILL.md' ? '待最终生成 / finalize pending' : '',
      generatedContent: '',
      bytesWritten: 0,
      error: '',
      showPreview: false,
      repairMessage: '',
      uploaded: false,
    }))
)

const localSkillName = ref(props.skillName)
const editingName = ref(false)
const nameError = ref('')
const nameInputRef = ref(null)

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
  }
  return icons[status] ?? '⬜'
}

function formatBytes(n) {
  if (!n) return '0 B'
  if (n < 1024) return `${n} B`
  return `${(n / 1024).toFixed(1)} KB`
}

function normalizeSkillPath(path) {
  return String(path || '').replace(/\\/g, '/').trim()
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
  file.error = ''
  file.generatedContent = ''
  file.repairMessage = ''

  try {
    if (file.path === 'SKILL.md') {
      const result = await finalizeSkillMd({
        skillName: localSkillName.value,
        description: file.purpose || props.skillName,
        blueprintText: props.blueprintText,
        model: props.model,
        references: localFiles.value.map(f => normalizeSkillPath(f.path)).filter(p => p.startsWith('references/')),
        assets: localFiles.value.map(f => normalizeSkillPath(f.path)).filter(p => p.startsWith('assets/')),
        finalOutputs: props.finalOutputs,
      })
      if (result?.success !== true) {
        const failures = Array.isArray(result?.failures)
          ? result.failures.map(item => `${item.id || 'check'}: ${item.message || ''}`).join('\n')
          : ''
        const error = new Error(failures || result?.error || 'SKILL.md finalize 未通过校验，已阻止预览/写入')
        error.detail = { failed_checks: result?.failures || [] }
        throw error
      }
      file.generatedContent = result.content || ''
      if (!file.generatedContent.trim()) {
        throw new Error('SKILL.md finalizer 未返回任何内容')
      }
      file.repairMessage = result.repair_attempts ? `SKILL.md 已完成 ${result.repair_attempts} 轮模型修复并通过校验。` : ''
      file.status = 'preview'
      return
    }
    for await (const chunk of generateFileStream({
      skillName: localSkillName.value,
      filePath: file.path,
      purpose: file.purpose,
      blueprintText: props.blueprintText,
      conversationHistory: props.conversationHistory,
      model: props.model,
      role: file.role || null,
      skillPlanEntry: file,
    })) {
      if (typeof chunk === 'string') {
        file.generatedContent += chunk
      } else if (typeof chunk?.content === 'string') {
        file.generatedContent += chunk.content
      } else if (typeof chunk?.delta === 'string') {
        file.generatedContent += chunk.delta
      } else if (chunk?.done) {
        break
      } else if (chunk?.validation) {
        const statusText = chunk.validation.status === 'failed' ? '自动修复失败' : '自动修复中'
        file.repairMessage = `${statusText}（第 ${chunk.validation.attempt} 次）：${chunk.validation.error || ''}`
      } else if (chunk?.error) {
        throw new Error(chunk.error)
      }
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

    if (file.path === 'SKILL.md' && file.generatedContent?.trim()) {
      file.showPreview = true
      file.repairMessage = 'SKILL.md 校验失败，可在下方手动微调后点击“写入”或“重新生成”。'
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
  const r = await initSkill(localSkillName.value)
  if (!r.success) throw new Error(r.message)
  skillInitialized.value = true
}

async function startCreation() {
  if (nameError.value) return
  currentIndex.value = 0
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

      if (localFiles.value[idx].status === 'error') {
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

    if (localFiles.value[idx].status === 'error') {
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
  packageResult.value = null

  try {
    validateResult.value = await validateSkill(localSkillName.value, {
      model: props.model,
      autoRepair: true,
      maxE2ERepairAttempts: 5,
    })
  } catch (err) {
    validateResult.value = {
      success: false,
      path: null,
      message: err.message || '严格端到端校验请求失败',
    }
  }

  if (!validateResult.value?.success) {
    phase.value = 'failed'
    emit('creation-error', validateResult.value?.message || '严格端到端校验失败，已停止打包。')
    return
  }

  phase.value = 'packaging'
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
    phase.value = 'failed'
    emit('creation-error', packageResult.value?.message || '打包失败')
    return
  }

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
