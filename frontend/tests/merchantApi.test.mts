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
  restoreOriginalReplacementArtifacts,
  listMerchants,
} from '../src/api.ts'
import type { WholeImageTask } from '../src/types.ts'
import * as finalizationApi from '../src/api.ts'

test('finalization APIs retain frozen source, revision, merchant scope and write protection', async () => {
  assert.equal(typeof finalizationApi.getPosterFinalization, 'function')
  const requests = mockBrowser()
  await finalizationApi.getPosterFinalization('poster/a', 'version/a', 'output/a')
  await finalizationApi.savePosterFinalization('poster/a', 'version/a', 'output/a', 'revision')
  await finalizationApi.revokePosterFinalization('poster/a', 'revision/a')
  await finalizationApi.listAssetUsages('am:photo/a')
  assert.deepEqual(requests.map(request => request.path), [
    '/api/merchants/merchant%2Fa/whole-image/tasks/poster%2Fa/finalization?version_id=version%2Fa&output_id=output%2Fa',
    '/api/merchants/merchant%2Fa/whole-image/tasks/poster%2Fa/finalization',
    '/api/merchants/merchant%2Fa/whole-image/tasks/poster%2Fa/finalization?expected_revision=revision%2Fa',
    '/api/merchants/merchant%2Fa/assets/am%3Aphoto%2Fa/usages',
  ])
  assert.deepEqual(JSON.parse(String(requests[1].init?.body)), {
    version_id: 'version/a', output_id: 'output/a', expected_revision: 'revision',
  })
  assert.equal(requests[1].init?.method, 'PUT')
  assert.equal(requests[2].init?.method, 'DELETE')
  for (const index of [1, 2]) assert.equal(new Headers(requests[index].init?.headers).get('X-Media-Request'), '1')
})

function mockBrowser(search = '?merchant_id=merchant%2Fa') {
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { location: { search } } })
  const requests: { path: string; init?: RequestInit }[] = []
  globalThis.fetch = async (path, init) => {
    requests.push({ path: String(path), init })
    return new Response(JSON.stringify({}), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }
  return requests
}

test('merchant picker reads the imported catalog with the active AM context', async () => {
  const requests = mockBrowser('?merchant_id=context%2Fa')
  await listMerchants('  饮品  ')

  assert.equal(requests.length, 1)
  assert.equal(requests[0].path, '/api/merchants?context_merchant_id=context%2Fa&keyword=%E9%A5%AE%E5%93%81')
  assert.equal(requests[0].init?.credentials, 'same-origin')
})

test('merchant list requests require an active merchant context', async () => {
  const requests = mockBrowser('')
  await assert.rejects(listMerchants('merchant'), /缺少商家上下文/)
  assert.equal(requests.length, 0)
})

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

test('original replacement sends the whole-element choice and defaults it off', async () => {
  const requests = mockBrowser()
  const replacements = [{ target: '', asset_id: 'asset' }]
  await createOriginalReplacement('poster', 'version', 'output', replacements)
  await createOriginalReplacement('poster', 'version', 'output', replacements, true)
  assert.equal(JSON.parse(String(requests[0].init?.body)).split_all_elements, false)
  assert.deepEqual(JSON.parse(String(requests[1].init?.body)), {
    source_version_id: 'version', source_output_id: 'output', replacements, split_all_elements: true,
  })
})

test('restoring replacement files queries the existing run within the merchant scope', async () => {
  const requests = mockBrowser()
  await restoreOriginalReplacementArtifacts('poster/a', 'replacement/a')
  assert.equal(requests[0].path, '/api/merchants/merchant%2Fa/whole-image/tasks/poster%2Fa/original-replacements/replacement%2Fa/artifacts/restore')
  assert.equal(requests[0].init?.method, 'POST')
  assert.equal(new Headers(requests[0].init?.headers).get('X-Media-Request'), '1')
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
  assert.equal(body.image_model, undefined) // Older callers keep the candidate's generation model.
})

test('edits can choose a model independently while retaining source image and reference order', async () => {
  const requests = mockBrowser()
  await createWholeImageEdit('candidate/a', '只改标题', 'version/a', 'output/a', ['ref-b', 'ref-a'], 'unified', 'seedream-5.5-image-to-image')
  assert.equal(requests[0].path, '/api/merchants/merchant%2Fa/whole-image/tasks/candidate%2Fa/edits')
  assert.deepEqual(JSON.parse(String(requests[0].init?.body)), {
    instruction: '只改标题', source_version_id: 'version/a', source_output_id: 'output/a',
    reference_asset_ids: ['ref-b', 'ref-a'], reference_mode: 'unified', image_model: 'seedream-5.5-image-to-image',
  })
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
