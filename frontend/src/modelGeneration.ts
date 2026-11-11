import type { ImageModel, WholeImageTask } from './types.ts'
import type { WholeImageTaskCreatePayload } from './api.ts'
import { DEFAULT_IMAGE_MODEL, IMAGE_MODEL_OPTIONS } from './imageModels.ts'

export type ModelCounts = Partial<Record<ImageModel, number>>
export type ModelSubmission = { batchId: string; count: number; error?: string }

export function normalizeModelCount(value: string): number {
  const count = Number(value)
  return Number.isFinite(count) ? Math.min(3, Math.max(0, Math.trunc(count))) : 0
}

export function candidateForModel(tasks: WholeImageTask[], model: ImageModel, preferredId?: string) {
  const matching = tasks.filter(task => (task.input.image_model ?? DEFAULT_IMAGE_MODEL) === model)
  return matching.find(task => task.id === preferredId) ?? matching.find(task => task.status === 'succeeded') ?? matching[0] ?? null
}

export function imageModelLabel(model: string) {
  return IMAGE_MODEL_OPTIONS.find(option => option.value === model)?.label ?? model
}

export function defaultModelCounts(): ModelCounts {
  return Object.fromEntries(IMAGE_MODEL_OPTIONS.map(option => [option.value, 0]))
}

export function restoreModelCounts(tasks: WholeImageTask[]): ModelCounts {
  const counts: ModelCounts = Object.fromEntries(IMAGE_MODEL_OPTIONS.map(option => [option.value, 0]))
  for (const option of IMAGE_MODEL_OPTIONS) {
    const latest = tasks.filter(task => (task.input.image_model ?? DEFAULT_IMAGE_MODEL) === option.value).at(-1)
    if (latest) counts[option.value] = Math.min(3, Math.max(1, latest.input.candidate_count ?? 3))
  }
  return counts
}

export function mergeCandidates(current: WholeImageTask[], incoming: WholeImageTask[]) {
  const merged = new Map(current.map(task => [task.id, task]))
  for (const task of incoming) {
    const previous = merged.get(task.id)
    if (!previous || task.updated_at >= previous.updated_at) merged.set(task.id, task)
  }
  return [...merged.values()]
}

export function modelGenerationState(tasks: WholeImageTask[], model: ImageModel) {
  const matching = tasks.filter(task => (task.input.image_model ?? DEFAULT_IMAGE_MODEL) === model)
  const latest = matching.at(-1)
  const round = latest ? matching.filter(task => task.input.candidate_batch_id === latest.input.candidate_batch_id) : []
  return {
    running: matching.some(task => task.status === 'queued' || task.status === 'running'),
    completed: round.filter(task => task.status === 'succeeded' || task.status === 'failed').length,
    total: latest?.input.candidate_count ?? round.length,
    hasHistory: matching.length > 0,
  }
}

export async function submitModelRound(
  payload: WholeImageTaskCreatePayload, model: ImageModel, count: number,
  workspaceId: string, batchId: string,
  create: (payload: WholeImageTaskCreatePayload) => Promise<WholeImageTask>,
) {
  if (!Number.isInteger(count) || count < 1 || count > 3) throw new Error('每个模型每轮可生成 1–3 张')
  const results = await Promise.allSettled(Array.from({ length: count }, (_, index) => create({
    ...payload, image_model: model, candidate_count: count, candidate_batch_id: batchId,
    candidate_index: index, generation_workspace_id: workspaceId,
  })))
  return {
    tasks: results.flatMap(result => result.status === 'fulfilled' ? [result.value] : []),
    errors: results.flatMap(result => result.status === 'rejected' ? [result.reason] : []),
  }
}
