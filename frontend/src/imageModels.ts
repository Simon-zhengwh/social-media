import type { ImageModel } from './types'

export const DEFAULT_IMAGE_MODEL: ImageModel = 'gpt-image-2.5-sunburst'

export type ImageModelOption = {
  value: ImageModel
  label: string
}

export const IMAGE_MODEL_OPTIONS: ImageModelOption[] = [
  { value: 'gpt-image-2.5-sunburst', label: 'GPT Image 2.5' },
  { value: 'seedream-5-pro-image-to-image', label: 'Seedream 5 Pro' },
  { value: 'seedream-5.5-image-to-image', label: 'Seedream 5.5' },
  // MiniMax H3 is called directly by this backend until core-ai publishes the same model.
  { value: 'minimax-h3-image', label: 'MiniMax H3' },
]

// `gemini-3.1-flash-image` stays valid on the API so older tasks keep working, but it is no
// longer offered here.
