import type { PosterFinalization } from './types.ts'

export type HistoryBatchRef = {
  id: string
  round: number
  model: string
  group?: string
  images: Array<{
    id: string
    taskId?: string | null
    label: string
    thumb: string | null
  }>
}

export type PosterFinalizedEntry = {
  id: string
  finalization: PosterFinalization
  taskId: string
  imageId: string
  batchId: string
  round: number
  model: string
  group?: string
  label: string
  version: number
  versionId: string
  outputId: string
  thumb: string | null
}

export type PosterFinalizedGroup = {
  batchId: string
  round: number
  model: string
  group?: string
  items: PosterFinalizedEntry[]
}

/**
 * Match workspace finalizations against history batches so the history modal can show
 * which round, model, group (series), image and version were finalized.
 */
export function groupFinalizedHistory(
  batches: HistoryBatchRef[],
  finalizations: PosterFinalization[],
): PosterFinalizedGroup[] {
  if (!batches.length || !finalizations.length) return []
  const byTaskId = new Map<string, PosterFinalization>()
  for (const item of finalizations) {
    if (!byTaskId.has(item.task_id)) byTaskId.set(item.task_id, item)
  }

  const orderedBatches = batches
    .map((batch, index) => ({ batch, index }))
    .sort((a, b) => b.batch.round - a.batch.round || a.index - b.index)

  const groups: PosterFinalizedGroup[] = []
  for (const { batch } of orderedBatches) {
    const items: PosterFinalizedEntry[] = []
    for (const image of batch.images) {
      const taskId = image.taskId ?? image.id
      if (!taskId) continue
      const finalization = byTaskId.get(taskId)
      if (!finalization) continue
      items.push({
        id: finalization.id,
        finalization,
        taskId,
        imageId: image.id,
        batchId: batch.id,
        round: batch.round,
        model: batch.model,
        group: batch.group,
        label: image.label,
        version: finalization.version,
        versionId: finalization.version_id,
        outputId: finalization.output_id,
        thumb: finalization.thumb_url || finalization.preview_url || image.thumb,
      })
    }
    if (items.length) {
      groups.push({
        batchId: batch.id,
        round: batch.round,
        model: batch.model,
        group: batch.group,
        items,
      })
    }
  }
  return groups
}
