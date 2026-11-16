import assert from 'node:assert/strict'
import test from 'node:test'
import { isEditorSaveMessage, latestSaveQueue } from '../src/editableProjectProtocol.ts'

const binding = { sessionId: 'nonce-1', projectId: 'project-1', revision: 3 }
const message = { type: 'image-editor-v2:save', ...binding, project: { schema: 'image-editor-v2', assets: {}, layers: [] } }

test('save accepts only the current project session and revision', () => {
  assert.equal(isEditorSaveMessage(message, binding), true)
  assert.equal(isEditorSaveMessage({ ...message, sessionId: 'old-session' }, binding), false)
  assert.equal(isEditorSaveMessage({ ...message, projectId: 'another-project' }, binding), false)
  assert.equal(isEditorSaveMessage({ ...message, revision: 2 }, binding), false)
})

test('save rejects non-project payloads and unknown message types', () => {
  assert.equal(isEditorSaveMessage(null, binding), false)
  assert.equal(isEditorSaveMessage({ ...message, type: 'request' }, binding), false)
  assert.equal(isEditorSaveMessage({ ...message, project: { schema: 'other' } }, binding), false)
  assert.equal(isEditorSaveMessage({ ...message, revision: Number.NaN }, binding), false)
})

test('a second save while the first is pending persists the latest snapshot next', async () => {
  let release!: () => void
  const gate = new Promise<void>(resolve => { release = resolve })
  const saved: string[] = []
  const queue = latestSaveQueue(async value => {
    saved.push(value.value as string)
    if (saved.length === 1) await gate
  })
  const pending = queue({ value: 'A' })
  await queue({ value: 'B' })
  await queue({ value: 'C' })
  assert.deepEqual(saved, ['A'])
  release(); await pending
  assert.deepEqual(saved, ['A', 'C'])
})

test('a rejected save stops queued writes and allows an explicit later retry', async () => {
  let reject!: (reason: Error) => void
  const gate = new Promise<void>((_, fail) => { reject = fail })
  const calls: string[] = []
  const queue = latestSaveQueue(async value => { calls.push(value.value as string); if (calls.length === 1) await gate })
  const pending = queue({ value: 'A' }); await queue({ value: 'B' })
  reject(new Error('conflict'))
  await assert.rejects(pending, /conflict/)
  assert.deepEqual(calls, ['A'])
  await queue({ value: 'B' })
  assert.deepEqual(calls, ['A', 'B'])
})
