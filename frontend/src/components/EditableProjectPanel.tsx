import { useEffect, useRef, useState } from 'react'
import { createEditableProject, listEditableProjects, merchantPath, refreshEditableProject } from '../api'
import type { EditablePosterProject, WholeImageTask, WholeImageOutput } from '../types'
import WholeArtifactPreview from './WholeArtifactPreview'

export default function EditableProjectPanel({ task, output, disabled }: { task: WholeImageTask; output: WholeImageOutput; disabled: boolean }) {
  const [project, setProject] = useState<EditablePosterProject | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [open, setOpen] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const operation = useRef({ source: '', id: '' })
  const source = `${task.id}:${task.current_version_id}:${output.id}`

  useEffect(() => {
    let active = true
    const poll = async () => {
      try {
        const items = await listEditableProjects(task.id)
        if (!active) return
        const selected = items.find(item => item.source_version_id === task.current_version_id && item.source_output_id === output.id) ?? null
        setProject(selected)
        setLoaded(true)
      } catch (e) {
        if (active) setError(e instanceof Error ? e.message : '读取可编辑工程失败')
      }
    }
    void poll()
    return () => { active = false }
  }, [source, task.id, task.current_version_id, output.id])

  async function create() {
    if (!task.current_version_id) return
    if (operation.current.source !== source) operation.current = { source, id: crypto.randomUUID() }
    setBusy(true); setError('')
    try {
      const created = await createEditableProject(task.id, task.current_version_id, output.id, operation.current.id)
      setProject(created)
    } catch (e) { setError(e instanceof Error ? e.message : '重建未能启动') }
    finally { setBusy(false) }
  }

  const projectId = project?.id
  const projectStatus = project?.status
  useEffect(() => {
    if (!projectId || !projectStatus || !['queued', 'running'].includes(projectStatus)) return
    let active = true
    let querying = false
    const timer = setInterval(() => {
      if (querying) return
      querying = true
      void listEditableProjects(task.id).then(items => {
        if (active) setProject(previous => items.find(item => item.id === projectId) ?? previous)
      }).catch(e => { if (active) setError(e instanceof Error ? e.message : '查询处理进度失败') })
        .finally(() => { querying = false })
    }, 3000)
    return () => { active = false; clearInterval(timer) }
  }, [projectId, projectStatus, task.id])

  async function refresh() {
    if (!project) return
    setBusy(true); setError('')
    try { setProject(await refreshEditableProject(project.id)) }
    catch (e) { setError(e instanceof Error ? e.message : '查询已有结果失败') }
    finally { setBusy(false) }
  }

  const active = project && ['queued', 'running'].includes(project.status)
  return <div className="preview-actions">
    {!project ? <button type="button" className="mp-btn-secondary mp-btn-small" disabled={disabled || busy || !loaded} onClick={() => void create()}>制作可编辑海报</button> : null}
    {active ? <span role="status">正在拆层并检查效果，可稍后从当前任务继续查看。</span> : null}
    {project?.status === 'ready' ? <button type="button" className="mp-btn-secondary mp-btn-small" onClick={() => setOpen(true)}>继续编辑海报</button> : null}
    {project && ['failed', 'interrupted'].includes(project.status) ? <>
      <span role="alert">{project.error || '本次重建尚未完成'}</span>
      {project.run_id ? <button type="button" className="mp-btn-ghost mp-btn-small" disabled={busy} onClick={() => void refresh()}>查询已有结果</button> : null}
    </> : null}
    {error ? <span role="alert">{error}</span> : null}
    <WholeArtifactPreview projectId={open ? project?.id : undefined} artifact={open && project ? {
      id: project.id, kind: 'html', title: '可编辑海报', file_name: 'editor.html', content_type: 'text/html', size: 0,
      preview_url: merchantPath(`/api/whole-image/editable-projects/${encodeURIComponent(project.id)}/preview`), download_url: null,
    } : null} onClose={() => setOpen(false)} />
  </div>
}
