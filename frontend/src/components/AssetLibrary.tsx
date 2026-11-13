import { useEffect, useRef, useState } from 'react'
import { ApiError, deleteAssets, renameAsset, updateAssetTags } from '../api'
import { MAX_ASSET_TAGS, normalizeTags } from '../assetTags'
import type { Asset } from '../types'
import { ZoomableImage } from './ImageLightbox'

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
  onSearch: (keyword: string, tag: string) => void
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

function formatDate(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })
}

export default function AssetLibrary({
  assets,
  loading,
  error,
  total,
  query,
  tagQuery,
  amMediaLibrary,
  onSearch,
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
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [tagEditor, setTagEditor] = useState<{ asset: Asset; tags: string[] } | null>(null)
  const [tagInput, setTagInput] = useState('')
  const [tagSaving, setTagSaving] = useState(false)
  const [tagError, setTagError] = useState<string | null>(null)
  const [nameEditor, setNameEditor] = useState<{ asset: Asset; draft: string } | null>(null)
  const [nameSaving, setNameSaving] = useState(false)
  const [nameError, setNameError] = useState<string | null>(null)

  const keyword = draft ?? query
  // One box matches a name or a tag; typing searches after a short pause instead of a button.
  const search = useRef(onSearch)
  useEffect(() => { search.current = onSearch }, [onSearch])
  useEffect(() => {
    const value = draft?.trim() ?? null
    if (value === null || value === query.trim()) return
    const timer = window.setTimeout(() => search.current(value, ''), 300)
    return () => window.clearTimeout(timer)
  }, [draft, query])

  const submitSearch = (nextTag: string) => {
    setDraft(nextTag)
    onSearch(nextTag.trim(), '')
  }

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
  const allSelected = assets.length > 0 && selectedAssets.length === assets.length
  const deleting = deletingIds !== null

  const openTagEditor = (asset: Asset) => {
    setTagEditor({ asset, tags: asset.tags ?? [] })
    setTagInput('')
    setTagError(null)
  }

  const addTag = (raw: string) => {
    if (!tagEditor) return
    const tag = raw.trim()
    if (!tag) {
      setTagInput('')
      return
    }
    if (tagEditor.tags.length >= MAX_ASSET_TAGS) {
      setTagError(`每张素材最多 ${MAX_ASSET_TAGS} 个 Tag`)
      return
    }
    if (tagEditor.tags.some((item) => item.toLowerCase() === tag.toLowerCase())) {
      setTagInput('')
      return
    }
    setTagEditor({ asset: tagEditor.asset, tags: [...tagEditor.tags, tag] })
    setTagInput('')
    setTagError(null)
  }

  const saveTags = async () => {
    if (!tagEditor) return
    // Typed text that was never committed with Enter still counts: dropping it would look
    // like a save that did nothing. Over-limit sets are rejected before they reach the API.
    const tags = normalizeTags(tagEditor.tags, tagInput)
    if (tags.length > MAX_ASSET_TAGS) {
      setTagError(`每张素材最多 ${MAX_ASSET_TAGS} 个 Tag`)
      return
    }
    setTagSaving(true)
    setTagError(null)
    try {
      const saved = await updateAssetTags(tagEditor.asset.id, tags)
      onTagsSaved(saved)
      setTagEditor(null)
      setTagInput('')
    } catch (reason: unknown) {
      // Keep the unsaved input visible: the card must not claim a sync that did not happen.
      setTagError(reason instanceof Error ? reason.message : '保存 Tag 失败，请稍后重试')
    } finally {
      setTagSaving(false)
    }
  }

  const label = amMediaLibrary ? 'AM 素材' : '本地素材'

  const saveName = async () => {
    if (!nameEditor) return
    const name = nameEditor.draft.trim()
    if (!name) {
      setNameError('素材名称不能为空')
      return
    }
    setNameSaving(true)
    setNameError(null)
    try {
      const saved = await renameAsset(nameEditor.asset.id, name)
      onNameSaved(saved)
      setNameEditor(null)
    } catch (reason: unknown) {
      // Keep the dialog open: the card must not claim a rename that AM rejected.
      setNameError(reason instanceof Error ? reason.message : '保存名称失败，请稍后重试')
    } finally {
      setNameSaving(false)
    }
  }

  return (
    <section className="asset-page">
      <form
        className="asset-toolbar"
        onSubmit={(event) => {
          event.preventDefault()
          onSearch(keyword.trim(), '')
        }}
      >
        <div className="asset-search-group">
          <input
            className="mp-input asset-search-input"
            type="search"
            value={keyword}
            aria-label="搜索素材名称或 Tag"
            placeholder="搜索名称或 Tag"
            onChange={(event) => setDraft(event.target.value)}
          />
        </div>
        <div className="asset-toolbar-actions">
          {assets.length > 0 ? (
            <button
              type="button"
              className="mp-btn-secondary mp-btn-small"
              onClick={() => setSelectedIds(allSelected ? [] : assets.map((asset) => asset.id))}
              disabled={deleting}
            >
              {allSelected ? '取消全选' : '全选'}
            </button>
          ) : null}
          {selectedAssets.length > 0 ? (
            <button
              type="button"
              className="mp-btn-secondary mp-btn-small asset-bulk-delete"
              onClick={() => setPendingDelete({ assets: selectedAssets, force: false })}
              disabled={deleting}
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
          <span className="asset-busy">{label}{total > 0 ? ` · ${total} 张` : ''}</span>
        </div>
      </form>

      {error ? <div className="asset-notice asset-notice-error">{error}</div> : null}
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
            {assets.map((asset) => (
              <article className="asset-card" key={asset.id}>
                <div className="asset-thumb">
                  {/* Selection drives bulk delete; the ✕ next to it deletes just this card. */}
                  <button
                    type="button"
                    className={`asset-select${selectedIds.includes(asset.id) ? ' is-on' : ''}`}
                    aria-pressed={selectedIds.includes(asset.id)}
                    aria-label={`${selectedIds.includes(asset.id) ? '取消选择' : '选择'} ${asset.original_name}`}
                    title="选择后可批量删除"
                    disabled={deleting}
                    onClick={() => toggleSelected(asset.id)}
                  >
                    {selectedIds.includes(asset.id) ? '✓' : ''}
                  </button>
                  <ZoomableImage
                    className="asset-thumb-preview"
                    src={asset.url}
                    alt={asset.original_name}
                    title={asset.original_name}
                  />
                  <button
                    type="button"
                    className="asset-delete-fab"
                    title={asset.source === 'am' ? '删除这张素材（含 AM 素材库记录）' : '删除这张素材'}
                    aria-label={`删除 ${asset.original_name}`}
                    disabled={deleting}
                    onClick={() => setPendingDelete({ assets: [asset], force: false })}
                  >
                    {deletingIds?.includes(asset.id) ? '…' : '×'}
                  </button>
                </div>
                <div className="asset-card-head">
                  <span className="asset-date">{formatDate(asset.created_at)}</span>
                </div>
                {/* The rename affordance sits next to the name it edits. */}
                <div className="asset-name-row">
                  <strong className="asset-name" title={asset.original_name}>
                    {asset.original_name}
                  </strong>
                  <button
                    type="button"
                    className="asset-rename-btn"
                    title="重命名这张素材"
                    aria-label={`重命名 ${asset.original_name}`}
                    onClick={() => {
                      setNameEditor({ asset, draft: asset.original_name })
                      setNameError(null)
                    }}
                  >
                    ✎
                  </button>
                </div>
                <span className="asset-dims tnum">
                  {asset.width > 0 && asset.height > 0 ? `${asset.width} × ${asset.height}` : '尺寸由 AM/模型读取'}
                </span>
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
                  <button
                    type="button"
                    className="asset-tag-edit"
                    aria-label={`编辑 ${asset.original_name} 的 Tag`}
                    onClick={() => openTagEditor(asset)}
                  >
                    {(asset.tags ?? []).length > 0 ? '✎' : '＋ Tag'}
                  </button>
                </div>
              </article>
            ))}
          </div>
          {assets.length < total ? (
            <div className="asset-toolbar">
              <button type="button" className="mp-btn-secondary mp-btn-small" onClick={onLoadMore} disabled={loadingMore}>
                {loadingMore ? '加载中…' : '加载更多'}
              </button>
            </div>
          ) : null}
        </>
      )}

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
                ? `${pendingDelete.assets.slice(0, 3).map((asset) => asset.original_name).join('、')}${pendingDelete.assets.length > 3 ? ` 等 ${pendingDelete.assets.length} 张` : ''}删除后无法恢复。`
                : `${pendingDelete.assets[0].original_name}删除后无法恢复。`}
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

      {tagEditor ? (
        <div
          className="brand-delete-modal-backdrop"
          onMouseDown={(event) => { if (event.target === event.currentTarget && !tagSaving) setTagEditor(null) }}
        >
          <div className="brand-delete-modal asset-tag-dialog" role="dialog" aria-modal="true" aria-labelledby="asset-tag-title">
            <strong id="asset-tag-title">编辑 Tag</strong>
            <p>{tagEditor.asset.original_name} · 最多 {MAX_ASSET_TAGS} 个</p>
            <div className="asset-tag-editor">
              {tagEditor.tags.map((tag) => (
                <span className="asset-tag-chip" key={tag}>
                  {tag}
                  <button
                    type="button"
                    aria-label={`移除 ${tag}`}
                    disabled={tagSaving}
                    onClick={() => setTagEditor({
                      asset: tagEditor.asset,
                      tags: tagEditor.tags.filter((item) => item !== tag),
                    })}
                  >
                    ×
                  </button>
                </span>
              ))}
              <input
                className="mp-input asset-tag-input"
                value={tagInput}
                maxLength={40}
                disabled={tagSaving || tagEditor.tags.length >= MAX_ASSET_TAGS}
                aria-label="新增 Tag"
                placeholder={tagEditor.tags.length >= MAX_ASSET_TAGS ? '已达上限' : '输入后按 Enter 添加'}
                onChange={(event) => setTagInput(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ',' || event.key === '，') {
                    event.preventDefault()
                    addTag(tagInput)
                  }
                }}
                onBlur={() => { if (tagInput.trim()) addTag(tagInput) }}
              />
            </div>
            {tagError ? <div className="brand-duplicate-error">{tagError}</div> : null}
            <div className="brand-delete-modal-actions">
              <button type="button" onClick={() => setTagEditor(null)} disabled={tagSaving} autoFocus>取消</button>
              <button type="button" className="is-primary" onClick={() => void saveTags()} disabled={tagSaving}>
                {tagSaving ? '保存中…' : '保存 Tag'}
              </button>
            </div>
          </div>
        </div>
      ) : null}
      {nameEditor ? (
        <div
          className="brand-delete-modal-backdrop"
          onMouseDown={(event) => { if (event.target === event.currentTarget && !nameSaving) setNameEditor(null) }}
        >
          <div className="brand-delete-modal asset-tag-dialog" role="dialog" aria-modal="true" aria-labelledby="asset-name-title">
            <strong id="asset-name-title">重命名素材</strong>
            <p>只修改显示名称，图片内容和 Tag 不受影响。</p>
            <input
              className="mp-input asset-name-input"
              value={nameEditor.draft}
              maxLength={300}
              disabled={nameSaving}
              aria-label="素材名称"
              autoFocus
              onChange={(event) => setNameEditor({ asset: nameEditor.asset, draft: event.target.value })}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault()
                  void saveName()
                }
                if (event.key === 'Escape' && !nameSaving) setNameEditor(null)
              }}
            />
            {nameError ? <div className="brand-duplicate-error">{nameError}</div> : null}
            <div className="brand-delete-modal-actions">
              <button type="button" onClick={() => setNameEditor(null)} disabled={nameSaving}>取消</button>
              <button type="button" className="is-primary" onClick={() => void saveName()} disabled={nameSaving}>
                {nameSaving ? '保存中…' : '保存名称'}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </section>
  )
}
