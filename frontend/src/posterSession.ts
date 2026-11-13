import type { Asset, BrandMode, ImageModel, Ratio } from './types'
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
  storeSelection: StoreSelection
  selectedModel: ImageModel
  modelCounts: ModelCounts
}

export type PosterSession =
  | { kind: 'draft'; workspaceId: string; form: PosterDraftForm }
  | { kind: 'task'; workspaceId: string; taskId: string | null; selectedModel: ImageModel }

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
    isRecord(store) && typeof store.placeId === 'string' &&
    ['enabled', 'address', 'phone'].every(key => typeof store[key] === 'boolean') &&
    isRecord(counts) && Object.entries(counts).every(([model, count]) =>
      isModel(model) && typeof count === 'number' && Number.isInteger(count) && count >= 0 && count <= 3)
}

/** Temporary editing state belongs to this tab and authenticated operator, never localStorage. */
export function readPosterSession(userId: string, merchantId: string): PosterSession | null {
  const key = sessionKey(userId, merchantId)
  if (!key) return null
  try {
    const raw = window.sessionStorage.getItem(key)
    const value: unknown = raw ? JSON.parse(raw) : null
    if (!isRecord(value) || typeof value.workspaceId !== 'string' || !value.workspaceId) return null
    if (value.kind === 'draft' && isDraftForm(value.form)) return value as PosterSession
    const taskModel = isModel(value.selectedModel) || value.selectedModel === 'gemini-3.1-flash-image' || value.selectedModel === 'minimax-h3-image'
    if (value.kind === 'task' && (value.taskId === null || (typeof value.taskId === 'string' && Boolean(value.taskId))) && taskModel) {
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
