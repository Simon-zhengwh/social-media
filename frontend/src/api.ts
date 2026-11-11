import type {
  Asset,
  OriginalReplacementItem,
  OriginalReplacementTask,
  BrandDetail,
  BrandLogoImage,
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
  PosterEditBatch,
  PosterEditTask,
  MaterialFilterDefaults,
  MaterialFilterEvaluation,
  MaterialFilterFolder,
  MaterialFilterFolderCreateResult,
  MaterialFilterResultsPage,
  MaterialFilterRun,
  MaterialFilterScheme,
} from './types'


export type LocalDevProbe = {
  /** Merchant to open when the backend runs with AM_BYPASS_LOCAL, else empty. */
  merchantId: string
  /** True when /api/health could not be reached at all, usually a stopped backend. */
  backendUnreachable: boolean
}

/**
 * Ask the backend which merchant local dev should open.
 *
 * Only a server started with AM_BYPASS_LOCAL answers with an id, so a deployed
 * build keeps asking the operator to come in through AM Console. The reachability
 * flag lets the caller tell "backend stopped" apart from "no bypass here".
 */
export async function probeLocalDevMerchant(): Promise<LocalDevProbe> {
  try {
    const response = await fetch('/api/health')
    if (!response.ok && response.status !== 503) {
      return { merchantId: '', backendUnreachable: false }
    }
    const payload = (await response.json()) as { local_merchant_id?: unknown }
    const value = payload.local_merchant_id
    return {
      merchantId: typeof value === 'string' ? value.trim() : '',
      backendUnreachable: false,
    }
  } catch {
    return { merchantId: '', backendUnreachable: true }
  }
}

export function merchantIdFromLocation(): string {
  return new URLSearchParams(window.location.search).get('merchant_id')?.trim() ?? ''
}

export function merchantPath(path: string): string {
  if (
    path === '/api/platforms' ||
    path === '/api/health' ||
    // The merchant list has no merchant yet: it is what picks one.
    path === '/api/merchants' ||
    path.startsWith('/api/merchants/') ||
    path.startsWith('/api/merchants?')
  ) {
    return path
  }
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
  /** HTTP status when the failure came from a response; 0 for local validation errors. */
  status: number

  constructor(message: string, code = 'request_failed', status = 0) {
    super(message)
    this.code = code
    this.status = status
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
    throw new ApiError(message, body?.error?.code ?? `http_${response.status}`, response.status)
  }
  return payload as T
}

export type AssetPage = { items: Asset[]; total: number; skip: number; limit: number }

export async function listAssetPage(
  keyword = '',
  skip = 0,
  limit = 24,
  tag = '',
): Promise<AssetPage> {
  const params = new URLSearchParams({ keyword, skip: String(skip), limit: String(limit), tag })
  const response = await apiFetch(`/api/am-assets?${params.toString()}`)
  return parseResponse<AssetPage>(response)
}

export async function listAssets(): Promise<Asset[]> {
  const page = await listAssetPage('', 0, 100)
  return page.items
}

export type MerchantFeatures = {
  /** True when AM Console's media library is reachable, so uploads are real AM resources. */
  am_media_library?: boolean
  maintenance_cli?: boolean
  /** Agent benchmark pages are a local-dev tool and stay hidden on deployed sites. */
  agent_tests?: boolean
  /** SMB material filtering reads this machine's folders, so it is local-only. */
  material_filters?: boolean
}

export type MerchantContext = {
  merchant: { user_id: string; user_name: string; merchant_id: string; merchant_name: string }
  profile: unknown
  features?: MerchantFeatures
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

export type BrandLogoSet = { logo_asset_ids: string[]; logo_images: BrandLogoImage[] }

export async function getBrandLogos(): Promise<BrandLogoSet> {
  return parseResponse<BrandLogoSet>(await apiFetch('/api/brand-logos'))
}

export async function uploadBrandLogos(files: File[]): Promise<BrandLogoSet> {
  const form = new FormData()
  files.forEach((file) => form.append('files', file))
  const response = await apiFetch('/api/brand-logos', { method: 'POST', body: form })
  return parseResponse<BrandLogoSet>(response)
}

export async function deleteBrandLogo(assetId: string): Promise<BrandLogoSet> {
  const response = await apiFetch(
    `/api/brand-logos/${encodeURIComponent(assetId)}`,
    { method: 'DELETE' },
  )
  return parseResponse<BrandLogoSet>(response)
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


export async function updateAssetTags(assetId: string, tags: string[]): Promise<Asset> {
  const response = await apiFetch(`/api/assets/${encodeURIComponent(assetId)}/tags`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tags }),
  })
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
  include_store_address?: boolean
  include_store_phone?: boolean
  store_place_id?: string | null
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
  include_store_address?: boolean
  include_store_phone?: boolean
  store_place_id?: string | null
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

export async function getPosterEdit(taskId: string): Promise<PosterEditTask> {
 const response = await fetch(`/api/poster-edits/${encodeURIComponent(taskId)}`)
 return parseResponse<PosterEditTask>(response)
}


/* --- Local SMB material filtering ---------------------------------------- */

export type MaterialFilterSchemePayload = {
  name: string
  rules?: Array<{ id: string; text: string; interpretation: string }>
  recursive?: boolean
  output_root?: string
  model?: string
}

export async function listMaterialFilterFolders(): Promise<MaterialFilterFolder[]> {
  const page = await apiFetch('/api/material-filters/folders')
  const body = await parseResponse<{ items: MaterialFilterFolder[] }>(page)
  return body.items ?? []
}

export async function addMaterialFilterFolder(input: string): Promise<MaterialFilterFolderCreateResult> {
  return parseResponse<MaterialFilterFolderCreateResult>(await apiFetch('/api/material-filters/folders', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ input }),
  }))
}

export async function deleteMaterialFilterFolder(
  folderId: string,
  purgeLocal = false,
): Promise<{ deleted: string; schemes: number; evaluations: number }> {
  const query = purgeLocal ? '?purge_local=true' : ''
  return parseResponse<{ deleted: string; schemes: number; evaluations: number }>(await apiFetch(
    `/api/material-filters/folders/${encodeURIComponent(folderId)}${query}`,
    { method: 'DELETE' },
  ))
}

export async function renameMaterialFilterFolder(
  folderId: string,
  displayName: string,
): Promise<MaterialFilterFolder> {
  return parseResponse<MaterialFilterFolder>(await apiFetch(
    `/api/material-filters/folders/${encodeURIComponent(folderId)}`,
    { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ display_name: displayName }) },
  ))
}

export async function listMaterialFilterSchemes(folderId: string): Promise<MaterialFilterScheme[]> {
  const body = await parseResponse<{ items: MaterialFilterScheme[] }>(await apiFetch(
    `/api/material-filters/folders/${encodeURIComponent(folderId)}/schemes`,
  ))
  return body.items ?? []
}

export async function createMaterialFilterScheme(
  folderId: string,
  payload: MaterialFilterSchemePayload,
): Promise<MaterialFilterScheme> {
  return parseResponse<MaterialFilterScheme>(await apiFetch(
    `/api/material-filters/folders/${encodeURIComponent(folderId)}/schemes`,
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) },
  ))
}

export async function getMaterialFilterScheme(schemeId: string): Promise<MaterialFilterScheme> {
  return parseResponse<MaterialFilterScheme>(await apiFetch(
    `/api/material-filters/schemes/${encodeURIComponent(schemeId)}`,
  ))
}

export async function cloneMaterialFilterScheme(
  schemeId: string,
  payload: { name?: string; rules?: MaterialFilterSchemePayload['rules']; recursive?: boolean },
): Promise<MaterialFilterScheme> {
  return parseResponse<MaterialFilterScheme>(await apiFetch(
    `/api/material-filters/schemes/${encodeURIComponent(schemeId)}/clone`,
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) },
  ))
}

export async function getMaterialFilterDefaults(): Promise<MaterialFilterDefaults> {
  return parseResponse<MaterialFilterDefaults>(await apiFetch('/api/material-filters/defaults'))
}

export async function startMaterialFilterRun(
  schemeId: string,
  requestId: string,
  limit?: number,
): Promise<MaterialFilterRun> {
  return parseResponse<MaterialFilterRun>(await apiFetch(
    `/api/material-filters/schemes/${encodeURIComponent(schemeId)}/runs`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(limit ? { request_id: requestId, limit } : { request_id: requestId }),
    },
  ))
}

export async function getMaterialFilterRun(runId: string): Promise<MaterialFilterRun> {
  return parseResponse<MaterialFilterRun>(await apiFetch(
    `/api/material-filters/runs/${encodeURIComponent(runId)}`,
  ))
}

export async function resumeMaterialFilterRun(runId: string): Promise<MaterialFilterRun> {
  return parseResponse<MaterialFilterRun>(await apiFetch(
    `/api/material-filters/runs/${encodeURIComponent(runId)}/resume`,
    { method: 'POST' },
  ))
}

export async function retryMaterialFilterFailed(runId: string): Promise<MaterialFilterRun> {
  return parseResponse<MaterialFilterRun>(await apiFetch(
    `/api/material-filters/runs/${encodeURIComponent(runId)}/retry-failed`,
    { method: 'POST' },
  ))
}

export type MaterialFilterSort = 'judged' | 'name' | 'path'

export async function listMaterialFilterResults(
  schemeId: string,
  options: { status?: string; q?: string; skip?: number; limit?: number; sort?: MaterialFilterSort } = {},
): Promise<MaterialFilterResultsPage> {
  const params = new URLSearchParams({
    status: options.status ?? 'all',
    q: options.q ?? '',
    skip: String(options.skip ?? 0),
    limit: String(options.limit ?? 24),
    sort: options.sort ?? 'judged',
  })
  return parseResponse<MaterialFilterResultsPage>(await apiFetch(
    `/api/material-filters/schemes/${encodeURIComponent(schemeId)}/results?${params.toString()}`,
  ))
}

export async function updateMaterialFilterMetadata(
  evaluationId: string,
  payload: { display_name?: string; tags?: string[] },
): Promise<MaterialFilterEvaluation> {
  return parseResponse<MaterialFilterEvaluation>(await apiFetch(
    `/api/material-filters/evaluations/${encodeURIComponent(evaluationId)}/metadata`,
    { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) },
  ))
}

export async function setMaterialFilterPick(
  evaluationId: string,
  picked: boolean,
): Promise<MaterialFilterEvaluation> {
  return parseResponse<MaterialFilterEvaluation>(await apiFetch(
    `/api/material-filters/evaluations/${encodeURIComponent(evaluationId)}/pick`,
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ picked }) },
  ))
}

export async function bulkPickMaterialFilter(
  schemeId: string,
  ids: string[],
  picked: boolean,
): Promise<{ updated: number; picked: boolean }> {
  return parseResponse<{ updated: number; picked: boolean }>(await apiFetch(
    `/api/material-filters/schemes/${encodeURIComponent(schemeId)}/picks`,
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids, picked }) },
  ))
}

export async function reviewMaterialFilterEvaluation(
  evaluationId: string,
  payload: { decision: 'pass' | 'fail'; source_sha256: string; request_id: string },
): Promise<MaterialFilterEvaluation> {
  return parseResponse<MaterialFilterEvaluation>(await apiFetch(
    `/api/material-filters/evaluations/${encodeURIComponent(evaluationId)}/review`,
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) },
  ))
}

export async function recheckMaterialFilterEvaluation(
  evaluationId: string,
): Promise<MaterialFilterEvaluation> {
  return parseResponse<MaterialFilterEvaluation>(await apiFetch(
    `/api/material-filters/evaluations/${encodeURIComponent(evaluationId)}/recheck`,
    { method: 'POST' },
  ))
}

export async function exportPickedMaterialFilter(
  schemeId: string,
): Promise<{ directory: string; count: number; manifest: string }> {
  return parseResponse<{ directory: string; count: number; manifest: string }>(await apiFetch(
    `/api/material-filters/schemes/${encodeURIComponent(schemeId)}/export-picked`,
    { method: 'POST' },
  ))
}

/** Absolute CSV download for the current merchant; the browser opens it directly. */
export function materialFilterExportPath(schemeId: string): string {
  return `/api/material-filters/schemes/${encodeURIComponent(schemeId)}/export`
}
