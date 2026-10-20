import type {
  Asset,
  AssetKind,
  BrandDetail,
  BrandMode,
  BrandStyle,
  LayerPatch,
  MerchantInfo,
  PlatformPreset,
  Ratio,
  SampleImportResult,
  Task,
  VersionSummary,
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

export async function getSamples(): Promise<SampleImportResult> {
  const response = await fetch('/api/samples')
  return parseResponse<SampleImportResult>(response)
}

export async function listAssets(kind?: AssetKind): Promise<Asset[]> {
  const query = kind ? `?kind=${encodeURIComponent(kind)}` : ''
  const response = await fetch(`/api/assets${query}`)
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
  name: string
  short_name: string
  summary: string
  layout_guide: string
  background_guide: string
  logo_keywords: string[]
  is_default: boolean
  cover_asset_id?: string | null
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

export async function uploadAsset(file: File, kind: AssetKind): Promise<Asset> {
  const form = new FormData()
  form.append('file', file)
  form.append('kind', kind)
  const response = await fetch('/api/assets', { method: 'POST', body: form })
  return parseResponse<Asset>(response)
}

export type CreateTaskPayload = {
  product_asset_ids: string[]
  brand_id?: string | null
  brand_mode?: BrandMode
  style_reference_asset_ids?: string[]
  candidate_count?: number
  candidate_batch_id?: string | null
  candidate_index?: number
  prompt?: string
  ratio?: Ratio
  generation_mode?: 'classic' | 'reference_guided'
  platform_id?: string | null
  placement_id?: string | null
  merchant_info?: MerchantInfo | null
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

export async function createTask(payload: CreateTaskPayload): Promise<Task> {
  const response = await fetch('/api/tasks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  return parseResponse<Task>(response)
}

export async function listTaskBatch(batchId: string): Promise<Task[]> {
  const response = await fetch(`/api/tasks/batches/${encodeURIComponent(batchId)}`)
  return parseResponse<Task[]>(response)
}

export async function getTask(taskId: string): Promise<Task> {
  const response = await fetch(`/api/tasks/${encodeURIComponent(taskId)}`)
  return parseResponse<Task>(response)
}

export async function resumeTask(taskId: string): Promise<Task> {
  const response = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/resume`, {
    method: 'POST',
  })
  return parseResponse<Task>(response)
}

export async function createEdit(taskId: string, instruction: string): Promise<Task> {
  const response = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/edits`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ instruction }),
  })
  return parseResponse<Task>(response)
}

export async function patchLayer(
  taskId: string,
  layerId: string,
  patch: LayerPatch,
): Promise<Task> {
  const response = await fetch(
    `/api/tasks/${encodeURIComponent(taskId)}/layers/${encodeURIComponent(layerId)}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    },
  )
  return parseResponse<Task>(response)
}

export async function regenerateComponent(
  taskId: string,
  componentId: string,
): Promise<Task> {
  const response = await fetch(
    `/api/tasks/${encodeURIComponent(taskId)}/components/${encodeURIComponent(componentId)}/regenerate`,
    { method: 'POST' },
  )
  return parseResponse<Task>(response)
}

export async function listVersions(taskId: string): Promise<VersionSummary[]> {
  const response = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/versions`)
  return parseResponse<VersionSummary[]>(response)
}

export async function restoreVersion(
  taskId: string,
  version: number,
): Promise<Task> {
  const response = await fetch(
    `/api/tasks/${encodeURIComponent(taskId)}/versions/${version}/restore`,
    { method: 'POST' },
  )
  return parseResponse<Task>(response)
}

export type WholeImageTaskCreatePayload = {
  prompt: string
  ratio?: Ratio
  brand_id?: string | null
  brand_mode?: BrandMode
  style_reference_asset_ids?: string[]
  candidate_count?: number
  candidate_batch_id?: string | null
  candidate_index?: number
  product_asset_ids?: string[]
  platform_id?: string | null
  placement_id?: string | null
  merchant_info?: MerchantInfo | null
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
): Promise<WholeImageTask> {
  const response = await fetch(
    `/api/whole-image/tasks/${encodeURIComponent(taskId)}/edits`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        instruction,
        source_version_id: sourceVersionId,
      }),
    },
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
