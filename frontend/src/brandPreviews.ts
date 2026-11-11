import type { BrandStyle } from './types'

export type BrandPreviewThumb = {
  key: string
  src: string
  alt: string
  title: string
}

/**
 * Thumbnails for one style in the style picker.
 *
 * The picker previews every reference, not just the cover: operators pick a style by the whole
 * reference set, and each thumbnail can be zoomed. Cover comes first so the familiar thumbnail
 * stays in the leading slot; older responses without the reference list fall back to the cover.
 */
export function stylePreviews(brand: BrandStyle): BrandPreviewThumb[] {
  const references = brand.reference_images ?? []
  if (references.length) {
    const ordered = [...references].sort(
      (left, right) => Number(right.is_cover) - Number(left.is_cover),
    )
    return ordered.map((reference) => ({
      key: reference.asset_id,
      src: reference.url,
      alt: `${brand.name} 参考图`,
      title: reference.original_name || brand.name,
    }))
  }
  return brand.cover_url
    ? [{ key: 'cover', src: brand.cover_url, alt: `${brand.name} 风格参考`, title: brand.name }]
    : []
}
