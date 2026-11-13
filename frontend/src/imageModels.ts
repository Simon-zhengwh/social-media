import type { ImageModel } from './types'

export const DEFAULT_IMAGE_MODEL: ImageModel = 'gpt-image-2.5-sunburst'

export function normalizeImageModel(model?: ImageModel | null): ImageModel
export function normalizeImageModel(model: string | null | undefined): string
export function normalizeImageModel(model?: string | null): string {
  return model === 'minimax-h3-image' ? 'minimax-h3-image-edit' : model ?? DEFAULT_IMAGE_MODEL
}

export type ImageModelOption = {
  value: ImageModel
  label: string
}

export const IMAGE_MODEL_OPTIONS: ImageModelOption[] = [
  { value: 'gpt-image-2.5-sunburst', label: 'GPT Image 2.5' },
  { value: 'seedream-5-pro-image-to-image', label: 'Seedream 5 Pro' },
  { value: 'seedream-5.5-image-to-image', label: 'Seedream 5.5' },
  // MiniMax H3 also goes through the core-ai gateway; only the model id differs.
  { value: 'minimax-h3-image-edit', label: 'MiniMax H3' },
]

// `gemini-3.1-flash-image` stays valid on the API so older tasks keep working, but it is no
// longer offered here.
