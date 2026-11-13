import assert from 'node:assert/strict'
import test from 'node:test'
import * as api from '../src/api.ts'

function mockBrowser(payload: unknown = { items: [], total: 0 }) {
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { location: { search: '?merchant_id=merchant%2Fa' } } })
  const requests: { path: string; init?: RequestInit }[] = []
  globalThis.fetch = async (path, init) => {
    requests.push({ path: String(path), init })
    return new Response(JSON.stringify(payload), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }
  return requests
}

test('group scan and retry preserve one operation id without starting recognition', async () => {
  assert.equal(typeof api.startMaterialGroupScan, 'function')
  const requests = mockBrowser()
  await api.startMaterialGroupScan('scheme/a', 'same-operation')
  await api.startMaterialGroupScan('scheme/a', 'same-operation')
  assert.deepEqual(requests.map(item => item.path), Array(2).fill('/api/merchants/merchant%2Fa/material-filters/schemes/scheme%2Fa/group-scans'))
  for (const request of requests) {
    assert.deepEqual(JSON.parse(String(request.init?.body)), { request_id: 'same-operation' })
    assert.equal(new Headers(request.init?.headers).get('X-Media-Request'), '1')
  }
})

test('selected recognition keeps exact file scope and only explicit retry enables force', async () => {
  assert.equal(typeof api.startMaterialRecognitionRun, 'function')
  const requests = mockBrowser()
  const selected = ['file/b', 'file/a']
  await api.startMaterialRecognitionRun('scheme', { request_id: 'first', mode: 'selected_files', representative_count: 1, file_ids: selected, force: false })
  await api.startMaterialRecognitionRun('scheme', { request_id: 'second', mode: 'selected_files', representative_count: 1, file_ids: selected, force: true })
  const bodies = requests.map(request => JSON.parse(String(request.init?.body)))
  assert.deepEqual(bodies.map(body => body.file_ids), [selected, selected])
  assert.deepEqual(bodies.map(body => body.force), [false, true])
  assert.deepEqual(bodies.map(body => body.request_id), ['first', 'second'])
  assert.ok(bodies.every(body => body.group_limit === undefined))
})

test('five-group trial uses group_limit and preserves two-representative plan', async () => {
  assert.equal(typeof api.startMaterialRecognitionRun, 'function')
  const requests = mockBrowser()
  await api.startMaterialRecognitionRun('scheme', { request_id: 'trial', mode: 'representatives', representative_count: 2, group_limit: 5, force: false })
  const body = JSON.parse(String(requests[0].init?.body))
  assert.equal(body.group_limit, 5)
  assert.equal(body.limit, undefined)
  assert.equal(body.file_ids, undefined)
  assert.equal(body.representative_count, 2)
})

test('group pages, members and explicit reference changes use protected record ids', async () => {
  assert.equal(typeof api.listMaterialGroups, 'function')
  const requests = mockBrowser()
  await api.listMaterialGroups('scheme/a', { skip: 30, limit: 30, q: 'beef', representative_count: 2 })
  await api.listMaterialGroupFiles('group/a', { skip: 48, limit: 48, representative_count: 2 })
  await api.setMaterialGroupRepresentatives('group/a', ['file/a'], 'grev_7')
  await api.adoptMaterialGroupReference('group/a', 'file/a', 'grev_8')
  await api.getMaterialGroupRun('run/a')
  await api.resumeMaterialGroupRun('run/a')
  const groupUrl = new URL(requests[0].path, 'http://localhost')
  assert.equal(groupUrl.searchParams.get('skip'), '30')
  assert.equal(groupUrl.searchParams.get('representative_count'), '2')
  const memberUrl = new URL(requests[1].path, 'http://localhost')
  assert.equal(memberUrl.pathname, '/api/merchants/merchant%2Fa/material-filters/groups/group%2Fa/files')
  assert.equal(memberUrl.searchParams.get('skip'), '48')
  assert.deepEqual(JSON.parse(String(requests[2].init?.body)), { file_ids: ['file/a'], version: 'grev_7' })
  assert.deepEqual(JSON.parse(String(requests[3].init?.body)), { file_id: 'file/a', version: 'grev_8' })
  assert.equal(requests[4].path, '/api/merchants/merchant%2Fa/material-filters/group-runs/run%2Fa')
  assert.equal(requests[5].path, '/api/merchants/merchant%2Fa/material-filters/group-runs/run%2Fa/resume')
})

test('recognition history pages by file id without creating a model request', async () => {
  assert.equal(typeof api.listMaterialFileAttempts, 'function')
  const requests = mockBrowser()
  await api.listMaterialFileAttempts('file/a', { skip: 10, limit: 10 })
  assert.equal(requests.length, 1)
  const url = new URL(requests[0].path, 'http://localhost')
  assert.equal(url.pathname, '/api/merchants/merchant%2Fa/material-filters/files/file%2Fa/attempts')
  assert.equal(url.searchParams.get('skip'), '10')
  assert.equal(url.searchParams.get('limit'), '10')
  assert.equal(requests[0].init?.method, 'GET')
  assert.equal(requests[0].init?.body, undefined)
})

test('splitting uses only the selected members and current group version', async () => {
  assert.equal(typeof api.splitMaterialGroup, 'function')
  const requests = mockBrowser()
  await api.splitMaterialGroup('group/a', ['file/2', 'file/4'], 'grev_9')
  assert.equal(requests[0].path, '/api/merchants/merchant%2Fa/material-filters/groups/group%2Fa/split')
  assert.equal(requests[0].init?.method, 'POST')
  assert.equal(new Headers(requests[0].init?.headers).get('X-Media-Request'), '1')
  assert.deepEqual(JSON.parse(String(requests[0].init?.body)), { file_ids: ['file/2', 'file/4'], version: 'grev_9' })
})

test('manual merge and network retry preserve selected groups, scheme version and request id', async () => {
  assert.equal(typeof api.mergeMaterialGroups, 'function')
  const requests = mockBrowser({ group: { id: 'merged', manual: true } })
  const payload = { group_ids: ['group/b', 'group/a'], version: 'grev_10', request_id: 'same-merge-operation' }
  const first = await api.mergeMaterialGroups('scheme/a', payload)
  const retry = await api.mergeMaterialGroups('scheme/a', payload)
  assert.deepEqual(first, { group: { id: 'merged', manual: true } })
  assert.deepEqual(retry, first)
  assert.deepEqual(requests.map(item => item.path), Array(2).fill('/api/merchants/merchant%2Fa/material-filters/schemes/scheme%2Fa/groups/merge'))
  for (const request of requests) {
    assert.equal(request.init?.method, 'POST')
    assert.equal(new Headers(request.init?.headers).get('X-Media-Request'), '1')
    assert.deepEqual(JSON.parse(String(request.init?.body)), payload)
  }
  assert.ok(requests.every(item => !item.path.includes('recognition')))
})

test('filtering group pages preserve independent per-photo eligibility and lightweight previews', async () => {
  const payload = {
    items: [{ id: 'group', count: 3, manual: true, previews: [
      { id: 'photo/1', group_id: 'group', original_name: 'one.jpg', relative_path: 'one.jpg', source_sha256: 'sha1', is_representative: true, preview_url: '/preview/one' },
      { id: 'photo/2', group_id: 'group', original_name: 'two.jpg', relative_path: 'two.jpg', source_sha256: 'sha2', is_representative: false, preview_url: '/preview/two' },
      { id: 'photo/3', group_id: 'group', original_name: 'three.jpg', relative_path: 'three.jpg', source_sha256: 'sha3', is_representative: false, preview_url: '/preview/three' },
    ] }],
    total: 1,
    sampling_enabled: false,
    version: 'grev_11',
    stats: { new_calls: 0 },
  }
  const requests = mockBrowser(payload)
  const page = await api.listMaterialGroups('hard-filter')
  assert.equal(page.sampling_enabled, false)
  assert.equal(page.version, 'grev_11')
  assert.deepEqual(page.items[0].previews, payload.items[0].previews)
  assert.equal(page.stats.new_calls, 0)
  assert.equal(requests.length, 1)
  assert.equal(requests[0].init?.method, 'GET')
})

test('group rename is a versioned patch that sends the exact label', async () => {
  assert.equal(typeof api.renameMaterialGroup, 'function')
  const requests = mockBrowser({ id: 'group/a' })
  await api.renameMaterialGroup('group/a', '卤牛腱肉_Spiced Beef', 'grev_9')
  assert.equal(requests[0].path, '/api/merchants/merchant%2Fa/material-filters/groups/group%2Fa/name')
  assert.equal(requests[0].init?.method, 'PATCH')
  assert.deepEqual(JSON.parse(String(requests[0].init?.body)), { name: '卤牛腱肉_Spiced Beef', version: 'grev_9' })
  assert.equal(new Headers(requests[0].init?.headers).get('X-Media-Request'), '1')
})
