/**
 * Skills API helpers.
 */

export async function fetchSkills(mode = 'manage', { includeHidden = false } = {}) {
  const params = new URLSearchParams({ mode })
  if (includeHidden) params.set('include_hidden', 'true')
  const res = await fetch(`/api/skills?${params.toString()}`)
  if (!res.ok) throw new Error(`Failed to fetch skills: ${res.statusText}`)
  return res.json()
}

export async function fetchSkill(name, mode = 'manage') {
  const params = new URLSearchParams({ mode })
  const res = await fetch(`/api/skills/${encodeURIComponent(name)}?${params.toString()}`)
  if (!res.ok) throw new Error(`Skill not found: ${name}`)
  return res.json()
}

export async function saveSkill(name, content) {
  const res = await fetch('/api/skills', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, content }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || 'Save failed')
  }
  return res.json()
}

export async function deleteSkill(name) {
  const res = await fetch(`/api/skills/${encodeURIComponent(name)}`, { method: 'DELETE' })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || 'Delete failed')
  }
  return res.json()
}

export async function fetchSkillAssets(skillName) {
  const res = await fetch(`/api/skills/${encodeURIComponent(skillName)}/assets`)
  if (!res.ok) throw new Error(`Failed to fetch assets: ${res.statusText}`)
  return res.json()
}

export async function uploadAsset(skillName, folder, file) {
  const form = new FormData()
  form.append('file', file)
  form.append('folder', folder)
  const res = await fetch(`/api/skills/${encodeURIComponent(skillName)}/assets`, {
    method: 'POST',
    body: form,
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || 'Upload failed')
  }
  return res.json()
}

export async function fetchAssetContent(skillName, folder, filename) {
  const res = await fetch(
    `/api/skills/${encodeURIComponent(skillName)}/assets/${encodeURIComponent(folder)}/${encodeURIComponent(filename)}`,
  )
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    const e = new Error(err.detail || 'Failed to load asset')
    e.status = res.status
    throw e
  }
  const data = await res.json()
  return data.content
}

export async function saveAssetContent(skillName, folder, filename, content) {
  const res = await fetch(
    `/api/skills/${encodeURIComponent(skillName)}/assets/${encodeURIComponent(folder)}/${encodeURIComponent(filename)}`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    },
  )
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || 'Save failed')
  }
  return res.json()
}

export async function deleteAsset(skillName, folder, filename) {
  const res = await fetch(
    `/api/skills/${encodeURIComponent(skillName)}/assets/${encodeURIComponent(folder)}/${encodeURIComponent(filename)}`,
    { method: 'DELETE' },
  )
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || 'Delete failed')
  }
  return res.json()
}

export async function importSkillZip(file, overwrite = false) {
  const form = new FormData()
  form.append('file', file)
  form.append('overwrite', overwrite ? 'true' : 'false')
  const res = await fetch('/api/skills/import', {
    method: 'POST',
    body: form,
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    const detail = err.detail
    const message = (typeof detail === 'object' ? detail.message : detail) || 'Import failed'
    const e = new Error(message)
    e.status = res.status
    e.skillName = typeof detail === 'object' ? (detail.skill_name || '') : ''
    throw e
  }
  return res.json()
}

export async function fetchSkillEvents(name) {
  const res = await fetch(`/api/skills/${encodeURIComponent(name)}/events`)
  if (!res.ok) throw new Error('Failed to fetch events')
  return res.json()
}

export async function fetchSkillVersions(name) {
  const res = await fetch(`/api/skills/${encodeURIComponent(name)}/versions`)
  if (!res.ok) throw new Error('Failed to fetch versions')
  return res.json()
}

export async function updateSkillStatus(name, action, reason = '') {
  const res = await fetch(`/api/skills/${encodeURIComponent(name)}/status`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, reason }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || 'Status update failed')
  }
  return res.json()
}

export async function upgradeSkillZip(name, file) {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch(`/api/skills/${encodeURIComponent(name)}/upgrade`, {
    method: 'POST',
    body: form,
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || 'Upgrade failed')
  }
  return res.json()
}

export async function rollbackSkillVersion(name, version) {
  const res = await fetch(`/api/skills/${encodeURIComponent(name)}/rollback`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ version }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || 'Rollback failed')
  }
  return res.json()
}

export async function fetchAllowlist() {
  const res = await fetch('/api/skills/governance/allowlist')
  if (!res.ok) throw new Error('Failed to fetch allowlist')
  return res.json()
}

export async function saveAllowlist(payload) {
  const res = await fetch('/api/skills/governance/allowlist', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || 'Failed to save allowlist')
  }
  return res.json()
}

export async function fetchLlmHealth(timeoutMs = 2500) {
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), timeoutMs)
  try {
    const res = await fetch('/api/health/llm', { signal: controller.signal })
    if (!res.ok) throw new Error('Health check failed')
    return res.json()
  } finally {
    clearTimeout(timeout)
  }
}
