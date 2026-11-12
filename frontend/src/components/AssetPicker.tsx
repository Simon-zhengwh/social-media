import { useCallback, useEffect, useRef, useState } from 'react'
import { listAssetPage, type AssetPage } from '../api'
import type { Asset } from '../types'

/** Three full rows on a wide dialog; small enough that the first page paints fast. */
const PAGE_SIZE = 24
/** Start the next page before the operator reaches the very last row. */
const LOAD_MORE_THRESHOLD = 200

type AssetQuery = { keyword: string; tag: string }
/** `loading` is the first page of a filter, `more` an appended page, `idle` nothing in flight. */
type Phase = 'idle' | 'loading' | 'more'

type Props = {
  open: boolean
  purpose: 'product' | 'logo'
  /**
   * Assets the parent already holds (e.g. the current selection).
   * They keep a selection resolvable while a search narrows the visible list.
   */
  assets: Asset[]
  /** Kept so callers can still report their own asset request; the picker loads its own pages. */
  loading?: boolean
  multi?: boolean
  selectedIds?: string[]
  max?: number
  title?: string
  onClose: () => void
  onSelect?: (asset: Asset) => void
  onConfirm?: (assets: Asset[]) => void
  onGoAssets: () => void
}

export default function AssetPicker({
  open,
  purpose,
  assets,
  multi = false,
  selectedIds = [],
  max = 6,
  title: customTitle,
  onClose,
  onSelect,
  onConfirm,
  onGoAssets,
}: Props) {
  const [selected, setSelected] = useState<string[]>(() => selectedIds)
  // Drafts only become the query on submit, so typing does not refetch per keystroke.
  const [keywordDraft, setKeywordDraft] = useState('')
  const [tagDraft, setTagDraft] = useState('')
  const [query, setQuery] = useState<AssetQuery>({ keyword: '', tag: '' })
  const [items, setItems] = useState<Asset[]>([])
  // `loaded` counts rows the server returned, which is also the next skip.
  const [loaded, setLoaded] = useState(0)
  const [total, setTotal] = useState(0)
  const [phase, setPhase] = useState<Phase>('loading')
  const [listError, setListError] = useState<string | null>(null)
  const [reloadToken, setReloadToken] = useState(0)
  const requestRef = useRef(0)
  // Ids stay selected across searches, so remembered records answer `confirm`.
  const knownRef = useRef<Map<string, Asset>>(new Map())

  /** Fold a server page into the grid; `skip` tells a fresh page from an appended one. */
  const applyPage = useCallback((page: AssetPage, skip: number) => {
    for (const asset of page.items) {
      knownRef.current.set(asset.id, asset)
    }
    setItems((previous) => {
      if (skip === 0) return page.items
      const seen = new Set(previous.map((asset) => asset.id))
      return [...previous, ...page.items.filter((asset) => !seen.has(asset.id))]
    })
    setLoaded(skip + page.items.length)
    setTotal(page.total)
    setListError(null)
    setPhase('idle')
  }, [])

  useEffect(() => {
    for (const asset of assets) {
      knownRef.current.set(asset.id, asset)
    }
  }, [assets])

  useEffect(() => {
    if (!open) {
      return
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onClose()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [open, onClose])

  const loadPage = useCallback((nextQuery: AssetQuery, skip: number) => {
    const requestId = requestRef.current + 1
    requestRef.current = requestId
    void listAssetPage(nextQuery.keyword, skip, PAGE_SIZE, nextQuery.tag)
      .then((page) => {
        if (requestId !== requestRef.current) return
        applyPage(page, skip)
      })
      .catch((reason: unknown) => {
        if (requestId !== requestRef.current) return
        setListError(reason instanceof Error ? reason.message : '素材加载失败，请稍后重试')
        setPhase('idle')
      })
  }, [applyPage])

  useEffect(() => {
    if (!open) return
    loadPage(query, 0)
  }, [open, query, reloadToken, loadPage])

  if (!open) {
    return null
  }

  const title = customTitle ?? (purpose === 'product' ? '选择素材图' : '选择品牌 Logo')
  const emptyCopy = purpose === 'product' ? '资产管理中还没有可用素材图' : '资产管理中还没有可用图片'
  const searching = Boolean(query.keyword || query.tag)
  const hasMore = loaded < total
  const listLoading = phase === 'loading'
  const loadingMore = phase === 'more'

  /** Reset the list for a new filter: the first page replaces whatever was on screen. */
  const startQuery = (next: AssetQuery) => {
    setItems([])
    setLoaded(0)
    setTotal(0)
    setListError(null)
    setPhase('loading')
    setQuery(next)
  }

  const loadMore = () => {
    if (phase !== 'idle' || !hasMore) return
    setPhase('more')
    loadPage(query, loaded)
  }

  const toggle = (assetId: string) => {
    setSelected((previous) => {
      if (previous.includes(assetId)) {
        return previous.filter((id) => id !== assetId)
      }
      if (previous.length >= max) {
        return previous
      }
      return [...previous, assetId]
    })
  }

  const confirm = () => {
    const ordered = selected
      .map((id) => knownRef.current.get(id))
      .filter((asset): asset is Asset => Boolean(asset))
    onConfirm?.(ordered)
  }

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="modal-head">
          <div>
            <span className="eyebrow">资产选择</span>
            <h2>{multi ? `${title}（可多选）` : title}</h2>
            <p>
              {multi
                ? `最多选择 ${max} 张，由 AI 自由组合。`
                : purpose === 'logo'
                  ? '从资产管理中选择一张图片作为 Logo。'
                  : '从资产管理中选择一张素材。'}
            </p>
          </div>
          <button type="button" className="modal-close" onClick={onClose}>
            关闭
          </button>
        </div>

        <form
          className="picker-search"
          onSubmit={(event) => {
            event.preventDefault()
            startQuery({ keyword: keywordDraft.trim(), tag: tagDraft.trim() })
          }}
        >
          <div className="asset-search-group">
            <input
              className="mp-input asset-search-input"
              type="search"
              value={keywordDraft}
              aria-label="搜索素材文件名"
              placeholder="搜索素材文件名"
              onChange={(event) => setKeywordDraft(event.target.value)}
            />
            <input
              className="mp-input asset-search-input"
              type="search"
              value={tagDraft}
              aria-label="按 Tag 搜索"
              placeholder="按 Tag 搜索"
              onChange={(event) => setTagDraft(event.target.value)}
            />
            <button type="submit" className="mp-btn-secondary mp-btn-small">
              搜索
            </button>
            {searching ? (
              <button
                type="button"
                className="mp-btn-ghost mp-btn-small"
                onClick={() => {
                  setKeywordDraft('')
                  setTagDraft('')
                  startQuery({ keyword: '', tag: '' })
                }}
              >
                清空
              </button>
            ) : null}
          </div>
          <span className="picker-search-count">
            {phase === 'idle' ? `共 ${total} 张` : '加载中…'}
          </span>
        </form>

        {listError ? (
          <div className="picker-notice" role="alert">
            <span>{listError}</span>
            <button
              type="button"
              className="mp-btn-ghost mp-btn-small"
              onClick={() => {
                setListError(null)
                setPhase('loading')
                setReloadToken((current) => current + 1)
              }}
            >
              重试
            </button>
          </div>
        ) : null}

        {listLoading && items.length === 0 ? (
          <div className="picker-grid">
            {[0, 1, 2, 3].map((item) => (
              <span className="picker-skeleton" key={item} />
            ))}
          </div>
        ) : items.length === 0 ? (
          <div className="picker-empty">
            <strong>{searching ? '没有匹配的素材' : emptyCopy}</strong>
            <span>{searching ? '清空或更换搜索词后重试。' : '上传图片后，再回来选择。'}</span>
            {searching ? null : (
              <button type="button" className="mp-btn-secondary mp-btn-small" onClick={onGoAssets}>
                去资产管理
              </button>
            )}
          </div>
        ) : (
          <div
            className="picker-grid"
            onScroll={(event) => {
              const grid = event.currentTarget
              if (phase !== 'idle' || !hasMore) return
              if (grid.scrollTop + grid.clientHeight < grid.scrollHeight - LOAD_MORE_THRESHOLD) return
              loadMore()
            }}
          >
            {items.map((asset) => {
              const checked = multi && selected.includes(asset.id)
              return (
                <button
                  type="button"
                  className={`picker-card${checked ? ' is-selected' : ''}`}
                  key={asset.id}
                  onClick={() => {
                    if (multi) {
                      toggle(asset.id)
                    } else {
                      onSelect?.(asset)
                    }
                  }}
                >
                  <img src={asset.url} alt={asset.original_name} loading="lazy" />
                  {checked ? <span className="picker-check">✓</span> : null}
                  <span className="picker-name" title={asset.original_name}>
                    {asset.original_name}
                  </span>
                  <span className="picker-meta">
                    {asset.width} × {asset.height}
                  </span>
                </button>
              )
            })}
            {hasMore ? (
              <div className="picker-more">
                <button
                  type="button"
                  className="mp-btn-secondary mp-btn-small"
                  disabled={loadingMore}
                  onClick={loadMore}
                >
                  {loadingMore ? '加载中…' : `加载更多（已显示 ${loaded} / ${total}）`}
                </button>
              </div>
            ) : null}
          </div>
        )}

        {multi ? (
          <div className="picker-footer">
            <span>
              已选 {selected.length} / {max}
            </span>
            <div className="picker-footer-actions">
              <button
                type="button"
                className="mp-btn-ghost mp-btn-small"
                onClick={() => setSelected([])}
              >
                清空
              </button>
              <button
                type="button"
                className="mp-btn-primary mp-btn-small"
                onClick={confirm}
                disabled={selected.length === 0}
              >
                确定
              </button>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  )
}
