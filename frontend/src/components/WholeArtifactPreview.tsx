import { useEffect, useRef } from 'react'
import type { WholeImageArtifact } from '../types'

type Props = {
  artifact: WholeImageArtifact | null
  onClose: () => void
}

export default function WholeArtifactPreview({ artifact, onClose }: Props) {
  const open = Boolean(artifact?.preview_url)
  const onCloseRef = useRef(onClose)

  useEffect(() => {
    onCloseRef.current = onClose
  })

  useEffect(() => {
    if (!open) {
      return
    }

    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopPropagation()
        onCloseRef.current()
      }
    }

    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.body.style.overflow = previousOverflow
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  if (!artifact?.preview_url) {
    return null
  }

  const title = artifact.title || artifact.file_name
  return (
    <div
      className="version-preview-backdrop"
      role="dialog"
      aria-modal="true"
      aria-label={`${title}预览`}
      onClick={onClose}
    >
      <div className="artifact-preview-card" onClick={(event) => event.stopPropagation()}>
        <div className="version-preview-head">
          <strong>{title}</strong>
          <div className="artifact-preview-actions">
            {artifact.download_url ? (
              <a className="mp-btn-ghost mp-btn-small" href={artifact.download_url} download>
                下载 HTML
              </a>
            ) : null}
            <button type="button" onClick={onClose}>
              关闭
            </button>
          </div>
        </div>
        <iframe
          title={`${title}编辑器`}
          src={artifact.preview_url}
          sandbox="allow-scripts allow-downloads"
        />
        <p className="model-input-hint">
          在编辑器里可拖动、缩放原照并导出 PNG；完全离线运行，不发起外部请求。
        </p>
      </div>
    </div>
  )
}
