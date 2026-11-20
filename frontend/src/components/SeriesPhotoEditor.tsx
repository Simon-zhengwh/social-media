import { useCallback, useEffect, useRef, useState } from 'react'
import {
  SERIES_EDITOR_EXPORT_REQUEST,
  editorConflictMessage,
  injectEditorBridge,
  parseEditorMessage,
} from '../posterSeries'

type Props = {
  open: boolean
  /** Authenticated HTML artifact URL; the host fetches the text and injects its own bridge. */
  htmlUrl: string | null
  canvas: { width: number; height: number } | null
  busy: boolean
  onSave: (input: {
    sessionId: string
    base64: string
    width: number
    height: number
  }) => Promise<void>
  onClose: () => void
}

const REQUEST_TIMEOUT_MS = 30000

/**
 * Series "save to this page" host.
 *
 * The Agent's editor HTML runs in a sandboxed iframe with an opaque origin, so the host
 * injects a small bridge into the fetched document and only accepts PNG exports posted
 * back by that same frame, for this session, at the group canvas size.
 */
export default function SeriesPhotoEditor({ open, htmlUrl, canvas, busy, onSave, onClose }: Props) {
  const frameRef = useRef<HTMLIFrameElement | null>(null)
  const [sessionId] = useState(() => crypto.randomUUID())
  const [html, setHtml] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [ready, setReady] = useState(false)
  const pendingRef = useRef<((result: { base64: string; width: number; height: number } | null) => void) | null>(null)
  const timeoutRef = useRef<number | null>(null)
  const saveRef = useRef(onSave)
  useEffect(() => { saveRef.current = onSave }, [onSave])

  const settle = useCallback((result: { base64: string; width: number; height: number } | null) => {
    if (timeoutRef.current !== null) {
      window.clearTimeout(timeoutRef.current)
      timeoutRef.current = null
    }
    const resolve = pendingRef.current
    pendingRef.current = null
    resolve?.(result)
  }, [])

  useEffect(() => {
    if (!open || !htmlUrl) {
      setHtml(null)
      return
    }
    let cancelled = false
    setLoading(true)
    setReady(false)
    setError(null)
    void fetch(htmlUrl, { credentials: 'include' })
      .then(async (response) => {
        if (!response.ok) throw new Error(`编辑器页面加载失败（${response.status}）`)
        return response.text()
      })
      .then((text) => { if (!cancelled) setHtml(injectEditorBridge(text, sessionId)) })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : '编辑器页面加载失败')
      })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [open, htmlUrl, sessionId])

  useEffect(() => {
    if (!open) return
    const onMessage = (event: MessageEvent) => {
      const frame = frameRef.current
      // Only the frame this host rendered may answer, and only for this session.
      if (!frame || event.source !== frame.contentWindow) return
      if (!canvas) return
      const result = parseEditorMessage(event.data, { sessionId, width: canvas.width, height: canvas.height })
      if (result.kind === 'ignore') return
      if (result.kind === 'ready') {
        setReady(true)
        return
      }
      if (result.kind === 'error') {
        setError(result.message)
        settle(null)
        return
      }
      settle({ base64: result.base64, width: result.width, height: result.height })
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [open, canvas, sessionId, settle])

  useEffect(() => () => {
    if (timeoutRef.current !== null) window.clearTimeout(timeoutRef.current)
  }, [])

  const requestExport = () => new Promise<{ base64: string; width: number; height: number } | null>((resolve) => {
    const frame = frameRef.current
    if (!frame?.contentWindow) {
      resolve(null)
      return
    }
    settle(null)
    pendingRef.current = resolve
    frame.contentWindow.postMessage({ type: SERIES_EDITOR_EXPORT_REQUEST, session_id: sessionId }, '*')
    timeoutRef.current = window.setTimeout(() => {
      setError('编辑器没有返回可导出的画布，请确认页面已完全加载')
      settle(null)
    }, REQUEST_TIMEOUT_MS)
  })

  const handleSave = async () => {
    if (!canvas) {
      setError('当前分组画布尺寸未知，暂时无法保存')
      return
    }
    setSaving(true)
    setError(null)
    try {
      const exported = await requestExport()
      if (!exported) return
      await saveRef.current({ sessionId, base64: exported.base64, width: exported.width, height: exported.height })
      onClose()
    } catch (reason: unknown) {
      setError(editorConflictMessage(reason as { status?: number; message?: string }))
    } finally {
      setSaving(false)
    }
  }

  if (!open) return null
  return (
    <div className="series-editor-backdrop" role="dialog" aria-modal="true" aria-label="编辑这一页">
      <div className="series-editor-card">
        <div className="series-editor-head">
          <strong>编辑这一页</strong>
          <span className="series-editor-size">
            {canvas ? `画布 ${canvas.width}×${canvas.height}` : '画布尺寸未知'}
          </span>
          <button type="button" className="mp-btn-ghost mp-btn-small" aria-label="关闭编辑器" onClick={onClose}
            disabled={saving || busy}>关闭</button>
        </div>
        {loading ? <p role="status">正在加载编辑器…</p> : null}
        {html ? (
          <iframe
            ref={frameRef}
            className="series-editor-frame"
            title="海报原照编辑"
            sandbox="allow-scripts allow-downloads"
            srcDoc={html}
          />
        ) : null}
        {!ready && !loading && html ? <p className="series-editor-hint" role="status">编辑器加载中，稍候即可导出当前画布。</p> : null}
        {error ? <div className="asset-notice asset-notice-error" role="alert"><p>{error}</p></div> : null}
        <div className="series-editor-actions">
          <button type="button" className="mp-btn-primary mp-btn-small" onClick={() => void handleSave()}
            disabled={!canvas || loading || saving || busy}>
            {saving ? '保存中…' : '确认保存到本页'}
          </button>
        </div>
      </div>
    </div>
  )
}
