<template>
  <div class="creator">
    <div class="header">
      <h2>技能创建器</h2>
      <p class="muted">由 <code>kernel/SKILL.md</code> 驱动 · 支持多轮对话</p>
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
            <p>向 AI 说明你想创建什么 Skill，它会引导你一步步完成。</p>
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

          <!-- Skill creation panel (shown after user confirms blueprint) -->
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
            @creation-complete="onCreationComplete"
            @creation-error="onCreationError"
          />
        </div>

        <div class="input-area">
          <div v-if="error" class="error">{{ error }}</div>
          
          <!-- Quick action buttons -->
          <div v-if="quickActions.length" class="quick-actions">
            <p class="quick-actions-label">选择选项或输入内容：</p>
            <div class="quick-actions-buttons">
              <button
                v-for="(action, index) in quickActions"
                :key="index"
                class="quick-action-btn"
                :class="action.style"
                @click="handleQuickAction(action.value)"
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
import { ref, computed, nextTick, onMounted } from 'vue'
import { streamChat } from '../composables/useChat.js'
import { analyzeBlueprintPlan } from '../composables/useCreator.js'
import ChatBubble from '../components/ChatBubble.vue'
import SkillCreationPanel from '../components/SkillCreationPanel.vue'
import ThinkingPanel from '../components/ThinkingPanel.vue'

// ---------------------------------------------------------------------------
// Keywords kept in sync with backend/_CONFIRM_KEYWORDS and _BLUEPRINT_MARKERS
// ---------------------------------------------------------------------------
const CONFIRM_KEYWORDS = [
  '对，开始做吧',
  '开始做吧',
  '开始创建',
  '开始生成',
  '确认，开始',
  '确认，继续构建',
  '继续构建',
  '确认继续',
  '确认开始',
  '可以开始',
  '没问题，开始',
]
const BLUEPRINT_MARKER = '📋 Skill 架构蓝图'

function isCreationConfirmation(text) {
  return CONFIRM_KEYWORDS.some(kw => text.includes(kw))
}

function hasBlueprintInHistory() {
  return messages.value.some(
    m => m.role === 'assistant' && (m.content || '').includes(BLUEPRINT_MARKER)
  )
}

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

const messages = ref([])
const input = ref('帮我创建一个查询系统时间的skill')
const streaming = ref(false)
const streamBuffer = ref('')
const error = ref('')
const messagesEl = ref(null)
const currentStatus = ref(null)

// Quick actions state
const quickActions = ref([])

// Thinking panel state
const thoughts = ref([])
const showThoughts = ref(false)

// Creation panel state
const showCreationPanel = ref(false)
const creationPlan = ref(null)

// The raw blueprint text extracted from the latest blueprint assistant message
const blueprintText = computed(() => {
  for (let i = messages.value.length - 1; i >= 0; i--) {
    const m = messages.value[i]
    if (m.role === 'assistant' && (m.content || '').includes(BLUEPRINT_MARKER)) {
      return m.content
    }
  }
  return ''
})

// History sent to the LLM excludes system action-result messages
const chatHistory = computed(() => messages.value.filter(m => m.role !== 'system'))

async function scrollBottom() {
  await nextTick()
  if (messagesEl.value) {
    messagesEl.value.scrollTop = messagesEl.value.scrollHeight
  }
}

// Handle quick action button click
async function handleQuickAction(value) {
  if (!value || streaming.value) return
  // Clear previous quick actions
  quickActions.value = []
  // Send the value as user input
  input.value = value
  await send()
}

// ---------------------------------------------------------------------------
// Send
// ---------------------------------------------------------------------------

async function send() {
  const text = input.value.trim()
  if (!text || streaming.value) return

  error.value = ''
  // Clear quick actions when user sends a message
  quickActions.value = []
  messages.value.push({ role: 'user', content: text })
  input.value = ''
  await scrollBottom()

  // If the user just confirmed the blueprint, switch to the explicit
  // file-creation panel and do not call /api/chat/creator again.  The chat
  // endpoint still supports a legacy Phase 3 auto-execution path that validates
  // artifacts immediately; here the user should stay in control and validation
  // must wait until they click "开始创建".
  if (isCreationConfirmation(text) && hasBlueprintInHistory()) {
    streaming.value = true
    try {
      const plan = await analyzeBlueprintPlan(chatHistory.value)
      creationPlan.value = plan
      showCreationPanel.value = true
      messages.value.push({
        role: 'system',
        action: 'creator_panel',
        name: plan.skill_name,
        success: true,
        message: '已整理文件清单，准备生成脚本',
      })
      await scrollBottom()
    } catch (err) {
      error.value = `蓝图解析失败：${err.message}，请重试`
    } finally {
      streaming.value = false
    }
    return
  }

  streaming.value = true
  streamBuffer.value = ''

  try {
    for await (const chunk of streamChat('/api/chat/creator', { messages: chatHistory.value })) {
      if (typeof chunk === 'string') {
        streamBuffer.value += chunk
        await scrollBottom()
      } else if (chunk.type === 'status') {
        currentStatus.value = chunk.data
        await scrollBottom()
      } else if (chunk.type === 'thought') {
        thoughts.value.push(chunk.data)
        if (!showThoughts.value) showThoughts.value = true
        await scrollBottom()
      } else if (chunk.type === 'action_result') {
        const r = chunk.data
        messages.value.push({
          role: 'system',
          action: r.action,
          name: r.name,
          success: r.success,
          message: r.message,
          path: r.path,
        })
        await scrollBottom()
      } else if (chunk.type === 'quick_actions') {
        // Show quick action buttons
        quickActions.value = chunk.data.actions || []
        await scrollBottom()
      }
    }
    if (streamBuffer.value) {
      messages.value.push({ role: 'assistant', content: streamBuffer.value })
      streamBuffer.value = ''
    }
  } catch (e) {
    error.value = e.message
  } finally {
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
}

// ---------------------------------------------------------------------------
// 自动启动对话
// ---------------------------------------------------------------------------

async function autoStartConversation() {
  if (messages.value.length > 0 || streaming.value) return
  
  error.value = ''
  quickActions.value = []
  streaming.value = true
  streamBuffer.value = ''

  try {
    for await (const chunk of streamChat('/api/chat/creator', { messages: [] })) {
      if (typeof chunk === 'string') {
        streamBuffer.value += chunk
        await scrollBottom()
      } else if (chunk.type === 'status') {
        currentStatus.value = chunk.data
        await scrollBottom()
      } else if (chunk.type === 'thought') {
        thoughts.value.push(chunk.data)
        if (!showThoughts.value) showThoughts.value = true
        await scrollBottom()
      } else if (chunk.type === 'action_result') {
        const r = chunk.data
        messages.value.push({
          role: 'system',
          action: r.action,
          name: r.name,
          success: r.success,
          message: r.message,
          path: r.path,
        })
        await scrollBottom()
      } else if (chunk.type === 'quick_actions') {
        quickActions.value = chunk.data.actions || []
        await scrollBottom()
      }
    }
    if (streamBuffer.value) {
      messages.value.push({ role: 'assistant', content: streamBuffer.value })
      streamBuffer.value = ''
    }
  } catch (e) {
    error.value = e.message
  } finally {
    streaming.value = false
    await scrollBottom()
  }
}

// 组件挂载时自动启动对话
onMounted(() => {
  if (messages.value.length === 0) {
    autoStartConversation()
  }
})
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
</style>
