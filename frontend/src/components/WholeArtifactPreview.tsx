import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { WholeImageArtifact } from '../types'
import { getEditableProject, saveEditableProject } from '../api'
import { isEditorSaveMessage, latestSaveQueue, type EditorBinding } from '../editableProjectProtocol'

type Props = {
  artifact: WholeImageArtifact | null
  onClose: () => void
  projectId?: string
}

export default function WholeArtifactPreview(props: Props) {
  return <ArtifactPreview key={props.projectId || props.artifact?.id || 'closed'} {...props} />
}

function ArtifactPreview({ artifact, onClose, projectId }: Props) {
  const open = Boolean(artifact?.preview_url)
  const onCloseRef = useRef(onClose)
  const frame = useRef<HTMLIFrameElement>(null)
  const [projectLoaded, setProjectLoaded] = useState(false)
  const [saveStatus, setSaveStatus] = useState(projectId ? '正在读取已保存工程…' : '')

  useEffect(() => {
    if (!open || !projectId) return
    let active = true
    let snapshot: Record<string, unknown> | undefined
    const binding: EditorBinding = { sessionId: crypto.randomUUID(), projectId, revision: 0 }
    const persist = latestSaveQueue(async project => {
      if (!active) return
      setSaveStatus('正在保存到当前任务…')
      try {
        const saved = await saveEditableProject(projectId, binding.revision, project)
        if (!active) return
        binding.revision = saved.revision
        snapshot = project
        frame.current?.contentWindow?.postMessage({ type: 'image-editor-v2:saved', ...binding }, '*')
        setSaveStatus('已保存到当前任务')
      } catch (e) {
        if (!active) return
        const message = e instanceof Error ? e.message : '保存失败，请重试'
        frame.current?.contentWindow?.postMessage({ type: 'image-editor-v2:save-error', ...binding, message }, '*')
        setSaveStatus(message)
        throw e
      }
    })
    const onMessage = async (event: MessageEvent) => {
      if (event.source !== frame.current?.contentWindow || !active) return
      if (event.data?.type === 'image-editor-v2:ready' && snapshot) {
        frame.current?.contentWindow?.postMessage({ type: 'image-editor-v2:init', ...binding, project: snapshot }, '*')
        return
      }
      if (!isEditorSaveMessage(event.data, binding)) return
      await persist(event.data.project).catch(() => {})
    }
    window.addEventListener('message', onMessage)
    void getEditableProject(projectId).then(data => {
      if (!active) return
      if (!data.project) throw new Error('已保存工程缺失')
      snapshot = data.project; binding.revision = data.revision
      setProjectLoaded(true); setSaveStatus('修改后点击保存，之后可从当前任务继续编辑。')
    }).catch(e => { if (active) setSaveStatus(e instanceof Error ? e.message : '读取工程失败') })
    return () => { active = false; window.removeEventListener('message', onMessage) }
  }, [open, projectId])

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
  // Portal to the body: the panel lives inside a sticky column, which would otherwise trap this
  // fixed overlay below the page's sticky toolbars and clip it to that column.
  return createPortal(
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
        {!projectId || projectLoaded ? <iframe
          key={projectId || artifact.id}
          ref={frame}
          title={`${title}编辑器`}
          src={artifact.preview_url}
          sandbox="allow-scripts allow-downloads"
        /> : null}
        <p className="model-input-hint">
          {projectId ? saveStatus : '在编辑器里可拖动、缩放原照并导出 PNG；完全离线运行，不发起外部请求。'}
        </p>
      </div>
    </div>,
    document.body,
  )
}
