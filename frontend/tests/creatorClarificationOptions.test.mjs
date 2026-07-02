import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { buildClarificationQuickActions, extractClarificationQuestionOptions } from '../src/composables/useCreator.js'

describe('extractClarificationQuestionOptions', () => {
  it('parses options containing PDF/DOCX/TXT, JSON, and Markdown without dropping letters', () => {
    const options = extractClarificationQuestionOptions(
      '需要支持哪些输入文件类型和输出格式？A. 支持 PDF/DOCX/TXT（推荐） B. 严格 JSON C. JSON + 可读 Markdown D. 只要可读文本'
    )

    assert.deepEqual(options.map((option) => option.text), [
      'A. 支持 PDF/DOCX/TXT（推荐）',
      'B. 严格 JSON',
      'C. JSON + 可读 Markdown',
      'D. 只要可读文本',
    ])
    assert.match(options[0].value, /问题：需要支持哪些输入文件类型和输出格式？/)
    assert.match(options[0].value, /选择：A\. 支持 PDF\/DOCX\/TXT（推荐）/)
    assert.match(options[2].value, /Markdown/)
  })

  it('builds quick actions for only one displayed question and includes question context', () => {
    const actions = buildClarificationQuickActions([
      '输出格式希望是哪种？A. 严格 JSON B. JSON + 可读 Markdown（推荐） C. 只要可读文本',
    ])

    assert.equal(actions.length, 3)
    assert.match(actions[1].value, /^问题：输出格式希望是哪种？/)
    assert.match(actions[1].value, /选择：B\. JSON \+ 可读 Markdown（推荐）/)
  })

  it('does not assign prepare actions to ordinary business clarification options', () => {
    const actions = extractClarificationQuestionOptions(
      '输入来源希望支持哪种？A. 只支持粘贴文本 B. 只支持上传文件 C. 两者都支持（推荐）',
      { prepareStage: 'business_clarification' }
    )

    assert.deepEqual(actions.map((action) => action.prepareAction), ['none', 'none', 'none'])
    assert.deepEqual(actions.map((action) => action.waitForInput), [false, false, false])
  })

  it('assigns prepare actions only for creation point supplement gates', () => {
    const actions = extractClarificationQuestionOptions(
      '以上创建要点是否还需要补充？A. 没有，按这些要点继续 B. 有，我补充说明',
      { prepareStage: 'creation_points_confirmation' }
    )

    assert.equal(actions[0].prepareAction, 'confirm')
    assert.equal(actions[0].waitForInput, false)
    assert.equal(actions[1].prepareAction, 'request_supplement')
    assert.equal(actions[1].waitForInput, true)
  })

  it('uses supplement confirmation stage for post-supplement gates', () => {
    const actions = buildClarificationQuickActions([
      '已根据补充内容更新创建要点。是否按这些要点继续？A. 没有其他补充，按这些要点继续 B. 继续补充说明',
    ], { prepareStage: 'supplement_confirmation' })

    assert.equal(actions[0].prepareAction, 'confirm')
    assert.equal(actions[1].prepareAction, 'request_supplement')
  })
})
