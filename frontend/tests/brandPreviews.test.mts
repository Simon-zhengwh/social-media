import assert from 'node:assert/strict'
import test from 'node:test'
import { stylePreviews } from '../src/brandPreviews.ts'
import type { BrandStyle } from '../src/types.ts'

function brand(overrides: Partial<BrandStyle> = {}): BrandStyle {
  return {
    id: 'brand-seejoy',
    name: 'SEEJOY 喜聚',
    short_name: '喜聚',
    summary: '',
    platform: '小红书',
    platform_id: 'xiaohongshu',
    placement_id: 'note',
    purpose: '',
    note: '',
    is_default: true,
    cover_url: '/api/brands/brand-seejoy/cover',
    logo_keywords: [],
    colors: [],
    logo_asset_ids: [],
    logo_images: [],
    ...overrides,
  }
}

test('every reference is previewed, with the cover first', () => {
  const previews = stylePreviews(
    brand({
      reference_images: [
        { asset_id: 'asset-b', original_name: 'b.png', url: '/b', width: 1, height: 1, is_cover: false },
        { asset_id: 'asset-a', original_name: 'a.png', url: '/a', width: 1, height: 1, is_cover: true },
      ],
    }),
  )

  assert.deepEqual(previews.map((preview) => preview.key), ['asset-a', 'asset-b'])
  assert.deepEqual(previews.map((preview) => preview.src), ['/a', '/b'])
})

test('a style without references falls back to its cover, then to nothing', () => {
  assert.deepEqual(stylePreviews(brand()).map((preview) => preview.src), ['/api/brands/brand-seejoy/cover'])
  assert.deepEqual(stylePreviews(brand({ cover_url: null })), [])
})
