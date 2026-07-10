<template>
  <div class="creator">
    <div class="header">
      <h2>技能创建器</h2>
      <p class="muted">快速解析需求 · 生成创建要点与文件清单</p>
    </div>

    <div class="toolbar">
      <button
        class="btn-ghost btn-thoughts"
        :class="{ active: showThoughts }"
        @click="showThoughts = !showThoughts"
        title="显示/隐藏执行过程面板"
      >
        🔍 执行过程{{ thoughts.length ? ` (${thoughts.length})` : '' }}
      </button>
    </div>

    <div class="content-area">
      <!-- Main chat column -->
      <div class="messages-column">
        <div class="messages" ref="messagesEl">
          <div v-if="messages.length === 0" class="empty">
            <p>说明你想创建或修改什么 Skill。信息足够时会直接生成创建要点和文件清单；只有真正缺少阻塞信息时才会追问。</p>
          </div>
          <template v-for="(msg, i) in messages" :key="i">
            <!-- action result card -->
            <div
              v-if="msg.role === 'system'"
              class="action-card"
              :class="msg.success ? 'ok' : 'fail'"
              :aria-label="msg.success ? '操作成功' : '操作失败'"
            >
              <span class="action-icon">{{ msg.success ? '✅' : '❌' }}</span>
              <span class="action-label">{{ actionLabel(msg.action) }}</span>
              <span class="action-name">{{ msg.name }}</span>
              <span class="action-msg">{{ msg.message }}</span>
              <span v-if="msg.path" class="action-path">{{ msg.path }}</span>
              <pre v-if="msg.stdout" class="action-output">{{ msg.stdout }}</pre>
              <pre v-if="msg.stderr" class="action-stderr">{{ msg.stderr }}</pre>
            </div>
            <!-- regular chat bubble -->
            <div v-else class="message" :class="msg.role">
              <div class="bubble">
                <ChatBubble :content="msg.content" />
              </div>
            </div>
          </template>
          <div v-if="currentStatus" class="status-bar">
            <span class="status-spinner" aria-hidden="true"></span>
            <span class="status-message">{{ currentStatus.message }}</span>
          </div>
          <div v-if="streaming" class="message assistant">
            <div class="bubble">
              <ChatBubble :content="streamBuffer" :streaming="true" />
            </div>
          </div>

          <div v-if="reviewSummary" class="review-card">
            <div class="review-header">
              <h3>{{ reviewSummaryTitle }}</h3>
              <button class="btn-ghost" @click="showInternalBlueprint = !showInternalBlueprint">
                {{ showInternalBlueprint ? '隐藏内部蓝图' : '查看内部蓝图' }}
              </button>
            </div>
            <ul>
              <li v-if="reviewSummary.goal"><strong>目标：</strong>{{ reviewSummary.goal }}</li>
              <li v-if="reviewSummary.input"><strong>输入：</strong>{{ reviewSummary.input }}</li>
              <li v-if="reviewSummary.output"><strong>输出：</strong>{{ reviewSummary.output }}</li>
              <li v-if="reviewSummary.workflow?.length"><strong>工作流：</strong>{{ reviewSummary.workflow.join(' → ') }}</li>
              <li v-if="reviewSummary.files_to_create_or_update?.length"><strong>文件：</strong>{{ reviewSummary.files_to_create_or_update.join('、') }}</li>
              <li v-if="reviewSummary.assets_to_upload?.length"><strong>需上传素材：</strong>{{ reviewSummary.assets_to_upload.join('、') }}</li>
            </ul>
            <details v-if="showInternalBlueprint && blueprintText" open>
              <summary>内部蓝图</summary>
              <pre>{{ blueprintText }}</pre>
            </details>
          </div>

          <div v-if="showPlanningProcessCard" class="planning-process-card">
            <div class="planning-process-header">
              <h3>规划过程</h3>
              <span v-if="streaming" class="planning-process-badge">更新中</span>
            </div>

            <section class="planning-section">
              <h4>责任图谱</h4>
              <div v-if="planningNodes.length || planningEdges.length" class="responsibility-flow">
                <div
                  v-for="node in planningNodes"
                  :key="node.target_file"
                  class="responsibility-node"
                >
                  <div class="node-title">{{ node.target_file }}</div>
                  <div v-if="node.role" class="node-meta">role: {{ node.role }}</div>
                  <div v-if="node.purpose" class="node-purpose">{{ node.purpose }}</div>
                  <div v-if="node.inputs?.length" class="node-list">inputs: {{ formatList(node.inputs) }}</div>
                  <div v-if="node.outputs?.length" class="node-list">outputs: {{ formatList(node.outputs) }}</div>
                  <div v-if="node.required_capabilities?.length" class="node-list">required_capabilities: {{ formatList(node.required_capabilities) }}</div>
                </div>
                <div
                  v-for="(edge, index) in planningEdges"
                  :key="`${edge.from_node || 'from'}-${edge.to_node || 'to'}-${index}`"
                  class="responsibility-edge"
                >
                  <span>{{ edge.from_node }}{{ edge.from_output ? `.${edge.from_output}` : '' }}</span>
                  <span class="edge-arrow">→</span>
                  <span>{{ edge.to_node }}{{ edge.to_input ? `.${edge.to_input}` : '' }}</span>
                </div>
              </div>
              <p v-else class="planning-muted">等待结构化责任图谱…</p>
            </section>

            <section class="planning-section">
              <h4>能力 / 工具匹配</h4>
              <div v-if="toolPlanningRows.length" class="tool-planning-list">
                <div
                  v-for="(row, index) in toolPlanningRows"
                  :key="`${row.name}-${row.capability}-${index}`"
                  class="tool-planning-row"
                >
                  <span class="tool-name">{{ row.name }}</span>
                  <span v-if="row.capability && row.capability !== row.name" class="tool-capability">{{ row.capability }}</span>
                  <span class="tool-status" :class="row.statusClass">{{ row.statusText }}</span>
                  <span v-if="row.blocking" class="tool-blocking">blocking</span>
                  <span v-if="row.score !== ''" class="tool-score">score: {{ row.score }}</span>
                  <span v-if="row.targetFiles?.length" class="tool-files">targets: {{ row.targetFiles.join('、') }}</span>
                  <span v-if="row.matchedFeatures?.length" class="tool-features">matched: {{ row.matchedFeatures.join('、') }}</span>
                </div>
              </div>
              <div v-if="selectedPrimaryToolBindings.length" class="primary-tool-bindings">
                <div
                  v-for="binding in selectedPrimaryToolBindings"
                  :key="binding.targetFile"
                  class="primary-tool-binding"
                >
                  <span class="binding-target">{{ binding.targetFile }}</span>
                  <span class="edge-arrow">→</span>
                  <span class="binding-tools">{{ binding.primaryToolIds.join('、') }}</span>
                </div>
              </div>
              <p v-if="!toolPlanningRows.length" class="planning-muted">等待能力需求…</p>
            </section>
          </div>

          <!-- Skill creation panel (shown after prepare-plan is ready) -->
          <SkillCreationPanel
            v-if="showCreationPanel && creationPlan"
            :skill-name="creationPlan.skill_name"
            :files="creationPlan.files"
            :blueprint-text="blueprintText"
            :conversation-history="chatHistory"
            :model="null"
            :warnings="creationPlan.warnings"
            :asset-requirements="creationPlan.asset_requirements || []"
            :final-outputs="creationPlan.final_outputs || []"
            :requirement-graph="creationPlan.requirement_graph || null"
            :workflow-allocation-summary="creationPlan.workflow_allocation_summary || ''"
            :tool-requirements="creationPlan.tool_requirements || []"
            :creation-blockers="creationPlan.creation_blockers || []"
            :confirmed-uploaded-assets="creationPlan.confirmed_uploaded_assets || []"
            @creation-complete="onCreationComplete"
            @creation-error="onCreationError"
          />
        </div>

        <div class="input-area">
          <div v-if="error" class="error">{{ error }}</div>
          
          <div class="context-upload-panel">
            <label class="context-upload-label">
              上传创建上下文（不会自动加入 assets）
              <input
                type="file"
                multiple
                accept=".pdf,.docx,.pptx,.xlsx,.csv,.txt,.md,.json,.yaml,.yml,.png,.jpg,.jpeg,.webp"
                @change="handleContextFileUpload"
                :disabled="streaming"
              />
            </label>
            <div v-if="uploadError" class="error">{{ uploadError }}</div>
            <div v-if="showAssetDecisionDialog" class="asset-decision-modal" role="dialog" aria-modal="true" @click.self="closeAssetDecisionDialog">
              <div class="asset-decision-card">
                <div class="asset-decision-header">
                  <div>
                    <h3>选择上传文件用途</h3>
                    <p class="muted">只有选择“固定加入 Skill assets”的文件会复制到 assets/**；关闭未选择项会默认作为参考。</p>
                  </div>
                  <button class="btn-ghost asset-decision-close" type="button" @click="closeAssetDecisionDialog">✕</button>
                </div>
                <div class="asset-decision-body">
                  <div v-for="file in pendingAssetDecisionFiles" :key="file.file_id" class="asset-decision-row">
                    <strong>{{ file.original_name || file.name }}</strong>
                    <select v-model="file.asset_decision">
                      <option value="include_as_asset">固定加入 Skill assets</option>
                      <option value="reference_only">只作为本次创建参考</option>
                      <option value="runtime_input">作为 Skill 运行时输入参考</option>
                      <option value="unknown">暂不决定</option>
                    </select>
                    <input v-if="file.asset_decision === 'include_as_asset'" v-model="file.asset_target_path" placeholder="assets/example.ext" />
                  </div>
                </div>
                <div class="asset-decision-actions">
                  <button class="btn-ghost" type="button" @click="markAllAssetDecisionsAsReference">全部作为参考</button>
                  <button class="btn-primary" type="button" @click="confirmAssetDecisions">确认选择</button>
                  <button class="btn-ghost" type="button" @click="closeAssetDecisionDialog">关闭</button>
                </div>
              </div>
            </div>
            <div v-if="uploadedContextFiles.length" class="uploaded-context-list">
              <div v-for="file in uploadedContextFiles" :key="file.file_id" class="uploaded-context-item">
                <span class="context-file-name">{{ file.original_name || file.name }}</span>
                <span class="context-file-kind">{{ file.content_kind }} / {{ file.extension }}</span>
                <span v-if="file.candidate_tools?.length" class="context-file-tools">tools: {{ file.candidate_tools.join(', ') }}</span>
                <span v-if="file.asset_decision === 'include_as_asset'" class="asset-selected-badge">已选择加入 assets: {{ file.asset_target_path }}</span>
                <button class="btn-ghost" type="button" @click="editAssetDecision(file)" :disabled="streaming">修改用途</button>
                <button class="btn-ghost" type="button" @click="removeUploadedContextFile(file.file_id)" :disabled="streaming">移除</button>
              </div>
            </div>
          </div>

          <!-- Quick action buttons -->
          <div v-if="quickActions.length" class="quick-actions">
            <p class="quick-actions-label">选择选项或输入内容：</p>
            <div class="quick-actions-buttons">
              <button
                v-for="(action, index) in quickActions"
                :key="index"
                class="quick-action-btn"
                :class="action.style"
                @click="handleQuickAction(action)"
                :disabled="streaming"
              >
                {{ action.text }}
              </button>
            </div>
          </div>
          
          <div class="row">
            <textarea
              v-model="input"
              rows="3"
              placeholder="输入你的想法…"
              @keydown.enter.exact.prevent="send"
              :disabled="streaming"
            />
            <div class="actions">
              <button class="btn-primary" @click="send" :disabled="streaming || !input.trim()">
                {{ streaming ? '生成中…' : '发送' }}
              </button>
              <button class="btn-ghost" @click="clearChat" :disabled="streaming">清空</button>
            </div>
          </div>
          <p class="hint muted">Enter 发送 · Shift+Enter 换行</p>
        </div>
      </div>

      <!-- Thinking panel sidebar -->
      <transition name="panel-slide">
        <div v-if="showThoughts" class="thinking-sidebar">
          <div class="thinking-sidebar-header">
            <span>执行过程</span>
            <button class="btn-ghost btn-close-panel" @click="showThoughts = false">✕</button>
          </div>
          <ThinkingPanel :thoughts="thoughts" />
        </div>
      </transition>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, nextTick } from 'vue'
import { streamPrepareCreationPlan, buildClarificationQuickActions, uploadCreatorContextFile } from '../composables/useCreator.js'
import ChatBubble from '../components/ChatBubble.vue'
import SkillCreationPanel from '../components/SkillCreationPanel.vue'
import ThinkingPanel from '../components/ThinkingPanel.vue'

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const ACTION_LABELS = {
  init: '初始化目录',
  write: '写入 SKILL.md',
  write_file: '写入文件',
  validate: '校验格式',
  package: '打包 Skill',
  run_script: '运行脚本',
  creator_panel: '文件清单预览',
}

function actionLabel(action) {
  return ACTION_LABELS[action] || action
}

function formatList(value) {
  if (!Array.isArray(value)) return '[]'
  return JSON.stringify(value)
}

const pendingBlueprintText = ref('')
const pendingFunctionItems = ref([])
const pendingResponsibilityEdges = ref([])
const pendingRequiredCapabilities = ref([])
const rootUserRequest = ref('')
const messages = ref([])
const input = ref('')
const streaming = ref(false)
const streamBuffer = ref('')
const error = ref('')
const messagesEl = ref(null)
const currentStatus = ref(null)
const uploadedContextFiles = ref([])
const uploadError = ref('')
const creatorUploadSessionId = ref(`creator-${Date.now()}-${Math.random().toString(36).slice(2)}`)
const showAssetDecisionDialog = ref(false)
const pendingAssetDecisionFiles = ref([])

// Quick actions state
const quickActions = ref([])

// Thinking panel state
const thoughts = ref([])
const showThoughts = ref(false)

// Creation panel state
const showCreationPanel = ref(false)
const creationPlan = ref(null)
const reviewSummary = ref(null)
const showInternalBlueprint = ref(false)
const skillName = ref('')
const selectedExistingSkillName = ref('')
const pendingSupplementQuestion = ref('')
const pendingPrepareAction = ref('none')
const reviewSummaryStage = ref('')
const reviewSummaryTitle = computed(() => {
  if (creationPlan.value) return '创建要点'
  if (reviewSummaryStage.value === 'supplement_confirmation') return '已根据补充内容更新的创建要点'
  return '已整理的创建要点'
})

const planningNodes = computed(() => (
  Array.isArray(pendingFunctionItems.value)
    ? pendingFunctionItems.value
        .filter(item => item && typeof item === 'object')
        .map(item => ({
          target_file: String(item.target_file || '').trim(),
          role: String(item.role || '').trim(),
          purpose: String(item.purpose || '').trim(),
          inputs: Array.isArray(item.inputs) ? item.inputs : [],
          outputs: Array.isArray(item.outputs) ? item.outputs : [],
          required_capabilities: Array.isArray(item.required_capabilities)
            ? item.required_capabilities
            : [],
        }))
        .filter(item => item.target_file)
    : []
))

const planningEdges = computed(() => (
  Array.isArray(pendingResponsibilityEdges.value)
    ? pendingResponsibilityEdges.value
        .filter(edge => edge && typeof edge === 'object')
        .map(edge => ({
          from_node: String(edge.from_node || '').trim(),
          from_output: String(edge.from_output || '').trim(),
          to_node: String(edge.to_node || '').trim(),
          to_input: String(edge.to_input || '').trim(),
        }))
        .filter(edge => edge.from_node || edge.to_node)
    : []
))

const requiredCapabilityRows = computed(() => {
  const seen = new Set()
  const rows = []
  const capabilities = pendingRequiredCapabilities.value.length
    ? pendingRequiredCapabilities.value
    : planningNodes.value.flatMap(item => item.required_capabilities || [])
  for (const capability of capabilities) {
    const name = String(capability || '').trim()
    if (!name || seen.has(name)) continue
    seen.add(name)
    rows.push({
      name,
      capability: name,
      statusText: '正在匹配工具…',
      statusClass: 'pending',
      blocking: false,
    })
  }
  return rows
})

function normalizeToolStatusText(status) {
  const normalized = String(status || '').trim()
  if (['ready', 'allowed'].includes(normalized)) return '已匹配'
  if (['not_authorized', 'disabled', 'missing'].includes(normalized)) return '未就绪'
  return normalized || '正在匹配…'
}

function normalizeToolStatusClass(statusText) {
  if (statusText === '已匹配') return 'ready'
  if (statusText === '未就绪') return 'blocked'
  return 'pending'
}

function toolRowFromRecord(record) {
  const toolId = String(record?.tool_id || '').trim()
  const capability = String(record?.capability || record?.name || '').trim()
  const name = String(record?.tool_name || toolId || record?.display_name || record?.name || capability || '').trim()
  const statusSource = record?.status || record?.readiness || ''
  const statusText = normalizeToolStatusText(statusSource)
  return {
    toolId,
    name,
    capability,
    statusText,
    statusClass: normalizeToolStatusClass(statusText),
    blocking: Boolean(record?.blocking),
    score: record?.score ?? '',
    targetFiles: Array.isArray(record?.target_files) ? record.target_files : [],
    matchedFeatures: Array.isArray(record?.matched_features) ? record.matched_features : [],
  }
}

const selectedPrimaryToolBindings = computed(() => {
  const selectedPrimaryTools = creationPlan.value?.tool_pool_summary?.selected_primary_tools
  if (!selectedPrimaryTools || typeof selectedPrimaryTools !== 'object' || Array.isArray(selectedPrimaryTools)) {
    return []
  }
  return Object.entries(selectedPrimaryTools)
    .map(([targetFile, toolIds]) => ({
      targetFile: String(targetFile || '').trim(),
      primaryToolIds: Array.isArray(toolIds)
        ? toolIds.map(toolId => String(toolId || '').trim()).filter(Boolean)
        : [],
    }))
    .filter(binding => binding.targetFile && binding.primaryToolIds.length)
})

const finalToolRows = computed(() => {
  const toolPoolTools = Array.isArray(creationPlan.value?.tool_pool_summary?.tools)
    ? creationPlan.value.tool_pool_summary.tools
    : []

  if (!toolPoolTools.length) return []

  return toolPoolTools
    .filter(item => item && typeof item === 'object')
    .map(toolRowFromRecord)
    .filter(row => row.name || row.capability)
})

const toolPlanningRows = computed(() => (
  finalToolRows.value.length
    ? finalToolRows.value
    : requiredCapabilityRows.value
))

const showPlanningProcessCard = computed(() => (
  planningNodes.value.length > 0 ||
  planningEdges.value.length > 0 ||
  (
    streaming.value &&
    (
      pendingFunctionItems.value.length > 0 ||
      pendingResponsibilityEdges.value.length > 0
    )
  )
))

// The raw blueprint text extracted from the latest blueprint assistant message
const blueprintText = computed(() => {
  return (
    creationPlan.value?.blueprint_text ||
    pendingBlueprintText.value ||
    ''
  )
})

// History sent to the LLM excludes system action-result messages
const chatHistory = computed(() => messages.value.filter(m => m.role !== 'system'))


function resolveCurrentSkillName() {
  return (
    creationPlan.value?.skill_name ||
    skillName.value ||
    selectedExistingSkillName.value ||
    ''
  )
}

function collectUploadedFileMetadata() {
  return uploadedContextFiles.value.map(file => ({
    file_id: file.file_id,
    session_id: file.session_id,
    name: file.name,
    original_name: file.original_name,
    path: file.path,
    size: file.size,
    mime_type: file.mime_type,
    extension: file.extension,
    suggested_role: file.suggested_role,
    asset_decision: file.asset_decision,
    candidate_tools: file.candidate_tools || [],
    content_kind: file.content_kind,
    asset_target_path: file.asset_target_path || '',
  }))
}

async function handleContextFileUpload(event) {
  const files = Array.from(event.target.files || [])
  event.target.value = ''
  if (!files.length || streaming.value) return
  uploadError.value = ''
  for (const file of files) {
    try {
      const metadata = await uploadCreatorContextFile({ file, sessionId: creatorUploadSessionId.value })
      uploadedContextFiles.value.push({ ...metadata, asset_target_path: defaultAssetTargetPath(metadata) })
      pendingAssetDecisionFiles.value.push(uploadedContextFiles.value[uploadedContextFiles.value.length - 1])
    } catch (e) {
      uploadError.value = e.message || '上下文文件上传失败'
    }
  }
  if (pendingAssetDecisionFiles.value.length) showAssetDecisionDialog.value = true
}

function defaultAssetTargetPath(file) {
  const name = String(file?.original_name || file?.name || 'upload').split(/[\\/]/).pop().replace(/[^A-Za-z0-9_.-]+/g, '_')
  return `assets/${name || 'upload'}`
}

function editAssetDecision(file) {
  pendingAssetDecisionFiles.value = [file]
  showAssetDecisionDialog.value = true
}

function normalizePendingAssetDecisions({ defaultDecision = 'reference_only' } = {}) {
  pendingAssetDecisionFiles.value.forEach(file => {
    if (!file.asset_decision || file.asset_decision === 'unknown') file.asset_decision = defaultDecision
    if (file.asset_decision === 'include_as_asset' && !file.asset_target_path) file.asset_target_path = defaultAssetTargetPath(file)
  })
}

function markAllAssetDecisionsAsReference() {
  pendingAssetDecisionFiles.value.forEach(file => {
    file.asset_decision = 'reference_only'
  })
  closeAssetDecisionDialog()
}

function confirmAssetDecisions() {
  normalizePendingAssetDecisions({ defaultDecision: 'reference_only' })
  pendingAssetDecisionFiles.value = []
  showAssetDecisionDialog.value = false
}

function closeAssetDecisionDialog() {
  normalizePendingAssetDecisions({ defaultDecision: 'reference_only' })
  pendingAssetDecisionFiles.value = []
  showAssetDecisionDialog.value = false
}

function removeUploadedContextFile(fileId) {
  uploadedContextFiles.value = uploadedContextFiles.value.filter(file => file.file_id !== fileId)
}

function shouldPreparePlanRevise({
  skillName,
  previousBlueprintText,
}) {
  return Boolean(
    skillName ||
    previousBlueprintText
  )
}

async function scrollBottom() {
  await nextTick()
  if (messagesEl.value) {
    messagesEl.value.scrollTop = messagesEl.value.scrollHeight
  }
}

// Handle quick action button click
async function handleQuickAction(value) {
  const action = typeof value === 'object' && value !== null ? value : { value }
  if (!action.value || streaming.value) return
  // Clear previous quick actions
  quickActions.value = []

  pendingRequiredCapabilities.value = []
  pendingPrepareAction.value = action.prepareAction || 'none'
  if (action.prepareAction === 'request_supplement') {
    input.value = action.value
    await send()
    pendingSupplementQuestion.value = action.value
    return
  }
  // Send the value as user input
  input.value = action.value
  await send()
}


// ---------------------------------------------------------------------------
// Send
// ---------------------------------------------------------------------------

async function send() {
  let text = input.value.trim()

  if (!text || streaming.value) {
    return
  }

  if (pendingSupplementQuestion.value) {
    text = (
      `${pendingSupplementQuestion.value}\n` +
      `补充：${text}`
    )

    pendingSupplementQuestion.value = ''

    pendingPrepareAction.value = (
      'submit_supplement'
    )
  }

  const currentPrepareAction = (
    pendingPrepareAction.value ||
    'none'
  )

  const conversationHistoryBeforeCurrent = (
    chatHistory.value.map(message => ({
      ...message,
    }))
  )

  if (
    !rootUserRequest.value &&
    !blueprintText.value &&
    currentPrepareAction === 'none'
  ) {
    rootUserRequest.value = text
  }

  const effectiveUserRequest = (
    rootUserRequest.value ||
    text
  )

  error.value = ''

  quickActions.value = []

  showCreationPanel.value = false

  messages.value.push({
    role: 'user',
    content: text,
  })

  input.value = ''

  await scrollBottom()

  streaming.value = true

  currentStatus.value = {
    message: (
      pendingFunctionItems.value.length > 0
        ? '正在校验责任图谱并匹配可用工具…'
        : '正在解析需求并准备创建计划…'
    ),
  }

  try {
    const currentSkillName = (
      resolveCurrentSkillName()
    )

    const previousBlueprintText = (
      blueprintText.value
    )

    const humanFeedback = text

    const mode = shouldPreparePlanRevise({
      skillName: currentSkillName,

      previousBlueprintText,
    })
      ? 'revise'
      : 'create'

    const payload = {
      mode,

      skill_name: currentSkillName,

      user_request: (
        effectiveUserRequest
      ),

      conversation_history: (
        conversationHistoryBeforeCurrent
      ),

      previous_blueprint_text: (
        previousBlueprintText
      ),

      responsibility_edges: (
        creationPlan.value?.responsibility_edges ||
        pendingResponsibilityEdges.value ||
        []
      ),

      function_items: (
        creationPlan.value?.function_items ||
        pendingFunctionItems.value ||
        []
      ),

      human_feedback: (
        humanFeedback
      ),

      prepare_action: (
        currentPrepareAction
      ),

      uploaded_files: (
        collectUploadedFileMetadata()
      ),

      model: null,
    }

    const plan = await streamPrepareCreationPlan(
      payload,
      event => {
        if (
          event.event === 'planner_draft' ||
          event.event === 'planner_converged'
        ) {
          pendingFunctionItems.value = Array.isArray(event.function_items)
            ? event.function_items
            : []
          pendingResponsibilityEdges.value = Array.isArray(event.responsibility_edges)
            ? event.responsibility_edges
            : []
          return
        }
        if (event.event === 'file_plan_ready') {
          currentStatus.value = { message: '文件规划已完成，正在绑定责任图谱…' }
          return
        }
        if (event.event === 'graph_resolved') {
          currentStatus.value = { message: '责任图谱已校验' }
          return
        }
        if (event.event === 'tool_planning') {
          pendingRequiredCapabilities.value = Array.isArray(event.required_capabilities)
            ? event.required_capabilities
            : []
          return
        }
        if (event.event === 'tool_pool_ready') {
          creationPlan.value = {
            ...(creationPlan.value || {}),
            tool_pool_summary: event.tool_pool_summary || {},
          }
        }
      },
    )

    if (
      typeof plan.blueprint_text === 'string' &&
      plan.blueprint_text.trim()
    ) {
      pendingBlueprintText.value = (
        plan.blueprint_text.trim()
      )
    }

    if (Array.isArray(plan.responsibility_edges)) {
      pendingResponsibilityEdges.value = plan.responsibility_edges
    }

    if (Array.isArray(plan.function_items)) {
      pendingFunctionItems.value = plan.function_items
    }

    if (plan.skill_name) {
      skillName.value = plan.skill_name
    }

    if (
      currentPrepareAction
      === 'request_supplement'
    ) {
      pendingSupplementQuestion.value = text

    } else if (
      currentPrepareAction === 'confirm' ||
      currentPrepareAction
      === 'submit_supplement'
    ) {
      pendingPrepareAction.value = 'none'
    }

    const summary = (
      plan.review_summary ||
      null
    )

    const question = (
      plan.clarifying_questions ||
      []
    )[0]

    const hasSummaryContent = Boolean(
      summary &&
      (
        summary.goal ||
        summary.input ||
        summary.output ||
        summary.workflow?.length ||
        summary.files_to_create_or_update
          ?.length ||
        summary.assets_to_upload?.length ||
        summary.changes?.length
      )
    )

    const stage = (
      plan.prepare_stage ||
      ''
    )

    const isCreationPointsConfirmation = (
      stage
      === 'creation_points_confirmation' ||
      stage
      === 'supplement_confirmation' ||
      plan.status === 'ready' ||
      (
        plan.status
        === 'needs_clarification' &&
        /创建要点|补充|按这些要点/.test(
          String(question || '')
        )
      )
    )

    reviewSummaryStage.value = stage

    reviewSummary.value = (
      hasSummaryContent &&
      isCreationPointsConfirmation
    )
      ? {
          ...summary,
          risks: [],
        }
      : null

    if (
      plan.status
      === 'needs_clarification'
    ) {
      messages.value.push({
        role: 'assistant',

        content: reviewSummary.value
          ? (
              question ||
              '请确认是否需要补充。'
            )
          : (
              '我还需要确认一个必要信息：\n\n' +
              (
                question ||
                '请补充当前最阻塞创建计划的信息。'
              )
            ),
      })

      quickActions.value = (
        buildClarificationQuickActions(
          question
            ? [question]
            : [],
          {
            prepareStage: stage,
          },
        )
      )

      return
    }

    if (plan.status === 'blocked') {
      const blockers = (
        plan.creation_blockers ||
        []
      )

      messages.value.push({
        role: 'assistant',

        content:
          '当前暂时无法继续创建：\n\n' +
          blockers
            .map(
              (blocker, index) => {
                const message = (
                  typeof blocker === 'string'
                    ? blocker
                    : (
                        blocker.message ||
                        JSON.stringify(blocker)
                      )
                )

                return (
                  `${index + 1}. ${message}`
                )
              },
            )
            .join('\n'),
      })

      return
    }

    creationPlan.value = plan

    pendingBlueprintText.value = (
      plan.blueprint_text ||
      pendingBlueprintText.value
    )

    pendingResponsibilityEdges.value = (
      Array.isArray(plan.responsibility_edges)
        ? plan.responsibility_edges
        : pendingResponsibilityEdges.value
    )

    pendingFunctionItems.value = (
      Array.isArray(plan.function_items)
        ? plan.function_items
        : pendingFunctionItems.value
    )

    skillName.value = (
      plan.skill_name ||
      currentSkillName
    )

    showCreationPanel.value = true

    messages.value.push({
      role: 'system',

      action: 'creator_panel',

      name: plan.skill_name,

      success: true,

      message: (
        '已生成创建要点和文件清单，' +
        '可直接开始生成。'
      ),
    })

  } catch (e) {
    error.value = e.message

  } finally {
    currentStatus.value = null

    streaming.value = false

    await scrollBottom()
  }
}

// ---------------------------------------------------------------------------
// Creation panel handlers
// ---------------------------------------------------------------------------

function onCreationComplete({ skillName, validateResult, packageResult }) {
  messages.value.push({
    role: 'assistant',
    content:
      `✅ Skill **${skillName}** 已创建完成！\n\n` +
      `- 严格端到端校验：${validateResult?.success ? '通过' : '未知'}\n` +
      `- 打包结果：${packageResult?.success ? '完成' : '未知'}\n\n` +
      `现在可以在沙盒模式下测试。`,
  })
  showCreationPanel.value = false
  scrollBottom()
}

function onCreationError(errMsg) {
  error.value = `Skill 创建未完成：${errMsg}`
}

// ---------------------------------------------------------------------------
// Clear
// ---------------------------------------------------------------------------

function clearChat() {
  messages.value = []

  streamBuffer.value = ''

  error.value = ''

  currentStatus.value = null

  thoughts.value = []

  showThoughts.value = false

  showCreationPanel.value = false

  creationPlan.value = null

  reviewSummary.value = null

  reviewSummaryStage.value = ''

  showInternalBlueprint.value = false

  skillName.value = ''

  selectedExistingSkillName.value = ''

  pendingBlueprintText.value = ''

  pendingFunctionItems.value = []

  pendingResponsibilityEdges.value = []

  pendingRequiredCapabilities.value = []

  rootUserRequest.value = ''

  pendingSupplementQuestion.value = ''

  pendingPrepareAction.value = 'none'

  quickActions.value = []

  uploadedContextFiles.value = []

  uploadError.value = ''
}

</script>

<style scoped>
.creator {
  --thinking-sidebar-width: 320px;
  --thinking-sidebar-mobile-height: 280px;
  --thinking-breakpoint: 900px;

  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}

.header {
  padding: 20px 24px 12px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.header h2 { font-size: 18px; font-weight: 600; margin-bottom: 4px; }
.context-upload-panel { margin-bottom: 10px; padding: 10px; border: 1px dashed var(--border); border-radius: 8px; }
.context-upload-label { display: flex; flex-direction: column; gap: 6px; font-size: 13px; color: var(--muted); }
.uploaded-context-list { display: flex; flex-direction: column; gap: 6px; margin-top: 8px; }
.uploaded-context-item { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 12px; }
.context-file-name { font-weight: 600; }
.context-file-kind, .context-file-tools { color: var(--muted); }

.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 24px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}

.btn-thoughts {
  margin-left: auto;
  font-size: 13px;
  padding: 5px 12px;
  border-radius: 6px;
  color: var(--text);
  transition: background 0.15s, color 0.15s;
}
.btn-thoughts.active {
  background: #eff6ff;
  color: #1e40af;
  border-color: #bfdbfe;
}

/* Main two-column layout */
.content-area {
  flex: 1;
  display: flex;
  overflow: hidden;
  min-height: 0;
}

.messages-column {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-width: 0;
}

.messages {
  flex: 1;
  overflow-y: auto;
  padding: 16px 24px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* Execution status bar */
.status-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 14px;
  border-radius: 8px;
  font-size: 13px;
  border: 1px solid var(--border);
  background: var(--surface2);
  color: var(--text);
  animation: status-fade-in 0.2s ease;
}

.status-spinner {
  display: inline-block;
  width: 14px;
  height: 14px;
  border: 2px solid currentColor;
  border-top-color: transparent;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  flex-shrink: 0;
  opacity: 0.7;
}
.status-message { flex: 1; }

@keyframes spin {
  to { transform: rotate(360deg); }
}
@keyframes status-fade-in {
  from { opacity: 0; transform: translateY(4px); }
  to   { opacity: 1; transform: translateY(0); }
}

.empty {
  margin: auto;
  text-align: center;
  color: var(--text-muted);
  max-width: 400px;
  padding: 40px 0;
}

.message { display: flex; }
.message.user { justify-content: flex-end; }
.message.assistant { justify-content: flex-start; }

.bubble {
  max-width: 72%;
  padding: 12px 16px;
  border-radius: 12px;
  background: var(--surface2);
  border: 1px solid var(--border);
}
.message.user .bubble {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
}

/* Action result card */
.action-card {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px 10px;
  padding: 10px 14px;
  border-radius: 8px;
  font-size: 13px;
  border: 1px solid transparent;
}
.action-card.ok {
  background: #f0fdf4;
  border-color: #bbf7d0;
  color: #166534;
}
.action-card.fail {
  background: #fef2f2;
  border-color: #fecaca;
  color: #991b1b;
}
.action-icon { font-size: 15px; }
.action-label { font-weight: 600; }
.action-name { font-family: monospace; background: rgba(0,0,0,.07); padding: 1px 6px; border-radius: 4px; }
.action-msg { flex: 1 1 100%; margin-top: 2px; opacity: .85; }
.action-path { flex: 1 1 100%; font-family: monospace; font-size: 12px; opacity: .7; word-break: break-all; }
.action-output, .action-stderr {
  flex: 1 1 100%;
  margin: 4px 0 0;
  padding: 6px 8px;
  border-radius: 4px;
  font-family: 'Fira Code', 'Cascadia Code', monospace;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 200px;
  overflow-y: auto;
}
.action-output { background: rgba(0,0,0,.06); }
.action-stderr { background: rgba(200,0,0,.07); }

.input-area {
  padding: 12px 24px 20px;
  border-top: 1px solid var(--border);
  flex-shrink: 0;
}
.input-area .error {
  margin-bottom: 12px;
  padding: 8px 12px;
  background: #fee2e2;
  border-radius: 6px;
  color: #991b1b;
  font-size: 13px;
}
.input-area .row {
  display: flex;
  gap: 12px;
  align-items: flex-start;
}
.input-area textarea {
  flex: 1;
  min-width: 0;
  resize: none;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  font-size: 14px;
  font-family: inherit;
  background: var(--surface);
  color: var(--text);
}
.input-area textarea:focus {
  outline: none;
  border-color: var(--accent);
}
.input-area textarea:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
.input-area .actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}
.input-area .hint {
  margin-top: 8px;
  font-size: 12px;
  color: var(--text-muted);
}

/* Quick actions */
.quick-actions {
  margin-bottom: 12px;
}
.quick-actions-label {
  font-size: 13px;
  color: var(--text-muted);
  margin: 0 0 8px 0;
}
.quick-actions-buttons {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.quick-action-btn {
  padding: 8px 14px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text);
  font-size: 14px;
  cursor: pointer;
  transition: all 0.15s ease;
}
.quick-action-btn:hover:not(:disabled) {
  background: var(--surface2);
  border-color: var(--accent);
}
.quick-action-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
.quick-action-btn.primary {
  background: var(--accent);
  border-color: var(--accent);
  color: white;
}
.quick-action-btn.primary:hover:not(:disabled) {
  filter: brightness(1.05);
}

/* Thinking panel sidebar */
.thinking-sidebar {
  width: var(--thinking-sidebar-width);
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  border-left: 1px solid var(--border);
  background: var(--surface);
  overflow: hidden;
}

.thinking-sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  font-size: 13px;
  font-weight: 600;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  color: var(--text);
}

.btn-close-panel {
  font-size: 12px;
  padding: 2px 6px;
  line-height: 1;
}

/* Slide-in transition for the sidebar */
.panel-slide-enter-active,
.panel-slide-leave-active {
  transition: width 0.25s ease, opacity 0.2s ease;
  overflow: hidden;
}
.panel-slide-enter-from,
.panel-slide-leave-to {
  width: 0;
  opacity: 0;
}
.panel-slide-enter-to,
.panel-slide-leave-from {
  width: var(--thinking-sidebar-width);
  opacity: 1;
}

/* On narrow viewports, sidebar stacks below the chat */
@media (max-width: 900px) {
  .content-area { flex-direction: column; }

  .thinking-sidebar {
    width: 100%;
    border-left: none;
    border-top: 1px solid var(--border);
    max-height: var(--thinking-sidebar-mobile-height);
  }

  .panel-slide-enter-from,
  .panel-slide-leave-to {
    width: 100%;
    max-height: 0;
    opacity: 0;
  }

  .panel-slide-enter-to,
  .panel-slide-leave-from {
    width: 100%;
    max-height: var(--thinking-sidebar-mobile-height);
    opacity: 1;
  }
}
.review-card {
  margin: 16px 0;
  padding: 16px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--surface, #fff);
}
.review-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
}
.review-header h3 { margin: 0; font-size: 16px; }
.review-card ul { margin: 0; padding-left: 20px; }
.review-card li { margin: 6px 0; }
.review-card pre {
  white-space: pre-wrap;
  overflow: auto;
  max-height: 360px;
  padding: 12px;
  border-radius: 8px;
  background: rgba(0, 0, 0, 0.04);
}

.planning-process-card {
  margin: 12px 0 16px;
  padding: 14px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--surface, #fff);
}

.planning-process-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
}

.planning-process-header h3 {
  margin: 0;
  font-size: 16px;
}

.planning-process-badge {
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
  color: #1d4ed8;
  background: #dbeafe;
}

.planning-section + .planning-section {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
}

.planning-section h4 {
  margin: 0 0 8px;
  font-size: 13px;
  color: var(--text-muted);
}

.responsibility-flow {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.responsibility-node {
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface2, #f8fafc);
  font-size: 12px;
}

.node-title {
  font-family: 'Fira Code', 'Cascadia Code', monospace;
  font-weight: 700;
  color: var(--text);
  word-break: break-all;
}

.node-meta,
.node-list,
.planning-muted,
.tool-capability {
  color: var(--text-muted);
}

.node-purpose {
  margin-top: 4px;
}

.node-list {
  margin-top: 3px;
  word-break: break-word;
}

.responsibility-edge {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 8px;
  font-family: 'Fira Code', 'Cascadia Code', monospace;
  font-size: 12px;
  color: var(--text-muted);
  word-break: break-all;
}

.edge-arrow {
  color: var(--accent);
  font-weight: 700;
}

.tool-planning-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.tool-planning-row {
  display: grid;
  grid-template-columns: minmax(140px, 1fr) minmax(120px, 1fr) auto auto;
  gap: 8px;
  align-items: center;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  font-size: 12px;
  background: var(--surface2, #f8fafc);
}

.tool-name {
  font-family: 'Fira Code', 'Cascadia Code', monospace;
  font-weight: 600;
  word-break: break-all;
}

.tool-status {
  justify-self: start;
  padding: 2px 8px;
  border-radius: 999px;
  background: #e5e7eb;
  color: #374151;
}

.tool-status.ready {
  background: #dcfce7;
  color: #166534;
}

.tool-status.blocked {
  background: #fee2e2;
  color: #991b1b;
}

.tool-status.pending {
  background: #fef3c7;
  color: #92400e;
}

.tool-blocking {
  color: #991b1b;
  font-weight: 600;
}

.tool-score,
.tool-files,
.tool-features {
  grid-column: 1 / -1;
  color: var(--text-muted);
  word-break: break-word;
}

.primary-tool-bindings {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.primary-tool-binding {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 8px;
  background: var(--surface2, #f8fafc);
  font-size: 12px;
}

.binding-target,
.binding-tools {
  font-family: 'Fira Code', 'Cascadia Code', monospace;
  word-break: break-all;
}

.asset-decision-modal {
  position: fixed;
  inset: 0;
  z-index: 2000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: rgba(0, 0, 0, 0.56);
  backdrop-filter: blur(2px);
}

.asset-decision-card {
  width: min(720px, 96vw);
  max-height: min(720px, 86vh);
  overflow: hidden;
  display: flex;
  flex-direction: column;
  gap: 14px;
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 20px;
  background: var(--surface, #fff);
  box-shadow: 0 24px 80px rgba(0, 0, 0, 0.35);
}

.asset-decision-header,
.asset-decision-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.asset-decision-header h3 {
  margin: 0 0 6px;
}

.asset-decision-close {
  align-self: flex-start;
}

.asset-decision-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
  overflow: auto;
  padding-right: 2px;
}

.asset-decision-row {
  display: grid;
  grid-template-columns: minmax(140px, 1fr) minmax(180px, 220px) minmax(180px, 1fr);
  gap: 10px;
  align-items: center;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: 10px;
}

.asset-decision-row select,
.asset-decision-row input {
  width: 100%;
}

.asset-decision-actions {
  justify-content: flex-end;
  flex-wrap: wrap;
}

@media (max-width: 720px) {
  .asset-decision-row {
    grid-template-columns: 1fr;
  }
}

</style>
