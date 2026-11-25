import { useEffect, useId, useMemo, useRef, useState } from 'react'
import type { LogoOverlayBox } from '../types'
import { compositeLogoPng, drawLogo, hasWhiteBacking, loadImage, scaleLogoBox, withoutWhiteBacking } from '../logoOverlay'

const directions: Record<string, [number, number]> = {
  ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1],
}

const ZOOM_MIN = 25
const ZOOM_MAX = 300
const clampZoom = (percent: number) => Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Math.round(percent)))
const centreOf = (value: LogoOverlayBox) => ({ x: value.x + value.width / 2, y: value.y + value.height / 2 })

type Props = {
  baseSrc: string
  logoSrc: string
  width: number
  height: number
  /** Box saved with the current version, i.e. what the operator last chose. */
  initialBox: LogoOverlayBox
  /** Box detected on the clean base: the 100% scale reference and what 重置 restores. */
  defaultBox: LogoOverlayBox
  busy?: boolean
  onClose: () => void
  onApply: (input: { base64: string; box: LogoOverlayBox }) => Promise<void>
}

const message = (error: unknown) => error instanceof Error ? error.message : '保存失败，请重试'

type Drag = {
  id: number
  mode: 'move' | 'scale'
  box: LogoOverlayBox
  zoom: number
  /** Pointer position in canvas pixels at drag start. */
  x: number
  y: number
}

export default function PosterLogoOverlayDialog({
  baseSrc, logoSrc, width, height, initialBox, defaultBox, busy = false, onClose, onApply,
}: Props) {
  const titleId = useId()
  const dialogRef = useRef<HTMLDialogElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const drag = useRef<Drag | null>(null)
  const [base, setBase] = useState<HTMLImageElement | null>(null)
  const [logo, setLogo] = useState<HTMLImageElement | null>(null)
  const [box, setBox] = useState<LogoOverlayBox>(initialBox)
  // A second visit starts at the ratio the operator already chose, not at 100% again.
  const [zoom, setZoom] = useState(() =>
    clampZoom((initialBox.width / Math.max(1, defaultBox.width)) * 100))
  const [removeWhiteBg, setRemoveWhiteBg] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const whiteBacked = useMemo(() => (logo ? hasWhiteBacking(logo) : false), [logo])

  useEffect(() => {
    const dialog = dialogRef.current
    if (dialog && !dialog.open) dialog.showModal()
    let cancelled = false
    Promise.all([loadImage(baseSrc), loadImage(logoSrc)]).then(([nextBase, nextLogo]) => {
      if (cancelled) return
      setBase(nextBase)
      setLogo(nextLogo)
    }).catch((reason: unknown) => {
      if (!cancelled) setError(message(reason))
    })
    return () => { cancelled = true }
  }, [baseSrc, logoSrc])

  // Keep the Logo reachable: at least a third of it must stay on the canvas.
  function constrain(next: LogoOverlayBox): LogoOverlayBox {
    const keepX = Math.max(8, Math.round(next.width / 3))
    const keepY = Math.max(8, Math.round(next.height / 3))
    return {
      ...next,
      x: Math.min(width - keepX, Math.max(keepX - next.width, Math.round(next.x))),
      y: Math.min(height - keepY, Math.max(keepY - next.height, Math.round(next.y))),
    }
  }

  /** Resize to `percent` of the detected box, anchored on the current box's centre. */
  function resize(percent: number) {
    const next = clampZoom(percent)
    setZoom(next)
    setBox((previous) => constrain(scaleLogoBox(previous, defaultBox, next)))
  }

  const handleSize = Math.max(10, Math.round(Math.min(width, height) / 48))
  const corners = [
    { x: box.x, y: box.y }, { x: box.x + box.width, y: box.y },
    { x: box.x, y: box.y + box.height }, { x: box.x + box.width, y: box.y + box.height },
  ]

  const nearCorner = (x: number, y: number) =>
    corners.some((corner) => Math.hypot(corner.x - x, corner.y - y) <= handleSize * 1.6)

  function toCanvas(event: { currentTarget: HTMLCanvasElement; clientX: number; clientY: number }) {
    const rect = event.currentTarget.getBoundingClientRect()
    return {
      x: (event.clientX - rect.left) * width / Math.max(1, rect.width),
      y: (event.clientY - rect.top) * height / Math.max(1, rect.height),
    }
  }

  useEffect(() => {
    const context = canvasRef.current?.getContext('2d')
    if (!base || !logo || !context) return
    context.clearRect(0, 0, width, height)
    context.imageSmoothingEnabled = true
    context.imageSmoothingQuality = 'high'
    context.drawImage(base, 0, 0, width, height)
    drawLogo({
      context,
      logo: removeWhiteBg ? withoutWhiteBacking(logo) : logo,
      logoWidth: logo.naturalWidth,
      logoHeight: logo.naturalHeight,
      box,
    })
    // The frame and its handles are a screen-only affordance; exported PNGs never contain them.
    const line = Math.max(1, Math.round(Math.min(width, height) / 480))
    context.save()
    context.setLineDash([line * 8, line * 6])
    context.lineWidth = line * 2
    context.strokeStyle = 'rgba(255, 255, 255, 0.92)'
    context.strokeRect(box.x, box.y, box.width, box.height)
    context.strokeStyle = 'rgba(23, 24, 32, 0.72)'
    context.lineDashOffset = line * 8
    context.strokeRect(box.x, box.y, box.width, box.height)
    context.restore()
    const side = Math.max(10, Math.round(Math.min(width, height) / 48))
    for (const [x, y] of [[box.x, box.y], [box.x + box.width, box.y],
      [box.x, box.y + box.height], [box.x + box.width, box.y + box.height]]) {
      context.save()
      context.fillStyle = 'rgba(23, 24, 32, 0.86)'
      context.strokeStyle = 'rgba(255, 255, 255, 0.92)'
      context.lineWidth = line * 2
      context.fillRect(x - side / 2, y - side / 2, side, side)
      context.strokeRect(x - side / 2, y - side / 2, side, side)
      context.restore()
    }
  }, [base, logo, box, width, height, removeWhiteBg])

  async function save() {
    setError('')
    setSaving(true)
    try {
      const base64 = await compositeLogoPng({
        baseSrc, logoSrc, canvasWidth: width, canvasHeight: height, box, removeWhiteBg,
      })
      await onApply({ base64, box })
      onClose()
    } catch (reason: unknown) {
      setError(message(reason))
    } finally {
      setSaving(false)
    }
  }

  const disabled = saving || busy
  const ready = Boolean(base && logo)

  return <dialog ref={dialogRef} className="poster-logo-dialog" aria-labelledby={titleId} onClose={onClose}>
    <header>
      <h2 id={titleId}>调整 Logo</h2>
      <span>{width} × {height}</span>
    </header>
    <canvas ref={canvasRef} width={width} height={height} tabIndex={ready ? 0 : -1}
      aria-label="拖动 Logo、滚轮缩放或用方向键微调"
      onKeyDown={(event) => {
        const delta = directions[event.key]
        if (!delta || !ready) return
        event.preventDefault()
        const step = event.shiftKey ? 10 : 2
        setBox((previous) => constrain({
          ...previous, x: previous.x + delta[0] * step, y: previous.y + delta[1] * step,
        }))
      }}
      onWheel={(event) => {
        if (!ready || disabled) return
        resize(zoom + (event.deltaY < 0 ? 5 : -5))
      }}
      onPointerDown={(event) => {
        if (!ready || drag.current || event.button !== 0) return
        const point = toCanvas(event)
        event.currentTarget.setPointerCapture(event.pointerId)
        drag.current = {
          id: event.pointerId, mode: nearCorner(point.x, point.y) ? 'scale' : 'move',
          box, zoom, x: point.x, y: point.y,
        }
      }}
      onPointerMove={(event) => {
        const start = drag.current
        const point = toCanvas(event)
        if (!start) {
          event.currentTarget.style.cursor = ready && nearCorner(point.x, point.y) ? 'nwse-resize' : 'grab'
          return
        }
        if (start.id !== event.pointerId) return
        if (start.mode === 'scale') {
          // Equal-ratio scaling around the centre keeps the Logo's aspect ratio intact.
          const centre = centreOf(start.box)
          const from = Math.hypot(start.x - centre.x, start.y - centre.y) || 1
          resize(start.zoom * (Math.hypot(point.x - centre.x, point.y - centre.y) / from))
          return
        }
        setBox(constrain({
          ...start.box, x: start.box.x + point.x - start.x, y: start.box.y + point.y - start.y,
        }))
      }}
      onLostPointerCapture={() => { drag.current = null }}
    />
    {error ? <p role="alert">{error}</p> : !ready ? <p role="status">正在加载图片…</p> : null}
    <div className="poster-logo-controls">
      <label>缩放 <input type="range" min={ZOOM_MIN} max={ZOOM_MAX} value={zoom} disabled={disabled || !ready}
        onChange={(event) => resize(event.currentTarget.valueAsNumber)} /> <output>{zoom}%</output></label>
      {whiteBacked ? <label className="poster-logo-checkbox">
        <input type="checkbox" checked={removeWhiteBg} disabled={disabled}
          onChange={(event) => setRemoveWhiteBg(event.target.checked)} />
        去除白底
      </label> : null}
      <button type="button" className="mp-btn-ghost mp-btn-small" disabled={disabled || !ready}
        onClick={() => { setZoom(100); setBox(constrain(defaultBox)); setRemoveWhiteBg(false) }}>重置</button>
    </div>
    <footer>
      <button type="button" className="mp-btn-secondary" disabled={disabled} onClick={onClose}>取消</button>
      <button type="button" className="mp-btn-primary" disabled={disabled || !ready} onClick={() => void save()}>
        {saving ? '保存中…' : '应用'}
      </button>
    </footer>
  </dialog>
}
