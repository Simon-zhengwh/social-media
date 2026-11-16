export const MAX_RECOGNITION_BATCH = 200

/**
 * How many photos one batch re-recognition request covers.
 *
 * The API rejects more than MAX_RECOGNITION_BATCH file ids per request, so a
 * partial batch is the smaller of the typed count and that limit; "全部" uses
 * every unsettled photo the server reports.
 */
export function recognitionBatchSize(value: string | number, total: number, all = false): number | null {
  if (!Number.isFinite(total) || total <= 0) return null
  if (all) return total
  const typed = typeof value === 'number' ? value : Number.parseInt(String(value).trim(), 10)
  if (!Number.isFinite(typed) || typed <= 0) return null
  return Math.min(typed, MAX_RECOGNITION_BATCH, total)
}
