export type PromptImage = {
  assetId: string
  name: string
  url: string
  removable?: boolean
  /** The base picture of an edit: shown as `@海报` instead of taking reference number 1. */
  role?: 'poster'
}
export type ImagePromptPart = { type: 'text'; text: string } | { type: 'image'; assetId: string }

/**
 * Mentions carry a stable token, so the label can differ per composer: the edit composer names
 * the base poster `@海报` and numbers the actual references `@图1…`; a generation prompt has no
 * poster and keeps the plain positional `@图片N`.
 */
export function imageLabel(images: PromptImage[], index: number): string {
  const posterIndex = images.findIndex(image => image.role === 'poster')
  if (posterIndex < 0) return `图片${index + 1}`
  if (index === posterIndex) return '海报'
  return `图${images.slice(0, index).filter(image => image.role !== 'poster').length + 1}`
}

export function imageMention(assetId: string): string {
  return `{{image:${encodeURIComponent(assetId)}}}`
}

export function parseImagePrompt(value: string): ImagePromptPart[] {
  const parts: ImagePromptPart[] = []
  let offset = 0
  for (const match of value.matchAll(/\{\{image:([^}]*)\}\}/g)) {
    if (match.index! > offset) parts.push({ type: 'text', text: value.slice(offset, match.index) })
    let assetId = match[1]
    try { assetId = decodeURIComponent(assetId) } catch { /* Invalid tokens remain invalid references. */ }
    parts.push({ type: 'image', assetId })
    offset = match.index! + match[0].length
  }
  if (offset < value.length) parts.push({ type: 'text', text: value.slice(offset) })
  return parts
}

function renderImagePrompt(value: string, images: PromptImage[]) {
  let missing = false
  const text = parseImagePrompt(value).map(part => {
    if (part.type === 'text') return part.text
    const index = images.findIndex(image => image.assetId === part.assetId)
    if (index < 0) { missing = true; return '@已移除图片' }
    return `@${imageLabel(images, index)}`
  }).join('')
  return { text, missing }
}

export function imagePromptIssue(value: string, images: PromptImage[], maxLength = 5000): string | null {
  const rendered = renderImagePrompt(value, images)
  if (rendered.missing) return '引用的图片已移除，请删除该引用或重新添加图片。'
  const hasPoster = images.some(image => image.role === 'poster')
  if (hasPoster) {
    const references = images.filter(image => image.role !== 'poster').length
    for (const match of rendered.text.matchAll(/@图(\d+)/g)) {
      const index = Number(match[1])
      if (index < 1 || index > references) return `引用的图${index}不存在，请重新选择。`
    }
  } else {
    for (const match of rendered.text.matchAll(/@图片(\d+)/g)) {
      const index = Number(match[1])
      if (index < 1 || index > images.length) return `引用的图片${index}不存在，请重新选择。`
    }
  }
  return rendered.text.length > maxLength ? `输入内容不能超过 ${maxLength} 字。` : null
}

export function serializeImagePrompt(value: string, images: PromptImage[], maxLength = 5000): string {
  const issue = imagePromptIssue(value, images, maxLength)
  if (issue) throw new Error(issue)
  return renderImagePrompt(value, images).text
}

export function hydrateImagePrompt(value: string, images: PromptImage[]): string {
  const byLabel = new Map<string, string>()
  images.forEach((image, index) => {
    byLabel.set(imageLabel(images, index), image.assetId)
    // Older drafts named every image by position; keep them restorable.
    byLabel.set(`图片${index + 1}`, image.assetId)
  })
  return value.replace(/@(海报|图\d+|图片\d+)/g, (match, label: string) => {
    const assetId = byLabel.get(label)
    return assetId ? imageMention(assetId) : match
  })
}
