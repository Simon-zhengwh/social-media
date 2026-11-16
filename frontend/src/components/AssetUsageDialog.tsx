import { useEffect, useRef, useState } from 'react'
import { listAssetUsages } from '../api'
import { assetDisplayName } from '../assetNames'
import type { Asset, PosterFinalization } from '../types'

export default function AssetUsageDialog({ asset, onClose }: { asset: Asset; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null)
  const [items, setItems] = useState<PosterFinalization[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    if (ref.current && !ref.current.open) ref.current.showModal()
    let cancelled = false
    void listAssetUsages(asset.id).then(value => { if (!cancelled) setItems(value) })
      .catch((reason: unknown) => { if (!cancelled) setError(reason instanceof Error ? reason.message : '引用明细加载失败') })
    return () => { cancelled = true }
  }, [asset.id])

  return <dialog ref={ref} className="asset-usage-dialog" aria-labelledby="asset-usage-title" onCancel={event => { event.preventDefault(); onClose() }}>
    <div className="modal-head">
      <div><h2 id="asset-usage-title">引用明细</h2><p>{assetDisplayName(asset.original_name)}</p></div>
      <button type="button" className="mp-btn-ghost mp-btn-small" onClick={onClose} autoFocus>关闭</button>
    </div>
    {error ? <p className="finalization-error" role="alert">{error}</p>
      : items === null ? <p role="status">加载中…</p>
      : !items.length ? <p>暂无定稿引用。</p>
      : <div className="asset-usage-list">{items.map(item => <article className="asset-usage-item" key={item.id}>
        <a href={item.preview_url} target="_blank" rel="noopener noreferrer" aria-label={`查看定稿 v${item.version}`}>
          <img src={item.preview_url} alt={`定稿海报 v${item.version}`} />
        </a>
        <div><strong>定稿 v{item.version}</strong>
          <time dateTime={item.finalized_at}>{new Date(item.finalized_at).toLocaleString('zh-CN', { hour12: false })}</time>
          <a href={item.task_url} target="_blank" rel="noopener noreferrer">打开设计任务</a>
        </div>
      </article>)}</div>}
  </dialog>
}
