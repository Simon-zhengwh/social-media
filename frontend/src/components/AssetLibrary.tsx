import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, deleteAssets, renameAsset, updateAssetTags, type AssetIndexSummary, type AssetSearch } from '../api'
import { MAX_ASSET_TAGS, normalizeTags } from '../assetTags'
import { assetDisplayName, assetRenameFileName } from '../assetNames'
import type { Asset } from '../types'
import { ZoomableImage } from './ImageLightbox'
import AssetUsageDialog from './AssetUsageDialog'
import AssetSearchVocabulary from './AssetSearchVocabulary'

type Props = {
  assets: Asset[]
  loading: boolean
  error: string | null
  total: number
  query: string
  /** Tag filter is submitted with the keyword and kept for later pages. */
  tagQuery: string
  /** Deployed sites show AM media; local mode labels the same rows as local assets. */
  amMediaLibrary: boolean
  /** The submitted search: its vocabulary is reused by every page, never rebuilt. */
  search: AssetSearch | null
  index: AssetIndexSummary | null
  indexBusy: boolean
  onSearch: (keyword: string, semantic: boolean) => void
  onReindex: (mode: 'reindex' | 'retry') => void
  onLoadMore: () => void
  loadingMore: boolean
  uploading: boolean
  onUpload: (file: File) => Promise<boolean>
  /** Remove a deleted asset from the list the page is showing. */
  onDeleted: (assetId: string) => void
  /** A saved tag set replaces the card in place; the server response is authoritative. */
  onTagsSaved: (asset: Asset) => void
  /** A renamed asset replaces the card in place; the server response is authoritative. */
  onNameSaved: (asset: Asset) => void
}

export default function AssetLibrary({
  assets,
  loading,
  error,
  total,
  query,
  tagQuery,
  amMediaLibrary,
  search,
  index,
  indexBusy,
  onSearch,
  onReindex,
  onLoadMore,
  loadingMore,
  uploading,
  onUpload,
  onDeleted,
  onTagsSaved,
  onNameSaved,
}: Props) {
  // Null means "show the submitted filter"; typing only overrides it until the next search.
  const [draft, setDraft] = useState<string | null>(null)
  // A referenced asset answers with a conflict; keep it pending so the operator can confirm.
  const [pendingDelete, setPendingDelete] = useState<{ assets: Asset[]; force: boolean } | null>(null)
  const [deletingIds, setDeletingIds] = useState<string[] | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [selecting, setSelecting] = useState(false)
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [assetEditor, setAssetEditor] = useState<{ asset: Asset; name: string; tags: string } | null>(null)
  const [assetSaving, setAssetSaving] = useState(false)
  const [assetEditError, setAssetEditError] = useState<string | null>(null)
  const [usageAsset, setUsageAsset] = useState<Asset | null>(null)

  const keyword = draft ?? query
  // Nothing searches while typing: a query runs on 搜索 or Enter, and that is also the only
  // moment a semantic search may call the query agent.
  const [semantic, setSemantic] = useState(true)

  const submitSearch = (nextTag: string) => {
    setDraft(nextTag)
    setSelectedIds([])
    onSearch(nextTag.trim(), false)
  }

  // The grid pages itself: the sentinel at the end of the list asks for the next page while it
  // is on screen, so there is no 「加载更多」 button to hunt for.
  const hasMore = assets.length < total
  const loadMore = useRef(onLoadMore)
  useEffect(() => { loadMore.current = onLoadMore }, [onLoadMore])
  // The row count and the last requested count live in refs, so the observer created by the
  // callback ref below never reads a stale closure. `requested` also stops a page that brings
  // nothing from looping, because AM's total is an estimate.
  const rows = useRef(assets.length)
  useEffect(() => { rows.current = assets.length }, [assets.length])
  const paging = useRef<{ requested: number | null; stalled: boolean }>({ requested: null, stalled: false })
  useEffect(() => {
    const current = paging.current
    if (current.requested !== null) current.stalled = current.requested === rows.current
    current.requested = null
  }, [assets.length])
  useEffect(() => {
    paging.current = { requested: null, stalled: false }
  }, [query, tagQuery])
  const observerRef = useRef<IntersectionObserver | null>(null)
  useEffect(() => () => observerRef.current?.disconnect(), [])
  // A callback ref attaches on the render that mounts the sentinel: an effect keyed on row
  // counts would miss it whenever the grid and the skeleton swap in separate renders.
  const sentinelRef = useCallback((node: HTMLDivElement | null) => {
    observerRef.current?.disconnect()
    observerRef.current = null
    if (!node) return
    const observer = new IntersectionObserver(
      (entries) => {
        const current = paging.current
        if (current.stalled || current.requested !== null) return
        if (!entries.some((entry) => entry.isIntersecting)) return
        current.requested = rows.current
        loadMore.current()
      },
      // Start the next page before the operator reaches the very last row.
      { rootMargin: '600px 0px' },
    )
    observer.observe(node)
    observerRef.current = observer
  }, [])

  const removeAssets = async (targets: Asset[], force: boolean) => {
    if (targets.length === 0) return
    setDeletingIds(targets.map((asset) => asset.id))
    setDeleteError(null)
    try {
      await deleteAssets(targets.map((asset) => asset.id), force)
      targets.forEach((asset) => onDeleted(asset.id))
      const removed = new Set(targets.map((asset) => asset.id))
      setSelectedIds((previous) => previous.filter((id) => !removed.has(id)))
      setPendingDelete(null)
    } catch (reason: unknown) {
      if (!force && reason instanceof ApiError && reason.code === 'http_409') {
        setPendingDelete({ assets: targets, force: true })
        return
      }
      setDeleteError(reason instanceof Error ? reason.message : '删除失败，请稍后重试')
      setPendingDelete(null)
    } finally {
      setDeletingIds(null)
    }
  }

  const toggleSelected = (assetId: string) => {
    setSelectedIds((previous) =>
      previous.includes(assetId) ? previous.filter((id) => id !== assetId) : [...previous, assetId],
    )
  }

  const selectedAssets = assets.filter((asset) => selectedIds.includes(asset.id))
  const deleting = deletingIds !== null

  const openAssetEditor = (asset: Asset) => {
    setAssetEditor({ asset, name: assetDisplayName(asset.original_name), tags: (asset.tags ?? []).join(', ') })
    setAssetEditError(null)
  }

  const tagsChanged = Boolean(assetEditor && assetEditor.tags !== (assetEditor.asset.tags ?? []).join(', '))
  const editedTags = assetEditor
    ? tagsChanged ? normalizeTags([], assetEditor.tags) : assetEditor.asset.tags ?? []
    : []
  const tagValidationError = editedTags.length > MAX_ASSET_TAGS
    ? `每张素材最多 ${MAX_ASSET_TAGS} 个 Tag，当前 ${editedTags.length} 个`
    : editedTags.some((tag) => tag.length > 40) ? '每个 Tag 最多 40 个字符' : null

  const saveAsset = async () => {
    if (!assetEditor || assetSaving || tagValidationError) return
    const name = assetEditor.name.trim()
    if (!name) {
      setAssetEditError('素材名称不能为空')
      return
    }
    setAssetSaving(true)
    setAssetEditError(null)
    let renamed = false
    try {
      const fileName = assetRenameFileName(name, assetEditor.asset.original_name)
      if (fileName !== assetEditor.asset.original_name) {
        const saved = await renameAsset(assetEditor.asset.id, fileName)
        renamed = true
        onNameSaved(saved)
        // Remember completed writes so retrying a failed Tag save does not rename again.
        setAssetEditor((current) => current ? { ...current, asset: saved } : null)
      }
      if (tagsChanged) {
        const saved = await updateAssetTags(assetEditor.asset.id, editedTags)
        onTagsSaved(saved)
      }
      setAssetEditor(null)
    } catch (reason: unknown) {
      const message = reason instanceof Error ? reason.message : '保存失败，请稍后重试'
      setAssetEditError(renamed ? `名称已保存，Tag 保存失败：${message}` : message)
    } finally {
      setAssetSaving(false)
    }
  }

  return (
    <section className="asset-page">
      <form
        className="asset-toolbar"
        onSubmit={(event) => {
          event.preventDefault()
          if (loading) return
          setSelectedIds([])
          onSearch(keyword.trim(), semantic)
        }}
      >
        <div className="asset-search-group">
          <input
            className="mp-input asset-search-input"
            type="search"
            value={keyword}
            aria-label="搜索素材名称、Tag 或关键词"
            placeholder="搜索名称、Tag 或关键词"
            onChange={(event) => setDraft(event.target.value)}
          />
          <button type="submit" className="mp-btn-secondary mp-btn-small" disabled={loading} aria-busy={loading}>
            {loading ? <span className="spinner spinner-dark" aria-hidden="true" /> : null}
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
        </div>
        <div className="asset-toolbar-actions">
          {index ? (
            <button
              type="button"
              className="mp-btn-secondary mp-btn-small"
              disabled={indexBusy || Boolean(index.active_run)}
              onClick={() => onReindex('reindex')}
              title="重新同步素材清单并重建全部素材的中英文关键词"
            >
              {index.active_run
                ? `索引中 ${(index.active_run.counters.total ?? index.total) - (index.active_run.counters.remaining ?? 0)}/${index.active_run.counters.total ?? index.total}`
                : '重新索引'}
            </button>
          ) : null}
          {!index?.active_run && (index?.failed ?? 0) > 0 ? (
            <button
              type="button"
              className="mp-btn-ghost mp-btn-small"
              disabled={indexBusy}
              onClick={() => onReindex('retry')}
            >
              重试失败 {index?.failed}
            </button>
          ) : null}
          {selecting || assets.length > 0 ? (
            <button
              type="button"
              className="mp-btn-secondary mp-btn-small"
              onClick={() => {
                setSelecting((previous) => !previous)
                setSelectedIds([])
              }}
              disabled={deleting}
            >
              {selecting ? '退出多选' : '多选'}
            </button>
          ) : null}
          {selecting ? (
            <button
              type="button"
              className="mp-btn-secondary mp-btn-small asset-bulk-delete"
              onClick={() => setPendingDelete({ assets: selectedAssets, force: false })}
              disabled={deleting || selectedAssets.length === 0}
            >
              删除选中 {selectedAssets.length}
            </button>
          ) : null}
          <label className={`mp-btn-secondary mp-btn-small upload-trigger${uploading ? ' is-disabled' : ''}`}>
            <input
              type="file"
              accept="image/*"
              disabled={uploading}
              onChange={(event) => {
                const file = event.target.files?.[0]
                if (file) void onUpload(file)
                event.target.value = ''
              }}
            />
            {uploading ? '上传中…' : '上传素材'}
          </label>
          {/* Only the count is shown: which library is loaded is clear from the cards and the
              empty state, and 「AM 素材」read as a filter. */}
          <span className="asset-busy">{total > 0 ? `共 ${total} 张` : ''}</span>
        </div>
      </form>

      {error ? <div className="asset-notice asset-notice-error">{error}</div> : null}
      {search?.degraded ? (
        <div className="asset-notice">搜索词生成失败，已按原词搜索。</div>
      ) : null}
      <AssetSearchVocabulary search={search} />
      {deleteError ? <div className="asset-notice asset-notice-error">{deleteError}</div> : null}

      {loading ? (
        <div className="asset-grid">
          {[0, 1, 2, 3, 4, 5].map((item) => (
            <div className="asset-card asset-card-skeleton" key={item}>
              <span className="asset-skeleton-thumb" />
              <span className="asset-skeleton-line" />
              <span className="asset-skeleton-line asset-skeleton-short" />
            </div>
          ))}
        </div>
      ) : assets.length === 0 ? (
        <div className="asset-empty">
          <strong>{query || tagQuery ? '没有匹配的素材' : '暂无可用图片'}</strong>
          <span>
            {query || tagQuery
              ? '清空或更换搜索词后重试。'
              : amMediaLibrary
                ? '请上传素材，或在 AM Console 资源库中添加。'
                : '请上传素材，本地模式下素材只保存在本机。'}
          </span>
        </div>
      ) : (
        <>
          <div className="asset-grid">
            {assets.map((asset) => {
              const selected = selectedIds.includes(asset.id)
              const name = assetDisplayName(asset.original_name)
              return <article className={`asset-card${selected ? ' is-selected' : ''}`} key={asset.id}>
                <div className="asset-thumb">
                  {selecting ? (
                    <button type="button" className="asset-thumb-preview asset-select-thumb"
                      aria-label={`选择 ${name}`} aria-pressed={selected} disabled={deleting}
                      onClick={() => toggleSelected(asset.id)}>
                      <img src={asset.url} alt="" />
                      <span className={`asset-select${selected ? ' is-on' : ''}`} aria-hidden="true">{selected ? '✓' : ''}</span>
                    </button>
                  ) : (
                    <>
                      <ZoomableImage className="asset-thumb-preview" src={asset.url} alt={name} title={name} />
                      <button
                        type="button"
                        className="asset-delete-fab"
                        title={asset.source === 'am' ? '删除这张素材（含 AM 素材库记录）' : '删除这张素材'}
                        aria-label={`删除 ${name}`}
                        disabled={deleting}
                        onClick={() => setPendingDelete({ assets: [asset], force: false })}
                      >
                        {deletingIds?.includes(asset.id) ? '…' : '×'}
                      </button>
                    </>
                  )}
                </div>
                {/* One editor handles both the name and tags. */}
                <div className="asset-name-row">
                  <strong className="asset-name" title={name}>{name}</strong>
                  <button
                    type="button"
                    className="asset-edit-btn"
                    title="编辑名称和 Tag"
                    aria-label={`编辑 ${name}`}
                    onClick={() => openAssetEditor(asset)}
                  >
                    ✎
                  </button>
                </div>
                {/* Tags double as shortcuts: clicking one filters the list by it. */}
                <div className="asset-tags">
                  {(asset.tags ?? []).map((tag) => (
                    <button
                      type="button"
                      className="asset-tag"
                      key={tag}
                      title={`按 Tag 筛选：${tag}`}
                      onClick={() => submitSearch(tag)}
                    >
                      {tag}
                    </button>
                  ))}
                </div>
                <button type="button" className="asset-usage-count" onClick={() => setUsageAsset(asset)}>
                  引用 {asset.usage_count ?? 0} 次
                </button>
              </article>
            })}
          </div>
          {/* Scrolling the grid keeps the sentinel in view, so pages load one after another. */}
          {hasMore ? (
            <div className="asset-load-more" ref={sentinelRef} role="status">
              {loadingMore ? '加载中…' : ''}
            </div>
          ) : null}
        </>
      )}

      {usageAsset ? <AssetUsageDialog key={usageAsset.id} asset={usageAsset} onClose={() => setUsageAsset(null)} /> : null}

      {pendingDelete ? (
      <div
        className="brand-delete-modal-backdrop"
        onMouseDown={(event) => { if (event.target === event.currentTarget) setPendingDelete(null) }}
      >
        <div className="brand-delete-modal" role="dialog" aria-modal="true" aria-labelledby="asset-delete-title">
          <strong id="asset-delete-title">
            {pendingDelete.force
              ? `${pendingDelete.assets.length > 1 ? '这些图片' : '这张图片'}正在被引用`
              : `删除${pendingDelete.assets.length > 1 ? `选中的 ${pendingDelete.assets.length} 张` : '这张'}素材？`}
          </strong>
          <p>
            {pendingDelete.force
              ? '它被品牌配置、任务或历史版本引用。强制删除会让这些引用失效，已生成的海报不受影响。'
              : pendingDelete.assets.length > 1
                ? `${pendingDelete.assets.slice(0, 3).map((asset) => assetDisplayName(asset.original_name)).join('、')}${pendingDelete.assets.length > 3 ? ` 等 ${pendingDelete.assets.length} 张` : ''}删除后无法恢复。`
                : `${assetDisplayName(pendingDelete.assets[0].original_name)}删除后无法恢复。`}
          </p>
          <div className="brand-delete-modal-actions">
            <button type="button" onClick={() => setPendingDelete(null)} autoFocus>取消</button>
            <button
            type="button"
            className="is-danger"
            disabled={deleting}
            onClick={() => void removeAssets(pendingDelete.assets, pendingDelete.force)}
            >
            {deleting ? '删除中…' : pendingDelete.force ? '强制删除' : '确认删除'}
            </button>
          </div>
        </div>
      </div>
    ) : null}

      {assetEditor ? (
        <div
          className="brand-delete-modal-backdrop"
          onMouseDown={(event) => { if (event.target === event.currentTarget && !assetSaving) setAssetEditor(null) }}
        >
          <form
            className="brand-delete-modal asset-edit-dialog"
            role="dialog"
            aria-modal="true"
            aria-label="编辑素材"
            onSubmit={(event) => { event.preventDefault(); void saveAsset() }}
            onKeyDown={(event) => { if (event.key === 'Escape' && !assetSaving) setAssetEditor(null) }}
          >
            <img
              className="asset-edit-preview"
              src={assetEditor.asset.url}
              alt={assetDisplayName(assetEditor.asset.original_name)}
            />
            <div className="asset-edit-fields">
              <label className="asset-edit-field">
                名称
                <input
                  className="mp-input"
                  value={assetEditor.name}
                  maxLength={300}
                  disabled={assetSaving}
                  autoFocus
                  onChange={(event) => {
                    setAssetEditor({ ...assetEditor, name: event.target.value })
                    setAssetEditError(null)
                  }}
                />
              </label>
              <label className="asset-edit-field">
                Tag（逗号分隔，最多 {MAX_ASSET_TAGS} 个）
                <textarea
                  className="mp-input"
                  rows={2}
                  value={assetEditor.tags}
                  disabled={assetSaving}
                  aria-invalid={Boolean(tagValidationError)}
                  aria-describedby={tagValidationError ? 'asset-edit-error' : undefined}
                  onChange={(event) => {
                    setAssetEditor({ ...assetEditor, tags: event.target.value })
                    setAssetEditError(null)
                  }}
                />
              </label>
              {tagValidationError || assetEditError ? (
                <div id="asset-edit-error" className="brand-duplicate-error" role="alert">{tagValidationError ?? assetEditError}</div>
              ) : null}
              <div className="brand-delete-modal-actions">
                <button type="button" onClick={() => setAssetEditor(null)} disabled={assetSaving}>取消</button>
                <button type="submit" className="is-primary" disabled={assetSaving || Boolean(tagValidationError)}>
                  {assetSaving ? '保存中…' : '保存'}
                </button>
              </div>
            </div>
          </form>
        </div>
      ) : null}
    </section>
  )
}
