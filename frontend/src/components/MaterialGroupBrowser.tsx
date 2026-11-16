import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ApiError,
  backfillMaterialGroupPicks,
  getMaterialGroupRun,
  inheritMaterialGroupPick,
  listMaterialGroupFiles,
  listMaterialGroups,
  listMaterialUnsettledRepresentatives,
  renameMaterialGroup,
  resumeMaterialGroupRun,
  setMaterialFilterPick,
  startMaterialGroupScan,
  startMaterialRecognitionRun,
} from '../api'
import type {
  MaterialGroup,
  MaterialGroupFile,
  MaterialGroupRun,
  MaterialGroupsPage,
  MaterialFilterScheme,
  MaterialRecognitionRequest,
  MaterialGroupScanError,
} from '../types'
import './MaterialGroupBrowser.css'
import { MAX_RECOGNITION_BATCH, recognitionBatchSize } from '../materialRecognitionBatch'

const GROUP_PAGE_SIZE = 30
// The groups endpoint caps one page at 100 rows; deeper scroll positions are read in pages.
const GROUP_FETCH_MAX = 100
const FILE_PAGE_SIZE = 48
const GROUP_STATUS = { unrecognized: '未识别', recognized: '已有参考名', ambiguous: '待确认', conflict: '参考名冲突', manual: '手动命名' }
const VERDICT_LABEL: Record<string, string> = { pass: '通过', fail: '不通过', uncertain: '待复核', error: '请求失败', unrecognized: '未识别' }
const GROUP_NAME_MAX = 60
const RUN_STATUS = { queued: '排队中', running: '运行中', succeeded: '已完成', interrupted: '已中断', failed: '失败' }
const GROUP_PREVIEW_LIMIT = 8
const REPRESENTATIVE_COUNT = 1
const GROUP_FILTER_TABS = [
  { key: 'all', label: '全部' },
  { key: 'unrecognized', label: '未识别' },
  { key: 'pass', label: '通过' },
  { key: 'fail', label: '不通过' },
  { key: 'uncertain', label: '待复核' },
  { key: 'error', label: '请求失败' },
  { key: 'picked', label: '已入选' },
] as const

type Operation =
  | { kind: 'scan'; requestId: string }
  | { kind: 'recognition'; payload: MaterialRecognitionRequest }
  | { kind: 'resume'; runId: string }

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : '请求失败，请重试'
}

function newRequestId(): string {
  return crypto.randomUUID()
}

function isActive(run: MaterialGroupRun | null): boolean {
  return run?.status === 'queued' || run?.status === 'running'
}

export default function MaterialGroupBrowser({ scheme, onChanged, blockedByFilterRun = false }: {
  scheme: MaterialFilterScheme; onChanged?: () => void; blockedByFilterRun?: boolean
}) {
  const [groups, setGroups] = useState<MaterialGroup[]>([])
  const [stats, setStats] = useState<MaterialGroupsPage['stats'] | null>(null)
  const [scanErrors, setScanErrors] = useState<MaterialGroupScanError[]>([])
  const [total, setTotal] = useState(0)
  const [loadedAll, setLoadedAll] = useState(false)
  const [query, setQuery] = useState('')
  const [searchDraft, setSearchDraft] = useState('')
  const [status, setStatus] = useState('all')
  const [counts, setCounts] = useState<Record<string, number>>({})
  const [batchLimit, setBatchLimit] = useState('60')
  const [batchRun, setBatchRun] = useState<MaterialGroupRun | null>(null)
  const [batchBusy, setBatchBusy] = useState(false)
  const [batchError, setBatchError] = useState<string | null>(null)
  const [pendingBatch, setPendingBatch] = useState<MaterialRecognitionRequest | null>(null)
  const [showDetails, setShowDetails] = useState(false)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [run, setRun] = useState<MaterialGroupRun | null>(null)
  const [busy, setBusy] = useState(false)
  const [pendingRetry, setPendingRetry] = useState<Operation | null>(null)
  const [editingName, setEditingName] = useState<string | null>(null)
  const [nameDraft, setNameDraft] = useState('')
  const [nameBusy, setNameBusy] = useState(false)
  const [nameError, setNameError] = useState<string | null>(null)
  const [samplingAllowed, setSamplingAllowed] = useState(false)
  const [revision, setRevision] = useState(0)
  const requestTicket = useRef(0)
  const groupsLoaded = useRef(0)
  const pageBusy = useRef(false)
  const loadMoreTarget = useRef<HTMLDivElement | null>(null)
  const operationBusy = useRef(false)
  const mounted = useRef(true)
  const changed = useRef(onChanged)
  const samplingEnabled = samplingAllowed && scheme.naming_mode === 'menu_match' && scheme.rules.length === 0
  useEffect(() => { changed.current = onChanged }, [onChanged])

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      requestTicket.current += 1
    }
  }, [])

  // A plain refresh re-reads the rows already on screen so scroll depth survives
  // polling, while an append continues below them.
  const loadGroups = useCallback(async (options: { append?: boolean } = {}) => {
    if (!mounted.current || pageBusy.current) return
    pageBusy.current = true
    const ticket = ++requestTicket.current
    const append = options.append === true
    if (append) setLoadingMore(true)
    else setLoading(true)
    try {
      const start = append ? groupsLoaded.current : 0
      const end = append ? start + GROUP_PAGE_SIZE : Math.max(GROUP_PAGE_SIZE, groupsLoaded.current)
      const items: MaterialGroup[] = []
      let page: MaterialGroupsPage | null = null
      for (let offset = start; offset < end;) {
        const limit = Math.min(GROUP_FETCH_MAX, end - offset)
        const next = await listMaterialGroups(scheme.id, { skip: offset, limit, q: query, status, representative_count: REPRESENTATIVE_COUNT })
        if (!mounted.current || ticket !== requestTicket.current) return
        page = next
        items.push(...next.items)
        offset += next.items.length
        if (next.items.length < limit) break
      }
      if (!page) return
      setSamplingAllowed(page.sampling_enabled === true)
      setTotal(page.total)
      setCounts(page.counts ?? {})
      setStats(page.stats)
      setScanErrors(page.scan_errors ?? [])
      if (page.last_run) setRun(page.last_run)
      setGroups(previous => {
        if (!append) return items
        const known = new Set(previous.map(item => item.id))
        return [...previous, ...items.filter(item => !known.has(item.id))]
      })
      groupsLoaded.current = start + items.length
      setLoadedAll(items.length === 0 || groupsLoaded.current >= page.total)
      setError(null)
    } catch (cause) {
      if (mounted.current && ticket === requestTicket.current) setError(errorMessage(cause))
    } finally {
      pageBusy.current = false
      if (mounted.current && ticket === requestTicket.current) {
        if (append) setLoadingMore(false)
        else setLoading(false)
      }
    }
  }, [scheme.id, query, status])

  // A new search term or status tab starts a new list, so scroll depth restarts too.
  // (A new scheme remounts this component, because the parent keys it by scheme id.)
  const resetScrollDepth = () => {
    groupsLoaded.current = 0
    setLoadedAll(false)
    setGroups([])
  }

  useEffect(() => { void Promise.resolve().then(() => loadGroups()) }, [loadGroups])
  const pendingPicks = stats?.pending_picks ?? 0
  useEffect(() => {
    if (!pendingPicks || loading) return
    const timer = setTimeout(() => void loadGroups(), 4000)
    return () => clearTimeout(timer)
  }, [pendingPicks, loading, loadGroups])

  const runActive = isActive(run)
  const runId = run?.id
  const batchActive = isActive(batchRun)
  useEffect(() => {
    if (!runActive || !runId) return
    let canceled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const poll = async () => {
      try {
        const next = await getMaterialGroupRun(runId)
        if (canceled) return
        setRun(next)
        await loadGroups()
        if (canceled) return
        if (isActive(next)) timer = setTimeout(() => void poll(), 2500)
        else {
          setRevision(value => value + 1)
          changed.current?.()
        }
      } catch (cause) {
        if (canceled) return
        setError(`进度暂时无法读取：${errorMessage(cause)}。后台任务仍会继续。`)
        timer = setTimeout(() => void poll(), 4000)
      }
    }
    timer = setTimeout(() => void poll(), 1200)
    return () => { canceled = true; clearTimeout(timer) }
  }, [runId, runActive, loadGroups])

  const execute = async (operation: Operation) => {
    if (operationBusy.current || runActive || blockedByFilterRun) return
    operationBusy.current = true
    setBusy(true)
    setError(null)
    try {
      const next = operation.kind === 'scan'
        ? await startMaterialGroupScan(scheme.id, operation.requestId)
        : operation.kind === 'recognition'
          ? await startMaterialRecognitionRun(scheme.id, operation.payload)
          : await resumeMaterialGroupRun(operation.runId)
      if (!mounted.current) return
      setRun(next)
      setPendingRetry(null)
      if (!isActive(next)) {
        await loadGroups()
        setRevision(value => value + 1)
        changed.current?.()
      }
    } catch (cause) {
      if (!mounted.current) return
      const ambiguous = !(cause instanceof ApiError) || cause.status === 0 || cause.status >= 500
      setPendingRetry(ambiguous ? operation : null)
      setError(ambiguous
        ? `操作结果尚不明确：${errorMessage(cause)}。重试会沿用原操作编号和照片范围。`
        : errorMessage(cause))
    } finally {
      operationBusy.current = false
      if (mounted.current) setBusy(false)
    }
  }

  const blocked = busy || runActive || blockedByFilterRun || pendingRetry !== null
  const backfillSuccessfulGroups = async () => {
    if (operationBusy.current || blocked || loading) return
    operationBusy.current = true
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const result = await backfillMaterialGroupPicks(scheme.id)
      if (!mounted.current) return
      setNotice(`已为 ${result.groups} 个通过组补齐入选，共 ${result.photos_picked} 张。`)
      if (result.errors.length) setError(`有 ${result.errors.length} 张未能生成可同步副本：${result.errors[0]}`)
      await loadGroups()
      changed.current?.()
    } catch (cause) {
      if (mounted.current) setError(errorMessage(cause))
    } finally {
      operationBusy.current = false
      if (mounted.current) setBusy(false)
    }
  }
  const recognize = (force = false, groupLimit?: number) => {
    if (blocked || loading || !samplingEnabled) return
    void execute({ kind: 'recognition', payload: {
      request_id: newRequestId(),
      mode: 'representatives',
      representative_count: REPRESENTATIVE_COUNT,
      force,
      ...(groupLimit ? { group_limit: groupLimit } : {}),
    } })
  }

  // Both buckets need a model verdict: 待复核 has an unsure one, 未识别 none yet.
  const batchVerdict = status === 'unrecognized' ? 'unrecognized' : 'uncertain'
  const batchTargets = counts[batchVerdict] ?? 0

  // The API refuses more than MAX_RECOGNITION_BATCH photo ids per request, so a
  // large batch runs as consecutive requests over the same ordered photo list.
  const batchRequests = (fileIds: string[]) => {
    const requests: MaterialRecognitionRequest[] = []
    for (let offset = 0; offset < fileIds.length; offset += MAX_RECOGNITION_BATCH) {
      requests.push({
        request_id: newRequestId(), mode: 'selected_files', representative_count: REPRESENTATIVE_COUNT,
        file_ids: fileIds.slice(offset, offset + MAX_RECOGNITION_BATCH), force: true,
      })
    }
    return requests
  }

  const waitForBatchRun = async (runId: string): Promise<MaterialGroupRun | null> => {
    for (;;) {
      const next = await getMaterialGroupRun(runId)
      if (!mounted.current) return null
      setBatchRun(next)
      if (!isActive(next)) return next
      await new Promise(resolve => window.setTimeout(resolve, 1800))
    }
  }

  const startBatch = async (all = false) => {
    if (batchBusy || blocked || batchActive || !samplingEnabled) return
    const size = recognitionBatchSize(batchLimit, batchTargets, all)
    if (!size) return
    if (all && !window.confirm(`将分批重新请求模型识别 ${size} 张代表图，可能产生相应模型费用。继续吗？`)) return
    setBatchBusy(true)
    setBatchError(null)
    try {
      // A retry after an unclear response reuses the exact photo range.
      const requests = pendingBatch
        ? [pendingBatch]
        : batchRequests((await listMaterialUnsettledRepresentatives(scheme.id, { verdict: batchVerdict, limit: size })).file_ids)
      if (!requests.length) throw new Error('没有读取到需要重新识别的代表图，请刷新后重试。')
      for (const request of requests) {
        setPendingBatch(request)
        const started = await startMaterialRecognitionRun(scheme.id, request)
        if (!mounted.current) return
        setBatchRun(started)
        setPendingBatch(null)
        const finished = await waitForBatchRun(started.id)
        if (!finished || finished.status === 'failed') break
      }
      if (!mounted.current) return
      await loadGroups()
      if (!mounted.current) return
      setRevision(value => value + 1)
      changed.current?.()
    } catch (cause) {
      if (!mounted.current) return
      const ambiguous = !(cause instanceof ApiError) || cause.status === 0 || cause.status >= 500
      if (!ambiguous) setPendingBatch(null)
      setBatchError(ambiguous
        ? `任务结果尚不明确：${errorMessage(cause)}。重试会沿用原照片范围。`
        : errorMessage(cause))
    } finally {
      if (mounted.current) setBatchBusy(false)
    }
  }

  const updateGroup = (updated: MaterialGroup) => {
    if (!mounted.current) return
    setGroups(items => items.map(item => item.id === updated.id ? updated : item))
    void loadGroups()
    changed.current?.()
  }

  const displayedScanErrors = scanErrors.length ? scanErrors : run?.scan_errors ?? []

  const startRename = (group: MaterialGroup) => {
    setEditingName(group.id)
    setNameDraft(group.reference_name ?? '')
    setNameError(null)
  }

  const cancelRename = () => {
    setEditingName(null)
    setNameDraft('')
    setNameError(null)
  }

  const saveName = (group: MaterialGroup) => {
    const name = nameDraft.trim()
    if (!name || nameBusy || blocked) return
    setNameBusy(true)
    setNameError(null)
    void renameMaterialGroup(group.id, name, group.version)
      .then(updated => {
        if (!mounted.current) return
        setEditingName(null)
        updateGroup(updated)
      })
      .catch(cause => { if (mounted.current) setNameError(errorMessage(cause)) })
      .finally(() => { if (mounted.current) setNameBusy(false) })
  }

  const hasGroups = (stats?.groups ?? 0) > 0
  const newCalls = stats?.new_calls ?? 0
  const hasMoreGroups = total > 0 && groups.length < total && !loadedAll

  // Scrolling near the sentinel fetches the next page; the observer is rebuilt after
  // each load, so an initial callback keeps filling while the sentinel stays in view.
  // A failed page stops auto-loading until the list is refreshed.
  useEffect(() => {
    const target = loadMoreTarget.current
    if (!target || !hasMoreGroups || loading || loadingMore || error) return
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) void loadGroups({ append: true })
    }, { rootMargin: '400px 0px' })
    observer.observe(target)
    return () => observer.disconnect()
  }, [hasMoreGroups, loading, loadingMore, loadGroups, error])

  return (
    <div className="mg-browser">
      <div className="mf-card mg-controls">
        <div className="mg-control-head">
          <div>
            <h3>照片分组</h3>
            <p className="mg-overview">{stats ? <><strong>{stats.files}</strong> 张照片 · <strong>{stats.groups}</strong> 组</> : '正在读取照片…'}{samplingEnabled ? ' · 每组自动选 1 张代表图' : null}{pendingPicks > 0 ? ` · 通过/待复核组自动入选剩余 ${pendingPicks} 组` : null}</p>
          </div>
          <div className="mg-control-actions">
            {!hasGroups ? <button type="button" className="mp-btn-primary" disabled={blocked || loading} onClick={() => void execute({ kind: 'scan', requestId: newRequestId() })}>开始分组</button> : null}
            {samplingEnabled && newCalls > 0 ? <button type="button" className="mp-btn-primary" disabled={blocked || loading} onClick={() => recognize()}>识别剩余 {newCalls} 组</button> : null}
            <details className="mg-more" onClick={event => { if ((event.target as HTMLElement).closest('button')) event.currentTarget.open = false }}>
              <summary>更多操作</summary>
              <div className="mg-more-panel">
                {hasGroups ? <button type="button" disabled={blocked || loading} onClick={() => void execute({ kind: 'scan', requestId: newRequestId() })}>重新分组</button> : null}
                {samplingEnabled && hasGroups ? <button type="button" disabled={blocked || loading} onClick={() => recognize(false, 5)}>先试 5 组</button> : null}
                {samplingEnabled && hasGroups ? <button type="button" disabled={blocked || loading} onClick={() => void backfillSuccessfulGroups()}>补齐通过/待复核组前两张入选</button> : null}
                <button type="button" disabled={loading} onClick={() => void loadGroups()}>刷新列表</button>
                <button type="button" onClick={() => setShowDetails(value => !value)}>{showDetails ? '收起处理详情' : '查看处理详情'}</button>
              </div>
            </details>
          </div>
        </div>
        <p className="mf-hint">{!hasGroups ? '先把相似照片归在一起，再看图挑选。' : samplingEnabled ? newCalls > 0 ? '每组只识别一张代表图，已有结果直接复用。' : '通过/待复核组的前两张已自动入选；待复核组会带「菜名待确认」命名一起上传。' : '已有逐图结果的照片可直接挑选入选。'}</p>
        {showDetails && stats ? (
          <div className="mg-stats" aria-label="全方案分组与请求统计">
            <span>原图 <strong>{stats.files}</strong> 张</span><span>相似组 <strong>{stats.groups}</strong> 个</span>
            <span>{samplingEnabled ? '已识别' : '已有逐图结果'} <strong>{stats.recognized}</strong> 张</span>
            {samplingEnabled ? <><span>代表图 <strong>{stats.representatives}</strong> 张</span><span>缓存可复用 <strong>{stats.cache_hits}</strong> 张</span></> : null}
            <span>{samplingEnabled ? '预计新增请求' : '分组模型请求'} <strong>{samplingEnabled ? stats.new_calls : 0}</strong> 次</span>
            {samplingEnabled && stats.blocked_unknown > 0 ? <span className="mg-warning">结果未知待主动重试 <strong>{stats.blocked_unknown}</strong> 张</span> : null}
          </div>
        ) : null}
        {samplingEnabled && stats && stats.blocked_unknown > 0 ? <p className="mg-warning">{stats.blocked_unknown} 张代表图的上次识别结果未返回。切到「未识别」筛选后可批量重新识别。</p> : null}
        {displayedScanErrors.length ? (
          <details className="mg-scan-errors">
            <summary>扫描异常 {displayedScanErrors.length} 项 · 展开查看</summary>
            <ul>{displayedScanErrors.map((item, index) => <li key={`${item.file_id}-${index}`}><strong>{item.relative_path || item.file_id}</strong><span>{item.error}</span></li>)}</ul>
          </details>
        ) : null}
        {error ? <p className="mf-error" role="alert">{error}</p> : null}
        {notice ? <p className="mf-hint" role="status">{notice}</p> : null}
        {pendingRetry ? <button type="button" className="mp-btn-secondary" disabled={busy || runActive || blockedByFilterRun} onClick={() => void execute(pendingRetry)}>重试同一操作</button> : null}
        {run && (showDetails || run.status !== 'succeeded') ? (
          <div className="mg-run" role="status" aria-live="polite">
            <strong>{run.kind === 'group_scan' ? '本机扫描分组' : '照片识别'} · {RUN_STATUS[run.status]}</strong>
            {runActive ? <span className="mf-spinner" aria-hidden="true" /> : null}
            {run.kind === 'group_scan'
              ? <span>扫描 {run.counters.scanned} · 已处理 {run.counters.processed} · 分组 {run.counters.groups}</span>
              : <span>计划识别 {run.counters.scanned} 张 · 已处理 {run.counters.processed} 张</span>}
            <span>缓存命中 {run.counters.cache_hits} · 实际模型请求 {run.counters.model_calls} · 失败 {run.counters.errors}</span>
            {runActive ? <p className="mf-hint">关闭页面不会停止后台任务，重新打开可继续查看。</p> : null}
            {run.error ? <p className="mf-error">{run.error}</p> : null}
            {run.auto_pick_errors?.length ? <p className="mf-error">有 {run.auto_pick_errors.length} 张未能自动生成入选副本：{run.auto_pick_errors[0]}</p> : null}
            {run.status === 'interrupted' && (samplingEnabled || run.kind === 'group_scan') ? <button type="button" className="mp-btn-secondary" disabled={blocked} onClick={() => void execute({ kind: 'resume', runId: run.id })}>继续中断任务</button> : null}
            {run.items?.some(item => item.error) ? (
              <details><summary>查看本次请求错误</summary><ul>{run.items.filter(item => item.error).map((item, index) => <li key={`${item.file_id}-${index}`}>{item.file_id}：{item.error}</li>)}</ul></details>
            ) : null}
          </div>
        ) : null}
      </div>

      <div className="mf-status-tabs" role="tablist" aria-label="按筛选结果过滤照片组">
        {GROUP_FILTER_TABS
          .filter(tab => tab.key !== 'unrecognized' || (counts[tab.key] ?? 0) > 0)
          .map(tab => <button
            key={tab.key}
            type="button"
            role="tab"
            aria-selected={status === tab.key}
            className={`mf-status-tab${status === tab.key ? ' is-active' : ''}`}
            onClick={() => { resetScrollDepth(); setStatus(tab.key) }}
          >
            {tab.label}
            <span>{counts[tab.key] ?? 0}</span>
          </button>)}
      </div>
      {(status === 'uncertain' || status === 'unrecognized') && samplingEnabled ? <div className="mf-run mf-recognition-batch">
        <div className="mf-recognition-batch-controls">
          <label>
            重新识别数量
            <input
              type="number"
              min={1}
              max={MAX_RECOGNITION_BATCH}
              step={1}
              value={batchLimit}
              onChange={event => setBatchLimit(event.target.value)}
              aria-label="重新识别数量"
            />
          </label>
          <button
            type="button"
            className="mp-btn-secondary"
            disabled={!recognitionBatchSize(batchLimit, batchTargets) || blocked || batchActive || batchBusy || loading || !!pendingBatch}
            onClick={() => void startBatch(false)}
          >
            重新识别 {recognitionBatchSize(batchLimit, batchTargets) ?? 0} 张
          </button>
          <button
            type="button"
            className="mp-btn-primary"
            disabled={!recognitionBatchSize(batchLimit, batchTargets, true) || blocked || batchActive || batchBusy || loading || !!pendingBatch}
            onClick={() => void startBatch(true)}
          >
            全部重新识别（{recognitionBatchSize(batchLimit, batchTargets, true) ?? 0} 张）
          </button>
        </div>
        <p className="mf-hint">按分组顺序取前 N 组，用每组代表图重新请求模型；每次最多 {MAX_RECOGNITION_BATCH} 张，更多时按 {MAX_RECOGNITION_BATCH} 张自动分批。正在识别或待确认的组也可在组详情里勾选照片单独重试。</p>
        {batchError ? <p className="mf-error" role="alert">{batchError}</p> : null}
        {pendingBatch ? <button type="button" className="mf-text-button" disabled={batchBusy} onClick={() => void startBatch(false)}>重试当前批次（{pendingBatch.file_ids?.length ?? 0} 张）</button> : null}
        {batchRun ? (
          <div className="mf-recognition-batch-status" role="status" aria-live="polite">
            <strong>批量重新识别 · {RUN_STATUS[batchRun.status] ?? batchRun.status}</strong>
            <div className="mf-progress-track"><span style={{ width: `${batchRun.counters.scanned ? Math.round(batchRun.counters.processed / batchRun.counters.scanned * 100) : 0}%` }} /></div>
            <div className="mf-progress-meta">
              <span>{batchRun.counters.processed} / {batchRun.counters.scanned} 张</span>
              <span>模型请求 {batchRun.counters.model_calls}</span>
              <span>失败 {batchRun.counters.errors}</span>
            </div>
            {batchRun.error ? <p className="mf-error">{batchRun.error}</p> : null}
            {batchRun.auto_pick_errors?.length ? <p className="mf-error">有 {batchRun.auto_pick_errors.length} 张未能生成自动入选副本：{batchRun.auto_pick_errors[0]}</p> : null}
          </div>
        ) : null}
      </div> : null}
      <div className="mg-list-head">
        <span className="mf-muted">{status !== 'all' || query ? `匹配 ${total} 组` : `共 ${total} 组`} · 组内照片直接预览</span>
        <form className="mf-search" onSubmit={event => { event.preventDefault(); resetScrollDepth(); setQuery(searchDraft.trim()) }}>
          <input value={searchDraft} onChange={event => setSearchDraft(event.target.value)} placeholder="搜索名称或原路径" aria-label="搜索照片组" />
          <button type="submit" className="mp-btn-secondary" disabled={loading}>搜索</button>
        </form>
      </div>
      {loading && !groups.length ? <p className="mf-muted">正在读取分组…</p> : null}
      {!loading && !groups.length ? <div className="mf-empty">{query || status !== 'all' ? '当前筛选下没有照片组。' : '尚无照片组，点击“开始分组”即可整理照片。'}</div> : null}
      <div className="mg-groups" aria-busy={loading || loadingMore}>
        {groups.map(group => {
          const editingThisName = editingName === group.id
          return (
          <article className={`mf-card mg-group is-${group.status}`} key={group.id}>
            <div className="mg-group-head">
              <div className="mg-group-summary">
                {editingThisName ? (
                  <form className="mg-name-edit" onSubmit={event => { event.preventDefault(); saveName(group) }}>
                    <input
                      autoFocus
                      value={nameDraft}
                      maxLength={GROUP_NAME_MAX}
                      aria-label="组名，同时作为入选照片的文件名"
                      placeholder="例如 卤牛腱肉_Spiced Beef"
                      onChange={event => setNameDraft(event.target.value)}
                      onKeyDown={event => { if (event.key === 'Escape') cancelRename() }}
                    />
                    <button type="submit" className="mp-btn-primary" disabled={nameBusy || !nameDraft.trim()}>保存名称</button>
                    <button type="button" className="mf-text-button" disabled={nameBusy} onClick={cancelRename}>取消</button>
                    <span className="mf-hint">最多 {GROUP_NAME_MAX} 字，作为入选照片导出的文件名。</span>
                  </form>
                ) : (
                  <div className="mg-name-row">
                    <h4>{group.reference_name || `未命名组 · ${group.id.slice(-6)}`}</h4>
                    {group.verdict ? <span className={`mf-badge is-${group.verdict}`}>{VERDICT_LABEL[group.verdict] ?? group.verdict}</span> : null}
                    <button type="button" className="mf-text-button" disabled={blocked || loading} onClick={() => startRename(group)}>{group.reference_name ? '改名' : '命名'}</button>
                  </div>
                )}
                {nameError && editingThisName ? <p className="mf-error" role="alert">{nameError}</p> : null}
                <GroupTags group={group} />
                {group.manual || (samplingEnabled && ['conflict', 'ambiguous'].includes(group.status)) ? <div className="mg-file-badges">{samplingEnabled && ['conflict', 'ambiguous'].includes(group.status) ? <span className="mf-badge is-uncertain">{GROUP_STATUS[group.status]}</span> : null}{group.manual ? <span className="mf-badge">人工分组</span> : null}</div> : null}
                {samplingEnabled && group.reference_stale ? <p className="mg-warning">代表图已变化，组名保持不变；需要时重新识别后复核。</p> : null}
                {samplingEnabled && group.status === 'conflict' ? <p className="mg-warning">代表图结论不一致，可补选照片识别；不会自动扩大请求。</p> : null}
              </div>
            </div>
            <GroupMembers key={`${group.id}-${revision}`} group={group} samplingEnabled={samplingEnabled} blocked={blocked} onPicked={() => { void loadGroups(); changed.current?.() }} />
          </article>
        )})}
        {hasMoreGroups ? <div className="mg-scroll-sentinel" ref={loadMoreTarget}>{loadingMore ? <><span className="mf-spinner" aria-hidden="true" />正在加载更多组…</> : null}</div> : null}
      </div>
      {total > 0 ? <div className="mf-results-foot"><span>已显示 <strong>{groups.length}</strong> / {total} 组</span></div> : null}
    </div>
  )
}

export function GroupTags({ group }: { group: MaterialGroup }) {
  const evaluation = group.cover?.evaluation
  const tags = evaluation?.tags ?? []
  if (!tags.length && !evaluation?.category) return null
  return (
    <div className="mg-group-tags" title="分类与标签来自代表图">
      {evaluation?.category ? <span className="mf-badge">{evaluation.category}</span> : null}
      {tags.length ? <div className="mf-tags">{tags.map(tag => <span key={tag}>{tag}</span>)}</div> : null}
    </div>
  )
}

function GroupMembers({ group, samplingEnabled, blocked, onPicked }: {
  group: MaterialGroup
  samplingEnabled: boolean
  blocked: boolean
  onPicked: () => void
}) {
  const [files, setFiles] = useState<MaterialGroupFile[]>(() => (group.previews ?? []).map(file => ({ ...file, display_name: null, recognition_status: 'unrecognized', source_size: 0, evaluation: null, latest_attempt: null, attempt_count: 0 })))
  const [visibleLimit, setVisibleLimit] = useState(GROUP_PREVIEW_LIMIT)
  const [total, setTotal] = useState(group.count)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const mounted = useRef(true)
  const mutationBusy = useRef(false)
  const pageBusy = useRef(false)

  const loadFiles = useCallback(async (skip: number) => {
    if (pageBusy.current) return
    pageBusy.current = true
    setLoading(true)
    try {
      const page = await listMaterialGroupFiles(group.id, { skip, limit: FILE_PAGE_SIZE, representative_count: REPRESENTATIVE_COUNT })
      if (!mounted.current) return
      setFiles(previous => skip === 0 ? page.items : [...previous, ...page.items.filter(item => !previous.some(old => old.id === item.id))])
      setTotal(page.total)
      setError(null)
    } catch (cause) {
      if (mounted.current) setError(errorMessage(cause))
    } finally {
      pageBusy.current = false
      if (mounted.current) setLoading(false)
    }
  }, [group.id])

  useEffect(() => {
    mounted.current = true
    void Promise.resolve().then(() => { if (mounted.current) void loadFiles(0) })
    return () => { mounted.current = false }
  }, [loadFiles])

  const mutate = async (action: () => Promise<void>) => {
    if (mutationBusy.current || blocked) return
    mutationBusy.current = true
    setBusy(true)
    setError(null)
    try { await action() }
    catch (cause) { if (mounted.current) setError(errorMessage(cause)) }
    finally { mutationBusy.current = false; if (mounted.current) setBusy(false) }
  }

  // Members are only recognized one by one, so a photo without its own record
  // borrows the representative result instead of forcing a model call first.
  const pick = (file: MaterialGroupFile) => void mutate(async () => {
    const evaluation = file.evaluation
      ? await setMaterialFilterPick(file.evaluation.id, !file.evaluation.manual_pick)
      : await inheritMaterialGroupPick(group.id, file.id)
    if (!mounted.current) return
    setFiles(previous => previous.map(item => item.id === file.id ? { ...item, evaluation } : item))
    await loadFiles(0)
    onPicked()
  })

  // Picked photos lead the group; sort() is stable, so unpicked photos keep the server order.
  const ordered = [...files].sort((left, right) => Number(Boolean(right.evaluation?.manual_pick)) - Number(Boolean(left.evaluation?.manual_pick)))
  // A member without its own record may still be picked by borrowing the
  // representative result, unless that verdict is fail/error.
  const canBorrow = samplingEnabled && (group.verdict === 'pass' || group.verdict === 'uncertain')

  return (
    <div className="mg-photo-browser">
      <div className="mg-group-previews" aria-label={`组内照片，共 ${total} 张`}>
        {ordered.slice(0, visibleLimit).map(file => {
          const evaluation = file.evaluation
          const picked = !!evaluation?.manual_pick
          const canPick = !!evaluation || canBorrow
          return <div className="mg-preview" key={file.id}>
            <a className="mg-preview-image" href={file.preview_url ?? undefined} target="_blank" rel="noreferrer" title={`查看原图：${file.original_name}`}>
              {file.preview_url ? <img src={file.preview_url} alt={file.original_name} loading="lazy" /> : <span>暂无缩略图</span>}
            </a>
            <span className="mg-preview-name" title={file.relative_path}>{file.original_name}</span>
            {canPick ? <button type="button" className={`mf-pick${picked ? ' is-picked' : ''}`} aria-pressed={picked} disabled={blocked || busy || loading} onClick={() => pick(file)}>{picked ? '★ 已入选' : '☆ 入选'}</button> : <span className="mf-hint mg-pick-status">{loading ? '读取中…' : '尚无可入选结果'}</span>}
          </div>
        })}
        {Math.min(visibleLimit, files.length) < total ? <button type="button" className="mg-preview-more" disabled={loading} onClick={() => { setVisibleLimit(value => value + FILE_PAGE_SIZE); if (visibleLimit >= files.length) void loadFiles(files.length) }}>+{total - Math.min(visibleLimit, files.length)}<span>{loading ? '读取中…' : '显示更多照片'}</span></button> : null}
      </div>
      {error ? <p className="mf-error" role="alert">{error}<button type="button" className="mf-text-button" disabled={loading} onClick={() => void loadFiles(0)}>重新读取照片</button></p> : null}
    </div>
  )
}
