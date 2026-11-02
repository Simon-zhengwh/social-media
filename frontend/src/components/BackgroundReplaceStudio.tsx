import { useEffect, useRef, useState } from 'react'
import { ApiError, createBackgroundReplacement, getBackgroundReplacement, refreshBackgroundReplacement, uploadAsset } from '../api'
import type { Asset, BackgroundReplacementTask, PosterEditReference } from '../types'
import { parseServerDate } from '../time'
import AssetPicker from './AssetPicker'
import { ZoomableImage } from './ImageLightbox'

type Props = { assets: Asset[]; assetsLoading: boolean; onLoadAssets: () => void; onGoAssets: () => void }
type Row = { id: string; target: string; asset: Asset | null }
const emptyRow = (): Row => ({ id: crypto.randomUUID(), target: '', asset: null })
const stages: Record<string, string> = { queued: '等待处理', preparing: '准备图片', agent_running: '正在替换', saving: '保存结果', done: '替换完成', failed: '处理未完成' }
const message = (error: unknown): string => error instanceof ApiError || error instanceof Error ? error.message : '请求失败，请稍后重试'

function toAsset(reference: PosterEditReference): Asset {
  return { id: reference.asset_id, original_name: reference.name, width: reference.width, height: reference.height,
    mime_type: 'image/png', url: reference.preview_url, created_at: '' }
}

function setTaskUrl(taskId: string | null) {
  const url = new URL(window.location.href)
  url.searchParams.set('view', 'background_replace')
  if (taskId) url.searchParams.set('replacement', taskId)
  else url.searchParams.delete('replacement')
  window.history.replaceState({}, '', url)
}

export default function BackgroundReplaceStudio({ assets, assetsLoading, onLoadAssets, onGoAssets }: Props) {
  const [source, setSource] = useState<Asset | null>(null)
  const [rows, setRows] = useState<Row[]>(() => [emptyRow()])
  const [task, setTask] = useState<BackgroundReplacementTask | null>(null)
  const [pickerTarget, setPickerTarget] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [loading, setLoading] = useState(() => Boolean(new URLSearchParams(window.location.search).get('replacement')))
  const [error, setError] = useState<string | null>(null)
  const [clock, setClock] = useState(Date.now)
  const submitLock = useRef(false)
  const active = task?.status === 'queued' || task?.status === 'running'
  const taskId = task?.id
  const busy = submitting || uploading || loading || active

  useEffect(() => {
    const taskId = new URLSearchParams(window.location.search).get('replacement')
    if (!taskId) return
    let cancelled = false
    void getBackgroundReplacement(taskId).then((next) => {
      if (cancelled) return
      setTask(next)
      setSource(toAsset(next.source))
      setRows(next.replacements.map((item) => ({ id: crypto.randomUUID(), target: item.target, asset: toAsset(item) })))
    }).catch((reason: unknown) => { if (!cancelled) setError(message(reason)) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    if (!active || !taskId) return
    let cancelled = false
    let timer: number
    const poll = async () => {
      try {
        const next = await getBackgroundReplacement(taskId)
        if (!cancelled) { setTask(next); setError(null) }
      } catch (reason: unknown) {
        if (!cancelled) setError(`状态查询暂时失败，将继续查询：${message(reason)}`)
      }
      if (!cancelled) timer = window.setTimeout(() => void poll(), 5000)
    }
    timer = window.setTimeout(() => void poll(), 2000)
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [active, taskId])

  useEffect(() => {
    if (!active) return
    const timer = window.setInterval(() => setClock(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [active])

  const assign = (target: string, asset: Asset) => {
    if (target === 'source') setSource(asset)
    else setRows((previous) => previous.map((row) => row.id === target ? { ...row, asset } : row))
  }

  const upload = async (target: string, file?: File) => {
    if (!file || busy) return
    setUploading(true); setError(null)
    try { assign(target, await uploadAsset(file)); onLoadAssets() }
    catch (reason: unknown) { setError(message(reason)) }
    finally { setUploading(false) }
  }

  const submit = async () => {
    if (submitLock.current || busy) return
    if (!source) { setError('请先选择原图'); return }
    if (rows.some((row) => !row.target.trim() || !row.asset)) { setError('请填写每项替换元素，并选择对应图片'); return }
    submitLock.current = true; setSubmitting(true); setError(null)
    try {
      const next = await createBackgroundReplacement(source.id, rows.map((row) => ({ target: row.target.trim(), asset_id: row.asset!.id })))
      setTask(next); setClock(Date.now()); setTaskUrl(next.id)
    } catch (reason: unknown) { setError(message(reason)) }
    finally { submitLock.current = false; setSubmitting(false) }
  }

  const refresh = async () => {
    if (!task || busy || submitLock.current) return
    submitLock.current = true; setSubmitting(true); setError(null)
    try { setTask(await refreshBackgroundReplacement(task.id)) }
    catch (reason: unknown) { setError(message(reason)) }
    finally { submitLock.current = false; setSubmitting(false) }
  }

  const imageControls = (target: string, asset: Asset | null) => (
    <div className="replacement-image-control">
      {asset ? <ZoomableImage src={asset.url} alt={target === 'source' ? '原图' : '对应替换图片'} className="replacement-thumb" /> : <span className="replacement-image-empty">{target === 'source' ? '选择需要修改的原图' : '选择对应替换图片'}</span>}
      <div className="replacement-image-actions">
        <label className={`mp-btn-secondary mp-btn-small replacement-upload${busy ? ' is-disabled' : ''}`}>
          {asset ? '重新上传' : '本地上传'}
          <input type="file" accept="image/*" aria-label={target === 'source' ? '上传原图' : '上传对应替换图片'} disabled={busy}
            onChange={(event) => { const file = event.target.files?.[0]; event.target.value = ''; void upload(target, file) }} />
        </label>
        <button type="button" className="mp-btn-ghost mp-btn-small" disabled={busy} onClick={() => setPickerTarget(target)}>从资产库选择</button>
        {asset ? <span className="replacement-filename" title={asset.original_name}>{asset.original_name}</span> : null}
      </div>
    </div>
  )

  const elapsed = task ? Math.max(0, Math.floor((clock - parseServerDate(task.started_at || task.created_at).getTime()) / 1000)) : 0
  const previewSource = task ? toAsset(task.source) : source

  return (
    <main className="poster-edit-page replacement-page">
      <div className="poster-edit-layout replacement-layout">
        <section className="poster-edit-form-card replacement-form">
          <div className="poster-edit-section-head"><div><strong>背景替换</strong><span>菜品、人物、背景等指定元素</span></div>
            {task && !busy ? <button type="button" className="mp-btn-ghost mp-btn-small" onClick={() => { setTask(null); setSource(null); setRows([emptyRow()]); setError(null); setTaskUrl(null) }}>新建替换</button> : null}
          </div>
          <div className="replacement-section"><h2>1. 选择原图</h2>{imageControls('source', source)}</div>
          <div className="replacement-section">
            <div className="poster-edit-section-head"><h2>2. 填写替换项</h2><span>{rows.length}/5 项</span></div>
            <p className="replacement-help">每项指定一个元素及对应图片。相似元素请说明位置，例如「右下角的牛肉」。</p>
            <div className="replacement-rows">{rows.map((row, index) => (
              <div className="replacement-row" key={row.id}>
                <div className="poster-edit-section-head"><label htmlFor={`target-${row.id}`}>替换项 {index + 1}</label>
                  <button type="button" className="mp-btn-ghost mp-btn-small" disabled={busy || rows.length === 1} onClick={() => setRows((previous) => previous.filter((item) => item.id !== row.id))}>删除</button></div>
                <input id={`target-${row.id}`} className="replacement-target" value={row.target} disabled={busy} maxLength={1000} placeholder={['右下角的牛肉', '左侧菜品', '左上角的人物'][index % 3]}
                  onChange={(event) => setRows((previous) => previous.map((item) => item.id === row.id ? { ...item, target: event.target.value } : item))} />
                {imageControls(row.id, row.asset)}
              </div>
            ))}</div>
            <button type="button" className="mp-btn-secondary mp-btn-small" disabled={busy || rows.length >= 5} onClick={() => setRows((previous) => [...previous, emptyRow()])}>＋ 添加替换项</button>
          </div>
          <p className="replacement-help">通常约 30 分钟，最长等待 45 分钟，可稍后从任务中心查看。</p>
          <button type="button" className="mp-btn-primary replacement-submit" disabled={busy} onClick={() => void submit()}>{uploading ? '正在上传图片…' : loading ? '正在恢复任务…' : active ? '正在替换…' : submitting ? '提交中…' : '开始替换'}</button>
          {error ? <p className="asset-notice asset-notice-error" role="alert">{error}</p> : null}
        </section>
        <section className="poster-edit-result-card replacement-results">
          <div className="poster-edit-section-head"><strong>替换结果</strong><span aria-live="polite">{task ? stages[task.stage] || task.stage : '等待提交'}</span></div>
          {active ? <div className="replacement-status" role="status"><span className="status-dot status-dot-live" />{stages[task?.stage || 'queued']} · 已等待 {Math.floor(elapsed / 60)} 分 {elapsed % 60} 秒<p>任务会在后台继续处理，可从任务中心找回。</p></div> : null}
          {task?.error ? <div className="asset-notice asset-notice-error" role="alert"><p>{task.error.message}</p>
            {task.run_id ? <button type="button" className="mp-btn-secondary mp-btn-small" disabled={busy} onClick={() => void refresh()}>查询已有结果</button> : null}
          </div> : null}
          <div className="replacement-comparison">
            {previewSource ? <figure><figcaption>原图</figcaption><ZoomableImage src={previewSource.url} alt="替换前原图" className="replacement-preview" /></figure> : null}
            {task?.output ? <figure><figcaption>替换结果 · {task.output.width} × {task.output.height}</figcaption><ZoomableImage src={task.output.preview_url} alt="替换结果" className="replacement-preview" /><a className="mp-btn-secondary mp-btn-small" href={task.output.download_url} download>下载 PNG</a></figure> : null}
          </div>
          {!task && !source ? <div className="asset-empty"><strong>按元素替换，保留原海报</strong><span>选择原图，为每个需要替换的元素指定对应图片。</span></div> : null}
          {task?.agent_output ? <details className="replacement-agent-output" open={task.status !== 'succeeded'}><summary>处理说明与辅助文件</summary><pre>{task.agent_output}</pre></details> : null}
        </section>
      </div>
      <AssetPicker key={pickerTarget || 'closed'} open={pickerTarget !== null} purpose="product" assets={assets} loading={assetsLoading}
        onClose={() => setPickerTarget(null)} onSelect={(asset) => { if (pickerTarget) assign(pickerTarget, asset); setPickerTarget(null) }}
        onGoAssets={() => { setPickerTarget(null); onGoAssets() }} />
    </main>
  )
}
