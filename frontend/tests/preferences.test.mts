import assert from 'node:assert/strict'
import test from 'node:test'
import {
  clearStoredMerchantId,
  readPosterPreference,
  readStoredMerchantId,
  savePosterPreference,
  storeMerchantId,
} from '../src/preferences.ts'

class MemoryStorage {
  private data = new Map<string, string>()
  getItem(key: string) {
    return this.data.has(key) ? this.data.get(key)! : null
  }
  setItem(key: string, value: string) {
    this.data.set(key, value)
  }
  removeItem(key: string) {
    this.data.delete(key)
  }
  clear() {
    this.data.clear()
  }
  key(index: number) {
    return [...this.data.keys()][index] ?? null
  }
  get length() {
    return this.data.size
  }
}

function mockStorage(impl: Storage | null | (() => Storage)) {
  const value = typeof impl === 'function' ? impl() : impl
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: { location: { search: '' }, localStorage: value },
  })
}

function throwingStorage(): Storage {
  const store = new MemoryStorage()
  return {
    ...store,
    getItem: () => {
      throw new Error('storage disabled')
    },
    setItem: () => {
      throw new Error('quota exceeded')
    },
  } as unknown as Storage
}

test('the merchant opened last survives a browser restart', () => {
  const store = new MemoryStorage()
  mockStorage(store as unknown as Storage)
  storeMerchantId('m/1')
  assert.equal(readStoredMerchantId(), 'm/1')
  // A blank id can never overwrite a usable record.
  storeMerchantId('   ')
  assert.equal(readStoredMerchantId(), 'm/1')
})

test('only the rejected merchant is forgotten', () => {
  const store = new MemoryStorage()
  mockStorage(store as unknown as Storage)
  storeMerchantId('m/1')
  // A stale link to another merchant must not wipe the working default.
  clearStoredMerchantId('m/2')
  assert.equal(readStoredMerchantId(), 'm/1')
  clearStoredMerchantId('m/1')
  assert.equal(readStoredMerchantId(), '')
  clearStoredMerchantId()
  assert.equal(readStoredMerchantId(), '')
})

test('a poster preference is remembered per merchant', () => {
  const store = new MemoryStorage()
  mockStorage(store as unknown as Storage)
  const value = {
    platform_id: 'platform-a',
    placement_id: 'placement-a',
    ratio: '4:5' as const,
    brand_mode: 'preset' as const,
    brand_id: 'brand-a',
  }
  savePosterPreference('m/1', value)
  assert.deepEqual(readPosterPreference('m/1'), value)
  // Another merchant keeps its own form memory.
  assert.equal(readPosterPreference('m/2'), null)
  // A missing merchant is never written to the shared fallback slot.
  savePosterPreference('', value)
  assert.equal(readPosterPreference(''), null)
})

test('corrupt or partial records fall back to the form defaults', () => {
  const store = new MemoryStorage()
  mockStorage(store as unknown as Storage)
  store.setItem('social-media:poster:v1:m/1', '{not json')
  assert.equal(readPosterPreference('m/1'), null)

  store.setItem(
    'social-media:poster:v1:m/1',
    JSON.stringify({ ratio: '7:3', brand_mode: 'flashy', platform_id: 'platform-a', brand_id: 7 }),
  )
  // Unsupported values are dropped; the usable ones are kept.
  assert.deepEqual(readPosterPreference('m/1'), { platform_id: 'platform-a' })

  store.setItem('social-media:poster:v1:m/1', '[]')
  assert.equal(readPosterPreference('m/1'), null)
})

test('disabled storage degrades to no preference instead of throwing', () => {
  mockStorage(throwingStorage)
  assert.equal(readStoredMerchantId(), '')
  assert.equal(readPosterPreference('m/1'), null)
  // Writes and removals are silently ignored.
  storeMerchantId('m/1')
  clearStoredMerchantId('m/1')
  savePosterPreference('m/1', {
    platform_id: 'p',
    placement_id: 'q',
    ratio: '1:1',
    brand_mode: 'none',
    brand_id: '',
  })
})

test('without a window the preference helpers stay inert', () => {
  Object.defineProperty(globalThis, 'window', { configurable: true, value: undefined })
  assert.equal(readStoredMerchantId(), '')
  assert.equal(readPosterPreference('m/1'), null)
})
