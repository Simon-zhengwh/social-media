import type {
  Asset,
  AssetKind,
  BrandStyle,
  LayerPatch,
  Ratio,
  SampleImportResult,
  Task,
  VersionSummary,
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

export async function listBrands(): Promise<BrandStyle[]> {
  const response = await fetch('/api/brands')
  return parseResponse<BrandStyle[]>(response)
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
  logo_asset_id?: string | null
  brand_id?: string | null
  prompt?: string
  ratio?: Ratio
}

export async function deleteAsset(assetId: string): Promise<void> {
  const response = await fetch(`/api/assets/${encodeURIComponent(assetId)}`, {
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
