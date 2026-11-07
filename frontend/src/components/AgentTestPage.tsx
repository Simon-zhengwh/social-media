import { useCallback, useEffect, useRef, useState } from 'react'
import { duration } from './agentTestTiming'
import './AgentTestPage.css'

type CaseImage = { name: string; sha256: string; size: number[]; preview_url: string; original_url: string }
type Case = { id: string; label: string; prompt: string; images: CaseImage[] }
type Entry = { ts?: string; role?: string; name?: string; content?: string; args?: string; result?: string; status?: string }
type Artifact = { file_name: string; content_type?: string; title?: string; download_url: string; size?: number }
type Run = {
  id: string; case_id: string; label: string; status: string; run_id: string | null; trace_id: string | null
  trace_url?: string; output: string; error?: string; transcript: Entry[]; artifacts: Artifact[]
  prompt: string; user_input?: string; submitted_input?: string; remote_input?: string; image_urls?: string[]
  started_at?: string; submitted_at?: string; finished_at?: string; remote_started_at?: string
  remote_completed_at?: string; report_url?: string; token_usage?: Record<string, number>
}
type Batch = { id: string; note: string; status: string; created_at: string; runs: Run[]; hidden?: boolean }
type History = { id: string; created_at: string; note: string; case_ids: string[]; hidden?: boolean }
type Catalog = { version: string; agent_id: string; server_url: string; cases: Case[] }

class RequestError extends Error {
  status: number
  constructor(message: string, status: number) { super(message); this.status = status }
}

const LABELS: Record<string, string> = {
  queued: '排队中', preparing: '准备素材', triggering: '提交中', running: '运行中', completed: '已完成',
  failed: '失败', timeout: '平台超时', cancelled: '已取消', trigger_unknown: '提交结果待确认',
  wait_timeout: '等待到期', needs_attention: '需要处理',
}

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api/agent-tests${path}`, options)
  const body = await response.json()
  if (!response.ok) throw new RequestError(typeof body.detail === 'string' ? body.detail : body.error?.message || `请求失败（${response.status}）`, response.status)
  return body as T
}

function localTime(value?: string) {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'
}

function safeUrl(value?: string) {
  try {
    const url = new URL(value || '')
    return ['https:', 'http:'].includes(url.protocol) ? url.href : undefined
  } catch { return undefined }
}

export default function AgentTestPage() {
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [selectedCase, setSelectedCase] = useState('text')
  const [history, setHistory] = useState<History[]>([])
  const [showHidden, setShowHidden] = useState(false)
  const [savingVisibility, setSavingVisibility] = useState(false)
  const [batch, setBatch] = useState<Batch | null>(null)
  const [runId, setRunId] = useState<string | null>(null)
  const [note, setNote] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [pendingId, setPendingId] = useState<string | null>(() => localStorage.getItem('agent-test-pending'))
  const [now, setNow] = useState(() => Date.now())
  const [copied, setCopied] = useState(false)
  const [previewArtifact, setPreviewArtifact] = useState(0)
  const loadSequence = useRef(0)

  const loadBatch = useCallback(async (id: string) => {
    const sequence = ++loadSequence.current
    const value = await api<Batch>(`/batches/${encodeURIComponent(id)}`)
    if (sequence !== loadSequence.current) return value
    setBatch(value)
    localStorage.setItem('agent-test-last-batch', value.id)
    if (localStorage.getItem('agent-test-pending') === value.id) {
      localStorage.removeItem('agent-test-pending')
      setPendingId(null)
    }
    setRunId(current => value.runs.some(r => r.id === current) ? current : value.runs[0]?.id || null)
    return value
  }, [])

  useEffect(() => {
    let active = true
    Promise.all([api<Catalog>('/cases'), api<History[]>('/batches?include_hidden=true')]).then(([cases, batches]) => {
      if (!active) return
      setCatalog(cases)
      setHistory(batches)
      setSelectedCase(current => cases.cases.some(c => c.id === current) ? current : cases.cases[0]?.id || '')
    }).catch(reason => { if (active) setError(String(reason.message)) })
    const saved = localStorage.getItem('agent-test-pending') || localStorage.getItem('agent-test-last-batch')
    if (saved) void Promise.resolve().then(() => loadBatch(saved)).catch(reason => { if (active) setError(reason.message) })
    return () => { active = false }
  }, [loadBatch])

  const activeBatchId = batch?.status === 'running' ? batch.id : null
  useEffect(() => {
    if (!activeBatchId) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try { await loadBatch(activeBatchId) } catch (reason) { if (!cancelled) setError(reason instanceof Error ? reason.message : '查询失败') }
      if (!cancelled) timer = setTimeout(poll, 3000)
    }
    timer = setTimeout(poll, 2000)
    return () => { cancelled = true; clearTimeout(timer) }
  }, [activeBatchId, loadBatch])

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [])

  const start = async (all: boolean) => {
    if (!catalog) return
    const requestId = crypto.randomUUID()
    localStorage.setItem('agent-test-pending', requestId)
    setPendingId(requestId)
    setSubmitting(true)
    setError(null)
    try {
      const value = await api<Batch>('/batches', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ request_id: requestId, case_ids: all ? catalog.cases.map(c => c.id) : [selectedCase], note }) })
      setBatch(value)
      setRunId(value.runs[0]?.id || null)
      localStorage.removeItem('agent-test-pending')
      localStorage.setItem('agent-test-last-batch', value.id)
      setPendingId(null)
      setHistory(await api<History[]>('/batches?include_hidden=true'))
    } catch (reason) {
      const rejected = reason instanceof RequestError && (reason.status < 500 || reason.message.startsWith('未配置') || reason.message.startsWith('固定测试素材'))
      if (rejected) { localStorage.removeItem('agent-test-pending'); setPendingId(null) }
      setError(`${reason instanceof Error ? reason.message : '提交失败'}${rejected ? '' : '。请先查询刚才批次，避免重复触发。'}`)
    } finally { setSubmitting(false) }
  }

  const clearBatch = () => {
    ++loadSequence.current
    setBatch(null)
    setRunId(null)
    localStorage.removeItem('agent-test-last-batch')
  }

  const toggleBatchVisibility = async () => {
    if (!batch || savingVisibility || submitting || pendingId !== null) return
    const hidden = !batch.hidden
    setSavingVisibility(true)
    setError(null)
    try {
      const updated = await api<Batch>(`/batches/${encodeURIComponent(batch.id)}/visibility`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ hidden }),
      })
      setHistory(current => current.map(item => item.id === updated.id ? { ...item, hidden: updated.hidden } : item))
      ++loadSequence.current
      if (hidden && !showHidden) clearBatch()
      else setBatch(updated)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '更新历史批次失败')
    } finally { setSavingVisibility(false) }
  }

  const visibleHistory = history.filter(item => showHidden || !item.hidden)
  const currentCase = catalog?.cases.find(c => c.id === selectedCase)
  const selectedRun = batch?.runs.find(r => r.id === runId) || batch?.runs[0]
  const htmlArtifacts = selectedRun?.artifacts.map((artifact, index) => ({ ...artifact, index }))
    .filter(artifact => artifact.content_type?.split(';')[0] === 'text/html' || /\.html?$/i.test(artifact.file_name)) || []
  const htmlPreview = htmlArtifacts.find(artifact => artifact.index === previewArtifact) || htmlArtifacts[0]
  const busy = submitting || savingVisibility || batch?.status === 'running' || pendingId !== null

  return <section className="agent-test-page">
    <div className="agent-test-intro"><div><span className="eyebrow">IMAGE EDITOR · BENCHMARK</span><h1>固定用例，一次看清运行过程</h1>
      <p>沿用五组原图和原始提示词。每例独立请求 Agent，全量测试依次运行。</p></div>
      <div className="agent-test-history"><label htmlFor="agent-test-history">历史批次（{visibleHistory.length}）</label>
        <select id="agent-test-history" aria-label="历史批次" value={visibleHistory.some(item => item.id === batch?.id) ? batch?.id : ''} disabled={savingVisibility}
          onChange={e => { if (e.target.value) void loadBatch(e.target.value).catch(reason => setError(reason.message)) }}>
          <option value="">选择测试记录</option>{visibleHistory.map(item => <option key={item.id} value={item.id}>{item.hidden ? '【已隐藏】' : ''}{localTime(item.created_at)} · {item.note || '未备注'}</option>)}
        </select>
        <div className="agent-test-history-actions"><label><input type="checkbox" checked={showHidden} disabled={savingVisibility} onChange={e => {
          setShowHidden(e.target.checked)
          if (!e.target.checked && batch?.hidden) clearBatch()
        }} />显示已隐藏</label>
          <button className="agent-test-text-button" disabled={!batch || savingVisibility || submitting || pendingId !== null} onClick={() => void toggleBatchVisibility()}>{savingVisibility ? '保存中…' : batch?.hidden ? '恢复显示' : '隐藏此批次'}</button>
        </div>
      </div></div>
    {error && <div className="agent-test-error" role="alert">{error}</div>}
    {pendingId && !submitting && <button className="secondary-button" onClick={() => void loadBatch(pendingId).then(() => setError(null)).catch(reason => setError(reason.message))}>查询刚才批次</button>}
    <div className="agent-test-layout">
      <div className="agent-test-input panel">
        <h2>基准用例</h2><div className="agent-test-cases" role="group" aria-label="选择基准用例">{catalog?.cases.map((item, index) =>
          <button key={item.id} className={item.id === selectedCase ? 'selected' : ''} aria-pressed={item.id === selectedCase} onClick={() => setSelectedCase(item.id)}><span>0{index + 1}</span>{item.label}</button>)}</div>
        {currentCase && <><div className="agent-test-reference-images">{currentCase.images.map((image, index) => <figure key={image.sha256}>
          <a href={image.original_url} target="_blank" rel="noreferrer"><img src={image.preview_url} alt={`图${index + 1}：${image.name}`} /></a>
          <figcaption>图{index + 1} · {image.name}<small>{image.size.join(' × ')}</small></figcaption></figure>)}</div>
          <label className="agent-test-prompt-label" htmlFor="agent-test-prompt">原始提示词</label><textarea id="agent-test-prompt" value={currentCase.prompt} readOnly rows={3} />
          <p className="agent-test-muted">原文直接发送，图片作为独立附件。素材固定复用。</p></>}
        <label className="agent-test-prompt-label" htmlFor="agent-test-note">本轮备注 <span>选填</span></label>
        <input id="agent-test-note" value={note} maxLength={100} onChange={e => setNote(e.target.value)} placeholder="例如：skill 优化版 v2" />
        <div className="agent-test-actions"><button className="primary-button" disabled={busy || !currentCase} onClick={() => void start(false)}>{submitting ? '正在提交…' : '运行此用例'}</button>
          <button className="secondary-button" disabled={busy || !catalog} onClick={() => void start(true)}>全量并发测试</button></div>
        <details className="agent-test-config"><summary>运行配置</summary><p>Agent <code>{catalog?.agent_id || '加载中'}</code></p><p>基准版本 <code>{catalog?.version || '—'}</code></p></details>
      </div>
      <div className="agent-test-results">
        {!batch ? <div className="panel agent-test-empty"><span>运行记录</span><h2>选择一个用例开始</h2><p>这里会显示每例耗时、工具记录、结果和 Trace 入口。</p></div> : <>
          <div className="panel agent-test-batch"><div><h2>{batch.note || '本轮测试'}</h2><p>{localTime(batch.created_at)} · {batch.runs.length} 个用例</p></div>
            {batch.status === 'needs_attention' && <button className="secondary-button" onClick={() => void api<Batch>(`/batches/${batch.id}/resume`, { method: 'POST' }).then(value => { setBatch(value); void loadBatch(value.id) }).catch(reason => setError(reason.message))}>继续查询已有运行</button>}</div>
          <div className="agent-test-run-tabs">{batch.runs.map(run => <button key={run.id} className={run.id === selectedRun?.id ? 'selected' : ''} onClick={() => setRunId(run.id)}><strong>{run.label}</strong><span>{LABELS[run.status] || run.status} · {duration(run, now)}</span></button>)}</div>
          {selectedRun && <div className="panel agent-test-run-detail">
            <div className="agent-test-run-heading"><div><span className={`agent-test-status ${selectedRun.status}`}>{LABELS[selectedRun.status] || selectedRun.status}</span><h2>{selectedRun.label}</h2></div><div><small className="agent-test-muted">运行耗时 </small><strong className="agent-test-elapsed">{duration(selectedRun, now)}</strong></div></div>
            <dl className="agent-test-times"><div><dt>开始准备</dt><dd>{localTime(selectedRun.started_at)}</dd></div><div><dt>提交 Agent</dt><dd>{localTime(selectedRun.submitted_at)}</dd></div><div><dt>远端开始</dt><dd>{localTime(selectedRun.remote_started_at)}</dd></div><div><dt>远端结束</dt><dd>{localTime(selectedRun.remote_completed_at)}</dd></div><div><dt>本地同步完成</dt><dd>{localTime(selectedRun.finished_at)}</dd></div></dl>
            <div className="agent-test-identifiers"><span>runId <code>{selectedRun.run_id || '等待返回'}</code></span><span>traceId <code>{selectedRun.trace_id || '平台尚未返回'}</code></span></div>
            <div className="agent-test-links">
              {safeUrl(selectedRun.trace_url) && <a className="secondary-button" href={safeUrl(selectedRun.trace_url)} target="_blank" rel="noreferrer">打开 Trace ↗</a>}
              {selectedRun.report_url && <a href={selectedRun.report_url}>下载运行详情 JSON</a>}
              {selectedRun.run_id && <button className="agent-test-text-button" onClick={() => void navigator.clipboard.writeText(`runId: ${selectedRun.run_id}\ntraceId: ${selectedRun.trace_id || ''}\n${selectedRun.trace_url || ''}`).then(() => setCopied(true))}>{copied ? '已复制' : '复制运行标识'}</button>}
            </div>
            <section className="agent-test-user-input">
              <div className="agent-test-user-input-head"><div><span>固定基准输入</span><h3>原始 userInput</h3></div><small>{selectedRun.user_input === selectedRun.remote_input ? '平台回读一致' : '以用例原文为准'}</small></div>
              <pre>{selectedRun.user_input || selectedRun.prompt}</pre>
              {selectedRun.submitted_input && <details><summary>查看实际提交给 Agent 的完整 input（含图片 URL）</summary><pre>{selectedRun.submitted_input}</pre></details>}
              {(selectedRun.image_urls?.length || 0) > 0 && <div className="agent-test-input-urls"><strong>随 input 发送的图片 URL</strong>{selectedRun.image_urls?.map((url, index) => <a key={url} href={url} target="_blank" rel="noreferrer">图 {index + 1} ↗</a>)}</div>}
              {selectedRun.remote_input && selectedRun.submitted_input !== selectedRun.remote_input && <details><summary>查看平台回读的 input</summary><pre>{selectedRun.remote_input}</pre></details>}
            </section>
            {selectedRun.error && <p className="agent-test-error">{selectedRun.error}</p>}
            <h3>结果与产物</h3>
            {htmlPreview && <section className="agent-test-html-preview">
              <div><strong>HTML 编辑器预览</strong>{htmlArtifacts.length > 1 && <select aria-label="选择 HTML 预览" value={htmlPreview.index} onChange={e => setPreviewArtifact(Number(e.target.value))}>{htmlArtifacts.map(file => <option key={file.index} value={file.index}>{file.title || file.file_name}</option>)}</select>}
                <a href={`/api/agent-tests/runs/${selectedRun.id}/artifacts/${htmlPreview.index}/preview`} target="_blank" rel="noreferrer">放大查看 ↗</a></div>
              <iframe key={`${selectedRun.id}-${htmlPreview.index}`} title={`${selectedRun.label} HTML 编辑器`} src={`/api/agent-tests/runs/${selectedRun.id}/artifacts/${htmlPreview.index}/preview`} sandbox="allow-scripts allow-downloads" />
              <p className="agent-test-muted">可以在预览中调整原照并导出 PNG。原始 HTML 与完整包见下方下载链接。</p>
            </section>}
            {selectedRun.artifacts.length > 0 && <div className="agent-test-artifacts">{selectedRun.artifacts.filter(file => safeUrl(file.download_url)).map((file, index) => <article key={`${file.download_url}-${index}`}>
              {file.content_type?.startsWith('image/') && <img src={safeUrl(file.download_url)} alt={file.title || file.file_name} loading="lazy" />}
              <a href={safeUrl(file.download_url)} target="_blank" rel="noreferrer">{file.title || file.file_name} ↗</a><small>{file.content_type || '文件'}{file.size ? ` · ${(file.size / 1024 / 1024).toFixed(2)} MB` : ''}</small></article>)}</div>}
            {selectedRun.output ? <pre className="agent-test-reply">{selectedRun.output}</pre> : <p className="agent-test-muted">等待 Agent 返回结果。</p>}
            <h3>执行过程 <span className="agent-test-muted">{selectedRun.transcript.length} 条记录</span></h3>
            {!selectedRun.transcript.length && <p className="agent-test-muted">平台尚未返回执行记录；运行状态会持续更新。</p>}
            <ol className="agent-test-timeline">{selectedRun.transcript.map((entry, index) => <li key={index}><details><summary><time>{entry.ts ? new Date(entry.ts).toLocaleTimeString('zh-CN', { hour12: false }) : '—'}</time><strong>{entry.name || entry.role || '记录'}</strong><span>{entry.status || entry.role}</span></summary>
              {entry.content && <pre>{entry.content}</pre>}{entry.args && <pre>{entry.args}</pre>}{entry.result && <pre>{entry.result}</pre>}</details></li>)}</ol>
          </div>}
        </>}
      </div>
    </div>
  </section>
}
