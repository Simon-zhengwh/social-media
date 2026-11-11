import assert from 'node:assert/strict'
import test from 'node:test'
import {
  apiFetch,
  applyOriginalReplacement,
  createOriginalReplacement,
  listOriginalReplacements,
  listPlatforms,
  listWholeImageWorkspace,
  deleteWholeImageCandidate,
  createWholeImageEdit,
  merchantPath,
  refreshOriginalReplacement,
} from '../src/api.ts'
import type { WholeImageTask } from '../src/types.ts'

function mockBrowser(search = '?merchant_id=merchant%2Fa') {
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { location: { search } } })
  const requests: { path: string; init?: RequestInit }[] = []
  globalThis.fetch = async (path, init) => {
    requests.push({ path: String(path), init })
    return new Response(JSON.stringify({}), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }
  return requests
}

test('original replacement requests retain merchant scope and write protection', async () => {
  const requests = mockBrowser()
  await listOriginalReplacements('poster/a')
  await createOriginalReplacement('poster/a', 'version', 'output', [{ target: '背景', asset_id: 'asset' }])
  await refreshOriginalReplacement('poster/a', 'replacement/a')
  await applyOriginalReplacement({ id: 'poster/a', current_version_id: 'version', current_output_id: 'output' } as WholeImageTask, 'replacement/a')
  assert.deepEqual(requests.map(request => request.path), [
    '/api/merchants/merchant%2Fa/whole-image/tasks/poster%2Fa/original-replacements',
    '/api/merchants/merchant%2Fa/whole-image/tasks/poster%2Fa/original-replacements',
    '/api/merchants/merchant%2Fa/whole-image/tasks/poster%2Fa/original-replacements/replacement%2Fa/refresh-result',
    '/api/merchants/merchant%2Fa/whole-image/tasks/poster%2Fa/original-replacements/replacement%2Fa/apply',
  ])
  for (const { init } of requests) assert.equal(init?.credentials, 'same-origin')
  for (const { init } of requests.slice(1)) assert.equal(new Headers(init?.headers).get('X-Media-Request'), '1')
})

test('missing merchant context prevents replacement requests before network access', async () => {
  const requests = mockBrowser('')
  await assert.rejects(listOriginalReplacements('poster'), /缺少商家上下文/)
  assert.equal(requests.length, 0)
})

test('public platform catalog remains accessible without merchant context', async () => {
  const requests = mockBrowser('')
  await listPlatforms()
  assert.equal(requests[0].path, '/api/platforms')
})

test('workspace queries, candidate deletion and edits preserve merchant and candidate identity', async () => {
  const requests = mockBrowser()
  await listWholeImageWorkspace('workspace/a')
  await deleteWholeImageCandidate('candidate/a')
  await createWholeImageEdit('seedream/a', '只改标题', 'version/a', 'output/a')
  assert.deepEqual(requests.map(request => request.path), [
    '/api/merchants/merchant%2Fa/whole-image/workspaces/workspace%2Fa/tasks',
    '/api/merchants/merchant%2Fa/whole-image/tasks/candidate%2Fa/candidate',
    '/api/merchants/merchant%2Fa/whole-image/tasks/seedream%2Fa/edits',
  ])
  for (const { init } of requests.slice(1)) assert.equal(new Headers(init?.headers).get('X-Media-Request'), '1')
  const body = JSON.parse(String(requests[2].init?.body))
  assert.equal(body.source_version_id, 'version/a')
  assert.equal(body.source_output_id, 'output/a')
  assert.equal(body.image_model, undefined) // The server owns this candidate's immutable model.
})

test('agent benchmark writes use the same merchant authentication and CSRF contract', async () => {
  const requests = mockBrowser('?merchant_id=merchant-b')
  await apiFetch('/api/agent-tests/batches/batch/visibility', {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ hidden: true }),
  })
  assert.equal(requests[0].path, '/api/merchants/merchant-b/agent-tests/batches/batch/visibility')
  assert.equal(requests[0].init?.credentials, 'same-origin')
  assert.equal(new Headers(requests[0].init?.headers).get('X-Media-Request'), '1')
})

test('embedded previews and image fallbacks produce scoped browser URLs without duplicate prefixes', () => {
  mockBrowser('?merchant_id=merchant-b')
  assert.equal(merchantPath('/api/agent-tests/runs/run/artifacts/0/preview'), '/api/merchants/merchant-b/agent-tests/runs/run/artifacts/0/preview')
  assert.equal(merchantPath('/api/assets/asset/file'), '/api/merchants/merchant-b/assets/asset/file')
  assert.equal(merchantPath('/api/merchants/merchant-b/whole-image/artifacts/artifact/preview'), '/api/merchants/merchant-b/whole-image/artifacts/artifact/preview')
})
