/**
 * useCreator — composable for the frontend-driven Skill creation flow (plan C).
 *
 * All API calls are routed through /api/creator/* which is handled by
 * backend/routers/creator.py.
 */

function assertActionSuccess(payload, fallbackMessage) {
  if (!payload || payload.success !== true) {
    throw new Error(payload?.message || fallbackMessage)
  }
  return payload
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
    body: JSON.stringify({ messages, model }),
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
 *   skillPlanEntry?: object|null
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
}) {
  const resp = await fetch('/api/creator/generate-file', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      skill_name: skillName,
      file_path: filePath,
      purpose,
      blueprint_text: blueprintText,
      conversation_history: conversationHistory,
      model,
      role,
      skill_plan_entry: skillPlanEntry,
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
          yield { error: parsed.error }
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
    maxE2ERepairAttempts = 5,
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
  return assertActionSuccess(payload, '严格端到端校验失败')
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
