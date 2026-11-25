import type { ImageModel, Ratio } from './types'

export const DEFAULT_IMAGE_MODEL: ImageModel = 'gpt-image-2.5-sunburst'

/** Models offered for "仅替换元素"; every click sends exactly one of them. */
export const ORIGINAL_REPLACEMENT_MODELS: ImageModel[] = [
  'seedream-5-pro-image-to-image', 'seedream-5.5-image-to-image', 'minimax-h3-image-edit',
]

/**
 * The picker always offers every replacement model. Selecting one that cannot keep the canvas
 * ratio defaults to a supported model, and the others still run at their own ratio so the
 * operator can crop the result back to the canvas.
 */
export function defaultReplacementModel(ratio?: Ratio): ImageModel {
  return ORIGINAL_REPLACEMENT_MODELS.find((model) => !ratio || imageModelSupportsRatio(model, ratio))
    ?? ORIGINAL_REPLACEMENT_MODELS[0]
}

export function normalizeImageModel(model?: ImageModel | null): ImageModel
export function normalizeImageModel(model: string | null | undefined): string
export function normalizeImageModel(model?: string | null): string {
  return model === 'minimax-h3-image' ? 'minimax-h3-image-edit' : model ?? DEFAULT_IMAGE_MODEL
}

export type ImageModelOption = {
  value: ImageModel
  label: string
  unsupportedRatios?: Ratio[]
}

export const IMAGE_MODEL_OPTIONS: ImageModelOption[] = [
  { value: 'gpt-image-2.5-sunburst', label: 'GPT Image 2.5' },
  { value: 'gemini-3-pro-image', label: 'Gemini 3 Pro Image' },
  { value: 'gemini-3.1-flash-image', label: 'Gemini 3.1 Flash Image' },
  { value: 'seedream-5-pro-image-to-image', label: 'Seedream 5 Pro', unsupportedRatios: ['4:5'] },
  { value: 'seedream-5.5-image-to-image', label: 'Seedream 5.5', unsupportedRatios: ['4:5'] },
  // MiniMax H3 also goes through the core-ai gateway; only the model id differs.
  { value: 'minimax-h3-image-edit', label: 'MiniMax H3' },
]

export function imageModelSupportsRatio(model: ImageModel, ratio: Ratio) {
  const option = IMAGE_MODEL_OPTIONS.find(item => item.value === normalizeImageModel(model))
  return !option?.unsupportedRatios?.includes(ratio)
}

const CANVAS_RATIO_PAIRS: readonly [Ratio, number][] = [
  ['1:1', 1], ['9:16', 9 / 16], ['4:5', 4 / 5], ['16:9', 16 / 9], ['3:4', 3 / 4], ['4:3', 4 / 3],
]

/** Which canvas ratio a picture keeps. The backend judges the source pixels, so the picker does too. */
export function canvasRatioOf(width?: number | null, height?: number | null): Ratio | undefined {
  if (!width || !height) return undefined
  const value = width / height
  return CANVAS_RATIO_PAIRS.find(([, pair]) => Math.abs(value / pair - 1) < 0.01)?.[0]
}
