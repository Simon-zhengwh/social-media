import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, applyOriginalReplacement, createOriginalReplacement, listOriginalReplacements, merchantPath, refreshOriginalReplacement, uploadAsset } from '../api'
import type { Asset, OriginalReplacementTask, WholeImageTask } from '../types'
import { parseServerDate } from '../time'
import AssetPicker from './AssetPicker'
import { ZoomableImage } from './ImageLightbox'

type Props = {
  task: WholeImageTask
  assets: Asset[]
  assetsLoading: boolean
  onLoadAssets: () => void
  onGoAssets: () => void
  onApplied: (task: WholeImageTask) => Promise<void>
  onRefreshTask: () => Promise<void>
}
type Row = { id: string; target: string; asset: Asset }
const row = (asset: Asset): Row => ({ id: crypto.randomUUID(), target: '', asset })
const stages: Record<string, string> = { queued: '等待处理', preparing: '准备图片', agent_running: '正在替换', saving: '保存结果', done: '替换完成', failed: '处理未完成' }
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

export default function OriginalReplacementPanel({ task, assets, assetsLoading, onLoadAssets, onGoAssets, onApplied, onRefreshTask }: Props) {
  const [rows, setRows] = useState<Row[]>(() => carried(task, assets))
  const [records, setRecords] = useState<OriginalReplacementTask[]>([])
  const [selectedId, setSelectedId] = useState<string | null>()
  const [loading, setLoading] = useState(true)
  const [operation, setOperation] = useState<'submit' | 'upload' | 'refresh' | 'apply' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [pickerTarget, setPickerTarget] = useState<string | null>(null)
  const [clock, setClock] = useState(Date.now)
  const lock = useRef(false)
  const mounted = useRef(true)
  const listRequest = useRef(0)
  const selected = records.find((item) => item.id === selectedId) ?? null
  const running = records.some(active)
  const filled = rows.filter((item) => item.target.trim())
  const capped = filled.length >= 5
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

  const submit = () => {
    if (!canSubmit || lock.current) return
    if (filled.length < 1) { setError('请至少填写一个替换位置'); return }
    if (filled.length > 5) { setError('最多提交 5 项替换'); return }
    void perform('submit', async () => {
      const next = await createOriginalReplacement(task.id, task.current_version_id!, task.current_output_id!,
        filled.map((item) => ({ target: item.target.trim(), asset_id: item.asset.id })))
      if (!mounted.current) return
      listRequest.current++
      setRecords((previous) => [next, ...previous]); setSelectedId(next.id); selectRecordUrl(next.id)
      setClock(Date.now()); setRows(carried(task, assets))
    })
  }

  const upload = (file?: File) => {
    if (!file || lock.current) return
    void perform('upload', async () => {
      const asset = await uploadAsset(file)
      if (!mounted.current) return
      setRows((previous) => [...previous, row(asset)]); onLoadAssets()
    })
  }

  const chooseRecord = (id: string | null) => { setSelectedId(id); setError(null); selectRecordUrl(id) }
  const elapsed = selected ? Math.max(0, Math.floor((clock - parseServerDate(selected.started_at || selected.created_at).getTime()) / 1000)) : 0
  const sourceChanged = selected && (selected.source_version_id !== task.current_version_id || selected.source_output_id !== task.current_output_id)

  return (
    <div className="original-replacement-panel">
      <div className="original-replacement-heading">
        <strong>原照替换</strong>
        <button type="button" className="mp-btn-ghost mp-btn-small" disabled={Boolean(operation) || loading} onClick={() => chooseRecord(null)}>新建替换</button>
      </div>
      {records.length > 0 ? (
        <label className="original-replacement-history">替换记录
          <select value={selectedId ?? ''} disabled={Boolean(operation)} onChange={(event) => chooseRecord(event.target.value || null)}>
            <option value="">新建替换</option>
            {records.map((item, index) => <option key={item.id} value={item.id}>记录 {records.length - index} · 基于 v{item.source_version} · {item.applied_version ? `已应用 v${item.applied_version}` : stages[item.stage] || item.stage}</option>)}
          </select>
        </label>
      ) : null}
      {loading ? <p role="status">正在恢复替换记录…</p> : selected ? (
        <>
          <div className="original-replacement-targets">
            {selected.replacements.map((item, index) => <div key={`${index}-${item.asset_id}`}><img src={item.preview_url} alt={item.name} /><span>{item.target}</span></div>)}
          </div>
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
          <div className="replacement-comparison">
            <figure><figcaption>替换前 · v{selected.source_version}</figcaption><ZoomableImage src={selected.source.preview_url} alt="替换前海报" className="replacement-preview" /></figure>
            {selected.output ? <figure><figcaption>替换结果</figcaption><ZoomableImage src={selected.output.preview_url} alt="原照替换结果" className="replacement-preview" /></figure> : null}
          </div>
          {selected.output && selected.status === 'succeeded' ? (
            <>
              {selected.applied_version_id ? <p className="original-replacement-applied" role="status">已应用为 v{selected.applied_version}，可在版本列表查看。</p> : (
                <>
                  {sourceChanged ? <p className="original-replacement-warning">替换结果基于 v{selected.source_version}，当前为 v{task.current_version}。应用将切换为新的原照替换版本，已有版本保留。</p> : null}
                  <p className="replacement-help">请对比确认菜品真实性及文字、排版是否完整。这里不提供自动像素保真认证。</p>
                  <button type="button" className="mp-btn-primary replacement-submit" disabled={Boolean(operation) || task.edit.status === 'running' || !task.current_output_id} onClick={() => void perform('apply', async () => {
                    const next = await applyOriginalReplacement(task, selected.id)
                    if (!mounted.current) return
                    await onApplied(next); await load()
                  })}>{operation === 'apply' ? '正在应用…' : '应用为新版本'}</button>
                  {task.edit.status === 'running' ? <p className="replacement-help">当前正在改图，完成后可应用替换结果。</p> : null}
                </>
              )}
              <a className="mp-btn-ghost mp-btn-small" href={selected.output.download_url} download>下载替换结果 PNG</a>
            </>
          ) : null}
          {selected.agent_output ? <details className="replacement-agent-output"><summary>处理说明与辅助文件</summary><pre>{selected.agent_output}</pre></details> : null}
        </>
      ) : (
        <>
          <div className="replacement-rows">
            {rows.map((item) => {
              const asset = assets.find((candidate) => candidate.id === item.asset.id) ?? item.asset
              return <div key={item.id} className={`replacement-row original-replacement-row${item.target.trim() ? ' is-checked' : ''}`}>
                <div className="original-replacement-choice">
                  <img src={asset.url} alt="" />
                  <span className="original-replacement-name">{asset.original_name}</span>
                  <button type="button" className="mp-btn-ghost mp-btn-small" disabled={Boolean(operation)} onClick={() => setRows((previous) => previous.filter((candidate) => candidate.id !== item.id))}>移除</button>
                </div>
                <label htmlFor={`original-target-${item.id}`}>替换位置</label>
                <input id={`original-target-${item.id}`} className="replacement-target" value={item.target} maxLength={1000} disabled={Boolean(operation) || (capped && !item.target.trim())} placeholder="例如：右下角牛肉" onChange={(event) => updateRow(item.id, { target: event.target.value })} />
              </div>
            })}
          </div>
          {rows.length === 0 ? <p className="replacement-help">还没有原照，可从资产库选择或上传，再填写替换位置。</p> : null}
          <div className="original-replacement-add">
            <button type="button" className="mp-btn-secondary mp-btn-small" disabled={Boolean(operation) || capped} onClick={() => { setPickerTarget('new'); onLoadAssets() }}>＋ 从资产库添加</button>
            <label className={`mp-btn-secondary mp-btn-small replacement-upload${operation || capped ? ' is-disabled' : ''}`}>上传原照
              <input type="file" accept="image/*" aria-label="上传额外原照" disabled={Boolean(operation) || capped} onChange={(event) => { const file = event.target.files?.[0]; event.target.value = ''; upload(file) }} />
            </label>
          </div>
          <p className="replacement-help">已填 {filled.length}/5 项，通常约 30 分钟出结果，可稍后找回。</p>
          <button type="button" className="mp-btn-primary replacement-submit" disabled={Boolean(operation) || !canSubmit || filled.length === 0} onClick={submit}>{operation === 'submit' ? '提交中…' : operation === 'upload' ? '正在上传…' : '开始原照替换'}</button>
          {!canSubmit ? <p className="replacement-help">请等待当前操作完成，并先选定一张完整海报结果。</p> : null}
        </>
      )}
      {error || loadError ? <div className="asset-notice asset-notice-error" role="alert"><p>{error || loadError}</p><button type="button" className="mp-btn-ghost mp-btn-small" disabled={Boolean(operation)} onClick={() => void load()}>刷新替换记录</button></div> : null}
      <AssetPicker key={pickerTarget ?? 'original-closed'} open={pickerTarget !== null} purpose="product" assets={assets} loading={assetsLoading}
        onClose={() => setPickerTarget(null)} onSelect={(asset) => {
          if (pickerTarget === 'new') setRows((previous) => [...previous, row(asset)])
          else if (pickerTarget) updateRow(pickerTarget, { asset })
          setPickerTarget(null)
        }} onGoAssets={() => { setPickerTarget(null); onGoAssets() }} />
    </div>
  )
}
