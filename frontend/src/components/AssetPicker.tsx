import { useEffect, useState } from 'react'
import type { Asset } from '../types'

type Props = {
  open: boolean
  purpose: 'product' | 'logo'
  assets: Asset[]
  loading: boolean
  multi?: boolean
  selectedIds?: string[]
  max?: number
  onClose: () => void
  onSelect?: (asset: Asset) => void
  onConfirm?: (assets: Asset[]) => void
  onGoAssets: () => void
}

export default function AssetPicker({
  open,
  purpose,
  assets,
  loading,
  multi = false,
  selectedIds = [],
  max = 6,
  onClose,
  onSelect,
  onConfirm,
  onGoAssets,
}: Props) {
  const [selected, setSelected] = useState<string[]>(() => selectedIds)

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

  if (!open) {
    return null
  }

  const matching = assets
  const title = purpose === 'product' ? '选择素材图' : '选择品牌 Logo'
  const emptyCopy =
    purpose === 'product'
      ? '资产管理中还没有可用素材图'
      : '资产管理中还没有可用图片'

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
      .map((id) => matching.find((asset) => asset.id === id))
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

        {loading ? (
          <div className="picker-grid">
            {[0, 1, 2, 3].map((item) => (
              <span className="picker-skeleton" key={item} />
            ))}
          </div>
        ) : matching.length === 0 ? (
          <div className="picker-empty">
            <strong>{emptyCopy}</strong>
            <span>上传图片后，再回来选择。</span>
            <button type="button" className="mp-btn-secondary mp-btn-small" onClick={onGoAssets}>
              去资产管理
            </button>
          </div>
        ) : (
          <div className="picker-grid">
            {matching.map((asset) => {
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
                  <img src={asset.url} alt={asset.original_name} />
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
