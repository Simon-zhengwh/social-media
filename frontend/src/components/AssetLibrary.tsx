import { useState } from 'react'
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
}: Props) {
  const [draft, setDraft] = useState(query)

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
    </section>
  )
}
