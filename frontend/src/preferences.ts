/*
  Browser-level preferences for the workspace.

  Two things are remembered on this origin only: which merchant was last opened (FB-001) and
  the poster form's last selection per merchant (FB-006). Nothing here is authoritative —
  the backend still validates access, and every value is re-checked against the live lists
  before use, so a stale record never blocks the page.
*/

import type { BrandMode, Ratio } from './types'

const MERCHANT_KEY = 'social-media:merchant:v1'
const POSTER_PREFIX = 'social-media:poster:v1:'

const BRAND_MODES: readonly BrandMode[] = ['preset', 'reference', 'none']
const RATIOS: readonly Ratio[] = ['1:1', '4:5', '9:16', '16:9', '3:4', '4:3']

/** localStorage can be disabled or throw; every access degrades to "no preference". */
function storage(): Storage | null {
  try {
    if (typeof window === 'undefined' || !window.localStorage) return null
    return window.localStorage
  } catch {
    return null
  }
}

function readKey(key: string): string | null {
  try {
    return storage()?.getItem(key) ?? null
  } catch {
    return null
  }
}

function writeKey(key: string, value: string): void {
  try {
    storage()?.setItem(key, value)
  } catch {
    // A full or disabled storage is not a reason to stop working.
  }
}

function removeKey(key: string): void {
  try {
    storage()?.removeItem(key)
  } catch {
    // Ignored on purpose.
  }
}

/** The merchant opened last on this browser, or '' when nothing usable is stored. */
export function readStoredMerchantId(): string {
  return (readKey(MERCHANT_KEY) ?? '').trim()
}

/** Only called after the merchant context was accepted by the backend. */
export function storeMerchantId(merchantId: string): void {
  const id = merchantId.trim()
  if (id) writeKey(MERCHANT_KEY, id)
}

/**
 * Drop a merchant that the backend explicitly rejected.
 *
 * Passing the rejected id keeps a stale link from wiping the record of another merchant
 * that the operator opened successfully before.
 */
export function clearStoredMerchantId(merchantId = ''): void {
  const current = readStoredMerchantId()
  if (merchantId && current && current !== merchantId) return
  removeKey(MERCHANT_KEY)
}

export type PosterPreference = {
  platform_id: string
  placement_id: string
  ratio: Ratio
  brand_mode: BrandMode
  brand_id: string
}

function posterKey(merchantId: string): string {
  return `${POSTER_PREFIX}${merchantId.trim() || '_'}`
}

/**
 * The last poster selection for this merchant.
 *
 * Fields keep their stored value only when they have a valid type: a corrupt or half-written
 * record falls back to the form defaults instead of breaking the page.
 */
export function readPosterPreference(merchantId: string): Partial<PosterPreference> | null {
  const raw = readKey(posterKey(merchantId))
  if (!raw) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return null
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null
  const record = parsed as Record<string, unknown>
  const result: Partial<PosterPreference> = {}
  if (typeof record.platform_id === 'string') result.platform_id = record.platform_id
  if (typeof record.placement_id === 'string') result.placement_id = record.placement_id
  if (typeof record.ratio === 'string' && RATIOS.includes(record.ratio as Ratio)) {
    result.ratio = record.ratio as Ratio
  }
  if (typeof record.brand_mode === 'string' && BRAND_MODES.includes(record.brand_mode as BrandMode)) {
    result.brand_mode = record.brand_mode as BrandMode
  }
  if (typeof record.brand_id === 'string') result.brand_id = record.brand_id
  return Object.keys(result).length ? result : null
}

export function savePosterPreference(merchantId: string, value: PosterPreference): void {
  if (!merchantId.trim()) return
  writeKey(posterKey(merchantId), JSON.stringify(value))
}
