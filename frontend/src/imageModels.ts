import type { ImageModel } from './types'

export const DEFAULT_IMAGE_MODEL: ImageModel = 'gpt-image-2.5-sunburst'

export const IMAGE_MODEL_OPTIONS: Array<{
  value: ImageModel
  label: string
  description: string
}> = [
  {
    value: 'gpt-image-2.5-sunburst',
    label: 'gpt-image-2.5-sunburst',
    description: '默认模型，适合常规视觉设计与改图。',
  },
  {
    value: 'gemini-3.1-flash-image',
    label: 'gemini-3.1-flash-image',
    description: 'Gemini Flash 图像模型。',
  },
  {
    value: 'seedream-5-pro-image-to-image',
    label: 'seedream-5-pro-image-to-image',
    description: 'Seedream 图生图模型，使用参考素材生成。',
  },
]
