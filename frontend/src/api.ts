import type {
  Asset,
  BrandDetail,
  BrandMode,
  BrandColor,
  BrandStyleFields,
  BrandRules,
  BrandStyle,
  ImageModel,
  PlatformPreset,
  PosterEditBatch,
  PosterEditTask,
  Ratio,
  WholeImageTaskGroupFilter,
  WholeImageTaskGroupPage,
  WholeImageTask,
  WholeImageVersion,
} from './types'

export class ApiError extends Error {
  code: string

  constructor(message: string, code = 'request_failed') {
    super(message)
    this.code = code
  }
}

async function parseResponse<T>(response: Response): Promise<T> {
  let payload: unknown = null
  try {
    payload = await response.json()
  } catch {
    payload = null
  }
  if (!response.ok) {
    const body = payload as
      | { detail?: unknown; error?: { code?: string; message?: string } }
      | null
    let message = '请求失败，请稍后重试'
    if (body?.error?.message) {
      message = body.error.message
    } else if (typeof body?.detail === 'string') {
      message = body.detail
    } else if (Array.isArray(body?.detail)) {
      message = body.detail
        .map((item) =>
          typeof item === 'object' && item && 'msg' in item
            ? String((item as { msg: unknown }).msg)
            : String(item),
        )
        .join('；')
    }
    throw new ApiError(message, body?.error?.code ?? `http_${response.status}`)
  }
  return payload as T
}

export async function listAssets(): Promise<Asset[]> {
  const response = await fetch('/api/assets')
  return parseResponse<Asset[]>(response)
}

export async function listPlatforms(): Promise<PlatformPreset[]> {
  const response = await fetch('/api/platforms')
  return parseResponse<PlatformPreset[]>(response)
}

export async function listBrands(): Promise<BrandStyle[]> {
  const response = await fetch('/api/brands')
  return parseResponse<BrandStyle[]>(response)
}

export async function getBrand(brandId: string): Promise<BrandDetail> {
  const response = await fetch(`/api/brands/${encodeURIComponent(brandId)}`)
  return parseResponse<BrandDetail>(response)
}

export type UpdateBrandPayload = {
  schema_version?: number
  style_fields?: BrandStyleFields | null
  name: string
  short_name: string
  summary: string
  platform_id: string
  placement_id: string
  colors: BrandColor[]
  logo_asset_ids: string[]
  platform: string
  note: string
  layout_guide: string
  background_guide: string
  logo_keywords: string[]
  is_default: boolean
  cover_asset_id?: string | null
}

export async function createBrand(payload: UpdateBrandPayload): Promise<BrandDetail> {
  return parseResponse<BrandDetail>(await fetch('/api/brands', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  }))
}

export async function previewBrand(payload: UpdateBrandPayload): Promise<{ prompt: string }> {
  return parseResponse<{ prompt: string }>(await fetch('/api/brands/preview', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  }))
}

export async function getBrandRules(): Promise<BrandRules> {
  return parseResponse<BrandRules>(await fetch('/api/brand-rules'))
}

export async function updateBrandRules(payload: BrandRules): Promise<BrandRules> {
  return parseResponse<BrandRules>(await fetch('/api/brand-rules', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  }))
}

export async function updateBrand(
  brandId: string,
  payload: UpdateBrandPayload,
): Promise<BrandDetail> {
  const response = await fetch(`/api/brands/${encodeURIComponent(brandId)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  return parseResponse<BrandDetail>(response)
}

export async function deleteBrand(brandId: string): Promise<void> {
  const response = await fetch(`/api/brands/${encodeURIComponent(brandId)}`, {
    method: 'DELETE',
  })
  await parseResponse<void>(response)
}
export async function uploadBrandReference(
  brandId: string,
  file: File,
): Promise<BrandDetail> {
  const form = new FormData()
  form.append('file', file)
  const response = await fetch(
    `/api/brands/${encodeURIComponent(brandId)}/references`,
    { method: 'POST', body: form },
  )
  return parseResponse<BrandDetail>(response)
}

export async function deleteBrandReference(
  brandId: string,
  assetId: string,
): Promise<BrandDetail> {
  const response = await fetch(
    `/api/brands/${encodeURIComponent(brandId)}/references/${encodeURIComponent(assetId)}`,
    { method: 'DELETE' },
  )
  return parseResponse<BrandDetail>(response)
}

export async function uploadBrandLogos(
  brandId: string,
  files: File[],
): Promise<BrandDetail> {
  const form = new FormData()
  files.forEach((file) => form.append('files', file))
  const response = await fetch(
    `/api/brands/${encodeURIComponent(brandId)}/logos`,
    { method: 'POST', body: form },
  )
  return parseResponse<BrandDetail>(response)
}

export async function deleteBrandLogo(
  brandId: string,
  assetId: string,
): Promise<BrandDetail> {
  const response = await fetch(
    `/api/brands/${encodeURIComponent(brandId)}/logos/${encodeURIComponent(assetId)}`,
    { method: 'DELETE' },
  )
  return parseResponse<BrandDetail>(response)
}

export async function duplicateBrand(
  brandId: string,
  payload: { name?: string; platform_id?: string; placement_id?: string },
): Promise<BrandDetail> {
  const response = await fetch(`/api/brands/${encodeURIComponent(brandId)}/duplicate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  return parseResponse<BrandDetail>(response)
}

export async function uploadAsset(file: File): Promise<Asset> {
  const form = new FormData()
  form.append('file', file)
  const response = await fetch('/api/assets', { method: 'POST', body: form })
  return parseResponse<Asset>(response)
}

export async function deleteAsset(assetId: string, force = false): Promise<void> {
  const query = force ? '?force=true' : ''
  const response = await fetch(`/api/assets/${encodeURIComponent(assetId)}${query}`, {
    method: 'DELETE',
  })
  if (!response.ok) {
    await parseResponse<unknown>(response)
  }
}

export type WholeImageTaskCreatePayload = {
  image_model?: ImageModel
  prompt: string
  prompt_optimized?: boolean
  ratio?: Ratio
  brand_id?: string | null
  brand_mode?: BrandMode
  logo_asset_id?: string | null
  style_reference_asset_ids?: string[]
  candidate_count?: number
  candidate_batch_id?: string | null
  candidate_index?: number
  product_asset_ids?: string[]
  platform_id?: string | null
  placement_id?: string | null
}

export async function createWholeImageTask(
  payload: WholeImageTaskCreatePayload,
): Promise<WholeImageTask> {
  const response = await fetch('/api/whole-image/tasks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  return parseResponse<WholeImageTask>(response)
}

export async function listWholeImageTaskBatch(batchId: string): Promise<WholeImageTask[]> {
  const response = await fetch(`/api/whole-image/batches/${encodeURIComponent(batchId)}`)
  return parseResponse<WholeImageTask[]>(response)
}

export async function listWholeImageTaskGroups(options: {
  status?: WholeImageTaskGroupFilter
  limit?: number
  cursor?: string | null
} = {}): Promise<WholeImageTaskGroupPage> {
  const params = new URLSearchParams({
    status: options.status ?? 'all',
    limit: String(options.limit ?? 24),
  })
  if (options.cursor) {
    params.set('cursor', options.cursor)
  }
  const response = await fetch(`/api/whole-image/tasks?${params.toString()}`)
  return parseResponse<WholeImageTaskGroupPage>(response)
}

export async function retryWholeImageTask(taskId: string): Promise<WholeImageTask> {
  const response = await fetch(
    `/api/whole-image/tasks/${encodeURIComponent(taskId)}/retry`,
    { method: 'POST' },
  )
  return parseResponse<WholeImageTask>(response)
}

export async function getWholeImageTask(taskId: string): Promise<WholeImageTask> {
  const response = await fetch(
    `/api/whole-image/tasks/${encodeURIComponent(taskId)}`,
  )
  return parseResponse<WholeImageTask>(response)
}

export async function createWholeImageEdit(
  taskId: string,
  instruction: string,
  sourceVersionId: string,
  sourceOutputId?: string | null,
): Promise<WholeImageTask> {
  const response = await fetch(
    `/api/whole-image/tasks/${encodeURIComponent(taskId)}/edits`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        instruction,
        source_version_id: sourceVersionId,
        source_output_id: sourceOutputId ?? null,
      }),
    },
  )
  return parseResponse<WholeImageTask>(response)
}

export async function selectWholeImageOutput(
  taskId: string,
  versionId: string,
  outputId: string,
): Promise<WholeImageTask> {
  const response = await fetch(
    `/api/whole-image/tasks/${encodeURIComponent(taskId)}/versions/${encodeURIComponent(versionId)}/outputs/${encodeURIComponent(outputId)}/select`,
    { method: 'POST' },
  )
  return parseResponse<WholeImageTask>(response)
}

export async function listWholeImageVersions(
  taskId: string,
): Promise<WholeImageVersion[]> {
  const response = await fetch(
    `/api/whole-image/tasks/${encodeURIComponent(taskId)}/versions`,
  )
  return parseResponse<WholeImageVersion[]>(response)
}

export async function selectWholeImageVersion(
  taskId: string,
  versionId: string,
): Promise<WholeImageTask> {
  const response = await fetch(
    `/api/whole-image/tasks/${encodeURIComponent(taskId)}/versions/${encodeURIComponent(versionId)}/select`,
    { method: 'POST' },
  )
  return parseResponse<WholeImageTask>(response)
}

export async function createPosterEdit(
  prompt: string,
  assetIds: string[],
): Promise<PosterEditTask> {
  const response = await fetch('/api/poster-edits', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prompt, asset_ids: assetIds }),
  })
  return parseResponse<PosterEditTask>(response)
}

export async function getPosterEdit(taskId: string): Promise<PosterEditTask> {
  const response = await fetch(`/api/poster-edits/${encodeURIComponent(taskId)}`)
  return parseResponse<PosterEditTask>(response)
}

export async function createPosterEditBatch(
  prompt: string,
  assetIds: string[],
  candidateCount: number,
): Promise<PosterEditBatch> {
  const response = await fetch('/api/poster-edits/batches', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      prompt,
      asset_ids: assetIds,
      candidate_count: candidateCount,
    }),
  })
  return parseResponse<PosterEditBatch>(response)
}

export async function getPosterEditBatch(batchId: string): Promise<PosterEditBatch> {
  const response = await fetch(`/api/poster-edits/batches/${encodeURIComponent(batchId)}`)
  return parseResponse<PosterEditBatch>(response)
}

export async function listTaskCenterGroups(options: {
  status?: WholeImageTaskGroupFilter
  kind?: 'all' | 'generate' | 'edit'
  limit?: number
  cursor?: string | null
} = {}): Promise<WholeImageTaskGroupPage> {
  const params = new URLSearchParams({
    status: options.status ?? 'all',
    kind: options.kind ?? 'all',
    limit: String(options.limit ?? 24),
  })
  if (options.cursor) params.set('cursor', options.cursor)
  const response = await fetch(`/api/task-center?${params.toString()}`)
  return parseResponse<WholeImageTaskGroupPage>(response)
}

export type PromptOptimizePayload = {
  prompt: string
  brand_mode: BrandMode
  brand_id?: string | null
  logo_asset_id?: string | null
  style_reference_asset_ids: string[]
  product_asset_ids: string[]
  platform_id?: string | null
  placement_id?: string | null
}

export async function optimizePrompt(
  payload: PromptOptimizePayload,
): Promise<{ prompt: string }> {
  const response = await fetch('/api/prompts/optimize', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  return parseResponse<{ prompt: string }>(response)
}
