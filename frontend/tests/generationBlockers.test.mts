import assert from 'node:assert/strict'
import test from 'node:test'
import { generationBlockers, presetStyleUnavailable, type GenerationRequirements } from '../src/generationBlockers.ts'

const image = { assetId: 'asset-a', name: '菜品.png', url: '/a.png' }

function requirements(overrides: Partial<GenerationRequirements> = {}): GenerationRequirements {
  return {
    promptImages: [image],
    prompt: '把@图片1放在画面中央，深绿色背景',
    brandMode: 'none',
    brandsLoading: false,
    brandsError: null,
    platformResolved: 'xiaohongshu',
    platformReady: true,
    uploading: false,
    ...overrides,
  }
}

test('a ready form reports no blocker', () => {
  assert.deepEqual(generationBlockers(requirements()), [])
})

test('an empty preset list degrades instead of blocking, but an unloaded one still waits', () => {
  assert.equal(presetStyleUnavailable('preset', { loading: false, error: null, matches: 0 }), true)
  assert.equal(presetStyleUnavailable('preset', { loading: true, error: null, matches: 0 }), false)
  assert.equal(presetStyleUnavailable('preset', { loading: false, error: '挂了', matches: 0 }), false)
  assert.equal(presetStyleUnavailable('none', { loading: false, error: null, matches: 0 }), false)

  // Degraded to none, the empty list no longer stands in the way of a poster.
  assert.deepEqual(generationBlockers(requirements({ brandMode: 'none' })), [])
  assert.deepEqual(
    generationBlockers(requirements({ brandMode: 'preset', brandsLoading: true })),
    ['正在读取品牌风格'],
  )
  assert.deepEqual(
    generationBlockers(requirements({ brandMode: 'preset', brandsError: '请求失败' })),
    ['品牌风格加载失败，请稍后重试'],
  )
})

test('blockers follow the order the operator has to solve them', () => {
  assert.deepEqual(
    generationBlockers(requirements({
      promptImages: [],
      prompt: '  ',
      brandMode: 'preset',
      brandsLoading: true,
      platformResolved: 'xiaohongshu',
      platformReady: false,
      uploading: true,
    })),
    ['请先添加参考图', '请填写设计需求', '正在读取品牌风格', '发布平台配置尚未就绪，请稍后重试', '素材上传中，请稍候'],
  )
})

test('an unreferenced image mention is reported by the prompt helpers', () => {
  assert.deepEqual(
    generationBlockers(requirements({ prompt: '把@图片2放在中央' })),
    ['引用的图片2不存在，请重新选择。'],
  )
})
