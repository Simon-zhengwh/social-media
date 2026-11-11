import { useEffect, useRef } from 'react'

type Props = {
  title: string
  description: string
  confirmLabel: string
  cancelLabel?: string
  /** While busy the dialog stays open and Esc/backdrop dismissal is ignored. */
  busy?: boolean
  busyLabel?: string
  tone?: 'danger' | 'primary'
  onConfirm: () => void
  onCancel: () => void
}

/**
 * In-app replacement for window.confirm.
 *
 * Uses a native <dialog> so focus trapping and the top layer come from the browser, and keeps the
 * project's own styling instead of the browser's unstyled prompt.
 */
export default function ConfirmDialog({
  title,
  description,
  confirmLabel,
  cancelLabel = '取消',
  busy = false,
  busyLabel,
  tone = 'danger',
  onConfirm,
  onCancel,
}: Props) {
  const dialogRef = useRef<HTMLDialogElement>(null)

  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog || dialog.open) return
    dialog.showModal()
  }, [])

  return (
    <dialog
      ref={dialogRef}
      className="confirm-dialog"
      aria-labelledby="confirm-dialog-title"
      aria-describedby="confirm-dialog-description"
      onCancel={(event) => {
        // Esc must not bypass the busy state; the panel behind keeps working until the call ends.
        event.preventDefault()
        if (!busy) onCancel()
      }}
    >
      <div className="confirm-dialog-head">
        <span className={`confirm-dialog-icon is-${tone}`} aria-hidden="true">
          {tone === 'danger' ? (
            <svg viewBox="0 0 20 20">
              <path d="M10 6.4v4.4" />
              <circle cx="10" cy="13.9" r=".9" fill="currentColor" stroke="none" />
              <path d="M10 3.4a6.6 6.6 0 1 0 0 13.2 6.6 6.6 0 0 0 0-13.2Z" />
            </svg>
          ) : (
            <svg viewBox="0 0 20 20">
              <circle cx="10" cy="10" r="6.6" />
              <path d="M10 9v4.2M10 6.6v.1" />
            </svg>
          )}
        </span>
        <div className="confirm-dialog-copy">
          <h2 id="confirm-dialog-title">{title}</h2>
          <p id="confirm-dialog-description">{description}</p>
        </div>
      </div>

      <div className="confirm-dialog-actions">
        <button type="button" onClick={onCancel} disabled={busy} autoFocus>
          {cancelLabel}
        </button>
        <button
          type="button"
          className={tone === 'danger' ? 'is-danger' : 'is-primary'}
          onClick={onConfirm}
          disabled={busy}
        >
          {busy ? (busyLabel ?? '处理中…') : confirmLabel}
        </button>
      </div>
    </dialog>
  )
}
