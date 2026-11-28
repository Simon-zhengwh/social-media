import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, applyOriginalReplacement, createOriginalReplacement, listOriginalReplacements, merchantPath, refreshOriginalReplacement, restoreOriginalReplacementArtifacts, retryModelReplacement, retryOriginalReplacementCandidate, selectWholeImageVersion } from '../api'
import type { Asset, BrandLogoImage, ImageModel, LogoOverlayBox, OriginalReplacementCandidate, OriginalReplacementTask, WholeImageTask } from '../types'
import { parseServerDate } from '../time'
import { assetDisplayName } from '../assetNames'
import { imageModelLabel } from '../modelGeneration'
import { ORIGINAL_REPLACEMENT_MODELS, canvasRatioOf, defaultReplacementModel, imageModelSupportsRatio } from '../imageModels'
import { ZoomableImage } from './ImageLightbox'
import { compositeLogoPng } from '../logoOverlay'
import PosterCropDialog from './PosterCropDialog'
import PosterLogoOverlayDialog from './PosterLogoOverlayDialog'
import SeriesPhotoEditor from './SeriesPhotoEditor'

type Props = {
  task: WholeImageTask
  assets: Asset[]
  logo: BrandLogoImage | null
  onRefreshTask: () => Promise<void>
  /**
   * Series page hooks. `initialPhotos` replaces the "images carried by the prompt" defaults
   * with the current version's content references, and `poolAssets` is the rest of this
   * task's pool the operator may substitute in.
   */
  initialPhotos?: Array<{ asset_id: string; target: string }>
  poolAssets?: Asset[]
  /** Group canvas; set only for series pages, where "save to this page" is offered. */
  seriesCanvas?: { width: number; height: number } | null
  onSaveSeriesHtml?: (input: {
    sourceVersionId: string
    expectedOutputId: string | null
    artifactId: string
    sessionId: string
    base64: string
    width: number
    height: number
  }) => Promise<void>
}
type Row = { id: string; target: string; asset: Asset }
const row = (asset: Asset): Row => ({ id: crypto.randomUUID(), target: '', asset })
const stages: Record<string, string> = { queued: '等待处理', preparing: '准备图片', agent_running: '正在替换', model_running: '模型正在替换', saving: '保存结果', done: '替换完成', failed: '处理未完成' }
const HTML_MISSING_ERROR = '该运行未返回 HTML 编辑器。'
const active = (record: OriginalReplacementTask) => record.status === 'queued' || record.status === 'running'
const message = (error: unknown) => error instanceof Error ? error.message : '请求失败，请稍后重试'
const boxKey = (box: LogoOverlayBox) => `${box.x},${box.y},${box.width},${box.height}`
/** Cached canvas composite; keyed by the applied box so a tweak forces a fresh render. */
const compositedKey = (recordId: string, candidate: OriginalReplacementCandidate) =>
  `${recordId}:${candidate.model}:${candidate.logo_box ? boxKey(candidate.logo_box) : 'raw'}`

function carriedAssets(task: WholeImageTask, assets: Asset[], ids: string[]): Asset[] {
  const images = task.model_input?.images ?? []
  return [...new Set(ids)].map((id) => {
    const asset = assets.find((item) => item.id === id)
    const meta = images.find((item) => item.asset_id === id)
    return asset ?? {
      id, original_name: meta?.name ?? '原照',
      url: meta?.url || meta?.preview_url || merchantPath(`/api/assets/${encodeURIComponent(id)}/file`),
      width: 0, height: 0, mime_type: 'image/png', created_at: task.created_at,
    }
  })
}

function carriedProducts(task: WholeImageTask, assets: Asset[]): Row[] {
  const ids = task.input.product_asset_ids?.length ? task.input.product_asset_ids
    : (task.model_input?.images ?? []).filter((item) => item.role === 'product').map((item) => item.asset_id)
  return carriedAssets(task, assets, ids).map(row)
}

function selectRecordUrl(id: string | null) {
  const url = new URL(window.location.href)
  const view = url.searchParams.get('view')
  if (view && view !== 'whole') return
  if (id) url.searchParams.set('replacement', id)
  else url.searchParams.delete('replacement')
  window.history.replaceState({}, '', url)
}

/**
 * A series page starts from the content references of its current version, so switching
 * back to an earlier version restores the photos that version was generated from.
 */
function carriedFrom(page: Array<{ asset_id: string; target: string }>, task: WholeImageTask, assets: Asset[]): Row[] {
  if (!page.length) return []
  const images = task.model_input?.images ?? []
  return page.map((ref) => {
    const asset = assets.find((item) => item.id === ref.asset_id)
    const meta = images.find((item) => item.asset_id === ref.asset_id)
    return {
      id: crypto.randomUUID(),
      target: ref.target ?? '',
      asset: asset ?? {
        id: ref.asset_id,
        original_name: meta?.name ?? '原照',
        url: meta?.url || meta?.preview_url || merchantPath(`/api/assets/${encodeURIComponent(ref.asset_id)}/file`),
        width: 0, height: 0, mime_type: 'image/png', created_at: task.created_at,
      },
    }
  })
}

export default function OriginalReplacementPanel({
  task, assets, logo, onRefreshTask, initialPhotos, poolAssets = [], seriesCanvas = null, onSaveSeriesHtml,
}: Props) {
  const [rows, setRows] = useState<Row[]>(() =>
    initialPhotos ? carriedFrom(initialPhotos, task, assets) : carriedProducts(task, assets))
  const [removedPool, setRemovedPool] = useState<Array<{ asset: Asset; target: string }>>([])
  // The official Logo is composited for model batches, so it must be removable from the list.
  // Remembering which asset was removed resets it automatically when the Logo changes.
  const [logoRemovedFor, setLogoRemovedFor] = useState<string | null>(null)
  const [splitAllElements, setSplitAllElements] = useState(false)
  // 仅替换元素 sends one model request per click; the rest stay idle candidates. Every model
  // stays selectable: one that cannot keep the canvas ratio runs at its own ratio and is
  // cropped back in the crop dialog, so it is never hidden from the picker. The ratio comes
  // from the source poster this batch replaces, because that is the size the backend judges.
  const replacementSource = seriesCanvas ?? task.outputs.find((output) => output.id === task.current_output_id)
  const replacementRatio = canvasRatioOf(replacementSource?.width, replacementSource?.height)
    ?? task.placement?.ratio ?? task.input.ratio
  const [requestedModel, setRequestedModel] = useState<ImageModel>(
    () => defaultReplacementModel(replacementRatio))
  const candidateModel = requestedModel
  const [records, setRecords] = useState<OriginalReplacementTask[]>([])
  const [crop, setCrop] = useState<{
    src: string; width: number; height: number; logoSrc?: string; defaultLogoBox?: LogoOverlayBox
  } | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>()
  const [loading, setLoading] = useState(true)
  const [operation, setOperation] = useState<'agent' | 'model' | 'feedback' | 'apply' | 'refresh' | 'restore' | 'retry' | null>(null)
  const [feedback, setFeedback] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [clock, setClock] = useState(Date.now)
  const [editorOpen, setEditorOpen] = useState(false)
  const [logoEditor, setLogoEditor] = useState<{ model: ImageModel; baseSrc: string; initialBox: LogoOverlayBox } | null>(null)
  const [composited, setComposited] = useState<Record<string, string>>({})
  const lock = useRef(false)
  const mounted = useRef(true)
  const listRequest = useRef(0)
  const selected = records.find((item) => item.id === selectedId) ?? null
  const recordedMaterials = [
    ...(selected?.replacements ?? []),
    ...(selected?.logo_reference ? [{ ...selected.logo_reference, target: '官方 Logo 参考' }] : []),
  ]
  const htmlArtifact = selected?.artifacts?.find((item) => item.kind === 'html')
  const htmlConfirmedMissing = selected?.artifact_error === HTML_MISSING_ERROR
  const running = records.some(active)
  const photos = rows.filter((item) => item.asset.id !== logo?.asset_id)
  // The backend adds the series group's Logo as a reference when it submits the request.
  const logoRemoved = Boolean(logo && logoRemovedFor === logo.asset_id)
  const showLogo = Boolean(logo && !seriesCanvas && !logoRemoved)
  const replacements = [
    ...(showLogo && logo ? [{ target: 'Logo', asset_id: logo.asset_id }] : []),
    ...photos.map((item) => ({ target: item.target.trim(), asset_id: item.asset.id })),
  ]
  const intoPoolLogo: Asset | null = logo && !seriesCanvas && logoRemoved ? {
    id: logo.asset_id, original_name: logo.original_name, url: logo.url, thumb_url: logo.thumb_url,
    width: logo.width, height: logo.height, mime_type: 'image/png', created_at: task.created_at,
  } : null
  // Unified design references can be complete posters. The operator picks which to swap in.
  const designReferences = seriesCanvas ? [] : carriedAssets(task, assets,
    task.input.reference_asset_ids
      ?? (task.model_input?.images ?? []).filter((item) => item.role === 'reference').map((item) => item.asset_id))
  const poolOptions = [...new Map([
    ...designReferences,
    ...poolAssets,
    ...removedPool.map((item) => item.asset),
  ].map((asset) => [asset.id, asset])).values()].filter((asset) =>
    asset.id !== logo?.asset_id && !rows.some((item) => item.asset.id === asset.id))
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

  // Polling rebuilds `selected` constantly, so the composite effect reads the record from a ref.
  const latest = useRef(selected)
  useEffect(() => { latest.current = selected }, [selected])

  const logoOverlay = selected?.kind === 'model' ? selected.logo_overlay ?? null : null
  const candidateSignature = (selected?.candidates ?? [])
    .map((item) => `${item.model}:${item.status}:${item.logo_box ? boxKey(item.logo_box) : 'raw'}`).join('|')
  // Candidates that have not been applied yet get their official Logo drawn here, so the
  // first result an operator sees is already the composited poster.
  useEffect(() => {
    const record = latest.current
    const layer = record?.kind === 'model' ? record.logo_overlay ?? null : null
    if (!record || !layer) return
    let cancelled = false
    for (const candidate of record.candidates ?? []) {
      const source = candidate.base_source_url
      if (!source || candidate.logo_box || candidate.status !== 'succeeded') continue
      const key = compositedKey(record.id, candidate)
      void compositeLogoPng({
        baseSrc: merchantPath(source), logoSrc: merchantPath(layer.source_url),
        canvasWidth: record.source.width, canvasHeight: record.source.height, box: layer.box,
        // A `for_crop` result keeps its own aspect ratio, so show the 100% centred crop.
        coverBase: Boolean(candidate.for_crop),
      }).then((url) => {
        if (cancelled) return
        setComposited((previous) => (previous[key] ? previous : { ...previous, [key]: url }))
      }).catch(() => undefined)
    }
    return () => { cancelled = true }
  }, [selected?.id, candidateSignature])

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

  const defaultRows = () => initialPhotos ? carriedFrom(initialPhotos, task, assets) : carriedProducts(task, assets)
  const updateRow = (id: string, values: Partial<Row>) => setRows((previous) => previous.map((item) => item.id === id ? { ...item, ...values } : item))
  const removeRow = (item: Row) => {
    const resolved = assets.find((candidate) => candidate.id === item.asset.id) ?? item.asset
    setRows((previous) => previous.filter((candidate) => candidate.id !== item.id))
    setRemovedPool((previous) => [
      ...previous.filter((entry) => entry.asset.id !== resolved.id),
      { asset: resolved, target: item.target },
    ])
  }
  const addPoolRow = (assetId: string) => {
    const remembered = removedPool.find((entry) => entry.asset.id === assetId)
    const asset = poolOptions.find((item) => item.id === assetId)
    if (!asset || rows.some((item) => item.asset.id === assetId)) return
    setRemovedPool((previous) => previous.filter((entry) => entry.asset.id !== assetId))
    setRows((previous) => [...previous, { id: crypto.randomUUID(), target: remembered?.target ?? '', asset }])
  }
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

  const updateRecord = (next: OriginalReplacementTask) => {
    if (!mounted.current) return
    listRequest.current++
    setRecords((previous) => previous.map((item) => item.id === next.id ? next : item))
  }

  const submit = (kind: 'agent' | 'model') => {
    if (!canSubmit || lock.current) return
    if (replacements.length < 1) { setError('请添加原照或选择 Logo'); return }
    if (replacements.length > 5) { setError('最多提交 5 项替换'); return }
    void perform(kind, async () => {
      const next = await createOriginalReplacement(task.id, task.current_version_id!, task.current_output_id!,
        replacements, kind === 'agent' && splitAllElements, kind, candidateModel)
      if (!mounted.current) return
      openNewRecord(next)
      setRows(defaultRows())
      setRemovedPool([])
      setSplitAllElements(false)
      setLogoRemovedFor(null)
    })
  }

  const chooseRecord = (id: string | null) => {
    setSelectedId(id); setError(null); setFeedback(''); selectRecordUrl(id)
    if (!id) { setSplitAllElements(false); setLogoRemovedFor(null) }
  }

  /** The official Logo is drawn here, then that PNG becomes the applied version. */
  const applyComposited = (candidate: OriginalReplacementCandidate, box: LogoOverlayBox) => {
    const baseSrc = candidate.base_source_url
    if (!selected || !logoOverlay || !baseSrc) return
    void perform('apply', async () => {
      const png = composited[compositedKey(selected.id, candidate)] ?? await compositeLogoPng({
        baseSrc: merchantPath(baseSrc), logoSrc: merchantPath(logoOverlay.source_url),
        canvasWidth: selected.source.width, canvasHeight: selected.source.height, box,
      })
      await applyOriginalReplacement(task, selected.id, candidate.model, png, box)
      await load()
      await onRefreshTask()
    })
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
                  {recordedMaterials.map((item, index) => (
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
              {recordedMaterials.map((item, index) => <div key={`${index}-${item.asset_id}`}><img src={item.preview_url} alt={item.name} /><span>{item.target || '自动识别'}</span></div>)}
            </div>
          )}
          {selected.split_all_elements ? <p className="original-replacement-mode">全部拆分元素（含艺术字）</p> : null}
          {active(selected) && selected.kind !== 'model' ? <div className="replacement-status" role="status">{stages[selected.stage] || selected.stage} · 已等待 {Math.floor(elapsed / 60)} 分 {elapsed % 60} 秒<p>可继续修改海报或关闭页面，结果仍基于提交时的 v{selected.source_version}。</p></div> : null}
          {selected.error && selected.kind !== 'model' ? <div className="asset-notice asset-notice-error" role="alert"><p>{selected.error.message}</p>
            {selected.run_id ? <button type="button" className="mp-btn-secondary mp-btn-small" disabled={Boolean(operation)} onClick={() => void perform('refresh', async () => {
              updateRecord(await refreshOriginalReplacement(task.id, selected.id))
            })}>查询已有结果</button> : null}
          </div> : null}
          {selected.kind === 'model' ? <>
            {selected.feedback ? <p className="original-model-feedback-text">本轮反馈：{selected.feedback}</p> : null}
            <div className="original-model-candidates">
              {selected.candidates?.map((candidate) => {
                const label = imageModelLabel(candidate.model)
                const versionId = candidate.applied_version_id
                const preview = composited[compositedKey(selected.id, candidate)] ?? candidate.output?.preview_url
                const box = candidate.logo_box ?? logoOverlay?.box ?? null
                const baseSource = candidate.base_source_url ? merchantPath(candidate.base_source_url) : null
                const canComposite = Boolean(logoOverlay && baseSource && box)
                return <div key={candidate.model} className="original-model-candidate">
                  <strong title={label}>{label}</strong>
                  {candidate.output ? <>
                    <ZoomableImage src={preview ?? candidate.output.preview_url} alt={label} />
                    {candidate.for_crop ? <div className="original-model-actions">
                      <button type="button" className="mp-btn-secondary mp-btn-small" aria-label={`裁剪 ${label}`}
                      onClick={() => setCrop({
                        src: merchantPath(`/api/whole-image/tasks/${encodeURIComponent(task.id)}/original-replacements/${encodeURIComponent(selected.id)}/candidates/${encodeURIComponent(candidate.model)}/crop-source`),
                        width: selected.source.width, height: selected.source.height,
                        // The model already erased the old Logo, so the crop must carry the official one.
                        ...(logoOverlay ? { logoSrc: merchantPath(logoOverlay.source_url),
                          defaultLogoBox: logoOverlay.box } : {}),
                      })}>裁剪导出</button>
                    </div> : <div className="original-model-actions">
                      {canComposite ? <button type="button"
                        className="mp-btn-secondary mp-btn-small" aria-label={`调整 ${label} 的 Logo`}
                        disabled={Boolean(operation) || !canSubmit}
                        onClick={() => setLogoEditor({ model: candidate.model, baseSrc: baseSource!, initialBox: box! })}>调整 Logo</button> : null}
                      {versionId && versionId === task.current_version_id ? <span>当前 v{candidate.applied_version}</span>
                        : <button type="button" className="mp-btn-secondary mp-btn-small"
                            aria-label={versionId ? `切换到 ${label} v${candidate.applied_version}` : `应用 ${label}`}
                            disabled={Boolean(operation) || !canSubmit}
                            onClick={() => void (versionId
                              ? perform('apply', async () => {
                                  await selectWholeImageVersion(task.id, versionId)
                                  await onRefreshTask()
                                })
                              : canComposite && box ? applyComposited(candidate, box)
                              : perform('apply', async () => {
                                  await applyOriginalReplacement(task, selected.id, candidate.model)
                                  await load()
                                  await onRefreshTask()
                                }))}>{versionId ? `切换到 v${candidate.applied_version}` : '应用'}</button>}
                    </div>}
                  </> : candidate.status === 'idle' ? <>
                    <span className="original-model-status">未生成</span>
                    <button type="button" className="mp-btn-secondary mp-btn-small"
                      aria-label={`用 ${label} 生成`} disabled={Boolean(operation)} onClick={() => void perform('retry', async () => {
                        updateRecord(await retryOriginalReplacementCandidate(task.id, selected.id, candidate.model))
                      })}>生成</button>
                  </> : candidate.status === 'unsupported' ? <>
                    <span className="original-model-status">需生成后裁剪</span>
                    <button type="button" className="mp-btn-secondary mp-btn-small"
                      aria-label={`生成 ${label} 后裁剪`} disabled={Boolean(operation) || selected.status === 'queued'}
                      onClick={() => void perform('retry', async () => {
                        updateRecord(await retryOriginalReplacementCandidate(task.id, selected.id, candidate.model, true))
                      })}>生成</button>
                  </> : candidate.status === 'failed' ? <>
                    <span title={candidate.error || undefined}>{(candidate.review_failed && candidate.error) || '替换失败'}</span>
                    <button type="button" className="mp-btn-secondary mp-btn-small"
                      aria-label={`重试 ${label}`} disabled={Boolean(operation) || selected.status === 'queued'}
                      onClick={() => void perform('retry', async () => {
                        updateRecord(await retryOriginalReplacementCandidate(task.id, selected.id, candidate.model,
                          Boolean(candidate.for_crop)))
                      })}>重试</button>
                  </> : <span className="original-model-status">
                    {candidate.status === 'queued' ? '等待处理' : '正在替换'}
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
          {htmlArtifact?.preview_url ? (
            <div className="original-replacement-html-actions">
              <a className="mp-btn-secondary replacement-submit" href={htmlArtifact.preview_url} target="_blank" rel="noopener noreferrer">打开 HTML</a>
              {seriesCanvas && onSaveSeriesHtml ? (
                <button type="button" className="mp-btn-primary replacement-submit" disabled={Boolean(operation)}
                  onClick={() => setEditorOpen(true)}>保存到本页</button>
              ) : null}
            </div>
          ) : null}
          {htmlArtifact && seriesCanvas && onSaveSeriesHtml ? (
            <SeriesPhotoEditor
              open={editorOpen}
              htmlUrl={htmlArtifact.preview_url}
              canvas={seriesCanvas}
              busy={Boolean(operation)}
              onClose={() => {
                setEditorOpen(false)
                void load()
                void onRefreshTask()
              }}
              onSave={(exported) => onSaveSeriesHtml({
                sourceVersionId: selected.source_version_id ?? task.current_version_id ?? '',
                expectedOutputId: selected.source_output_id ?? null,
                artifactId: htmlArtifact.id,
                sessionId: exported.sessionId,
                base64: exported.base64,
                width: exported.width,
                height: exported.height,
              })}
            />
          ) : null}
          {selected.kind !== 'model' && !active(selected) && !htmlArtifact && selected.output ? (
            <div className="original-replacement-fallback">
              <ZoomableImage src={selected.output.preview_url} alt="替换成图" />
              <div className="original-replacement-html-actions">
                {selected.applied_version_id && selected.applied_version_id === task.current_version_id ? (
                  <span>当前 v{selected.applied_version}</span>
                ) : (
                  <button
                    type="button"
                    className="mp-btn-primary mp-btn-small"
                    disabled={Boolean(operation) || !canSubmit}
                    onClick={() => void (selected.applied_version_id
                      ? perform('apply', async () => {
                          await selectWholeImageVersion(task.id, selected.applied_version_id!)
                          await onRefreshTask()
                        })
                      : perform('apply', async () => {
                          await applyOriginalReplacement(task, selected.id)
                          await load()
                          await onRefreshTask()
                        }))}
                  >
                    {selected.applied_version_id ? `切换到 v${selected.applied_version}` : '应用'}
                  </button>
                )}
                <a className="mp-btn-secondary mp-btn-small" href={selected.output.download_url} download>下载 PNG</a>
              </div>
            </div>
          ) : null}
          {selected.artifact_error ? <p className="original-replacement-warning" role="status">{selected.artifact_error}</p> : null}
          {selected.run_id && !active(selected) && !htmlConfirmedMissing && (!htmlArtifact || selected.artifact_error) ? <button type="button" className="mp-btn-secondary mp-btn-small" disabled={Boolean(operation)} onClick={() => void perform('restore', async () => {
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
            {showLogo && logo ? <div className="replacement-row original-replacement-row original-replacement-logo is-checked">
              <div className="original-replacement-choice">
                <img src={logo.thumb_url || logo.url} alt="Logo" />
                <span className="original-replacement-name">Logo</span>
                <button type="button" className="mp-btn-ghost mp-btn-small original-replacement-remove"
                  aria-label="移除原照：Logo" disabled={Boolean(operation)}
                  onClick={() => setLogoRemovedFor(logo.asset_id)}>移除</button>
              </div>
            </div> : null}
            {photos.map((item) => {
              const asset = assets.find((candidate) => candidate.id === item.asset.id) ?? item.asset
              return <div key={item.id} className="replacement-row original-replacement-row is-checked">
                <div className="original-replacement-choice">
                  <img src={asset.thumb_url || asset.url} alt="" loading="lazy" decoding="async" />
                  <input id={`original-target-${item.id}`} aria-label={`替换位置：${assetDisplayName(asset.original_name)}`} className="replacement-target" value={item.target} maxLength={1000} disabled={Boolean(operation)} placeholder="留空自动识别，如：右下角牛肉、背景" onChange={(event) => updateRow(item.id, { target: event.target.value })} />
                  <button type="button" className="mp-btn-ghost mp-btn-small original-replacement-remove" aria-label={`移除原照：${assetDisplayName(asset.original_name)}`} disabled={Boolean(operation)} onClick={() => removeRow(item)}>移除</button>
                </div>
              </div>
            })}
          </div>
          {intoPoolLogo || poolOptions.length ? (
            <section className="original-replacement-pool" aria-label="从素材池添加原照">
              <div className="original-replacement-pool-head">
                <span>素材池</span>
                <span className="original-replacement-pool-count">{poolOptions.length + (intoPoolLogo ? 1 : 0)}</span>
              </div>
              <div className="original-replacement-pool-list">
                {intoPoolLogo ? (
                  <button
                    key="logo"
                    type="button"
                    className="original-replacement-pool-item"
                    disabled={Boolean(operation)}
                    aria-label="添加素材：Logo"
                    title="Logo"
                    onClick={() => setLogoRemovedFor(null)}
                  >
                    <img src={intoPoolLogo.thumb_url || intoPoolLogo.url} alt="" loading="lazy" decoding="async" />
                    <span className="original-replacement-pool-add" aria-hidden="true">+</span>
                  </button>
                ) : null}
                {poolOptions.map((asset) => {
                  const name = assetDisplayName(asset.original_name)
                  return (
                    <button
                      key={asset.id}
                      type="button"
                      className="original-replacement-pool-item"
                      disabled={Boolean(operation)}
                      aria-label={`添加素材：${name}`}
                      title={name}
                      onClick={() => addPoolRow(asset.id)}
                    >
                      <img src={asset.thumb_url || asset.url} alt="" loading="lazy" decoding="async" />
                      <span className="original-replacement-pool-add" aria-hidden="true">+</span>
                    </button>
                  )
                })}
              </div>
            </section>
          ) : null}
          {replacements.length === 0 ? <p className="replacement-help">{poolOptions.length
            ? '从素材池选入要替换的原照。' : '请先在设计需求中添加参考图或选择 Logo。'}</p> : null}
          {replacements.length > 5 ? <p className="original-replacement-warning" role="alert">最多提交 5 项，请移除部分原照或关闭 Logo。</p> : null}
          <div className="original-model-picker" role="radiogroup" aria-label="仅替换元素模型">
            {ORIGINAL_REPLACEMENT_MODELS.map((model) => {
              const label = imageModelLabel(model)
              const needsCrop = Boolean(replacementRatio && !imageModelSupportsRatio(model, replacementRatio))
              return <button key={model} type="button" role="radio" aria-checked={model === candidateModel}
                className={`original-model-option${model === candidateModel ? ' is-selected' : ''}`}
                title={needsCrop ? `${label}不支持 ${replacementRatio}，将先生成再裁剪` : undefined}
                aria-label={needsCrop ? `${label}，生成后裁剪` : label}
                disabled={Boolean(operation)} onClick={() => setRequestedModel(model)}>{label}</button>
            })}
          </div>
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
      {crop ? <PosterCropDialog {...crop} onClose={() => setCrop(null)} /> : null}
      {logoEditor && selected && logoOverlay ? <PosterLogoOverlayDialog
        baseSrc={logoEditor.baseSrc}
        logoSrc={merchantPath(logoOverlay.source_url)}
        width={selected.source.width}
        height={selected.source.height}
        initialBox={logoEditor.initialBox}
        defaultBox={logoOverlay.box}
        busy={Boolean(operation)}
        onClose={() => setLogoEditor(null)}
        onApply={async ({ base64, box }) => {
          lock.current = true
          setOperation('apply')
          setError(null)
          try {
            await applyOriginalReplacement(task, selected.id, logoEditor.model, base64, box)
            await load()
            await onRefreshTask()
          } catch (reason: unknown) {
            // A stale task after a version switch: refresh so the next click targets the new one.
            if (reason instanceof ApiError && reason.code === 'http_409') await onRefreshTask().catch(() => undefined)
            throw reason
          } finally {
            lock.current = false
            if (mounted.current) setOperation(null)
          }
        }}
      /> : null}
    </div>
  )
}
