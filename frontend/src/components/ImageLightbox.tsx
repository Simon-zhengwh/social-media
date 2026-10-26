import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { createPortal } from 'react-dom'

type PreviewImage = {
  src: string
  alt: string
  title?: string
}

type ImageLightboxContextValue = {
  openImage: (image: PreviewImage, trigger: HTMLElement | null) => void
}

const ImageLightboxContext = createContext<ImageLightboxContextValue | null>(null)

function MagnifierIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="10.5" cy="10.5" r="6.5" />
      <path d="m15.5 15.5 5 5" />
      <path d="M10.5 7.5v6M7.5 10.5h6" />
    </svg>
  )
}

export function ImageLightboxProvider({ children }: { children: ReactNode }) {
  const [preview, setPreview] = useState<PreviewImage | null>(null)
  const [imageFailed, setImageFailed] = useState(false)
  const closeButtonRef = useRef<HTMLButtonElement>(null)
  const dialogRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLElement | null>(null)

  const openImage = useCallback((image: PreviewImage, trigger: HTMLElement | null) => {
    triggerRef.current =
      trigger ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null)
    setImageFailed(false)
    setPreview(image)
  }, [])

  const close = useCallback(() => {
    setPreview(null)
  }, [])

  useEffect(() => {
    if (!preview) {
      return
    }

    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const focusFrame = window.requestAnimationFrame(() => closeButtonRef.current?.focus())

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        close()
        return
      }

      if (event.key !== 'Tab' || !dialogRef.current) {
        return
      }

      const focusable = Array.from(
        dialogRef.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      )
      if (focusable.length === 0) {
        event.preventDefault()
        dialogRef.current.focus()
        return
      }

      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', onKeyDown)
    return () => {
      window.cancelAnimationFrame(focusFrame)
      document.body.style.overflow = previousOverflow
      document.removeEventListener('keydown', onKeyDown)
      triggerRef.current?.focus()
    }
  }, [close, preview])

  return (
    <ImageLightboxContext.Provider value={{ openImage }}>
      {children}
      {preview
        ? createPortal(
            <div
              className="image-lightbox-backdrop"
              onMouseDown={(event) => {
                if (event.target === event.currentTarget) {
                  close()
                }
              }}
            >
              <div
                className="image-lightbox-card"
                ref={dialogRef}
                role="dialog"
                aria-modal="true"
                aria-labelledby="image-lightbox-title"
                tabIndex={-1}
              >
                <div className="image-lightbox-head">
                  <strong id="image-lightbox-title">{preview.title || preview.alt}</strong>
                  <button
                    ref={closeButtonRef}
                    type="button"
                    className="image-lightbox-close"
                    onClick={close}
                    aria-label="关闭大图"
                  >
                    <span aria-hidden="true">×</span>
                    关闭
                  </button>
                </div>
                {imageFailed ? (
                  <div className="image-lightbox-error" role="alert">
                    <strong>图片加载失败</strong>
                    <span>{preview.title || preview.alt}</span>
                  </div>
                ) : (
                  <img
                    className="image-lightbox-image"
                    src={preview.src}
                    alt={preview.alt}
                    onError={() => setImageFailed(true)}
                  />
                )}
              </div>
            </div>,
            document.body,
          )
        : null}
    </ImageLightboxContext.Provider>
  )
}

type ZoomableImageProps = {
  src: string
  alt: string
  title?: string
  className?: string
  imageClassName?: string
}

export function ZoomableImage({
  src,
  alt,
  title,
  className = '',
  imageClassName = '',
}: ZoomableImageProps) {
  const context = useContext(ImageLightboxContext)
  const buttonRef = useRef<HTMLButtonElement>(null)

  if (!context) {
    throw new Error('ZoomableImage must be used within ImageLightboxProvider')
  }

  return (
    <button
      ref={buttonRef}
      type="button"
      className={`zoomable-image${className ? ` ${className}` : ''}`}
      onClick={() => context.openImage({ src, alt, title }, buttonRef.current)}
      aria-label={`放大查看：${title || alt}`}
      title="点击查看大图"
    >
      <img className={imageClassName || undefined} src={src} alt={alt} />
      <span className="zoomable-image-indicator" aria-hidden="true">
        <MagnifierIcon />
      </span>
    </button>
  )
}