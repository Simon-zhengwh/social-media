import type { ImageModel } from './types'

export const DEFAULT_IMAGE_MODEL: ImageModel = 'gpt-image-2.5-sunburst'

export type ImageModelOption = {
  value: ImageModel
  label: string
  hint: string
  // MiniMax is called directly by this backend until core-ai publishes the same models.
  temporary?: boolean
}

export const IMAGE_MODEL_OPTIONS: ImageModelOption[] = [
  {
    value: 'gpt-image-2.5-sunburst',
    label: 'GPT Image 2.5',
    hint: '默认 · core-ai',
  },
  {
    value: 'seedream-5-pro-image-to-image',
    label: 'Seedream 5 Pro',
    hint: 'core-ai',
  },
  {
    value: 'seedream-5.5-image-to-image',
    label: 'Seedream 5.5',
    hint: 'core-ai',
  },
  {
    value: 'minimax-h3-image',
    label: 'MiniMax H3',
    hint: '临时直连',
    temporary: true,
  },
]

// `gemini-3.1-flash-image` stays valid on the API so older tasks keep working, but it is no
// longer offered here.
