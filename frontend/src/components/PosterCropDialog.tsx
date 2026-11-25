import { useEffect, useId, useRef, useState } from 'react'
import type { LogoOverlayBox } from '../types'
import { drawLogo, hasWhiteBacking, loadImage, scaleLogoBox, withoutWhiteBacking } from '../logoOverlay'

type Props = {
  src: string
  width: number
  height: number
  /** Official Logo to draw over the cropped base before exporting. */
  logoSrc?: string
  /** Detected Logo box on the poster canvas; the 100% Logo scale reference. */
  defaultLogoBox?: LogoOverlayBox
  onClose: () => void
}
type Framing = { zoom: number; x: number; y: number }
const initialFraming = { zoom: 100, x: 0, y: 0 }
const LOGO_ZOOM_MIN = 25
const LOGO_ZOOM_MAX = 300
const directions: Record<string, [number, number]> = {
  ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1],
}
const centreOf = (box: LogoOverlayBox) => ({ x: box.x + box.width / 2, y: box.y + box.height / 2 })
const clampLogoZoom = (percent: number) =>
  Math.min(LOGO_ZOOM_MAX, Math.max(LOGO_ZOOM_MIN, Math.round(percent)))

type Paint = {
  context: CanvasRenderingContext2D
  base: HTMLImageElement
  logo: HTMLImageElement | null
  removeWhiteBg: boolean
  canvasWidth: number
  canvasHeight: number
  imageWidth: number
  imageHeight: number
  framing: Framing
  box: LogoOverlayBox | null
}

/** The screen preview and the exported PNG share this, so only the affordances can differ. */
function paint({ context, base, logo, removeWhiteBg, canvasWidth, canvasHeight,
  imageWidth, imageHeight, framing, box }: Paint): void {
  const drawnWidth = imageWidth * framing.zoom / 100
  const drawnHeight = imageHeight * framing.zoom / 100
  context.clearRect(0, 0, canvasWidth, canvasHeight)
  context.imageSmoothingEnabled = true
  context.imageSmoothingQuality = 'high'
  context.drawImage(base, (canvasWidth - drawnWidth) / 2 + framing.x,
    (canvasHeight - drawnHeight) / 2 + framing.y, drawnWidth, drawnHeight)
  if (!logo || !box) return
  drawLogo({
    context, logo: removeWhiteBg ? withoutWhiteBacking(logo) : logo,
    logoWidth: logo.naturalWidth, logoHeight: logo.naturalHeight, box,
  })
}

type Drag = {
  id: number
  mode: 'frame' | 'logo-move' | 'logo-scale'
  framing: Framing
  logoBox: LogoOverlayBox | null
  logoZoom: number
  /** Pointer position in canvas pixels at drag start. */
  x: number
  y: number
}

export default function PosterCropDialog({ src, width, height, logoSrc, defaultLogoBox, onClose }: Props) {
  const titleId = useId()
  const dialogRef = useRef<HTMLDialogElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const drag = useRef<Drag | null>(null)
  const [image, setImage] = useState<HTMLImageElement | null>(null)
  const [logo, setLogo] = useState<HTMLImageElement | null>(null)
  const [whiteBacked, setWhiteBacked] = useState(false)
  const [framing, setFraming] = useState<Framing>(initialFraming)
  const [logoBox, setLogoBox] = useState<LogoOverlayBox | null>(defaultLogoBox ?? null)
  const [logoZoom, setLogoZoom] = useState(100)
  const [removeWhiteBg, setRemoveWhiteBg] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    const dialog = dialogRef.current
    if (dialog && !dialog.open) dialog.showModal()
    const source = new Image()
    source.onload = () => setImage(source)
    source.onerror = () => setError('图片加载失败，请关闭后重试。')
    source.src = src
    return () => { source.onload = source.onerror = null }
  }, [src])

  useEffect(() => {
    if (!logoSrc) return
    let cancelled = false
    loadImage(logoSrc).then((next) => {
      if (cancelled) return
      setWhiteBacked(hasWhiteBacking(next))
      setLogo(next)
    }).catch(() => { if (!cancelled) setError('Logo 加载失败，请关闭后重试。') })
    return () => { cancelled = true }
  }, [logoSrc])

  const cover = image ? Math.max(width / image.naturalWidth, height / image.naturalHeight) : 1
  const imageWidth = (image?.naturalWidth ?? width) * cover
  const imageHeight = (image?.naturalHeight ?? height) * cover
  const handleSize = Math.max(10, Math.round(Math.min(width, height) / 48))
  const corners = logoBox && logo ? [
    { x: logoBox.x, y: logoBox.y }, { x: logoBox.x + logoBox.width, y: logoBox.y },
    { x: logoBox.x, y: logoBox.y + logoBox.height },
    { x: logoBox.x + logoBox.width, y: logoBox.y + logoBox.height },
  ] : []

  function constrain(next: Framing): Framing {
    const maxX = Math.max(0, (imageWidth * next.zoom / 100 - width) / 2)
    const maxY = Math.max(0, (imageHeight * next.zoom / 100 - height) / 2)
    return { zoom: next.zoom, x: Math.min(maxX, Math.max(-maxX, next.x)), y: Math.min(maxY, Math.max(-maxY, next.y)) }
  }

  // The Logo may hang past an edge by its transparent padding, but a third stays visible.
  function constrainLogo(next: LogoOverlayBox): LogoOverlayBox {
    const keepX = Math.max(8, Math.round(next.width / 3))
    const keepY = Math.max(8, Math.round(next.height / 3))
    return {
      ...next,
      x: Math.min(width - keepX, Math.max(keepX - next.width, Math.round(next.x))),
      y: Math.min(height - keepY, Math.max(keepY - next.height, Math.round(next.y))),
    }
  }

  function resizeLogo(percent: number, from: LogoOverlayBox | null) {
    if (!from) return
    const next = clampLogoZoom(percent)
    setLogoZoom(next)
    setLogoBox(constrainLogo(scaleLogoBox(from, defaultLogoBox ?? from, next)))
  }

  const insideLogo = (x: number, y: number) => Boolean(logoBox && logo
    && x >= logoBox.x && x <= logoBox.x + logoBox.width
    && y >= logoBox.y && y <= logoBox.y + logoBox.height)

  const nearHandle = (x: number, y: number) =>
    corners.some((corner) => Math.hypot(corner.x - x, corner.y - y) <= handleSize * 1.8)

  const cursorFor = (x: number, y: number) =>
    nearHandle(x, y) ? 'nwse-resize' : insideLogo(x, y) ? 'move' : 'grab'

  useEffect(() => {
    const context = canvasRef.current?.getContext('2d')
    if (!image || !context) return
    paint({ context, base: image, logo, removeWhiteBg, canvasWidth: width, canvasHeight: height,
      imageWidth, imageHeight, framing, box: logoBox })
    if (!logo || !logoBox) return
    // The dashed frame and handles are screen-only; the exported PNG never contains them.
    const line = Math.max(1, Math.round(Math.min(width, height) / 480))
    const handle = Math.max(10, Math.round(Math.min(width, height) / 48))
    const handles = [
      { x: logoBox.x, y: logoBox.y }, { x: logoBox.x + logoBox.width, y: logoBox.y },
      { x: logoBox.x, y: logoBox.y + logoBox.height },
      { x: logoBox.x + logoBox.width, y: logoBox.y + logoBox.height },
    ]
    context.save()
    context.setLineDash([line * 8, line * 6])
    context.lineWidth = line * 2
    context.strokeStyle = 'rgba(255, 255, 255, 0.92)'
    context.strokeRect(logoBox.x, logoBox.y, logoBox.width, logoBox.height)
    context.strokeStyle = 'rgba(23, 24, 32, 0.72)'
    context.lineDashOffset = line * 8
    context.strokeRect(logoBox.x, logoBox.y, logoBox.width, logoBox.height)
    context.restore()
    for (const corner of handles) {
      context.save()
      context.fillStyle = 'rgba(23, 24, 32, 0.86)'
      context.strokeStyle = 'rgba(255, 255, 255, 0.92)'
      context.lineWidth = line * 2
      context.fillRect(corner.x - handle / 2, corner.y - handle / 2, handle, handle)
      context.strokeRect(corner.x - handle / 2, corner.y - handle / 2, handle, handle)
      context.restore()
    }
  }, [image, logo, width, height, imageWidth, imageHeight, framing, logoBox, removeWhiteBg])

  function exportPng() {
    if (!image) return
    setError('')
    // Export from an offscreen canvas so the dashed frame and handles never reach the file.
    const output = document.createElement('canvas')
    output.width = width
    output.height = height
    const context = output.getContext('2d')
    if (!context) { setError('PNG 导出失败，请重试。'); return }
    paint({ context, base: image, logo, removeWhiteBg, canvasWidth: width, canvasHeight: height,
      imageWidth, imageHeight, framing, box: logoBox })
    output.toBlob((blob) => {
      if (!blob) { setError('PNG 导出失败，请重试。'); return }
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `原照替换-${width}x${height}.png`
      link.click()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    }, 'image/png')
  }

  return <dialog ref={dialogRef} className="poster-crop-dialog" aria-labelledby={titleId} onClose={onClose}>
    <header>
      <h2 id={titleId}>裁剪画面</h2>
      <span>{width} × {height}</span>
    </header>
    <canvas ref={canvasRef} width={width} height={height} tabIndex={image ? 0 : -1}
      aria-label="拖动画面、拖动 Logo 或用方向键移动画面"
      onKeyDown={(event) => {
        const delta = directions[event.key]
        if (!delta || !image) return
        event.preventDefault()
        const step = event.shiftKey ? 40 : 10
        setFraming((previous) => constrain({ zoom: previous.zoom, x: previous.x + delta[0] * step, y: previous.y + delta[1] * step }))
      }}
      onPointerDown={(event) => {
        if (!image || drag.current || event.button !== 0) return
        const rect = event.currentTarget.getBoundingClientRect()
        const x = (event.clientX - rect.left) * width / Math.max(1, rect.width)
        const y = (event.clientY - rect.top) * height / Math.max(1, rect.height)
        event.currentTarget.setPointerCapture(event.pointerId)
        const onLogo = Boolean(logo && logoBox)
        drag.current = {
          id: event.pointerId,
          mode: !onLogo ? 'frame' : nearHandle(x, y) ? 'logo-scale'
            : insideLogo(x, y) ? 'logo-move' : 'frame',
          framing, logoBox, logoZoom, x, y,
        }
      }}
      onPointerMove={(event) => {
        const start = drag.current
        const rect = event.currentTarget.getBoundingClientRect()
        const x = (event.clientX - rect.left) * width / Math.max(1, rect.width)
        const y = (event.clientY - rect.top) * height / Math.max(1, rect.height)
        if (!start) {
          event.currentTarget.style.cursor = cursorFor(x, y)
          return
        }
        if (start.id !== event.pointerId) return
        if (start.mode === 'logo-scale') {
          // Equal-ratio scaling around the Logo's centre keeps its aspect ratio.
          const centre = start.logoBox ? centreOf(start.logoBox) : { x: 0, y: 0 }
          const from = Math.hypot(start.x - centre.x, start.y - centre.y) || 1
          resizeLogo(start.logoZoom * (Math.hypot(x - centre.x, y - centre.y) / from), start.logoBox)
          return
        }
        if (start.mode === 'logo-move' && start.logoBox) {
          setLogoBox(constrainLogo({
            ...start.logoBox, x: start.logoBox.x + x - start.x, y: start.logoBox.y + y - start.y,
          }))
          return
        }
        setFraming(constrain({
          zoom: start.framing.zoom, x: start.framing.x + x - start.x, y: start.framing.y + y - start.y,
        }))
      }}
      onLostPointerCapture={() => { drag.current = null }}
    />
    {error ? <p role="alert">{error}</p> : !image ? <p role="status">正在加载图片…</p> : null}
    <div className="poster-crop-controls">
      <label>{logo && logoBox ? '画面' : '缩放'} <input type="range" min="100" max="400"
        value={framing.zoom} disabled={!image}
        onChange={(event) => {
          const zoom = event.currentTarget.valueAsNumber
          setFraming((previous) => constrain({ zoom, x: previous.x * zoom / previous.zoom, y: previous.y * zoom / previous.zoom }))
        }} /> <output>{framing.zoom}%</output></label>
      {logo && logoBox ? <label>Logo <input type="range" min={LOGO_ZOOM_MIN} max={LOGO_ZOOM_MAX}
        value={logoZoom} disabled={!image}
        onChange={(event) => resizeLogo(event.currentTarget.valueAsNumber, logoBox)} /> <output>{logoZoom}%</output></label> : null}
      {whiteBacked && logoBox ? <label className="poster-crop-checkbox">
        <input type="checkbox" checked={removeWhiteBg} disabled={!image}
          onChange={(event) => setRemoveWhiteBg(event.target.checked)} />
        去除白底
      </label> : null}
      <button type="button" className="mp-btn-ghost mp-btn-small" disabled={!image}
        onClick={() => {
          setFraming(initialFraming)
          setLogoBox(defaultLogoBox ?? null)
          setLogoZoom(100)
          setRemoveWhiteBg(false)
        }}>重置</button>
    </div>
    <footer>
      <button type="button" className="mp-btn-secondary" onClick={onClose}>关闭</button>
      <button type="button" className="mp-btn-primary" disabled={!image} onClick={exportPng}>导出 PNG</button>
    </footer>
  </dialog>
}
