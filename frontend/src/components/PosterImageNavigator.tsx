import { useCallback, useEffect, useId, useRef, useState } from 'react'
import type { WholeImageVersion } from '../types'
import CandidateVersionPicker from './CandidateVersionPicker'

export type PosterImageItem = {
  id: string
  label: string
  thumb: string | null
  status?: string
  error?: string | null
  onSelect: () => void
  remove?: { onClick: () => void; disabled: boolean; pending: boolean }
  retry?: { onClick: () => void; disabled: boolean; pending: boolean }
}

export type PosterImageBatch = {
  id: string
  round: number
  model: string
  group?: string
  status?: string
  onSelectEmpty?: () => void
  images: PosterImageItem[]
}

type Props = {
  batches: PosterImageBatch[]
  activeBatchId: string | null
  selectedId: string | null
  disabled: boolean
  versions: WholeImageVersion[]
  versionsLoading: boolean
  currentVersionId: string | null
  versionSelectionDisabled: boolean
  onSelectVersion: (versionId: string) => Promise<boolean>
}

function Thumbnail({ image }: { image: PosterImageItem }) {
  return image.thumb
    ? <img src={image.thumb} alt="" loading="lazy" decoding="async" />
    : <span>{image.status || '无预览'}</span>
}

export default function PosterImageNavigator({ batches, activeBatchId, selectedId, disabled,
  versions, versionsLoading, currentVersionId, versionSelectionDisabled, onSelectVersion }: Props) {
  const railRef = useRef<HTMLDivElement>(null)
  const dialogRef = useRef<HTMLDialogElement>(null)
  const dialogTitle = useId()
  const [versionAnchor, setVersionAnchor] = useState<{ id: string; element: HTMLButtonElement } | null>(null)
  const closeVersions = useCallback(() => setVersionAnchor(null), [])
  const activeBatch = batches.find(batch => batch.id === activeBatchId)
  const images = activeBatch?.images ?? []
  const selectedVersion = versions.find(version => version.id === currentVersionId)
  const activeVersionAnchor = versionAnchor && versionAnchor.id === selectedId ? versionAnchor.element : null

  useEffect(() => {
    const items = activeBatch?.images
    if (!items || items.length <= 1 || disabled) return
    const selectAdjacent = (event: KeyboardEvent) => {
      const offset = event.key === 'ArrowLeft' || event.key === 'ArrowUp' ? -1
        : event.key === 'ArrowRight' || event.key === 'ArrowDown' ? 1 : 0
      if (!offset || event.defaultPrevented || event.isComposing || event.altKey || event.ctrlKey || event.metaKey) return
      const target = event.target
      if (target instanceof HTMLElement && (target.isContentEditable ||
        target.closest('input, textarea, select, [role="textbox"], [role="tab"], [role="menu"], [role="listbox"], [role="combobox"], [role="dialog"]'))) return
      if (document.querySelector('dialog[open], [aria-modal="true"]')) return

      event.preventDefault()
      closeVersions()
      const index = Math.max(0, items.findIndex(image => image.id === selectedId))
      const next = (index + offset + items.length) % items.length
      items[next].onSelect()
      const thumbnail = railRef.current?.querySelectorAll<HTMLButtonElement>('.candidate-thumbnail')[next]
      thumbnail?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
      if (target instanceof HTMLElement && target.closest('.candidate-thumbnail')) thumbnail?.focus({ preventScroll: true })
    }
    document.addEventListener('keydown', selectAdjacent)
    return () => document.removeEventListener('keydown', selectAdjacent)
  }, [activeBatch, selectedId, disabled, closeVersions])

  return (
    <aside className="poster-image-nav" aria-label="本轮图片">
      <div className="poster-rail-scroll" ref={railRef}>
        {images.map(image => {
          const active = image.id === selectedId
          const expanded = active && Boolean(activeVersionAnchor)
          return (
            <div className="poster-rail-item" key={image.id}>
              <div className="candidate-item">
                <button type="button" className={`candidate-thumbnail${active ? ' is-active' : ''}`}
                  aria-label={`查看 ${image.label}${image.status ? ` · ${image.status}` : ''}`} aria-pressed={active}
                  title={image.error || image.label} disabled={disabled} onClick={() => {
                    closeVersions()
                    if (!active) image.onSelect()
                  }}>
                  <Thumbnail image={image} />
                </button>
                {image.remove ? (
                  <button type="button" className="candidate-delete" aria-label={`删除 ${image.label}`}
                    disabled={image.remove.disabled} onClick={image.remove.onClick}>{image.remove.pending ? '…' : '×'}</button>
                ) : null}
              </div>
              <span className="poster-rail-label">{image.label}</span>
              {active && selectedVersion && versions.length > 1 ? (
                <button type="button" className="poster-rail-version" aria-haspopup="dialog" aria-expanded={expanded}
                  aria-label={`切换版本，当前 v${selectedVersion.version}`} disabled={versionSelectionDisabled || disabled}
                  onClick={event => setVersionAnchor(expanded ? null : { id: image.id, element: event.currentTarget })}>
                  v{selectedVersion.version}<span aria-hidden="true">⌄</span>
                </button>
              ) : null}
              {image.retry ? (
                <button type="button" className="poster-rail-retry" aria-label={`重试 ${image.label}`}
                  disabled={image.retry.disabled} onClick={image.retry.onClick}>{image.retry.pending ? '重试中' : '重试'}</button>
              ) : null}
            </div>
          )
        })}
      </div>
      <div className="poster-rail-actions">
        <button type="button" className="poster-history-trigger" aria-haspopup="dialog" onClick={() => {
          closeVersions()
          dialogRef.current?.showModal()
        }}>
          <svg viewBox="0 0 20 20" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
            <path d="M3.5 9a6.5 6.5 0 1 1 1 4.6M3.5 5.5V9H7M10 6.5V10l2.5 1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          历史记录
        </button>
      </div>

      <dialog className="poster-history-dialog" ref={dialogRef} aria-labelledby={dialogTitle}
        onClick={event => { if (event.target === event.currentTarget) event.currentTarget.close() }}>
        <div className="poster-history-head">
          <h2 id={dialogTitle}>历史记录</h2>
          <button type="button" className="mp-btn-ghost" aria-label="关闭历史记录" onClick={() => dialogRef.current?.close()}>×</button>
        </div>
        <div className="poster-history-list">
          {batches.toSorted((a, b) => b.round - a.round).map(batch => (
            <section className="poster-history-batch" key={batch.id}>
              <h3>第 {batch.round} 轮 <span>{batch.model}</span>{batch.group ? <> <span>{batch.group}</span></> : null}</h3>
              <div className="poster-history-images">
                {!batch.images.length && batch.status ? (
                  <button type="button" className="mp-btn-secondary mp-btn-small" disabled={disabled} onClick={() => {
                    batch.onSelectEmpty?.()
                    dialogRef.current?.close()
                  }}>{batch.status}</button>
                ) : null}
                {batch.images.map(image => (
                  <button type="button" key={image.id} className="poster-history-image"
                    aria-pressed={image.id === selectedId} disabled={disabled} onClick={() => {
                      image.onSelect()
                      dialogRef.current?.close()
                    }}>
                    <span className="poster-history-thumb"><Thumbnail image={image} /></span>
                    <span>{image.label}</span>
                  </button>
                ))}
              </div>
            </section>
          ))}
        </div>
      </dialog>
      {activeVersionAnchor ? (
        <CandidateVersionPicker anchor={activeVersionAnchor} versions={versions} loading={versionsLoading}
          currentVersionId={currentVersionId} busy={versionSelectionDisabled || disabled}
          onSelect={onSelectVersion} onClose={closeVersions} />
      ) : null}
    </aside>
  )
}
