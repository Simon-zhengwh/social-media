import type { AssetSearch } from '../api'
import { vocabularyGroups } from '../assetSearchVocabulary'

type Props = {
  search: AssetSearch | null
  className?: string
}

/**
 * The vocabulary of the submitted search, as the same chips a card uses for its tags:
 * solid for the original condition and its equivalents, dashed for expansions.
 */
export default function AssetSearchVocabulary({ search, className }: Props) {
  const groups = vocabularyGroups(search)
  if (groups.length === 0) {
    return null
  }
  return (
    <div className={className ? `asset-terms ${className}` : 'asset-terms'} aria-label="本次搜索使用的词表">
      {groups.map((group, index) => (
        <span className="asset-term-group" key={`${group.input}-${index}`}>
          {index > 0 ? <span className="asset-term-divider" aria-hidden="true" /> : null}
          {group.originals.map((term) => (
            <span
              className="asset-tag asset-term"
              key={`original-${term}`}
              title={term.toLowerCase() === group.input.toLowerCase() ? '输入条件' : '等价表达（按原词处理）'}
            >
              {term}
            </span>
          ))}
          {group.expansions.map((term) => (
            <span
              className="asset-tag asset-term is-expansion"
              key={`expansion-${term}`}
              title="扩展词（命中结果排在原词之后）"
            >
              {term}
            </span>
          ))}
        </span>
      ))}
    </div>
  )
}
