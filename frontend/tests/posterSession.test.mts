import assert from 'node:assert/strict'
import { beforeEach, test } from 'node:test'
import { clearPosterSession, readPosterSession, savePosterSession, type PosterSession } from '../src/posterSession.ts'
import { imageMention, serializeImagePrompt } from '../src/imageMentions.ts'

class MemoryStorage {
  data = new Map<string, string>()
  getItem(key: string) { return this.data.get(key) ?? null }
  setItem(key: string, value: string) { this.data.set(key, value) }
  removeItem(key: string) { this.data.delete(key) }
}

let tab: MemoryStorage
function selectTab(storage: MemoryStorage) {
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: { sessionStorage: storage, get localStorage() { throw new Error('drafts must not use localStorage') } },
  })
}
beforeEach(() => { tab = new MemoryStorage(); selectTab(tab) })

function draft(): PosterSession {
  const asset = (id: string) => ({ id, original_name: id, width: 100, height: 100,
    mime_type: 'image/png', url: `/api/assets/${id}/file`, created_at: '2026-11-12' })
  return {
    kind: 'draft', workspaceId: 'workspace-1',
    form: {
      products: [asset('second'), asset('first')],
      prompt: `保留${imageMention('first')}，参考${imageMention('second')}`,
      promptOptimized: true, selectedPlatformId: 'instagram', selectedPlacementId: 'feed', selectedRatio: '4:5',
      selectedBrandId: 'brand', brandMode: 'none', selectedLogoId: '', logoChoiceTouched: true,
      styleReferences: [], storeSelection: { enabled: true, placeId: 'store-2', address: false, phone: true },
      selectedModel: 'seedream-5.5-image-to-image', modelCounts: { 'gpt-image-2.5-sunburst': 0, 'seedream-5.5-image-to-image': 3 },
    },
  }
}

test('a draft survives remount/reload with image order, bound mentions and every form setting', () => {
  const value = draft()
  assert.equal(savePosterSession('alice', 'merchant', value), true)
  const restored = readPosterSession('alice', 'merchant')
  assert.deepEqual(restored, value)
  assert.equal(restored?.kind, 'draft')
  if (restored?.kind !== 'draft') return
  assert.equal(serializeImagePrompt(restored.form.prompt, restored.form.products.map(asset => ({
    assetId: asset.id, name: asset.original_name, url: asset.url,
  }))), '保留@图片2，参考@图片1')
})

test('users, merchants and fresh tabs have independent temporary sessions', () => {
  savePosterSession('alice', 'one', draft())
  assert.equal(readPosterSession('bob', 'one'), null)
  assert.equal(readPosterSession('alice', 'two'), null)
  savePosterSession('bob', 'one', { kind: 'task', workspaceId: 'bob-workspace', taskId: 'bob-task', selectedModel: 'gpt-image-2.5-sunburst' })
  assert.deepEqual(readPosterSession('alice', 'one'), draft())
  selectTab(new MemoryStorage())
  assert.equal(readPosterSession('alice', 'one'), null)
  selectTab(tab)
  assert.deepEqual(readPosterSession('alice', 'one'), draft())
})

test('accepted generation replaces the draft with a task pointer without storing form content', () => {
  savePosterSession('alice', 'merchant', draft())
  const task = { kind: 'task', workspaceId: 'workspace-1', taskId: 'task-1', selectedModel: 'seedream-5.5-image-to-image' } as const
  assert.equal(savePosterSession('alice', 'merchant', task), true)
  assert.deepEqual(readPosterSession('alice', 'merchant'), task)
  assert.ok([...tab.data.values()].every(value => !value.includes('保留') && !value.includes('products')))
})

test('historical models can resume saved tasks without becoming available for new drafts', () => {
  const old = { kind: 'task', workspaceId: 'legacy-workspace', taskId: 'legacy-task', selectedModel: 'gemini-3.1-flash-image' } as const
  savePosterSession('alice', 'merchant', old)
  assert.deepEqual(readPosterSession('alice', 'merchant'), old)
})

test('new task clears only the active user/merchant session', () => {
  savePosterSession('alice', 'one', draft())
  savePosterSession('alice', 'two', draft())
  savePosterSession('bob', 'one', draft())
  assert.equal(clearPosterSession('alice', 'one'), true)
  assert.equal(readPosterSession('alice', 'one'), null)
  assert.deepEqual(readPosterSession('alice', 'two'), draft())
  assert.deepEqual(readPosterSession('bob', 'one'), draft())
})

test('missing identity never reads or writes a shared fallback draft', () => {
  assert.equal(savePosterSession('', 'merchant', draft()), false)
  assert.equal(savePosterSession('alice', '', draft()), false)
  assert.equal(readPosterSession('', 'merchant'), null)
  assert.equal(tab.data.size, 0)
})

test('corrupt and incompatible sessions fall back safely without poisoning the form', () => {
  savePosterSession('alice', 'merchant', draft())
  const key = [...tab.data.keys()][0]
  for (const corrupt of ['{', '[]', 'null', '{}', JSON.stringify({ ...draft(), form: {} }),
    JSON.stringify({ ...draft(), kind: 'unknown' }),
    JSON.stringify({ ...draft(), form: { ...(draft() as Extract<PosterSession, { kind: 'draft' }>).form, products: [null] } }),
    JSON.stringify({ ...draft(), form: { ...(draft() as Extract<PosterSession, { kind: 'draft' }>).form, modelCounts: { wrong: 'three' } } }),
  ]) {
    tab.data.set(key, corrupt)
    assert.equal(readPosterSession('alice', 'merchant'), null)
  }
})

test('blocked or full storage reports failure while reads and resets never throw', () => {
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { get sessionStorage() { throw new Error('blocked') } } })
  assert.equal(readPosterSession('alice', 'merchant'), null)
  assert.equal(savePosterSession('alice', 'merchant', draft()), false)
  assert.equal(clearPosterSession('alice', 'merchant'), false)
  Object.defineProperty(globalThis, 'window', { configurable: true, value: undefined })
  assert.equal(readPosterSession('alice', 'merchant'), null)
})
