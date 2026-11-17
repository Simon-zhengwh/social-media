import type { AssetSearch } from './api'

/** One submitted search, split into the terms that count as the original condition and the
 *  expansions that only rank below them. Identical surface forms collapse into one chip. */
export type VocabularyGroup = { input: string; originals: string[]; expansions: string[] }

function unique(terms: string[]): string[] {
  return terms
    .map((term) => term.trim())
    .filter(
      (term, index, all) =>
        Boolean(term) && all.findIndex((other) => other.trim().toLowerCase() === term.toLowerCase()) === index,
    )
}

export function vocabularyGroups(search: AssetSearch | null): VocabularyGroup[] {
  // A literal search only repeats the typed words: showing them would be noise.
  if (!search || search.mode !== 'semantic' || search.degraded) return []
  return search.groups
    .map((group) => {
      const originals = unique([
        group.input,
        group.equivalents.simplified,
        group.equivalents.traditional,
        group.equivalents.english,
      ])
      const expansions = unique(group.expansions).filter(
        (term) => !originals.some((original) => original.toLowerCase() === term.toLowerCase()),
      )
      return { input: group.input, originals, expansions }
    })
    .filter((group) => group.originals.length > 1 || group.expansions.length > 0)
}
