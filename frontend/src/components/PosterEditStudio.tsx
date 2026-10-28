import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, createPosterEditBatch, getPosterEdit, getPosterEditBatch, uploadAsset } from '../api'
import type { Asset, PosterEditOutput, PosterEditTask } from '../types'
import { parseServerDate } from '../time'
import AssetPicker from './AssetPicker'
import CandidateCountSelector from './CandidateCountSelector'
import { ZoomableImage } from './ImageLightbox'

type Props = { assets: Asset[]; assetsLoading: boolean; onLoadAssets: () => void; onGoAssets: () => void }

const STATUS_LABELS: Record<string, string> = { queued: '等待中', running: '处理中', succeeded: '已完成', partial: '部分完成', failed: '失败', interrupted: '已中断' }
const STAGE_LABELS: Record<string, string> = { queued: '等待处理', preparing: '准备图片', agent_running: '正在修改', saving: '正在保存', done: '已完成', failed: '处理失败' }

function errorMessage(error: unknown): string { return error instanceof ApiError ? error.message : error instanceof Error ? error.message : '请求失败，请稍后重试' }
function outputTitle(output: PosterEditOutput): string { return output.role === 'agent_transparent' ? '透明图层' : output.role === 'agent_result' ? '修改结果' : output.label }
function finalOutput(task: PosterEditTask | null): PosterEditOutput | null { if (!task) return null; return task.outputs.find((output) => output.role.startsWith('agent_result')) ?? task.outputs[0] ?? null }
function elapsedText(startedAt: string): string { const started = parseServerDate(startedAt).getTime(); if (!Number.isFinite(started)) return ''; const seconds = Math.max(0, Math.floor((Date.now() - started) / 1000)); return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒` }
function readPosterRoute(): { batchId: string | null; taskId: string | null; candidateIndex: number | null } { const params = new URLSearchParams(window.location.search); const candidate = Number(params.get('candidate')); return { batchId: params.get('poster_batch'), taskId: params.get('poster'), candidateIndex: Number.isInteger(candidate) && candidate >= 0 ? candidate : null } }
function setPosterBatchUrl(batchId: string | null, candidateIndex?: number | null) { const url = new URL(window.location.href); url.searchParams.set('view', 'poster_edit'); if (batchId) { url.searchParams.set('poster_batch', batchId); url.searchParams.delete('poster'); if (candidateIndex !== undefined && candidateIndex !== null) url.searchParams.set('candidate', String(candidateIndex)); } else { url.searchParams.delete('poster_batch'); url.searchParams.delete('poster'); url.searchParams.delete('candidate') } window.history.replaceState({}, '', url) }
function taskReferenceAssets(task: PosterEditTask | undefined): Asset[] { return task ? task.references.map((reference) => ({ id: reference.asset_id, original_name: reference.name, width: reference.width, height: reference.height, mime_type: 'image/png', url: reference.preview_url, created_at: task.created_at })) : [] }

export default function PosterEditStudio({ assets, assetsLoading, onLoadAssets, onGoAssets }: Props) {
  const [references, setReferences] = useState<Asset[]>([])
  const [prompt, setPrompt] = useState('')
  const [candidateCount, setCandidateCount] = useState(3)
  const [pickerOpen, setPickerOpen] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [batchId, setBatchId] = useState<string | null>(null)
  const [tasks, setTasks] = useState<PosterEditTask[]>([])
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [, forceTick] = useState(0)
  const submitLock = useRef(false)
  const active = tasks.some((task) => task.status === 'queued' || task.status === 'running')
  const selectedTask = tasks.find((task) => task.id === selectedTaskId) ?? tasks[0] ?? null

  const applyBatch = useCallback((nextTasks: PosterEditTask[], nextBatchId: string) => {
    setTasks(nextTasks)
    setBatchId(nextBatchId)
    setSelectedTaskId((current) => { if (current && nextTasks.some((task) => task.id === current)) return current; const preferred = readPosterRoute().candidateIndex; return nextTasks[preferred ?? 0]?.id ?? nextTasks[0]?.id ?? null })
    const first = nextTasks[0]
    if (first) {
      setPrompt((value) => value || first.prompt)
      setReferences((value) => value.length ? value : taskReferenceAssets(first))
      setCandidateCount(first.candidate_count)
    }
  }, [])

  const loadBatch = useCallback(async (nextBatchId: string) => { const batch = await getPosterEditBatch(nextBatchId); applyBatch(batch.tasks, batch.id) }, [applyBatch])
  const loadTask = useCallback(async (taskId: string) => { const task = await getPosterEdit(taskId); applyBatch([task], task.batch_id || task.id) }, [applyBatch])

  useEffect(() => {
    const route = readPosterRoute()
    if (!route.batchId && !route.taskId) return
    const timer = window.setTimeout(() => { const load = route.batchId ? loadBatch(route.batchId) : loadTask(route.taskId!); void load.catch((reason: unknown) => setError(errorMessage(reason))) }, 0)
    return () => window.clearTimeout(timer)
  }, [loadBatch, loadTask])

  useEffect(() => {
    if (!active || !batchId) return
    const timer = window.setInterval(() => { void loadBatch(batchId).catch((reason: unknown) => setError(errorMessage(reason))) }, 2000)
    return () => window.clearInterval(timer)
  }, [active, batchId, loadBatch])

  useEffect(() => { if (!active) return; const timer = window.setInterval(() => forceTick((value) => value + 1), 1000); return () => window.clearInterval(timer) }, [active])

  const addFiles = async (files: File[]) => {
    const remaining = Math.max(0, 5 - references.length)
    const accepted = files.slice(0, remaining)
    if (!accepted.length) return
    setUploading(true); setError(null)
    try { const uploaded = await Promise.all(accepted.map((file) => uploadAsset(file))); setReferences((current) => [...current, ...uploaded].slice(0, 5)); onLoadAssets() }
    catch (reason: unknown) { setError(errorMessage(reason)) }
    finally { setUploading(false) }
  }

  const submit = async () => {
    if (submitLock.current || submitting || active) return
    if (!references.length) { setError('请先添加 1–5 张参考图片'); return }
    const instruction = prompt.trim()
    if (!instruction) { setError('请输入修改要求'); return }
    submitLock.current = true; setSubmitting(true); setError(null); setTasks([])
    try { const batch = await createPosterEditBatch(instruction, references.map((item) => item.id), candidateCount); applyBatch(batch.tasks, batch.id); setPosterBatchUrl(batch.id, 0) }
    catch (reason: unknown) { setError(errorMessage(reason)) }
    finally { submitLock.current = false; setSubmitting(false) }
  }

  const busy = submitting || uploading || active
  const completedCount = tasks.filter((task) => task.status === 'succeeded').length
  const selectedOutput = finalOutput(selectedTask)

  return (
    <main className="poster-edit-page">
      <div className="poster-edit-layout">
        <section className="poster-edit-form-card">
          <div className="poster-edit-section-head"><div><strong>参考图片</strong><span>已选 {references.length}/5</span></div><button type="button" className="mp-btn-ghost mp-btn-small" onClick={() => setPickerOpen(true)} disabled={busy}>从资产库选择</button></div>
          <div className="poster-edit-reference-grid">
            {references.map((asset, index) => (
              <figure key={asset.id} className="poster-edit-reference">
                <ZoomableImage src={asset.url} alt={asset.original_name} title={asset.original_name} />
                <figcaption><strong>图 {index + 1}</strong><span>{asset.original_name}</span></figcaption>
                <button type="button" className="poster-edit-reference-remove" onClick={() => setReferences((items) => items.filter((item) => item.id !== asset.id))} disabled={busy} aria-label={`移除 ${asset.original_name}`}>×</button>
              </figure>
            ))}
            {references.length < 5 ? <label className={`poster-edit-upload${busy ? ' is-disabled' : ''}`}><input type="file" multiple accept="image/*" disabled={busy} onChange={(event) => { void addFiles(Array.from(event.target.files ?? [])); event.target.value = '' }} /><strong>{uploading ? '上传中…' : '＋ 添加图片'}</strong><span>PNG / JPG / WebP</span></label> : null}
          </div>
          <label className="poster-edit-prompt"><span>修改要求</span><textarea rows={6} maxLength={5000} value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="例如：把图一的背景换成图二的餐厅场景，保留海报文字、Logo 和菜品主体不变。" disabled={busy} /></label>
          <CandidateCountSelector value={candidateCount} busy={busy} onChange={setCandidateCount} />
          <button type="button" className="mp-btn-dark poster-edit-submit" onClick={() => void submit()} disabled={busy}>{active ? '正在修改…' : submitting ? '正在提交…' : '开始修改'}</button>
        </section>

        <section className="poster-edit-result-card">
          {!tasks.length ? <div className="poster-edit-empty"><span className="poster-edit-empty-mark">改</span><strong>等待修改</strong><p>结果会显示在这里。</p></div> : <>
            <div className="poster-edit-result-head"><div><span className="eyebrow">修改记录</span><h2>{active ? `正在生成 ${completedCount}/${tasks.length}` : selectedTask ? (STAGE_LABELS[selectedTask.stage] ?? selectedTask.stage) : '处理完成'}</h2></div><span className={`poster-edit-status is-${selectedTask?.status ?? 'queued'}`}>{STATUS_LABELS[selectedTask?.status ?? 'queued'] ?? selectedTask?.status}</span></div>
            <div className="poster-edit-candidates" role="tablist" aria-label="修改候选">
              {tasks.map((task, index) => { const output = finalOutput(task); const running = task.status === 'queued' || task.status === 'running'; return <button type="button" role="tab" aria-selected={task.id === selectedTask?.id} className={`poster-edit-candidate${task.id === selectedTask?.id ? ' is-active' : ''}`} key={task.id} onClick={() => { setSelectedTaskId(task.id); setPosterBatchUrl(batchId, index) }}><span className="poster-edit-candidate-thumb">{output?.preview_url ? <img src={output.preview_url} alt="" /> : <span>{running ? '生成中' : '无结果'}</span>}</span><strong>候选 {index + 1}</strong><span className={`poster-edit-candidate-status is-${task.status}`}>{running ? elapsedText(task.created_at) : STATUS_LABELS[task.status] ?? task.status}</span></button> })}
            </div>
            {selectedTask && (selectedTask.status === 'queued' || selectedTask.status === 'running') ? <div className="poster-edit-running" role="status" aria-live="polite"><span className="generating-dots" aria-hidden="true"><span /><span /><span /></span><strong>正在修改候选 {selectedTask.candidate_index + 1}</strong><span>已等待 {elapsedText(selectedTask.created_at)}</span><div className="poster-edit-progress"><span style={{ width: `${Math.max(8, selectedTask.progress)}%` }} /></div></div> : null}
            {selectedTask?.error ? <div className="poster-edit-error" role="alert"><strong>候选修改失败</strong><span>{selectedTask.error.message}</span></div> : null}
            {selectedTask?.outputs.length ? <div className="poster-edit-output-grid">{selectedTask.outputs.map((output) => <article className={`poster-edit-output${output.role === 'agent_transparent' ? ' is-transparent' : ''}`} key={output.id}><div className="poster-edit-output-head"><strong>{outputTitle(output)}</strong><span>{output.width} × {output.height}</span></div>{output.preview_url ? <ZoomableImage className="poster-edit-output-image" src={output.preview_url} alt={outputTitle(output)} title={outputTitle(output)} /> : null}{output.download_url ? <a className="mp-btn-secondary mp-btn-small" href={output.download_url} download>下载 PNG</a> : null}</article>)}</div> : null}
            {selectedOutput ? <p className="poster-edit-selected-note">当前候选结果：候选 {(selectedTask?.candidate_index ?? 0) + 1}</p> : null}
            {selectedTask?.agent_input ? <details className="poster-edit-agent-input"><summary>查看修改参数</summary><pre>{selectedTask.agent_input}</pre></details> : null}
          </>}
        </section>
      </div>
      {error ? <div className="global-error" role="alert"><span>{error}</span><button type="button" onClick={() => setError(null)}>关闭</button></div> : null}
      <AssetPicker key={pickerOpen ? 'poster-edit-picker' : 'poster-edit-closed'} open={pickerOpen} purpose="product" assets={assets} loading={assetsLoading} multi selectedIds={references.map((item) => item.id)} max={5} onClose={() => setPickerOpen(false)} onConfirm={(selected) => { setReferences(selected.slice(0, 5)); setPickerOpen(false) }} onGoAssets={() => { setPickerOpen(false); onGoAssets() }} />
    </main>
  )
}
