import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, applyOriginalReplacement, createOriginalReplacement, listOriginalReplacements, merchantPath, refreshOriginalReplacement, restoreOriginalReplacementArtifacts, retryModelReplacement, selectWholeImageVersion } from '../api'
import type { Asset, BrandLogoImage, OriginalReplacementTask, WholeImageTask } from '../types'
import { parseServerDate } from '../time'
import { assetDisplayName } from '../assetNames'
import { imageModelLabel } from '../modelGeneration'
import { ZoomableImage } from './ImageLightbox'

type Props = {
  task: WholeImageTask
  assets: Asset[]
  logo: BrandLogoImage | null
  onRefreshTask: () => Promise<void>
}
type Row = { id: string; target: string; asset: Asset }
const row = (asset: Asset): Row => ({ id: crypto.randomUUID(), target: '', asset })
const stages: Record<string, string> = { queued: '等待处理', preparing: '准备图片', agent_running: '正在替换', model_running: '模型正在替换', saving: '保存结果', done: '替换完成', failed: '处理未完成' }
const active = (record: OriginalReplacementTask) => record.status === 'queued' || record.status === 'running'
const message = (error: unknown) => error instanceof Error ? error.message : '请求失败，请稍后重试'

/** Reuse the images already attached to the design prompt so only positions stay empty. */
function carried(task: WholeImageTask, assets: Asset[]): Row[] {
  const images = task.model_input?.images ?? []
  const declared = [...(task.input.reference_asset_ids ?? []), ...(task.input.product_asset_ids ?? [])]
  const ids = declared.length
    ? declared
    : images.filter((item) => item.role === 'reference' || item.role === 'product').map((item) => item.asset_id)
  return [...new Set(ids)].map((id) => {
    const asset = assets.find((item) => item.id === id)
    const meta = images.find((item) => item.asset_id === id)
    return row(asset ?? {
      id, original_name: meta?.name ?? '原照',
      url: meta?.url || meta?.preview_url || merchantPath(`/api/assets/${encodeURIComponent(id)}/file`),
      width: 0, height: 0, mime_type: 'image/png', created_at: task.created_at,
    })
  })
}

function selectRecordUrl(id: string | null) {
  const url = new URL(window.location.href)
  const view = url.searchParams.get('view')
  if (view && view !== 'whole') return
  if (id) url.searchParams.set('replacement', id)
  else url.searchParams.delete('replacement')
  window.history.replaceState({}, '', url)
}

export default function OriginalReplacementPanel({ task, assets, logo, onRefreshTask }: Props) {
  const [rows, setRows] = useState<Row[]>(() => carried(task, assets))
  const [splitAllElements, setSplitAllElements] = useState(false)
  const [records, setRecords] = useState<OriginalReplacementTask[]>([])
  const [selectedId, setSelectedId] = useState<string | null>()
  const [loading, setLoading] = useState(true)
  const [operation, setOperation] = useState<'agent' | 'model' | 'feedback' | 'apply' | 'refresh' | 'restore' | null>(null)
  const [feedback, setFeedback] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [clock, setClock] = useState(Date.now)
  const lock = useRef(false)
  const mounted = useRef(true)
  const listRequest = useRef(0)
  const selected = records.find((item) => item.id === selectedId) ?? null
  const htmlArtifact = selected?.artifacts?.find((item) => item.kind === 'html')
  const running = records.some(active)
  const photos = rows.filter((item) => item.asset.id !== logo?.asset_id)
  const replacements = [
    ...(logo ? [{ target: 'Logo', asset_id: logo.asset_id }] : []),
    ...photos.map((item) => ({ target: item.target.trim(), asset_id: item.asset.id })),
  ]
  const currentOutput = task.outputs.find((item) => item.id === task.current_output_id)
  const canSubmit = Boolean(task.status === 'succeeded' && task.current_version_id && currentOutput?.selectable && task.edit.status !== 'running')

  const load = useCallback(() => {
    const request = ++listRequest.current
    return listOriginalReplacements(task.id).then((next) => {
      if (!mounted.current || request !== listRequest.current) return
      setRecords(next)
      setLoadError(null)
      setSelectedId((previous) => {
        if (previous !== undefined) return previous
        const fromUrl = new URLSearchParams(window.location.search).get('replacement')
        return next.find((item) => item.id === fromUrl)?.id ?? next[0]?.id ?? null
      })
    }).catch((reason: unknown) => {
      if (mounted.current && request === listRequest.current) setLoadError(message(reason))
    }).finally(() => {
      if (mounted.current && request === listRequest.current) setLoading(false)
    })
  }, [task.id])

  useEffect(() => {
    mounted.current = true
    void load()
    return () => { mounted.current = false }
  }, [load])

  useEffect(() => {
    if (!running) return
    let cancelled = false
    let timer: number
    const poll = async () => {
      await load()
      if (!cancelled) timer = window.setTimeout(() => void poll(), 5000)
    }
    timer = window.setTimeout(() => void poll(), 2000)
    const clockTimer = window.setInterval(() => setClock(Date.now()), 1000)
    return () => { cancelled = true; window.clearTimeout(timer); window.clearInterval(clockTimer) }
  }, [load, running])

  const updateRow = (id: string, values: Partial<Row>) => setRows((previous) => previous.map((item) => item.id === id ? { ...item, ...values } : item))
  const perform = async (kind: NonNullable<typeof operation>, action: () => Promise<void>) => {
    if (lock.current) return
    lock.current = true; setOperation(kind); setError(null)
    try { await action() }
    catch (reason: unknown) {
      if (reason instanceof ApiError && reason.code === 'http_409') {
        try { await onRefreshTask() }
        catch (refreshError: unknown) {
          if (mounted.current) setError(`${message(reason)}；状态刷新失败：${message(refreshError)}`)
          return
        }
      }
      if (mounted.current) setError(message(reason))
    } finally {
      lock.current = false
      if (mounted.current) setOperation(null)
    }
  }

  const openNewRecord = (next: OriginalReplacementTask) => {
    if (!mounted.current) return
    listRequest.current++
    setRecords((previous) => [next, ...previous])
    setSelectedId(next.id)
    selectRecordUrl(next.id)
    setClock(parseServerDate(next.created_at).getTime())
    setFeedback('')
  }

  const submit = (kind: 'agent' | 'model') => {
    if (!canSubmit || lock.current) return
    if (replacements.length < 1) { setError('请添加原照或选择 Logo'); return }
    if (replacements.length > 5) { setError('最多提交 5 项替换'); return }
    void perform(kind, async () => {
      const next = await createOriginalReplacement(task.id, task.current_version_id!, task.current_output_id!,
        replacements, kind === 'agent' && splitAllElements, kind)
      if (!mounted.current) return
      openNewRecord(next)
      setRows(carried(task, assets))
      setSplitAllElements(false)
    })
  }

  const chooseRecord = (id: string | null) => {
    setSelectedId(id); setError(null); setFeedback(''); selectRecordUrl(id)
    if (!id) setSplitAllElements(false)
  }
  const elapsed = selected ? Math.max(0, Math.floor((clock - parseServerDate(selected.started_at || selected.created_at).getTime()) / 1000)) : 0

  return (
    <div className="original-replacement-panel">
      {!loading && !selected ? <label className="original-replacement-mode">
        <input type="checkbox" checked={splitAllElements} disabled={Boolean(operation)} onChange={(event) => setSplitAllElements(event.target.checked)} />
        全部拆分元素（含艺术字）
      </label> : null}
      {records.length > 0 ? (
        <label className="original-replacement-history">替换记录
          <select value={selectedId ?? ''} disabled={Boolean(operation)} onChange={(event) => chooseRecord(event.target.value || null)}>
            <option value="">新建替换</option>
            {records.map((item, index) => {
              const modelBatch = item.kind === 'model'
              const status = stages[item.stage] || item.stage
              const label = modelBatch ? '已保存' : '已应用'
              return <option key={item.id} value={item.id}>记录 {records.length - index} · {modelBatch ? '仅替换元素 · ' : ''}基于 v{item.source_version} · {item.applied_version ? `${label} v${item.applied_version}` : status}</option>
            })}
          </select>
        </label>
      ) : null}
      {loading ? <p role="status">正在恢复替换记录…</p> : selected ? (
        <>
          {selected.kind === 'model' ? (
            <div className="original-model-inputs">
              <div className="original-model-base">
                <span>底稿 v{selected.source_version}</span>
                <ZoomableImage src={selected.source.preview_url} alt={`底稿 v${selected.source_version}`} />
              </div>
              <div>
                <span>替换素材</span>
                <div className="original-model-references">
                  {selected.replacements.map((item, index) => (
                    <figure key={`${index}-${item.asset_id}`}>
                      <ZoomableImage src={item.preview_url} alt={item.name} title={item.target ? `${item.target} · ${item.name}` : item.name} />
                      {item.target ? <figcaption title={item.target}>{item.target}</figcaption> : null}
                    </figure>
                  ))}
                </div>
              </div>
            </div>
          ) : (
            <div className="original-replacement-targets">
              {selected.replacements.map((item, index) => <div key={`${index}-${item.asset_id}`}><img src={item.preview_url} alt={item.name} /><span>{item.target || '自动识别'}</span></div>)}
            </div>
          )}
          {selected.split_all_elements ? <p className="original-replacement-mode">全部拆分元素（含艺术字）</p> : null}
          {active(selected) ? <div className="replacement-status" role="status">{stages[selected.stage] || selected.stage} · 已等待 {Math.floor(elapsed / 60)} 分 {elapsed % 60} 秒<p>可继续修改海报或关闭页面，结果仍基于提交时的 v{selected.source_version}。</p></div> : null}
          {selected.error ? <div className="asset-notice asset-notice-error" role="alert"><p>{selected.error.message}</p>
            {selected.run_id ? <button type="button" className="mp-btn-secondary mp-btn-small" disabled={Boolean(operation)} onClick={() => void perform('refresh', async () => {
              const next = await refreshOriginalReplacement(task.id, selected.id)
              if (mounted.current) {
                listRequest.current++
                setRecords((previous) => previous.map((item) => item.id === next.id ? next : item))
              }
            })}>查询已有结果</button> : null}
          </div> : null}
          {selected.kind === 'model' ? <>
            {selected.feedback ? <p className="original-model-feedback-text">本轮反馈：{selected.feedback}</p> : null}
            <div className="original-model-candidates">
              {selected.candidates?.map((candidate) => {
                const label = imageModelLabel(candidate.model)
                const versionId = candidate.applied_version_id
                return <div key={candidate.model} className="original-model-candidate">
                  <strong title={label}>{label}</strong>
                  {candidate.output ? <>
                    <ZoomableImage src={candidate.output.preview_url} alt={label} />
                    {versionId && versionId === task.current_version_id ? <span>当前 v{candidate.applied_version}</span>
                      : <button type="button" className="mp-btn-secondary mp-btn-small"
                          aria-label={versionId ? `切换到 ${label} v${candidate.applied_version}` : `应用 ${label}`}
                          disabled={Boolean(operation) || !canSubmit}
                          onClick={() => void perform('apply', async () => {
                            if (versionId) {
                              await selectWholeImageVersion(task.id, versionId)
                            } else {
                              await applyOriginalReplacement(task, selected.id, candidate.model)
                              await load()
                            }
                            await onRefreshTask()
                          })}>{versionId ? `切换到 v${candidate.applied_version}` : '应用'}</button>}
                  </> : <span className="original-model-status" title={candidate.error || undefined}>
                    {candidate.status === 'failed' ? candidate.error || '替换失败' : candidate.status === 'queued' ? '等待处理' : '正在替换'}
                  </span>}
                </div>
              })}
            </div>
            {!active(selected) ? <form className="original-model-feedback" onSubmit={(event) => {
              event.preventDefault()
              if (!feedback.trim()) return
              void perform('feedback', async () => openNewRecord(await retryModelReplacement(task.id, selected.id, feedback.trim())))
            }}>
              <label htmlFor="original-model-feedback">反馈</label>
              <textarea id="original-model-feedback" value={feedback} maxLength={1000} rows={2}
                onChange={(event) => setFeedback(event.target.value)} disabled={Boolean(operation)} />
              <button type="submit" className="mp-btn-primary replacement-submit" disabled={Boolean(operation) || !feedback.trim()}>
                {operation === 'feedback' ? '提交中…' : '再生成一批'}
              </button>
            </form> : null}
          </> : null}
          {htmlArtifact?.preview_url ? <a className="mp-btn-primary replacement-submit" href={htmlArtifact.preview_url} target="_blank" rel="noopener noreferrer">打开 HTML</a> : null}
          {selected.artifact_error ? <p className="original-replacement-warning" role="status">{selected.artifact_error}</p> : null}
          {selected.run_id && !active(selected) && (!htmlArtifact || selected.artifact_error) ? <button type="button" className="mp-btn-secondary mp-btn-small" disabled={Boolean(operation)} onClick={() => void perform('restore', async () => {
            const next = await restoreOriginalReplacementArtifacts(task.id, selected.id)
            if (mounted.current) {
              listRequest.current++
              setRecords((previous) => previous.map((item) => item.id === next.id ? next : item))
            }
          })}>{operation === 'restore' ? '正在加载…' : '加载 HTML 编辑器'}</button> : null}
        </>
      ) : (
        <>
          <div className="replacement-rows">
            {logo ? <div className="replacement-row original-replacement-row original-replacement-logo is-checked">
              <div className="original-replacement-choice">
                <img src={logo.thumb_url || logo.url} alt="Logo" />
                <span className="original-replacement-name">Logo</span>
              </div>
            </div> : null}
            {photos.map((item) => {
              const asset = assets.find((candidate) => candidate.id === item.asset.id) ?? item.asset
              return <div key={item.id} className="replacement-row original-replacement-row is-checked">
                <div className="original-replacement-choice">
                  <img src={asset.thumb_url || asset.url} alt="" loading="lazy" decoding="async" />
                  <input id={`original-target-${item.id}`} aria-label={`替换位置：${assetDisplayName(asset.original_name)}`} className="replacement-target" value={item.target} maxLength={1000} disabled={Boolean(operation)} placeholder="留空自动识别，如：右下角牛肉、背景" onChange={(event) => updateRow(item.id, { target: event.target.value })} />
                  <button type="button" className="mp-btn-ghost mp-btn-small original-replacement-remove" aria-label={`移除原照：${assetDisplayName(asset.original_name)}`} disabled={Boolean(operation)} onClick={() => setRows((previous) => previous.filter((candidate) => candidate.id !== item.id))}>移除</button>
                </div>
              </div>
            })}
          </div>
          {photos.length === 0 && !logo ? <p className="replacement-help">请先在设计需求中添加参考图或选择 Logo。</p> : null}
          {replacements.length > 5 ? <p className="original-replacement-warning" role="alert">最多提交 5 项，请移除部分原照或关闭 Logo。</p> : null}
          <div className="original-replacement-actions">
            <button type="button" className="mp-btn-secondary replacement-submit" disabled={Boolean(operation) || !canSubmit || replacements.length === 0 || replacements.length > 5}
              onClick={() => submit('agent')}>{operation === 'agent' ? '提交中…' : '生成可编辑海报'}</button>
            <button type="button" className="mp-btn-primary replacement-submit" disabled={Boolean(operation) || !canSubmit || replacements.length === 0 || replacements.length > 5}
              onClick={() => submit('model')}>{operation === 'model' ? '提交中…' : '仅替换元素'}</button>
          </div>
          {!canSubmit ? <p className="replacement-help">请等待当前操作完成，并先选定一张完整海报结果。</p> : null}
        </>
      )}
      {error || loadError ? <div className="asset-notice asset-notice-error" role="alert"><p>{error || loadError}</p><button type="button" className="mp-btn-ghost mp-btn-small" disabled={Boolean(operation)} onClick={() => void load()}>刷新替换记录</button></div> : null}
    </div>
  )
}
