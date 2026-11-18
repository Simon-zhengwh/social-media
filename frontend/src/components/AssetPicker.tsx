import { useCallback, useEffect, useRef, useState } from 'react'
import { createAssetSearch, listAssetPage, type AssetPage, type AssetSearch } from '../api'
import type { Asset } from '../types'
import { assetDisplayName } from '../assetNames'
import AssetSearchVocabulary from './AssetSearchVocabulary'

/** Rows that fill a wide dialog without making the first paint slow. */
const PAGE_SIZE = 36
/** Start the next page before the operator reaches the very last row. */
const LOAD_MORE_THRESHOLD = 200

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
  /** `null` leaves the count open, so the operator decides how many to pick. */
  max?: number | null
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
  max = null,
  title: customTitle,
  onClose,
  onSelect,
  onConfirm,
  onGoAssets,
}: Props) {
  const [selected, setSelected] = useState<string[]>(() => selectedIds)
  // The draft only becomes the query on submit, so typing does not refetch per keystroke.
  const [draft, setDraft] = useState('')
  const [query, setQuery] = useState('')
  // The submitted search owns the vocabulary; a page never asks the model for it again.
  const [search, setSearch] = useState<AssetSearch | null>(null)
  const [semantic, setSemantic] = useState(true)
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

  const loadPage = useCallback((nextQuery: string, searchId: string, skip: number) => {
    const requestId = requestRef.current + 1
    requestRef.current = requestId
    void listAssetPage(nextQuery, skip, PAGE_SIZE, '', searchId)
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
    loadPage(query, search?.id ?? '', 0)
  }, [open, query, search?.id, reloadToken, loadPage])

  if (!open) {
    return null
  }

  const title = customTitle ?? (purpose === 'product' ? '选择素材图' : '选择品牌 Logo')
  const emptyCopy = purpose === 'product' ? '资产管理中还没有可用素材图' : '资产管理中还没有可用图片'
  const searching = Boolean(query)
  const hasMore = loaded < total
  const listLoading = phase === 'loading'
  const loadingMore = phase === 'more'

  /** Submit one search: this is the only moment the vocabulary may call a model.
   *  The grid keeps its current rows until the answer lands, so a failed submit leaves the
   *  previous list readable instead of an empty dialog. */
  const startQuery = (next: string, useSemantic: boolean) => {
    if (listLoading) return
    setListError(null)
    setPhase('loading')
    void createAssetSearch(next, useSemantic)
      .then((submitted) => {
        setSearch(submitted)
        setQuery(next)
      })
      .catch((reason: unknown) => {
        setListError(reason instanceof Error ? reason.message : '搜索失败，请稍后重试')
        setPhase('idle')
      })
  }

  const loadMore = () => {
    if (phase !== 'idle' || !hasMore) return
    setPhase('more')
    loadPage(query, search?.id ?? '', loaded)
  }

  const toggle = (assetId: string) => {
    setSelected((previous) => {
      if (previous.includes(assetId)) {
        return previous.filter((id) => id !== assetId)
      }
      if (max !== null && previous.length >= max) {
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
        className="modal modal-picker"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <form
          className="picker-search"
          onSubmit={(event) => {
            event.preventDefault()
            startQuery(draft.trim(), semantic)
          }}
        >
          <div className="asset-search-group">
            <input
              className="mp-input asset-search-input"
              type="search"
              value={draft}
              aria-label="搜索素材名称、Tag 或关键词"
              placeholder="搜索名称、Tag 或关键词"
              onChange={(event) => setDraft(event.target.value)}
            />
            <button type="submit" className="mp-btn-secondary mp-btn-small" disabled={listLoading} aria-busy={listLoading}>
              {listLoading ? <span className="spinner spinner-dark" aria-hidden="true" /> : null}
              搜索
            </button>
            <label className="asset-semantic-toggle">
              <input
                type="checkbox"
                checked={semantic}
                onChange={(event) => setSemantic(event.target.checked)}
              />
              <span>语义搜索</span>
            </label>
            {searching ? (
              <button
                type="button"
                className="mp-btn-ghost mp-btn-small"
                disabled={listLoading}
                onClick={() => {
                  setDraft('')
                  startQuery('', false)
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

        <AssetSearchVocabulary search={search} className="picker-terms" />

        {search?.degraded ? (
          <div className="asset-notice">搜索词生成失败，已按原词搜索。</div>
        ) : null}

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
                  <img src={asset.url} alt={assetDisplayName(asset.original_name)} loading="lazy" />
                  {checked ? <span className="picker-check">✓</span> : null}
                  <span className="picker-name" title={assetDisplayName(asset.original_name)}>
                    {assetDisplayName(asset.original_name)}
                  </span>
                  {(asset.tags ?? []).length > 0 ? (
                    <span className="picker-tags">
                      {(asset.tags ?? []).map((tag) => (
                        <span className="picker-tag" key={tag} title={tag}>
                          {tag}
                        </span>
                      ))}
                    </span>
                  ) : null}
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

        <div className="picker-footer">
          <div className="picker-footer-left">
            <button type="button" className="mp-btn-ghost mp-btn-small" onClick={onClose}>
              关闭
            </button>
            {multi ? (
              <span>
                已选 {selected.length}{max === null ? ' 张' : ` / ${max}`}
              </span>
            ) : null}
          </div>
          {multi ? (
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
          ) : null}
        </div>
      </div>
    </div>
  )
}
