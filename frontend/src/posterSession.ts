import type { Asset, BrandMode, ImageModel, PosterSeriesMode, Ratio } from './types'
import type { StoreSelection } from './components/StoreInfoSelector'
import type { ModelCounts } from './modelGeneration'
import { IMAGE_MODEL_OPTIONS } from './imageModels.ts'

export type PosterDraftForm = {
  products: Asset[]
  prompt: string
  promptOptimized: boolean
  selectedPlatformId: string
  selectedPlacementId: string
  selectedRatio: Ratio
  selectedBrandId: string
  brandMode: BrandMode
  selectedLogoId: string
  logoChoiceTouched: boolean
  styleReferences: Asset[]
  storeSelection?: StoreSelection
  selectedModel: ImageModel
  modelCounts: ModelCounts
  /** Opt-in poster series choice. Absent means the historical single-poster form. */
  mode?: PosterSeriesMode
}

export type PosterSession =
  | { kind: 'blank'; workspaceId: string; title: string; mode?: PosterSeriesMode }
  | { kind: 'draft'; workspaceId: string; title?: string; form: PosterDraftForm }
  | {
    kind: 'task'
    workspaceId: string
    taskId: string | null
    selectedModel: ImageModel
    /** Series tasks also remember the current group/page so a reload lands on the same page. */
    mode?: PosterSeriesMode
    seriesGroupId?: string | null
    seriesPageId?: string | null
  }

export function formatPosterTitle(value: Date | string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '未命名海报'
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  const time = [date.getHours(), date.getMinutes(), date.getSeconds()]
    .map(part => String(part).padStart(2, '0')).join(':')
  return `${date.getFullYear()}-${month}-${day} ${time}`
}

export function newPosterDesignUrl(currentUrl: string): string {
  const url = new URL(currentUrl)
  const merchantId = url.searchParams.get('merchant_id')
  url.search = ''
  url.hash = ''
  if (merchantId) url.searchParams.set('merchant_id', merchantId)
  return url.toString()
}

function sessionKey(userId: string, merchantId: string): string | null {
  if (!userId?.trim() || !merchantId?.trim()) return null
  return `social-media:poster-session:v1:${encodeURIComponent(userId)}:${encodeURIComponent(merchantId)}`
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function isModel(value: unknown): boolean {
  return IMAGE_MODEL_OPTIONS.some(option => option.value === value)
}

function isOptionalMode(value: unknown): boolean {
  return value === undefined || value === 'single' || value === 'series'
}

function isOptionalId(value: unknown): boolean {
  return value === undefined || value === null || (typeof value === 'string' && Boolean(value))
}

function isAsset(value: unknown): boolean {
  return isRecord(value) && ['id', 'original_name', 'url', 'mime_type', 'created_at'].every(key => typeof value[key] === 'string') &&
    ['width', 'height'].every(key => typeof value[key] === 'number' && Number.isFinite(value[key]))
}

function isDraftForm(value: unknown): boolean {
  if (!isRecord(value)) return false
  const store = value.storeSelection
  const counts = value.modelCounts
  return ['prompt', 'selectedPlatformId', 'selectedPlacementId', 'selectedBrandId', 'selectedLogoId'].every(key => typeof value[key] === 'string') &&
    ['promptOptimized', 'logoChoiceTouched'].every(key => typeof value[key] === 'boolean') &&
    ['1:1', '9:16', '4:5', '16:9', '3:4', '4:3'].includes(String(value.selectedRatio)) &&
    ['preset', 'reference', 'none'].includes(String(value.brandMode)) && isModel(value.selectedModel) &&
    Array.isArray(value.products) && value.products.length <= 6 && value.products.every(isAsset) &&
    Array.isArray(value.styleReferences) && value.styleReferences.length <= 3 && value.styleReferences.every(isAsset) &&
    (store === undefined || (isRecord(store) && typeof store.placeId === 'string' &&
    ['enabled', 'address', 'phone'].every(key => typeof store[key] === 'boolean'))) &&
    isRecord(counts) && Object.entries(counts).every(([model, count]) =>
      isModel(model) && typeof count === 'number' && Number.isInteger(count) && count >= 0 && count <= 3) &&
    isOptionalMode(value.mode)
}

/** Temporary editing state belongs to this tab and authenticated operator, never localStorage. */
export function readPosterSession(userId: string, merchantId: string): PosterSession | null {
  const key = sessionKey(userId, merchantId)
  if (!key) return null
  try {
    const raw = window.sessionStorage.getItem(key)
    const value: unknown = raw ? JSON.parse(raw) : null
    if (!isRecord(value) || typeof value.workspaceId !== 'string' || !value.workspaceId) return null
    const hasTitle = typeof value.title === 'string' && Boolean(value.title.trim()) && value.title.length <= 100
    if (value.kind === 'blank' && hasTitle && isOptionalMode(value.mode)) return value as PosterSession
    if (value.kind === 'draft' && isDraftForm(value.form) && (value.title === undefined || hasTitle)) {
      return value as PosterSession
    }
    const taskModel = isModel(value.selectedModel) || value.selectedModel === 'gemini-3.1-flash-image' || value.selectedModel === 'minimax-h3-image'
    if (value.kind === 'task' && (value.taskId === null || (typeof value.taskId === 'string' && Boolean(value.taskId))) && taskModel &&
      isOptionalMode(value.mode) && isOptionalId(value.seriesGroupId) && isOptionalId(value.seriesPageId)) {
      return value as PosterSession
    }
  } catch { /* Storage may be disabled, full or left with an incompatible record. */ }
  return null
}

export function savePosterSession(userId: string, merchantId: string, session: PosterSession): boolean {
  const key = sessionKey(userId, merchantId)
  if (!key) return false
  try {
    window.sessionStorage.setItem(key, JSON.stringify(session))
    return true
  } catch {
    return false
  }
}

export function clearPosterSession(userId: string, merchantId: string): boolean {
  const key = sessionKey(userId, merchantId)
  if (!key) return false
  try {
    window.sessionStorage.removeItem(key)
    return true
  } catch {
    return false
  }
}
