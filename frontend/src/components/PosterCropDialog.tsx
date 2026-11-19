import { useEffect, useId, useRef, useState } from 'react'

type Props = { src: string; width: number; height: number; onClose: () => void }
type Framing = { zoom: number; x: number; y: number }
const initialFraming = { zoom: 100, x: 0, y: 0 }
const directions: Record<string, [number, number]> = {
  ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1],
}

export default function PosterCropDialog({ src, width, height, onClose }: Props) {
  const titleId = useId()
  const dialogRef = useRef<HTMLDialogElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const drag = useRef<{ id: number; clientX: number; clientY: number; x: number; y: number } | null>(null)
  const [image, setImage] = useState<HTMLImageElement | null>(null)
  const [framing, setFraming] = useState<Framing>(initialFraming)
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

  const cover = image ? Math.max(width / image.naturalWidth, height / image.naturalHeight) : 1
  const imageWidth = (image?.naturalWidth ?? width) * cover
  const imageHeight = (image?.naturalHeight ?? height) * cover

  function constrain(next: Framing): Framing {
    const maxX = Math.max(0, (imageWidth * next.zoom / 100 - width) / 2)
    const maxY = Math.max(0, (imageHeight * next.zoom / 100 - height) / 2)
    return { zoom: next.zoom, x: Math.min(maxX, Math.max(-maxX, next.x)), y: Math.min(maxY, Math.max(-maxY, next.y)) }
  }

  useEffect(() => {
    const context = canvasRef.current?.getContext('2d')
    if (!image || !context) return
    const drawnWidth = imageWidth * framing.zoom / 100
    const drawnHeight = imageHeight * framing.zoom / 100
    context.clearRect(0, 0, width, height)
    context.imageSmoothingQuality = 'high'
    context.drawImage(image, (width - drawnWidth) / 2 + framing.x, (height - drawnHeight) / 2 + framing.y,
      drawnWidth, drawnHeight)
  }, [image, width, height, imageWidth, imageHeight, framing])

  function exportPng() {
    setError('')
    canvasRef.current?.toBlob((blob) => {
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
    <canvas ref={canvasRef} width={width} height={height} tabIndex={image ? 0 : -1} aria-label="拖动或用方向键移动画面"
      onKeyDown={(event) => {
        const delta = directions[event.key]
        if (!delta || !image) return
        event.preventDefault()
        const step = event.shiftKey ? 40 : 10
        setFraming((previous) => constrain({ zoom: previous.zoom, x: previous.x + delta[0] * step, y: previous.y + delta[1] * step }))
      }}
      onPointerDown={(event) => {
        if (!image || drag.current || event.button !== 0) return
        event.currentTarget.setPointerCapture(event.pointerId)
        drag.current = { id: event.pointerId, clientX: event.clientX, clientY: event.clientY, x: framing.x, y: framing.y }
      }}
      onPointerMove={(event) => {
        const start = drag.current
        if (!start || start.id !== event.pointerId) return
        const rect = event.currentTarget.getBoundingClientRect()
        const x = start.x + (event.clientX - start.clientX) * width / rect.width
        const y = start.y + (event.clientY - start.clientY) * height / rect.height
        setFraming((previous) => constrain({ zoom: previous.zoom, x, y }))
      }}
      onLostPointerCapture={() => { drag.current = null }}
    />
    {error ? <p role="alert">{error}</p> : !image ? <p role="status">正在加载图片…</p> : null}
    <div className="poster-crop-controls">
      <label>缩放 <input type="range" min="100" max="400" value={framing.zoom} disabled={!image}
        onChange={(event) => {
          const zoom = event.currentTarget.valueAsNumber
          setFraming((previous) => constrain({ zoom, x: previous.x * zoom / previous.zoom, y: previous.y * zoom / previous.zoom }))
        }} /> <output>{framing.zoom}%</output></label>
      <button type="button" className="mp-btn-ghost mp-btn-small" disabled={!image} onClick={() => setFraming(initialFraming)}>重置</button>
    </div>
    <footer>
      <button type="button" className="mp-btn-secondary" onClick={onClose}>关闭</button>
      <button type="button" className="mp-btn-primary" disabled={!image} onClick={exportPng}>导出 PNG</button>
    </footer>
  </dialog>
}
