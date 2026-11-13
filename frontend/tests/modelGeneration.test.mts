import assert from 'node:assert/strict'
import test from 'node:test'
import { defaultModelCounts, restoreModelCounts, mergeCandidates, modelGenerationState, submitModelRound,
  candidateForModel, normalizeModelCount, imageModelLabel } from '../src/modelGeneration.ts'
import type { WholeImageTask } from '../src/types.ts'

const gpt = 'gpt-image-2.5-sunburst'
const seed = 'seedream-5-pro-image-to-image'
const minimax = 'minimax-h3-image-edit'
const legacyMinimax = 'minimax-h3-image'

test('MiniMax aliases share their label and candidates without mixing other models', () => {
  const tasks = [candidate('legacy-ready', legacyMinimax), candidate('new-queued', minimax, 'new', 'queued'), candidate('gpt')]
  assert.equal(imageModelLabel(legacyMinimax), 'MiniMax H3')
  assert.equal(candidateForModel(tasks, minimax)?.id, 'legacy-ready')
  assert.equal(candidateForModel(tasks, minimax, 'new-queued')?.id, 'new-queued')
  assert.equal(candidateForModel(tasks, legacyMinimax, 'new-queued')?.id, 'new-queued')
  assert.equal(candidateForModel(tasks, minimax, 'gpt')?.id, 'legacy-ready')
})

test('legacy MiniMax counts and running tasks belong to the canonical model', () => {
  const old = candidate('legacy-running', legacyMinimax, 'old', 'running', 2)
  assert.equal(restoreModelCounts([old])[minimax], 2)
  assert.equal(restoreModelCounts([old])[legacyMinimax], undefined)
  const tasks = [old, candidate('new-ready', minimax, 'new', 'succeeded', 1)]
  const expected = { running: true, completed: 1, total: 1, hasHistory: true }
  assert.deepEqual(modelGenerationState(tasks, minimax), expected)
  assert.deepEqual(modelGenerationState(tasks, legacyMinimax), expected)
})

test('new MiniMax generation requests always use the canonical model id', async () => {
  const requested: string[] = []
  await submitModelRound({ prompt: 'test' }, legacyMinimax, 1, 'workspace', 'round', async payload => {
    requested.push(payload.image_model!)
    return candidate('new', payload.image_model)
  })
  assert.deepEqual(requested, [minimax])
})
test('numeric counts stay whole and within zero to three', () => {
  assert.deepEqual(['', '0', '2', '9', '-2', '1.5', 'invalid'].map(normalizeModelCount), [0, 0, 2, 3, 0, 1, 0])
})

test('model switching restores its selected candidate and never borrows another model image', () => {
  const tasks = [candidate('gpt'), candidate('seed-queued', seed, 'seed', 'queued'), candidate('seed-ready', seed)]
  assert.equal(candidateForModel(tasks, seed)?.id, 'seed-ready')
  assert.equal(candidateForModel(tasks, seed, 'seed-queued')?.id, 'seed-queued')
  assert.equal(candidateForModel(tasks, seed, 'gpt')?.id, 'seed-ready')
  assert.equal(candidateForModel(tasks, 'minimax-h3-image'), null)
})
function candidate(id: string, model = gpt, batch = 'round-1', status = 'succeeded', count = 2, updated = '2026-11-11T01:00:00Z') {
  return { id, status, input: { image_model: model, candidate_batch_id: batch, candidate_count: count },
    created_at: updated, updated_at: updated } as WholeImageTask
}

test('defaults and restored latest rounds respect the three-per-model limit', () => {
  const counts = defaultModelCounts()
  assert.equal(counts[gpt], 0)
  assert.equal(counts[seed], 0)
  assert.ok(Object.values(counts).every(count => count === 0))
  const restored = restoreModelCounts([
    candidate('old', gpt, 'old', 'succeeded', 6),
    candidate('new', gpt, 'new', 'succeeded', 1, '2026-11-11T02:00:00Z'),
    candidate('seed', seed, 'seed', 'failed', 6),
  ])
  assert.equal(restored[gpt], 1)
  assert.equal(restored[seed], 3)
  const legacy = candidate('legacy')
  delete legacy.input.image_model
  assert.equal(restoreModelCounts([legacy])[gpt], 2)
})

test('appending or polling never removes other models or regresses newer task state', () => {
  const original = [candidate('gpt'), candidate('seed', seed)]
  const added = candidate('gpt-2', gpt, 'round-2')
  assert.deepEqual(mergeCandidates(original, [added]).map(t => t.id), ['gpt', 'seed', 'gpt-2'])
  const latest = candidate('gpt', gpt, 'round-1', 'succeeded', 2, '2026-11-11T03:00:00Z')
  assert.equal(mergeCandidates([latest], [candidate('gpt', gpt, 'round-1', 'running')])[0].status, 'succeeded')
})

test('generation locks and progress belong only to the running model and latest round', () => {
  const tasks = [candidate('old'), candidate('one', gpt, 'round-2'),
    candidate('two', gpt, 'round-2', 'running'), candidate('seed', seed)]
  assert.deepEqual(modelGenerationState(tasks, gpt), { running: true, completed: 1, total: 2, hasHistory: true })
  assert.equal(modelGenerationState(tasks, seed).running, false)
})

test('independent rounds keep successful requests when another request fails', async () => {
  const requests: any[] = []
  const create = async (payload: any) => {
    requests.push(payload)
    if (payload.candidate_index === 1) throw new Error('submission failed')
    return candidate(`${payload.image_model}-${payload.candidate_index}`, payload.image_model)
  }
  const [a, b] = await Promise.all([
    submitModelRound({ prompt: 'first' }, gpt, 3, 'workspace', 'gpt-round', create),
    submitModelRound({ prompt: 'second' }, seed, 1, 'workspace', 'seed-round', create),
  ])
  assert.equal(a.tasks.length, 2)
  assert.equal(a.errors.length, 1)
  assert.equal(b.tasks.length, 1)
  assert.deepEqual(requests.filter(p => p.image_model === gpt).map(p => p.candidate_index), [0, 1, 2])
  assert.ok(requests.every(p => p.generation_workspace_id === 'workspace'))
  assert.equal(requests.find(p => p.image_model === seed).candidate_count, 1)
  await assert.rejects(submitModelRound({ prompt: 'invalid' }, gpt, 4, 'workspace', 'bad', create))
})
