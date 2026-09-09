import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const creator = readFileSync(resolve(here, '../src/views/CreatorView.vue'), 'utf8')
const creation = readFileSync(resolve(here, '../src/components/SkillCreationPanel.vue'), 'utf8')

describe('Creator Skill naming step', () => {
  it('asks for the final name in the creation panel instead of the planning toolbar', () => {
    assert.doesNotMatch(creator, /id="creator-derived-skill-name"/)
    assert.match(creation, /生成文件前确认 Skill 名称/)
    assert.ok(creation.indexOf('class="name-confirmation"') < creation.indexOf('class="panel-actions"'))
  })

  it('blocks a derived Skill name that matches its source before file generation', () => {
    assert.match(creation, /raw === props\.sourceSkillName/)
    assert.match(creation, /新 Skill 名称不能与来源 Skill 相同/)
    const startCreation = creation.slice(
      creation.indexOf('async function startCreation()'),
      creation.indexOf('async function resumeCreation()'),
    )
    assert.match(startCreation, /if \(!validateName\(\)\)/)
    assert.ok(startCreation.indexOf('validateName()') < startCreation.indexOf('runCreationFromCurrentIndex()'))
  })
})
