import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { WholeImageVersion } from '../types'

type Props = {
  anchor: HTMLButtonElement
  versions: WholeImageVersion[]
  loading: boolean
  currentVersionId: string | null
  busy: boolean
  onSelect: (versionId: string) => Promise<boolean>
  onClose: () => void
}

export default function CandidateVersionPicker({ anchor, versions, loading, currentVersionId,
  busy, onSelect, onClose }: Props) {
  const rootRef = useRef<HTMLDivElement>(null)
  const [position, setPosition] = useState({ left: 0, top: 0, width: 240 })
  const visible = !loading && versions.length > 1
  const ordered = [...versions].sort((a, b) => b.version - a.version)
  const updatePosition = useCallback(() => {
    const bounds = anchor.getBoundingClientRect()
    const rail = anchor.closest('.candidate-model-groups')?.getBoundingClientRect()
    if (!anchor.isConnected || bounds.bottom <= Math.max(0, rail?.top ?? 0) ||
      bounds.top >= Math.min(window.innerHeight, rail?.bottom ?? window.innerHeight)) {
      onClose()
      return false
    }
    const width = Math.min(400, versions.length * 72 + 20, window.innerWidth - 24)
    const height = rootRef.current?.offsetHeight ?? 128
    setPosition({
      left: Math.max(12, Math.min(bounds.right + 10, window.innerWidth - width - 12)),
      top: Math.max(12, Math.min(bounds.top - 4, window.innerHeight - height - 12)),
      width,
    })
    return true
  }, [anchor, versions.length, onClose])

  useEffect(() => {
    if (!loading && versions.length <= 1) onClose()
  }, [loading, versions.length, onClose])

  useEffect(() => {
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !anchor.contains(event.target) && !rootRef.current?.contains(event.target)) onClose()
    }
    const escape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      anchor.focus({ preventScroll: true })
      onClose()
    }
    const scroll = (event: Event) => {
      if (event.target instanceof Node && rootRef.current?.contains(event.target)) return
      if (rootRef.current) updatePosition()
    }
    const resize = () => { if (rootRef.current) updatePosition() }
    document.addEventListener('pointerdown', outside)
    document.addEventListener('keydown', escape)
    window.addEventListener('scroll', scroll, true)
    window.addEventListener('resize', resize)
    return () => {
      document.removeEventListener('pointerdown', outside)
      document.removeEventListener('keydown', escape)
      window.removeEventListener('scroll', scroll, true)
      window.removeEventListener('resize', resize)
    }
  }, [anchor, onClose, updatePosition])

  useLayoutEffect(() => {
    if (!visible) return
    if (!updatePosition()) return
    const selected = rootRef.current?.querySelector<HTMLButtonElement>('[aria-pressed="true"]')
    selected?.focus({ preventScroll: true })
  }, [visible, updatePosition])

  if (!visible) return null

  return createPortal(
    <div ref={rootRef} id="candidate-version-picker" className="candidate-version-picker"
      role="dialog" aria-label="选择海报版本" style={position}
      onKeyDown={event => {
        const offset = event.key === 'ArrowLeft' || event.key === 'ArrowUp' ? -1
          : event.key === 'ArrowRight' || event.key === 'ArrowDown' ? 1 : 0
        if (!offset) return
        const options = [...(rootRef.current?.querySelectorAll<HTMLButtonElement>('.candidate-version-option') ?? [])]
        const index = options.findIndex(option => option === document.activeElement)
        if (index < 0) return
        event.preventDefault()
        event.stopPropagation()
        const next = options[(index + offset + options.length) % options.length]
        next.focus({ preventScroll: true })
        next.scrollIntoView({ block: 'nearest', inline: 'nearest' })
      }}>
      <div className="candidate-version-heading">
        <button type="button" aria-label="关闭版本选择" onClick={() => {
          anchor.focus({ preventScroll: true })
          onClose()
        }}>×</button>
      </div>
      <div className="candidate-version-options">
        {ordered.map(version => (
          <button key={version.id} type="button" className={`candidate-version-option${version.id === currentVersionId ? ' is-active' : ''}`}
            aria-label={`选择版本 v${version.version}`} aria-pressed={version.id === currentVersionId}
            title={version.instruction || '初始设计'} disabled={busy}
            onClick={async () => {
              const selected = await onSelect(version.id)
              if (selected) {
                if (rootRef.current?.contains(document.activeElement)) anchor.focus({ preventScroll: true })
                onClose()
              }
            }}>
            {version.preview_url ? <img src={version.preview_url} alt="" /> : <span className="candidate-version-missing">无预览</span>}
            <span>v{version.version}</span>
          </button>
        ))}
      </div>
    </div>,
    document.body,
  )
}
