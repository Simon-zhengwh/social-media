import type {
  Asset,
  OriginalReplacementItem,
  OriginalReplacementTask,
  BrandDetail,
  BrandMode,
  BrandColor,
  BrandStyleFields,
  BrandRules,
  BrandStyle,
  ImageModel,
  PlatformPreset,
  Ratio,
  WholeImageTaskGroupFilter,
  WholeImageTaskGroupPage,
  WholeImageTask,
  WholeImageVersion,
} from './types'


export function merchantIdFromLocation(): string {
  return new URLSearchParams(window.location.search).get('merchant_id')?.trim() ?? ''
}

export function merchantPath(path: string): string {
  if (path === '/api/platforms' || path === '/api/health' || path.startsWith('/api/merchants/')) return path
  if (!path.startsWith('/api/')) throw new ApiError('API 路径缺少 /api 前缀', 'invalid_api_path')
  const merchantId = merchantIdFromLocation()
  if (!merchantId) throw new ApiError('缺少商家上下文，请从 AM Console 重新进入', 'merchant_context_missing')
  return `/api/merchants/${encodeURIComponent(merchantId)}${path.slice(4)}`
}

export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const method = (init.method ?? 'GET').toUpperCase()
  const headers = new Headers(init.headers)
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) headers.set('X-Media-Request', '1')
  return fetch(merchantPath(path), { ...init, method, headers, credentials: 'same-origin' })
}

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

export type AssetPage = { items: Asset[]; total: number; skip: number; limit: number }

export async function listAssetPage(keyword = '', skip = 0, limit = 24): Promise<AssetPage> {
  const params = new URLSearchParams({ keyword, skip: String(skip), limit: String(limit) })
  const response = await apiFetch(`/api/am-assets?${params.toString()}`)
  return parseResponse<AssetPage>(response)
}

export async function listAssets(): Promise<Asset[]> {
  const page = await listAssetPage('', 0, 100)
  return page.items
}

export type MerchantContext = {
  merchant: { user_id: string; user_name: string; merchant_id: string; merchant_name: string }
  profile: unknown
}

export type MerchantOption = { id: string; name: string }

/**
 * The merchants the operator may switch to.
 *
 * Unscoped on purpose: this is the call that picks a merchant, so it cannot carry one.
 */
export async function listMerchants(keyword = ''): Promise<MerchantOption[]> {
  const query = keyword.trim() ? `?keyword=${encodeURIComponent(keyword.trim())}` : ''
  const page = await apiFetch(`/api/merchants${query}`)
  const result = await parseResponse<{ total: number; merchants: MerchantOption[] }>(page)
  return result.merchants ?? []
}

export async function getMerchantContext(): Promise<MerchantContext> {
  return parseResponse<MerchantContext>(await apiFetch('/api/context'))
}

export async function refreshOriginalReplacement(taskId: string, replacementId: string): Promise<OriginalReplacementTask> {
  return parseResponse<OriginalReplacementTask>(await apiFetch(`/api/whole-image/tasks/${encodeURIComponent(taskId)}/original-replacements/${encodeURIComponent(replacementId)}/refresh-result`, { method: 'POST' }))
}

export async function listOriginalReplacements(taskId: string): Promise<OriginalReplacementTask[]> {
  return parseResponse<OriginalReplacementTask[]>(await apiFetch(`/api/whole-image/tasks/${encodeURIComponent(taskId)}/original-replacements`))
}

export async function createOriginalReplacement(taskId: string, sourceVersionId: string, sourceOutputId: string,
  replacements: OriginalReplacementItem[]): Promise<OriginalReplacementTask> {
  return parseResponse<OriginalReplacementTask>(await apiFetch(`/api/whole-image/tasks/${encodeURIComponent(taskId)}/original-replacements`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ source_version_id: sourceVersionId, source_output_id: sourceOutputId, replacements }),
  }))
}

export async function applyOriginalReplacement(task: WholeImageTask, replacementId: string): Promise<WholeImageTask> {
  return parseResponse<WholeImageTask>(await apiFetch(`/api/whole-image/tasks/${encodeURIComponent(task.id)}/original-replacements/${encodeURIComponent(replacementId)}/apply`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ expected_current_version_id: task.current_version_id, expected_current_output_id: task.current_output_id }),
  }))
}

export async function listPlatforms(): Promise<PlatformPreset[]> {
  const response = await apiFetch('/api/platforms')
  return parseResponse<PlatformPreset[]>(response)
}

export async function listBrands(): Promise<BrandStyle[]> {
  const response = await apiFetch('/api/brands')
  return parseResponse<BrandStyle[]>(response)
}

export async function getBrand(brandId: string): Promise<BrandDetail> {
  const response = await apiFetch(`/api/brands/${encodeURIComponent(brandId)}`)
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
  return parseResponse<BrandDetail>(await apiFetch('/api/brands', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  }))
}

export async function initializeBrandFromProfile(): Promise<BrandDetail & { created: boolean }> {
  return parseResponse<BrandDetail & { created: boolean }>(await apiFetch('/api/brands/initialize-from-profile', {
    method: 'POST',
  }))
}

export async function previewBrand(payload: UpdateBrandPayload): Promise<{ prompt: string }> {
  return parseResponse<{ prompt: string }>(await apiFetch('/api/brands/preview', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  }))
}

export async function getBrandRules(): Promise<BrandRules> {
  return parseResponse<BrandRules>(await apiFetch('/api/brand-rules'))
}

export async function updateBrandRules(payload: BrandRules): Promise<BrandRules> {
  return parseResponse<BrandRules>(await apiFetch('/api/brand-rules', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  }))
}

export async function updateBrand(
  brandId: string,
  payload: UpdateBrandPayload,
): Promise<BrandDetail> {
  const response = await apiFetch(`/api/brands/${encodeURIComponent(brandId)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  return parseResponse<BrandDetail>(response)
}

export async function deleteBrand(brandId: string): Promise<void> {
  const response = await apiFetch(`/api/brands/${encodeURIComponent(brandId)}`, {
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
  const response = await apiFetch(
    `/api/brands/${encodeURIComponent(brandId)}/references`,
    { method: 'POST', body: form },
  )
  return parseResponse<BrandDetail>(response)
}

export async function deleteBrandReference(
  brandId: string,
  assetId: string,
): Promise<BrandDetail> {
  const response = await apiFetch(
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
  const response = await apiFetch(
    `/api/brands/${encodeURIComponent(brandId)}/logos`,
    { method: 'POST', body: form },
  )
  return parseResponse<BrandDetail>(response)
}

export async function deleteBrandLogo(
  brandId: string,
  assetId: string,
): Promise<BrandDetail> {
  const response = await apiFetch(
    `/api/brands/${encodeURIComponent(brandId)}/logos/${encodeURIComponent(assetId)}`,
    { method: 'DELETE' },
  )
  return parseResponse<BrandDetail>(response)
}

export async function duplicateBrand(
  brandId: string,
  payload: { name?: string; platform_id?: string; placement_id?: string },
): Promise<BrandDetail> {
  const response = await apiFetch(`/api/brands/${encodeURIComponent(brandId)}/duplicate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  return parseResponse<BrandDetail>(response)
}

const MAX_UPLOAD_BYTES = 20_000_000

export async function uploadAsset(file: File): Promise<Asset> {
  if (file.size > MAX_UPLOAD_BYTES) {
    throw new ApiError(`文件超过 ${MAX_UPLOAD_BYTES / 1_000_000} MB 限制`, 'file_too_large')
  }
  const form = new FormData()
  form.append('file', file)
  const response = await apiFetch('/api/assets', { method: 'POST', body: form })
  return parseResponse<Asset>(response)
}

export async function deleteAsset(assetId: string, force = false): Promise<void> {
  const query = force ? '?force=true' : ''
  const response = await apiFetch(`/api/assets/${encodeURIComponent(assetId)}${query}`, {
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
  const response = await apiFetch('/api/whole-image/tasks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  return parseResponse<WholeImageTask>(response)
}

export async function listWholeImageTaskBatch(batchId: string): Promise<WholeImageTask[]> {
  const response = await apiFetch(`/api/whole-image/batches/${encodeURIComponent(batchId)}`)
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
  const response = await apiFetch(`/api/whole-image/tasks?${params.toString()}`)
  return parseResponse<WholeImageTaskGroupPage>(response)
}

export async function retryWholeImageTask(taskId: string): Promise<WholeImageTask> {
  const response = await apiFetch(
    `/api/whole-image/tasks/${encodeURIComponent(taskId)}/retry`,
    { method: 'POST' },
  )
  return parseResponse<WholeImageTask>(response)
}

export async function getWholeImageTask(taskId: string): Promise<WholeImageTask> {
  const response = await apiFetch(
    `/api/whole-image/tasks/${encodeURIComponent(taskId)}`,
  )
  return parseResponse<WholeImageTask>(response)
}

export async function createWholeImageEdit(
  taskId: string,
  instruction: string,
  sourceVersionId: string,
  sourceOutputId?: string | null,
  referenceAssetIds: string[] = [],
): Promise<WholeImageTask> {
  const response = await apiFetch(
    `/api/whole-image/tasks/${encodeURIComponent(taskId)}/edits`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        instruction,
        source_version_id: sourceVersionId,
        source_output_id: sourceOutputId ?? null,
        reference_asset_ids: referenceAssetIds,
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
  const response = await apiFetch(
    `/api/whole-image/tasks/${encodeURIComponent(taskId)}/versions/${encodeURIComponent(versionId)}/outputs/${encodeURIComponent(outputId)}/select`,
    { method: 'POST' },
  )
  return parseResponse<WholeImageTask>(response)
}

export async function listWholeImageVersions(
  taskId: string,
): Promise<WholeImageVersion[]> {
  const response = await apiFetch(
    `/api/whole-image/tasks/${encodeURIComponent(taskId)}/versions`,
  )
  return parseResponse<WholeImageVersion[]>(response)
}

export async function selectWholeImageVersion(
  taskId: string,
  versionId: string,
): Promise<WholeImageTask> {
  const response = await apiFetch(
    `/api/whole-image/tasks/${encodeURIComponent(taskId)}/versions/${encodeURIComponent(versionId)}/select`,
    { method: 'POST' },
  )
  return parseResponse<WholeImageTask>(response)
}

export async function listTaskCenterGroups(options: {
  status?: WholeImageTaskGroupFilter
  kind?: 'all' | 'generate' | 'original_replace'
  limit?: number
  cursor?: string | null
} = {}): Promise<WholeImageTaskGroupPage> {
  const params = new URLSearchParams({
    status: options.status ?? 'all',
    kind: options.kind ?? 'all',
    limit: String(options.limit ?? 24),
  })
  if (options.cursor) params.set('cursor', options.cursor)
  const response = await apiFetch(`/api/task-center?${params.toString()}`)
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
  const response = await apiFetch('/api/prompts/optimize', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  return parseResponse<{ prompt: string }>(response)
}
