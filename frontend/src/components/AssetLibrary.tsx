import { useState } from 'react'
import { ApiError, deleteAsset } from '../api'
import type { Asset } from '../types'
import { ZoomableImage } from './ImageLightbox'

type Props = {
  assets: Asset[]
  loading: boolean
  error: string | null
  total: number
  query: string
  onSearch: (keyword: string) => void
  onLoadMore: () => void
  loadingMore: boolean
  uploading: boolean
  onUpload: (file: File) => Promise<boolean>
  /** Remove a deleted asset from the list the page is showing. */
  onDeleted: (assetId: string) => void
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
  onSearch,
  onLoadMore,
  loadingMore,
  uploading,
  onUpload,
  onDeleted,
}: Props) {
  const [draft, setDraft] = useState(query)
  // A referenced asset answers with a conflict; keep it pending so the operator can confirm.
  const [pendingDelete, setPendingDelete] = useState<{ asset: Asset; force: boolean } | null>(null)
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)
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

  return (
    <section className="asset-page">
      <form
        className="asset-toolbar"
        onSubmit={(event) => {
          event.preventDefault()
          onSearch(draft.trim())
        }}
      >
        <input
          className="mp-input"
          type="search"
          value={draft}
          aria-label="搜索 AM 素材"
          placeholder="搜索 AM 素材或临时上传文件名"
          onChange={(event) => setDraft(event.target.value)}
        />
        <button type="submit" className="mp-btn-secondary mp-btn-small">
          搜索
        </button>
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
          {uploading ? '上传中…' : '上传临时图片'}
        </label>
        <span className="asset-busy">AM 资源库 + 临时上传{total > 0 ? ` · ${total} 张` : ''}</span>
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
          <strong>{query ? '没有匹配的素材' : '暂无可用图片'}</strong>
          <span>{query ? '清空或更换搜索词后重试。' : '请先上传临时图片或在 AM Console 资源库中添加素材。'}</span>
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
                      title="删除这张临时图片"
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
                  <span className="asset-date">{asset.source === 'am' ? 'AM 只读' : '临时上传'}</span>
                </div>
                <strong className="asset-name" title={asset.original_name}>
                  {asset.original_name}
                </strong>
                <span className="asset-dims tnum">
                  {asset.width > 0 && asset.height > 0 ? `${asset.width} × ${asset.height}` : '尺寸由 AM/模型读取'}
                </span>
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
            {pendingDelete.force ? '这张图片正在被引用' : '删除这张临时图片？'}
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
    </section>
  )
}
