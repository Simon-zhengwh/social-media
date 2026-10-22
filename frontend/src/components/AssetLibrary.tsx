import { useState } from 'react'
import type { Asset, AssetKind } from '../types'

type Props = {
  assets: Asset[]
  loading: boolean
  error: string | null
  uploadingKind: AssetKind | null
  onUpload: (kind: AssetKind, file: File) => Promise<boolean>
  onUseSamples: () => Promise<string | null>
  onDelete: (asset: Asset) => Promise<boolean>
  onRefresh: () => void
}

function kindLabel(kind: AssetKind) {
  return kind === 'product' ? '图片素材' : '参考图'
}

function formatDate(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return ''
  }
  return date.toLocaleDateString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
  })
}

export default function AssetLibrary({
  assets,
  loading,
  error,
  uploadingKind,
  onUpload,
  onUseSamples,
  onDelete,
  onRefresh,
}: Props) {
  const [notice, setNotice] = useState<string | null>(null)
  const [sampleBusy, setSampleBusy] = useState(false)
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [confirmingId, setConfirmingId] = useState<string | null>(null)
  const productCount = assets.filter((asset) => asset.kind === 'product').length
  const referenceCount = assets.length - productCount

  const handleSamples = async () => {
    setSampleBusy(true)
    setNotice(null)
    try {
      const message = await onUseSamples()
      if (message) {
        setNotice(message)
      }
    } finally {
      setSampleBusy(false)
    }
  }

  const handleFile = async (kind: AssetKind, file: File) => {
    setNotice(null)
    const ok = await onUpload(kind, file)
    if (ok) {
      setNotice(kind === 'product' ? '图片已上传' : '素材已上传')
    }
  }

  const handleDelete = async (asset: Asset) => {
    setDeletingId(asset.id)
    setConfirmingId(null)
    setNotice(null)
    try {
      const ok = await onDelete(asset)
      if (ok) {
        setNotice('素材已删除')
      }
    } finally {
      setDeletingId(null)
    }
  }

  const disabled = uploadingKind !== null || sampleBusy

  return (
    <section className="asset-page">
      <div className="asset-head">
        <div>
          <span className="eyebrow">02 · 资产库</span>
          <h1>资产管理</h1>
          <p className="panel-lead">管理图片素材；品牌参考图请在「品牌风格」中维护。</p>
        </div>
        <div className="asset-stats">
          <span>
            <strong className="tnum">{productCount}</strong> 图片素材
          </span>
          <span>
            <strong className="tnum">{referenceCount}</strong> 参考素材
          </span>
        </div>
      </div>

      <div className="asset-toolbar">
        <label className={`mp-btn-secondary mp-btn-small upload-trigger${disabled ? ' is-disabled' : ''}`}>
          <input
            type="file"
            accept="image/jpeg,image/png,image/apng,image/webp,image/gif,image/bmp,image/tiff,image/avif,image/heic,image/heif,image/jp2,image/x-icon,image/x-tga,.jpg,.jpeg,.jfif,.png,.apng,.webp,.gif,.bmp,.dib,.tif,.tiff,.avif,.avifs,.heic,.heif,.jp2,.j2k,.jpf,.jpx,.ico,.tga,.vda,.vst"
            disabled={disabled}
            onChange={(event) => {
              const file = event.target.files?.[0]
              if (file) {
                void handleFile('product', file)
              }
              event.target.value = ''
            }}
          />
          上传图片
        </label>
        <button
          type="button"
          className="mp-btn-secondary mp-btn-small"
          onClick={() => void handleSamples()}
          disabled={disabled}
        >
          {sampleBusy ? '载入中…' : '载入示例素材'}
        </button>
        <button type="button" className="mp-btn-ghost mp-btn-small" onClick={onRefresh} disabled={loading}>
          刷新
        </button>
        {uploadingKind ? <span className="asset-busy">上传中…</span> : null}
      </div>

      {notice ? <div className="asset-notice">{notice}</div> : null}
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
          <strong>还没有素材</strong>
          <span>上传图片，或载入示例素材后开始生成。</span>
        </div>
      ) : (
        <div className="asset-grid">
          {assets.map((asset) => {
            return (
              <article className="asset-card" key={asset.id}>
                <div className={`asset-thumb asset-thumb-${asset.kind}`}>
                  <img src={asset.url} alt={asset.original_name} />
                </div>
                <div className="asset-card-head">
                  <span className={`asset-kind asset-kind-${asset.kind}`}>
                    {kindLabel(asset.kind)}
                  </span>
                  <span className="asset-date">{formatDate(asset.created_at)}</span>
                </div>
                <strong className="asset-name" title={asset.original_name}>
                  {asset.original_name}
                </strong>
                <span className="asset-dims tnum">
                  {asset.width} × {asset.height}
                </span>
                <div className="asset-actions">
                  {confirmingId === asset.id ? (
                    <span className="asset-confirm">
                      <span>删除后历史记录中的图片将无法显示，确定删除？</span>
                      <button
                        type="button"
                        className="asset-confirm-cancel"
                        onClick={() => setConfirmingId(null)}
                      >
                        取消
                      </button>
                      <button
                        type="button"
                        className="asset-confirm-delete"
                        onClick={() => void handleDelete(asset)}
                        disabled={deletingId === asset.id}
                      >
                        {deletingId === asset.id ? '删除中…' : '确认'}
                      </button>
                    </span>
                  ) : (
                    <button
                      type="button"
                      className="asset-delete"
                      onClick={() => setConfirmingId(asset.id)}
                      disabled={deletingId === asset.id}
                    >
                      删除
                    </button>
                  )}
                </div>
              </article>
            )
          })}
        </div>
      )}
    </section>
  )
}
