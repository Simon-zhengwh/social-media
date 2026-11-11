import assert from 'node:assert/strict'
import test from 'node:test'
import { listAssetPage, updateAssetTags } from '../src/api.ts'
import { MAX_ASSET_TAGS, normalizeTags } from '../src/assetTags.ts'

function mockBrowser(search = '?merchant_id=merchant%2Fa') {
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { location: { search } } })
  const requests: { path: string; init?: RequestInit }[] = []
  globalThis.fetch = async (path, init) => {
    requests.push({ path: String(path), init })
    return new Response(JSON.stringify({ items: [], total: 0, tags: [] }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  }
  return requests
}

test('typed-but-uncommitted tags are saved with the set they belong to', () => {
  assert.deepEqual(normalizeTags(['Dish'], '  Hotpot '), ['Dish', 'Hotpot'])
  assert.deepEqual(normalizeTags(['Dish'], ''), ['Dish'])
  // Blank input never becomes a tag.
  assert.deepEqual(normalizeTags([], '   '), [])
  assert.deepEqual(normalizeTags([' Dish ', ''], ''), ['Dish'])
  // Duplicates are dropped case-insensitively and keep the first spelling.
  assert.deepEqual(normalizeTags(['Dish', 'dish', 'HotPot'], ' HOTPOT '), ['Dish', 'HotPot'])
  // The helper never truncates: the caller reports "too many" instead of losing a tag silently.
  const many = ['a', 'b', 'c', 'd', 'e', 'f']
  assert.equal(normalizeTags(many).length, many.length)
  assert.equal(MAX_ASSET_TAGS, 5)
})

test('the asset page sends the tag filter with the keyword and paging', async () => {
  const requests = mockBrowser()
  await listAssetPage('招牌 菜', 120, 24, 'Hot Pot')
  const url = new URL(requests[0].path, 'http://localhost')
  assert.equal(url.pathname, '/api/merchants/merchant%2Fa/am-assets')
  assert.equal(url.searchParams.get('keyword'), '招牌 菜')
  assert.equal(url.searchParams.get('tag'), 'Hot Pot')
  assert.equal(url.searchParams.get('skip'), '120')
  assert.equal(url.searchParams.get('limit'), '24')
  assert.equal(requests[0].init?.credentials, 'same-origin')
})

test('saving tags replaces the whole array behind the write-protection header', async () => {
  const requests = mockBrowser()
  await updateAssetTags('asset/a', [])
  assert.equal(requests[0].path, '/api/merchants/merchant%2Fa/assets/asset%2Fa/tags')
  assert.equal(requests[0].init?.method, 'PUT')
  assert.equal(new Headers(requests[0].init?.headers).get('X-Media-Request'), '1')
  assert.equal(requests[0].init?.body, JSON.stringify({ tags: [] }))
})

test('a rejected tag save surfaces the server message', async () => {
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: { location: { search: '?merchant_id=merchant-a' } },
  })
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ error: { code: 'MERCHANT_ACCESS_DENIED', message: '无权修改素材' } }), {
      status: 403,
      headers: { 'Content-Type': 'application/json' },
    })
  await assert.rejects(updateAssetTags('asset', ['Dish']), /无权修改素材/)
})
