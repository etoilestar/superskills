/**
 * useCreator — composable for the frontend-driven Skill creation flow (plan C).
 *
 * All API calls are routed through /api/creator/* which is handled by
 * backend/routers/creator.py.
 */



function isSupplementGatePrepareStage(prepareStage) {
  return prepareStage === 'creation_points_confirmation' || prepareStage === 'supplement_confirmation'
}

function inferPrepareActionFromOptionLabel(label, { isSupplementGate = false, optionIndex = -1 } = {}) {
  if (!isSupplementGate) return 'none'

  const text = String(label || '')
  if (/补充|我补充|继续补充/.test(text) && !/没有.*补充|无.*补充|暂时没有/.test(text)) {
    return 'request_supplement'
  }
  if (optionIndex === 0) {
    return 'confirm'
  }
  return 'none'
}

export function extractClarificationQuestionOptions(question, { prepareStage = '' } = {}) {
  const text = String(question || '')
  const isSupplementGate = isSupplementGatePrepareStage(prepareStage)
  const optionPattern = /(?:^|[\s？?])([A-D])[\.\)、]\s*([\s\S]*?)(?=(?:\s+[A-D][\.\)、]\s*)|$)/g
  return [...text.matchAll(optionPattern)]
    .map((match, index) => {
      const label = `${match[1]}. ${String(match[2] || '').trim()}`.trim()
      if (label.length <= 3) return null
      const prepareAction = inferPrepareActionFromOptionLabel(label, { isSupplementGate, optionIndex: index })
      return {
        text: label,
        value: `问题：${text}\n选择：${label}`,
        question: text,
        waitForInput: prepareAction === 'request_supplement',
        prepareAction,
      }
    })
    .filter(Boolean)
}

export function buildClarificationQuickActions(questions, { prepareStage = '' } = {}) {
  return (questions || [])
    .flatMap((question) => extractClarificationQuestionOptions(question, { prepareStage }))
    .slice(0, 8)
}

function blueprintBodyOnly(text) {
  const raw = String(text || '').trim()
  if (!raw) return ''
  const marker = raw.search(/(^|\n)\s*#{0,2}\s*📋\s*Skill\s+架构蓝图/)
  const fromMarker = marker >= 0 ? raw.slice(marker).trim() : raw
  const stop = fromMarker.search(/(^|\n)\s*(AskUserQuestion|确认问题|用户确认|请选择|选项|按钮状态|创建进度|文件生成进度)\b|(^|\n)\s*```text\s*$/i)
  return (stop >= 0 ? fromMarker.slice(0, stop) : fromMarker).trim()
}

function assertActionSuccess(payload, fallbackMessage) {
  if (!payload || payload.success !== true) {
    throw new Error(payload?.message || fallbackMessage)
  }
  return payload
}


/**
 * Prepare an internal blueprint and creation plan from a user request.
 * This is the new Creator front-half entrypoint; analyzeBlueprintPlan remains
 * available for legacy/debug flows.
 *
 * @param {object} payload
 * @returns {Promise<object>}
 */
export async function prepareCreationPlan(payload) {
  const resp = await fetch('/api/creator/prepare-plan', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw new Error(err.detail || '创建计划准备失败')
  }
  return resp.json()
}

/**
 * Analyze the conversation blueprint and extract a file-creation plan.
 * This is a pure rule-based call — no LLM is involved.
 *
 * @param {Array<{role:string, content:string}>} messages - full conversation history
 * @param {string|null} [model] - optional model name (ignored by the endpoint but kept for symmetry)
 * @returns {Promise<{skill_name:string, files:Array, warnings:string[]}>}
 */
export async function analyzeBlueprintPlan(messages, model = null) {
  const resp = await fetch('/api/creator/analyze-blueprint', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages, model, strict: true }),
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw new Error(err.detail || '蓝图解析请求失败')
  }
  return resp.json()
}


/**
 * Initialise a new Skill directory structure on the backend.
 *
 * @param {string} skillName
 * @returns {Promise<{success:boolean, path:string|null, message:string}>}
 */
export async function initSkill(skillName) {
  const resp = await fetch('/api/creator/init-skill', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ skill_name: skillName }),
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw new Error(err.detail || '目录初始化失败')
  }
  return resp.json()
}

/**
 * Stream the generated content for a single Skill file.
 *
 * Yields:
 *   - string chunks while the model is generating
 *   - { done: true } when generation is complete
 *   - { validation: object } when backend is repairing generated output
 *   - { error: string } on failure
 *
 * @param {{
 *   skillName: string,
 *   filePath: string,
 *   purpose: string,
 *   blueprintText: string,
 *   conversationHistory: Array,
 *   model?: string|null,
 *   role?: string|null,
 *   skillPlanEntry?: object|null,
 *   requirementGraph?: object|null,
 *   workflowAllocationSummary?: string,
 *   finalOutputs?: Array
 * }} params
 * @yields {string | {done:true} | {validation:object} | {error:string}}
 */
export async function* generateFileStream({
  skillName,
  filePath,
  purpose,
  blueprintText,
  conversationHistory,
  model = null,
  role = null,
  skillPlanEntry = null,
  requirementGraph = null,
  workflowAllocationSummary = '',
  finalOutputs = [],
}) {
  const resp = await fetch('/api/creator/generate-file', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      skill_name: skillName,
      file_path: filePath,
      purpose,
      blueprint_text: blueprintBodyOnly(blueprintText),
      conversation_history: conversationHistory,
      model,
      role,
      skill_plan_entry: skillPlanEntry,
      requirement_graph: requirementGraph,
      workflow_allocation_summary: workflowAllocationSummary,
      final_outputs: finalOutputs,
    }),
  })

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw new Error(err.detail || '文件内容生成请求失败')
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() // keep incomplete last line

    for (const line of lines) {
      if (!line.startsWith('data: ')) continue
      const raw = line.slice(6).trim()
      if (raw === '[DONE]') return
      try {
        const parsed = JSON.parse(raw)
        if (parsed.error) {
          yield {
            error: parsed.error,
            errorType: parsed.error_type,
            editable: parsed.editable,
            disabled: parsed.disabled,
            recoverable: parsed.recoverable,
            content: parsed.content || parsed.draft_content || '',
            validationStatus: parsed.validation_status || parsed.status,
            raw: parsed,
          }
          return
        }
        if (parsed.done) {
          yield { done: true }
          return
        }
        if (parsed.validation) {
          yield { validation: parsed.validation }
          continue
        }
        if (parsed.runtime_spec) {
          yield { runtimeSpec: parsed.runtime_spec }
        }
        if (typeof parsed.content === 'string') {
          yield parsed.content
        }
      } catch {
        // skip unparseable lines
      }
    }
  }
}


/**
 * Write the final file content to disk.
 * The backend automatically strips any spurious code-fence wrapping.
 *
 * @param {string} skillName
 * @param {string} filePath  - e.g. "SKILL.md" or "scripts/main.py"
 * @param {string} content
 * @param {string|null} [role]
 * @param {object|null} [skillPlanEntry]
 * @returns {Promise<{success:boolean, path:string|null, bytes:number, message:string}>}
 */
export async function writeFile(skillName, filePath, content, role = null, skillPlanEntry = null) {
  const resp = await fetch('/api/creator/write-file', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      skill_name: skillName,
      file_path: filePath,
      content,
      role,
      skill_plan_entry: skillPlanEntry,
    }),
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw new Error(err.detail || '文件写入失败')
  }
  return resp.json()
}

export async function uploadAsset({ skillName, filePath, file }) {
  const form = new FormData()
  form.append('skill_name', skillName)
  form.append('file_path', filePath)
  form.append('file', file)

  const res = await fetch('/api/creator/upload-asset', {
    method: 'POST',
    body: form,
  })
  const data = await res.json()
  if (!res.ok || !data.success) throw new Error(data.detail || data.message || '上传失败')
  return data
}

export async function uploadAssetAPI({ skillName, filePath, file }) {
  const form = new FormData()
  form.append('skill_name', skillName)
  form.append('file_path', filePath)
  form.append('file', file)

  const res = await fetch('/api/creator/upload-asset', { method: 'POST', body: form })
  const data = await res.json()
  if (!res.ok || !data.success) throw new Error(data.detail || data.message || '上传失败')
  return data
}

/**
 * Validate the SKILL.md of a Skill package.
 *
 * @param {string} skillName
 * @returns {Promise<{success:boolean, path:string|null, message:string}>}
 */
export async function validateSkill(
  skillName,
  {
    model = null,
    autoRepair = true,
    maxE2ERepairAttempts = 10,
  } = {}
) {
  const resp = await fetch('/api/creator/validate-skill', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      skill_name: skillName,
      model,
      auto_repair: autoRepair,
      max_e2e_repair_attempts: maxE2ERepairAttempts,
    }),
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw new Error(err.detail || '校验请求失败')
  }
  const payload = await resp.json()
  return payload
}

/**
 * Package a Skill directory into a distributable archive.
 *
 * @param {string} skillName
 * @returns {Promise<{success:boolean, path:string|null, message:string}>}
 */
export async function packageSkill(
  skillName,
  {
    model = null,
    validateBeforePackage = true,
  } = {}
) {
  const resp = await fetch('/api/creator/package-skill', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      skill_name: skillName,
      model,
      validate_before_package: validateBeforePackage,
    }),
  })

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw new Error(err.detail || '打包请求失败')
  }

  const payload = await resp.json()
  return assertActionSuccess(payload, '打包失败')
}

export async function listCreatorTools() {
  const resp = await fetch('/api/creator/tools')
  if (!resp.ok) throw new Error('工具列表加载失败')
  return resp.json()
}

async function postCreatorTool(path, payload) {
  const resp = await fetch(`/api/creator/tools/${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) throw new Error(data.detail?.message || data.detail || data.message || '工具请求失败')
  return data
}

export function draftCreatorTool(payload) {
  return postCreatorTool('draft', payload)
}

export function authorCreatorTool(payload) {
  return postCreatorTool('author', payload)
}


export async function* authorCreatorToolStream(payload, signal) {
  const resp = await fetch('/api/creator/tools/author/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal,
  })
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}))
    throw new Error(data.detail?.message || data.detail || data.message || '工具流式请求失败')
  }
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const chunks = buffer.split('\n\n')
    buffer = chunks.pop() || ''
    for (const chunk of chunks) {
      const line = chunk.split('\n').find(item => item.startsWith('data:'))
      if (!line) continue
      yield JSON.parse(line.slice(5).trim())
    }
  }
}

export async function saveCreatorToolConfig(payload) {
  const resp = await fetch('/api/creator/tool-config/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) throw new Error(data.detail?.message || data.detail || data.message || '配置保存失败')
  return data
}

export async function getCreatorToolConfigStatus(sessionId = 'default') {
  const resp = await fetch(`/api/creator/tool-config/status?session_id=${encodeURIComponent(sessionId)}`)
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) throw new Error(data.detail?.message || data.detail || data.message || '配置状态加载失败')
  return data
}

export function liveTestCreatorTool(payload) {
  return postCreatorTool('author', { ...payload, action: 'live_test' })
}

export function generateCreatorToolCode(payload) {
  return postCreatorTool('generate-code', payload)
}

export function validateCreatorTool(payload) {
  return postCreatorTool('validate', payload)
}

export function registerCreatorTool(payload) {
  return postCreatorTool('register', payload)
}

export function enableCreatorTool(name) {
  return postCreatorTool(`${encodeURIComponent(name)}/enable`, {})
}

export function disableCreatorTool(name) {
  return postCreatorTool(`${encodeURIComponent(name)}/disable`, {})
}


export async function listCreatorToolSnippets(name) {
  const resp = await fetch(`/api/creator/tools/${encodeURIComponent(name)}/snippets`)
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) throw new Error(data.detail || data.message || 'Snippet 加载失败')
  return data
}

export function createCreatorToolSnippet(name, snippet) {
  return postCreatorTool(`${encodeURIComponent(name)}/snippets`, { snippet })
}

export async function updateCreatorToolSnippet(name, snippetId, snippet) {
  const resp = await fetch(`/api/creator/tools/${encodeURIComponent(name)}/snippets/${encodeURIComponent(snippetId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ snippet }),
  })
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) throw new Error(data.detail?.message || data.detail || data.message || 'Snippet 更新失败')
  return data
}

export async function deleteCreatorToolSnippet(name, snippetId) {
  const resp = await fetch(`/api/creator/tools/${encodeURIComponent(name)}/snippets/${encodeURIComponent(snippetId)}`, { method: 'DELETE' })
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) throw new Error(data.detail || data.message || 'Snippet 删除失败')
  return data
}

export function resolveCreatorToolSnippets(payload) {
  return postCreatorTool('resolve-snippets', payload)
}

export function testCreatorToolSnippet(name, snippetId) {
  return postCreatorTool(`${encodeURIComponent(name)}/snippets/${encodeURIComponent(snippetId)}/test`, {})
}
