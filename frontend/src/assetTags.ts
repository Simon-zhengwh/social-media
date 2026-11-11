/*
  Tag rules shared by the asset library UI and its tests.

  AM stores a whole tag array per media item and caps it at five entries, so the editor
  builds one complete set before saving instead of patching individual tags.
*/

export const MAX_ASSET_TAGS = 5

/**
 * Merge typed-but-uncommitted text into the saved set.
 *
 * Trims, drops blanks and de-duplicates case-insensitively while keeping the first spelling.
 * No cap is applied here so the caller can tell "too many" apart from "saved".
 */
export function normalizeTags(tags: string[], pending = ''): string[] {
  const merged = pending.trim() ? [...tags, pending] : tags
  const result: string[] = []
  for (const raw of merged) {
    const tag = raw.trim()
    if (!tag) continue
    if (result.some((item) => item.toLowerCase() === tag.toLowerCase())) continue
    result.push(tag)
  }
  return result
}
