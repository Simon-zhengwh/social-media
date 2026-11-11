import { useState } from 'react'
import { ApiError, deleteAsset, updateAssetTags } from '../api'
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
}: Props) {
  // Null means "show the submitted filter"; typing only overrides it until the next search.
  const [keywordDraft, setKeywordDraft] = useState<string | null>(null)
  const [tagDraftValue, setTagDraftValue] = useState<string | null>(null)
  // A referenced asset answers with a conflict; keep it pending so the operator can confirm.
  const [pendingDelete, setPendingDelete] = useState<{ asset: Asset; force: boolean } | null>(null)
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [tagEditor, setTagEditor] = useState<{ asset: Asset; tags: string[] } | null>(null)
  const [tagInput, setTagInput] = useState('')
  const [tagSaving, setTagSaving] = useState(false)
  const [tagError, setTagError] = useState<string | null>(null)

  const keyword = keywordDraft ?? query
  const tag = tagDraftValue ?? tagQuery

  const submitSearch = (nextTag: string) => {
    setTagDraftValue(nextTag)
    onSearch(keyword.trim(), nextTag.trim())
  }

  const removeAsset = async (asset: Asset, force: boolean) => {
    setDeletingId(asset.id)
    setDeleteError(null)
    try {
      await deleteAsset(asset.id, force)
      onDeleted(asset.id)
      setPendingDelete(null)
    } catch (reason: unknown) {
      if (!force && reason instanceof ApiError && reason.code === 'http_409') {
        setPendingDelete({ asset, force: true })
        return
      }
      setDeleteError(reason instanceof Error ? reason.message : '删除失败，请稍后重试')
      setPendingDelete(null)
    } finally {
      setDeletingId(null)
    }
  }

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

  return (
    <section className="asset-page">
      <form
        className="asset-toolbar"
        onSubmit={(event) => {
          event.preventDefault()
          onSearch(keyword.trim(), tag.trim())
        }}
      >
        {/* Keyword and its trigger stay in one compact group; the tag filter sits beside it. */}
        <div className="asset-search-group">
          <input
            className="mp-input asset-search-input"
            type="search"
            value={keyword}
            aria-label="搜索素材文件名"
            placeholder="搜索素材文件名"
            onChange={(event) => setKeywordDraft(event.target.value)}
          />
          <input
            className="mp-input asset-search-input"
            type="search"
            value={tag}
            aria-label="按 Tag 搜索"
            placeholder="按 Tag 搜索"
            onChange={(event) => setTagDraftValue(event.target.value)}
          />
          <button type="submit" className="mp-btn-secondary mp-btn-small">
            搜索
          </button>
        </div>
        <div className="asset-toolbar-actions">
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
                  <ZoomableImage
                    className="asset-thumb-preview"
                    src={asset.url}
                    alt={asset.original_name}
                    title={asset.original_name}
                  />
                  {asset.source !== 'am' && !asset.read_only ? (
                    <button
                      type="button"
                      className="asset-delete-fab"
                      title="删除这张素材"
                      aria-label={`删除 ${asset.original_name}`}
                      disabled={deletingId === asset.id}
                      onClick={() => setPendingDelete({ asset, force: false })}
                    >
                      {deletingId === asset.id ? '…' : '×'}
                    </button>
                  ) : null}
                </div>
                <div className="asset-card-head">
                  <span className="asset-date">{formatDate(asset.created_at)}</span>
                  <span className="asset-date">{asset.source === 'am' ? 'AM 只读' : label}</span>
                </div>
                <strong className="asset-name" title={asset.original_name}>
                  {asset.original_name}
                </strong>
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
            {pendingDelete.force ? '这张图片正在被引用' : '删除这张素材？'}
          </strong>
          <p>
            {pendingDelete.force
              ? '它被品牌配置、任务或历史版本引用。强制删除会让这些引用失效，已生成的海报不受影响。'
              : `${pendingDelete.asset.original_name}删除后无法恢复。`}
          </p>
          <div className="brand-delete-modal-actions">
            <button type="button" onClick={() => setPendingDelete(null)} autoFocus>取消</button>
            <button
            type="button"
            className="is-danger"
            disabled={deletingId !== null}
            onClick={() => void removeAsset(pendingDelete.asset, pendingDelete.force)}
            >
            {deletingId ? '删除中…' : pendingDelete.force ? '强制删除' : '确认删除'}
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
    </section>
  )
}
