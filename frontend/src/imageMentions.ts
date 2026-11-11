export type PromptImage = { assetId: string; name: string; url: string; removable?: boolean }
export type ImagePromptPart = { type: 'text'; text: string } | { type: 'image'; assetId: string }

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
    return `@图片${index + 1}`
  }).join('')
  return { text, missing }
}

export function imagePromptIssue(value: string, images: PromptImage[], maxLength = 5000): string | null {
  const rendered = renderImagePrompt(value, images)
  if (rendered.missing) return '引用的图片已移除，请删除该引用或重新添加图片。'
  for (const match of rendered.text.matchAll(/@图片(\d+)/g)) {
    const index = Number(match[1])
    if (index < 1 || index > images.length) return `引用的图片${index}不存在，请重新选择。`
  }
  return rendered.text.length > maxLength ? `输入内容不能超过 ${maxLength} 字。` : null
}

export function serializeImagePrompt(value: string, images: PromptImage[], maxLength = 5000): string {
  const issue = imagePromptIssue(value, images, maxLength)
  if (issue) throw new Error(issue)
  return renderImagePrompt(value, images).text
}

export function hydrateImagePrompt(value: string, images: PromptImage[]): string {
  return value.replace(/@图片(\d+)/g, (match, number) => {
    const image = images[Number(number) - 1]
    return image ? imageMention(image.assetId) : match
  })
}
