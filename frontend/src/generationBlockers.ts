import { imagePromptIssue, type PromptImage } from './imageMentions.ts'
import type { BrandMode } from './types.ts'

/**
 * A preset style the loaded list cannot satisfy must not lock the studio.
 *
 * While the list is still loading, or after it failed, the switch keeps its remembered state:
 * only an empty result for a loaded list means there is nothing to apply, and the form then runs
 * without a brand style instead of blocking generation.
 */
export function presetStyleUnavailable(
  brandMode: BrandMode,
  list: { loading: boolean; error: string | null; matches: number },
): boolean {
  return brandMode === 'preset' && !list.loading && !list.error && list.matches === 0
}

export type GenerationRequirements = {
  /** Reference images currently attached to the prompt. */
  promptImages: PromptImage[]
  prompt: string
  /** The brand mode the request will actually use, so a degraded preset never blocks. */
  brandMode: BrandMode
  brandsLoading: boolean
  brandsError: string | null
  /** The remembered platform id, empty when none is chosen. */
  platformResolved: string
  /** False while the platform or its placement is still unresolved. */
  platformReady: boolean
  /** Uploads or a pending submission make the form busy. */
  uploading: boolean
}

/**
 * Every unmet generation condition, in the order the operator has to solve it.
 *
 * The list is what the disabled generate button explains, so it also has to cover the reasons
 * that used to fail silently: a brand list still loading or failing, and a platform that has
 * not resolved yet.
 */
export function generationBlockers(requirements: GenerationRequirements): string[] {
  const blockers: string[] = []
  if (requirements.promptImages.length === 0) blockers.push('请先添加参考图')
  if (!requirements.prompt.trim()) blockers.push('请填写设计需求')
  const promptIssue = imagePromptIssue(requirements.prompt, requirements.promptImages)
  if (promptIssue) blockers.push(promptIssue)
  if (requirements.brandMode === 'preset') {
    if (requirements.brandsLoading) blockers.push('正在读取品牌风格')
    else if (requirements.brandsError) blockers.push('品牌风格加载失败，请稍后重试')
  }
  if (requirements.platformResolved && !requirements.platformReady) {
    blockers.push('发布平台配置尚未就绪，请稍后重试')
  }
  if (requirements.uploading) blockers.push('素材上传中，请稍候')
  return blockers
}
