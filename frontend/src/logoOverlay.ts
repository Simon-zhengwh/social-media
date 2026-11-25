import type { LogoOverlayBox } from './types'

/**
 * The official Logo is never redrawn by the edit model: the model only erases the old one,
 * and the browser draws the real file onto that clean base. Both sources are same-origin
 * endpoints, so the canvas stays untainted and `toDataURL` works.
 */

const WHITE = 242
const SOFT_WHITE = 226

export function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image()
    image.onload = () => resolve(image)
    image.onerror = () => reject(new Error('图片加载失败，请重试'))
    image.src = src
  })
}

/** Four opaque near-white corners mean the Logo was exported on a white card. */
export function hasWhiteBacking(image: HTMLImageElement): boolean {
  const canvas = document.createElement('canvas')
  canvas.width = image.naturalWidth
  canvas.height = image.naturalHeight
  const context = canvas.getContext('2d', { willReadFrequently: true })
  if (!context) return false
  context.drawImage(image, 0, 0)
  const corners: Array<[number, number]> = [
    [0, 0], [canvas.width - 1, 0], [0, canvas.height - 1], [canvas.width - 1, canvas.height - 1],
  ]
  return corners.every(([x, y]) => {
    const [r, g, b, alpha] = context.getImageData(x, y, 1, 1).data
    return alpha >= 250 && r >= WHITE && g >= WHITE && b >= WHITE
  })
}

/** Knock the white card out of a flat Logo export, softening the anti-aliased rim. */
export function withoutWhiteBacking(image: HTMLImageElement): HTMLCanvasElement {
  const canvas = document.createElement('canvas')
  canvas.width = image.naturalWidth
  canvas.height = image.naturalHeight
  const context = canvas.getContext('2d', { willReadFrequently: true })
  if (!context) return canvas
  context.drawImage(image, 0, 0)
  const pixels = context.getImageData(0, 0, canvas.width, canvas.height)
  const data = pixels.data
  for (let index = 0; index < data.length; index += 4) {
    const [r, g, b] = [data[index], data[index + 1], data[index + 2]]
    if (r >= WHITE && g >= WHITE && b >= WHITE) data[index + 3] = 0
    else if (r >= SOFT_WHITE && g >= SOFT_WHITE && b >= SOFT_WHITE) {
      data[index + 3] = Math.min(data[index + 3], Math.round(data[index + 3] / 2))
    }
  }
  context.putImageData(pixels, 0, 0)
  return canvas
}

type DrawInput = {
  context: CanvasRenderingContext2D
  logo: CanvasImageSource
  logoWidth: number
  logoHeight: number
  box: LogoOverlayBox
}

/** `contain` the Logo inside the box so its aspect ratio can never change. */
export function drawLogo({ context, logo, logoWidth, logoHeight, box }: DrawInput): void {
  const scale = Math.min(box.width / Math.max(logoWidth, 1), box.height / Math.max(logoHeight, 1))
  const width = Math.max(1, logoWidth * scale)
  const height = Math.max(1, logoHeight * scale)
  context.drawImage(logo, box.x + (box.width - width) / 2, box.y + (box.height - height) / 2, width, height)
}

export type CompositeLogoInput = {
  baseSrc: string
  logoSrc: string
  canvasWidth: number
  canvasHeight: number
  box: LogoOverlayBox
  removeWhiteBg?: boolean
  /** `for_crop` results need the poster's 100% centred crop framing, not a stretched fit. */
  coverBase?: boolean
}

/** Draw the clean base plus the official Logo and return a full-size `data:image/png` URL. */
export async function compositeLogoPng({
  baseSrc, logoSrc, canvasWidth, canvasHeight, box, removeWhiteBg = false, coverBase = false,
}: CompositeLogoInput): Promise<string> {
  const [base, logo] = await Promise.all([loadImage(baseSrc), loadImage(logoSrc)])
  const canvas = document.createElement('canvas')
  canvas.width = canvasWidth
  canvas.height = canvasHeight
  const context = canvas.getContext('2d')
  if (!context) throw new Error('无法合成图片，请重试')
  context.imageSmoothingEnabled = true
  context.imageSmoothingQuality = 'high'
  if (coverBase) {
    // Same framing as the crop dialog at 100%: cover the poster and stay centred.
    const cover = Math.max(canvasWidth / Math.max(1, base.naturalWidth),
      canvasHeight / Math.max(1, base.naturalHeight))
    const drawnWidth = base.naturalWidth * cover
    const drawnHeight = base.naturalHeight * cover
    context.drawImage(base, (canvasWidth - drawnWidth) / 2, (canvasHeight - drawnHeight) / 2,
      drawnWidth, drawnHeight)
  } else {
    context.drawImage(base, 0, 0, canvasWidth, canvasHeight)
  }
  drawLogo({
    context,
    logo: removeWhiteBg ? withoutWhiteBacking(logo) : logo,
    logoWidth: logo.naturalWidth,
    logoHeight: logo.naturalHeight,
    box,
  })
  return canvas.toDataURL('image/png')
}

/** Scale around the box's own centre so the Logo's aspect ratio never changes. */
export function scaleLogoBox(box: LogoOverlayBox, initial: LogoOverlayBox, percent: number): LogoOverlayBox {
  const factor = percent / 100
  const width = Math.max(8, Math.round(initial.width * factor))
  const height = Math.max(8, Math.round(initial.height * factor))
  return {
    ...box,
    x: Math.round(box.x + box.width / 2 - width / 2),
    y: Math.round(box.y + box.height / 2 - height / 2),
    width,
    height,
  }
}
