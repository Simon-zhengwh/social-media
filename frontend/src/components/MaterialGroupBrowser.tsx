import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ApiError,
  getMaterialGroupRun,
  listMaterialGroupFiles,
  listMaterialGroups,
  listMaterialFileAttempts,
  renameMaterialGroup,
  resumeMaterialGroupRun,
  setMaterialFilterPick,
  setMaterialGroupRepresentatives,
  startMaterialGroupScan,
  startMaterialRecognitionRun,
  splitMaterialGroup,
} from '../api'
import type {
  MaterialGroup,
  MaterialGroupFile,
  MaterialGroupRun,
  MaterialGroupsPage,
  MaterialFilterScheme,
  MaterialRecognitionRequest,
  MaterialFileAttempt,
  MaterialGroupScanError,
} from '../types'
import './MaterialGroupBrowser.css'

const GROUP_PAGE_SIZE = 30
const FILE_PAGE_SIZE = 48
const GROUP_STATUS = { unrecognized: '未识别', recognized: '已有参考名', ambiguous: '待确认', conflict: '参考名冲突', manual: '手动命名' }
const FILE_STATUS = { unrecognized: '未识别', recognized: 'AI 已识别', uncertain: '待确认', error: '请求失败', unknown: '结果未知 · 不自动重发' }
const GROUP_NAME_MAX = 60
const RUN_STATUS = { queued: '排队中', running: '运行中', succeeded: '已完成', interrupted: '已中断', failed: '失败' }
const ATTEMPT_STATUS: Record<string, string> = { running: '请求中', queued: '排队中', succeeded: '已获得结果', cached: '复用已有结果', failed: '请求失败', unknown: '结果未知' }
const DECISION_LABEL = { pass: '通过', fail: '不通过', uncertain: '待复核', error: '请求失败' }
const GROUP_PREVIEW_LIMIT = 8
const REPRESENTATIVE_COUNT = 1

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

export default function MaterialGroupBrowser({ scheme, onChanged, blockedByFilterRun = false }: { scheme: MaterialFilterScheme; onChanged?: () => void; blockedByFilterRun?: boolean }) {
  const [groups, setGroups] = useState<MaterialGroup[]>([])
  const [stats, setStats] = useState<MaterialGroupsPage['stats'] | null>(null)
  const [scanErrors, setScanErrors] = useState<MaterialGroupScanError[]>([])
  const [total, setTotal] = useState(0)
  const [skip, setSkip] = useState(0)
  const [query, setQuery] = useState('')
  const [searchDraft, setSearchDraft] = useState('')
  const [showDetails, setShowDetails] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [run, setRun] = useState<MaterialGroupRun | null>(null)
  const [busy, setBusy] = useState(false)
  const [pendingRetry, setPendingRetry] = useState<Operation | null>(null)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [editingName, setEditingName] = useState<string | null>(null)
  const [nameDraft, setNameDraft] = useState('')
  const [nameBusy, setNameBusy] = useState(false)
  const [nameError, setNameError] = useState<string | null>(null)
  const [samplingAllowed, setSamplingAllowed] = useState(false)
  const [revision, setRevision] = useState(0)
  const requestTicket = useRef(0)
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

  const loadGroups = useCallback(async (options: { skip?: number; q?: string } = {}) => {
    if (!mounted.current) return
    const ticket = ++requestTicket.current
    setLoading(true)
    try {
      const page = await listMaterialGroups(scheme.id, { skip: options.skip ?? skip, limit: GROUP_PAGE_SIZE, q: options.q ?? query, representative_count: REPRESENTATIVE_COUNT })
      if (!mounted.current || ticket !== requestTicket.current) return
      setSamplingAllowed(page.sampling_enabled === true)
      setGroups(page.items)
      setTotal(page.total)
      setStats(page.stats)
      setScanErrors(page.scan_errors ?? [])
      if (page.last_run) setRun(page.last_run)
      setError(null)
    } catch (cause) {
      if (mounted.current && ticket === requestTicket.current) setError(errorMessage(cause))
    } finally {
      if (mounted.current && ticket === requestTicket.current) setLoading(false)
    }
  }, [scheme.id, skip, query])

  useEffect(() => { void Promise.resolve().then(() => loadGroups()) }, [loadGroups])

  const runActive = isActive(run)
  const runId = run?.id
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
          if (next.kind === 'group_scan') setSelected(new Set())
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
  const recognize = (mode: 'representatives' | 'selected_files', force = false, groupLimit?: number) => {
    if (blocked || loading || !samplingEnabled) return
    void execute({ kind: 'recognition', payload: {
      request_id: newRequestId(),
      mode,
      representative_count: REPRESENTATIVE_COUNT,
      force,
      ...(mode === 'selected_files' ? { file_ids: Array.from(selected) } : {}),
      ...(groupLimit ? { group_limit: groupLimit } : {}),
    } })
  }

  const updateGroup = (updated: MaterialGroup) => {
    if (!mounted.current) return
    setGroups(items => items.map(item => item.id === updated.id ? updated : item))
    void loadGroups()
    changed.current?.()
  }

  const updateSplitGroups = (updated: MaterialGroup[]) => {
    if (!mounted.current) return
    setGroups(items => items.map(item => updated.find(next => next.id === item.id) ?? item))
    setExpanded(previous => new Set([...previous, ...updated.map(item => item.id)]))
    setRevision(value => value + 1)
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

  const toggleSelected = (fileId: string) => {
    setSelected(previous => {
      const next = new Set(previous)
      if (next.has(fileId)) next.delete(fileId)
      else next.add(fileId)
      return next
    })
  }

  const hasGroups = (stats?.groups ?? 0) > 0
  const newCalls = stats?.new_calls ?? 0

  return (
    <div className="mg-browser">
      <div className="mf-card mg-controls">
        <div className="mg-control-head">
          <div>
            <h3>照片分组</h3>
            <p className="mg-overview">{stats ? <><strong>{stats.files}</strong> 张照片 · <strong>{stats.groups}</strong> 组</> : '正在读取照片…'}{samplingEnabled ? ' · 每组自动选 1 张代表图' : null}</p>
          </div>
          <div className="mg-control-actions">
            {!hasGroups ? <button type="button" className="mp-btn-primary" disabled={blocked || loading} onClick={() => void execute({ kind: 'scan', requestId: newRequestId() })}>开始分组</button> : null}
            {samplingEnabled && newCalls > 0 ? <button type="button" className="mp-btn-primary" disabled={blocked || loading} onClick={() => recognize('representatives')}>识别剩余 {newCalls} 组</button> : null}
            <details className="mg-more" onClick={event => { if ((event.target as HTMLElement).closest('button')) event.currentTarget.open = false }}>
              <summary>更多操作</summary>
              <div className="mg-more-panel">
                {hasGroups ? <button type="button" disabled={blocked || loading} onClick={() => void execute({ kind: 'scan', requestId: newRequestId() })}>重新分组</button> : null}
                {samplingEnabled && hasGroups ? <button type="button" disabled={blocked || loading} onClick={() => recognize('representatives', false, 5)}>先试 5 组</button> : null}
                <button type="button" disabled={loading} onClick={() => void loadGroups()}>刷新列表</button>
                <button type="button" onClick={() => setShowDetails(value => !value)}>{showDetails ? '收起处理详情' : '查看处理详情'}</button>
              </div>
            </details>
          </div>
        </div>
        <p className="mf-hint">{!hasGroups ? '先把相似照片归在一起，再看图挑选。' : samplingEnabled ? newCalls > 0 ? '每组只识别一张代表图，已有结果直接复用。' : '代表图已有识别结果，可展开照片并点「☆ 入选」。' : '展开照片即可查看筛选结果、挑选入选照片。'}</p>
        {showDetails && stats ? (
          <div className="mg-stats" aria-label="全方案分组与请求统计">
            <span>原图 <strong>{stats.files}</strong> 张</span><span>相似组 <strong>{stats.groups}</strong> 个</span>
            <span>{samplingEnabled ? '已识别' : '已有逐图结果'} <strong>{stats.recognized}</strong> 张</span>
            {samplingEnabled ? <><span>代表图 <strong>{stats.representatives}</strong> 张</span><span>缓存可复用 <strong>{stats.cache_hits}</strong> 张</span></> : null}
            <span>{samplingEnabled ? '预计新增请求' : '分组模型请求'} <strong>{samplingEnabled ? stats.new_calls : 0}</strong> 次</span>
            {samplingEnabled && stats.blocked_unknown > 0 ? <span className="mg-warning">结果未知待主动重试 <strong>{stats.blocked_unknown}</strong> 张</span> : null}
          </div>
        ) : null}
        {samplingEnabled && stats && stats.blocked_unknown > 0 ? <p className="mg-warning">{stats.blocked_unknown} 张代表图的上次识别结果未返回。可在照片详情中选择后重新识别。</p> : null}
        {displayedScanErrors.length ? (
          <details className="mg-scan-errors">
            <summary>扫描异常 {displayedScanErrors.length} 项 · 展开查看</summary>
            <ul>{displayedScanErrors.map((item, index) => <li key={`${item.file_id}-${index}`}><strong>{item.relative_path || item.file_id}</strong><span>{item.error}</span></li>)}</ul>
          </details>
        ) : null}
        {samplingEnabled && selected.size > 0 ? <div className="mg-selection-actions">
          <strong>已选择 {selected.size} 张用于识别</strong>
          <button type="button" className="mp-btn-secondary" disabled={blocked || loading || !selected.size} onClick={() => recognize('selected_files')}>识别所选 {selected.size} 张</button>
          <button type="button" className="mp-btn-secondary" disabled={blocked || loading || !selected.size} onClick={() => recognize('selected_files', true)}>重新识别所选 {selected.size} 张</button>
          <button type="button" className="mf-text-button" disabled={blocked} onClick={() => setSelected(new Set())}>取消选图</button>
          <p className="mf-hint">识别复用已有结果；重新识别会对选中的照片重新请求。</p>
        </div> : null}
        {error ? <p className="mf-error" role="alert">{error}</p> : null}
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
            {run.status === 'interrupted' && (samplingEnabled || run.kind === 'group_scan') ? <button type="button" className="mp-btn-secondary" disabled={blocked} onClick={() => void execute({ kind: 'resume', runId: run.id })}>继续中断任务</button> : null}
            {run.items?.some(item => item.error) ? (
              <details><summary>查看本次请求错误</summary><ul>{run.items.filter(item => item.error).map((item, index) => <li key={`${item.file_id}-${index}`}>{item.file_id}：{item.error}</li>)}</ul></details>
            ) : null}
          </div>
        ) : null}
      </div>

      <div className="mg-list-head">
        <span className="mf-muted">{query ? `匹配 ${total} 组` : `共 ${total} 组`} · 组内照片直接预览</span>
        <form className="mf-search" onSubmit={event => { event.preventDefault(); setQuery(searchDraft.trim()); setSkip(0) }}>
          <input value={searchDraft} onChange={event => setSearchDraft(event.target.value)} placeholder="搜索名称或原路径" aria-label="搜索照片组" />
          <button type="submit" className="mp-btn-secondary" disabled={loading}>搜索</button>
        </form>
      </div>
      {loading && !groups.length ? <p className="mf-muted">正在读取分组…</p> : null}
      {!loading && !groups.length ? <div className="mf-empty">{query ? '没有匹配的照片组。' : '尚无照片组，点击“开始分组”即可整理照片。'}</div> : null}
      <div className="mg-groups" aria-busy={loading}>
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
                    <button type="button" className="mf-text-button" disabled={blocked || loading} onClick={() => startRename(group)}>{group.reference_name ? '改名' : '命名'}</button>
                  </div>
                )}
                {nameError && editingThisName ? <p className="mf-error" role="alert">{nameError}</p> : null}
                {group.manual || (samplingEnabled && ['conflict', 'ambiguous'].includes(group.status)) ? <div className="mg-file-badges">{samplingEnabled && ['conflict', 'ambiguous'].includes(group.status) ? <span className="mf-badge is-uncertain">{GROUP_STATUS[group.status]}</span> : null}{group.manual ? <span className="mf-badge">人工分组</span> : null}</div> : null}
                {samplingEnabled && group.reference_stale ? <p className="mg-warning">代表图已变化，组名保持不变；需要时重新识别后复核。</p> : null}
                {samplingEnabled && group.status === 'conflict' ? <p className="mg-warning">代表图结论不一致，可补选照片识别；不会自动扩大请求。</p> : null}
              </div>
              <button type="button" className="mf-text-button" aria-expanded={expanded.has(group.id)} onClick={() => setExpanded(previous => { const next = new Set(previous); if (next.has(group.id)) next.delete(group.id); else next.add(group.id); return next })}>{expanded.has(group.id) ? '收起详情' : '更多详情'}</button>
            </div>
            <GroupMembers key={`${group.id}-${revision}`} group={group} detailsOpen={expanded.has(group.id)} samplingEnabled={samplingEnabled} selected={selected} onSelected={toggleSelected} blocked={blocked} onGroupChanged={updateGroup} onSplit={updateSplitGroups} onPicked={() => { void loadGroups(); changed.current?.() }} />
          </article>
        )})}
      </div>
      {total > 0 ? <div className="mf-results-foot"><span>第 {Math.floor(skip / GROUP_PAGE_SIZE) + 1} 页 · 显示 {skip + 1}–{Math.min(skip + groups.length, total)} / {total} 组</span><button type="button" className="mp-btn-secondary" disabled={loading || skip === 0} onClick={() => setSkip(value => Math.max(0, value - GROUP_PAGE_SIZE))}>上一页</button><button type="button" className="mp-btn-secondary" disabled={loading || skip + groups.length >= total} onClick={() => setSkip(value => value + GROUP_PAGE_SIZE)}>下一页</button></div> : null}
    </div>
  )
}

function GroupMembers({ group, detailsOpen, samplingEnabled, selected, onSelected, blocked, onGroupChanged, onSplit, onPicked }: {
  group: MaterialGroup
  detailsOpen: boolean
  samplingEnabled: boolean
  selected: Set<string>
  onSelected: (id: string) => void
  blocked: boolean
  onGroupChanged: (group: MaterialGroup) => void
  onSplit: (groups: MaterialGroup[]) => void
  onPicked: () => void
}) {
  const [files, setFiles] = useState<MaterialGroupFile[]>(() => (group.previews ?? []).map(file => ({ ...file, display_name: null, recognition_status: 'unrecognized', source_size: 0, evaluation: null, latest_attempt: null, attempt_count: 0 })))
  const [visibleLimit, setVisibleLimit] = useState(GROUP_PREVIEW_LIMIT)
  const [total, setTotal] = useState(group.count)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [splitMode, setSplitMode] = useState(false)
  const [splitSelected, setSplitSelected] = useState<Set<string>>(new Set())
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

  const pick = (file: MaterialGroupFile) => void mutate(async () => {
    if (!file.evaluation) return
    const evaluation = await setMaterialFilterPick(file.evaluation.id, !file.evaluation.manual_pick)
    if (!mounted.current) return
    setFiles(previous => previous.map(item => item.id === file.id ? { ...item, evaluation } : item))
    onPicked()
  })

  const split = () => void mutate(async () => {
    if (!splitSelected.size || splitSelected.size >= total) return
    const result = await splitMaterialGroup(group.id, Array.from(splitSelected), group.version)
    if (!mounted.current) return
    setSplitMode(false)
    setSplitSelected(new Set())
    onSplit(result.groups)
  })

  // Picked photos lead the group; sort() is stable, so unpicked photos keep the server order.
  const ordered = [...files].sort((left, right) => Number(Boolean(right.evaluation?.manual_pick)) - Number(Boolean(left.evaluation?.manual_pick)))

  return (
    <div className="mg-photo-browser">
      <div className="mg-group-previews" aria-label={`组内照片，共 ${total} 张`}>
        {ordered.slice(0, visibleLimit).map(file => {
          const evaluation = file.evaluation
          const canPick = !!evaluation && (!samplingEnabled || ['recognized', 'uncertain'].includes(file.recognition_status))
          return <div className="mg-preview" key={file.id}>
            <a className="mg-preview-image" href={file.preview_url ?? undefined} target="_blank" rel="noreferrer" title={`查看原图：${file.original_name}`}>
              {file.preview_url ? <img src={file.preview_url} alt={file.original_name} loading="lazy" /> : <span>暂无缩略图</span>}
            </a>
            <span className="mg-preview-name" title={file.relative_path}>{file.original_name}</span>
            {canPick ? <button type="button" className={`mf-pick${evaluation.manual_pick ? ' is-picked' : ''}`} aria-pressed={evaluation.manual_pick} disabled={blocked || busy || loading} onClick={() => pick(file)}>{evaluation.manual_pick ? '★ 已入选' : '☆ 入选'}</button> : <span className="mf-hint mg-pick-status">{loading ? '读取中…' : '尚无可入选结果'}</span>}
          </div>
        })}
        {Math.min(visibleLimit, files.length) < total ? <button type="button" className="mg-preview-more" disabled={loading} onClick={() => { setVisibleLimit(value => value + FILE_PAGE_SIZE); if (visibleLimit >= files.length) void loadFiles(files.length) }}>+{total - Math.min(visibleLimit, files.length)}<span>{loading ? '读取中…' : '显示更多照片'}</span></button> : null}
      </div>
      {error ? <p className="mf-error" role="alert">{error}<button type="button" className="mf-text-button" disabled={loading} onClick={() => void loadFiles(0)}>重新读取照片</button></p> : null}
      {detailsOpen ? <div className="mg-members">
      <p className="mf-hint">{samplingEnabled ? '每组已自动选好一张代表图，可直接挑选已有结果的照片。' : '每张照片保留自己的筛选结果，可直接挑选。'}</p>
      <div className="mg-split-actions">
        <button type="button" className="mp-btn-secondary" disabled={blocked || busy || group.count < 2} aria-expanded={splitMode} onClick={() => { setSplitMode(value => !value); setSplitSelected(new Set()) }}>{splitMode ? '取消拆组' : '选择成员拆组'}</button>
        {splitMode ? <>
          <span>已选 {splitSelected.size} / {total} 张用于拆组</span>
          <button type="button" className="mp-btn-secondary" disabled={blocked || busy || !splitSelected.size || splitSelected.size >= total} onClick={split}>将所选 {splitSelected.size} 张拆为新组</button>
          <p className="mf-hint">仅修改本机分组记录，不移动源文件，也不请求模型。请保留至少一张在当前组；拆组选择与识别选择、☆ 入选独立。</p>
        </> : null}
      </div>
      <div className="mg-file-grid">
        {ordered.map(file => {
          const representative = group.representative_file_ids.includes(file.id)
          const evaluation = file.evaluation
          const individuallyRecognized = samplingEnabled ? file.recognition_status === 'recognized' || file.recognition_status === 'uncertain' : !!evaluation
          const decision = evaluation?.effective_decision ?? evaluation?.human_decision ?? evaluation?.decision
          const needsAttention = samplingEnabled
            ? ['uncertain', 'error', 'unknown'].includes(file.recognition_status)
            : !!decision && decision !== 'pass'
          return (
            <article className="mg-file" key={file.id}>
              <div className="mg-file-body">
                <div className="mg-file-title">
                  <strong>{individuallyRecognized ? file.display_name || evaluation?.display_name || file.original_name : file.original_name}</strong>
                  {samplingEnabled && representative ? <span className="mf-badge is-picked">代表</span> : null}
                  {needsAttention ? <span className="mf-badge is-uncertain">{samplingEnabled ? FILE_STATUS[file.recognition_status] : decision ? DECISION_LABEL[decision] : '尚无逐图判定'}</span> : null}
                </div>
                <div className="mg-file-actions">
                  {splitMode ? <label className="mg-file-select mg-split-select"><input type="checkbox" checked={splitSelected.has(file.id)} disabled={blocked || busy} onChange={() => setSplitSelected(previous => { const next = new Set(previous); if (next.has(file.id)) next.delete(file.id); else next.add(file.id); return next })} />选入新组</label> : null}
                </div>
                {samplingEnabled && file.recognition_status === 'unknown' ? <p className="mg-warning">上次识别结果未返回，可在详情中选择后重新识别。</p> : null}
                {evaluation?.error ? <p className="mg-warning">{evaluation.error}</p> : null}
                <details className="mg-file-more">
                  <summary>详情</summary>
                  {samplingEnabled ? <label className="mg-file-select"><input type="checkbox" checked={selected.has(file.id)} disabled={blocked || busy} onChange={() => onSelected(file.id)} />用于识别</label> : null}
                  <p className="mg-path" title={file.relative_path}>{file.relative_path}</p>
                  {samplingEnabled && !individuallyRecognized && group.reference_name ? <p className="mf-hint">组名：{group.reference_name}（此图未独立识别）</p> : null}
                  {samplingEnabled && individuallyRecognized && group.reference_name && (file.display_name || evaluation?.display_name) !== group.reference_name ? <p className="mf-hint">单图识别名与组名不一致</p> : null}
                  {individuallyRecognized && evaluation?.description ? <p className="mf-hint">{evaluation.description}</p> : null}
                  {!samplingEnabled && evaluation?.human_decision ? <p className="mf-hint">已人工确认</p> : null}
                  {evaluation?.uat_asset_id ? <p className="mf-hint">已同步 UAT</p> : null}
                  <div className="mg-file-actions">
                    {samplingEnabled ? <button type="button" className="mf-text-button" disabled={blocked || busy || (representative && group.representative_file_ids.length === 1)} onClick={() => void mutate(async () => onGroupChanged(await setMaterialGroupRepresentatives(group.id, [file.id], group.version)))}>{representative && group.representative_file_ids.length === 1 ? '当前代表' : '仅用此图为代表'}</button> : null}
                    {evaluation?.download_url ? <a className="mf-text-button" href={evaluation.download_url}>下载原图</a> : null}
                  </div>
                  {samplingEnabled ? <FileAttemptHistory fileId={file.id} count={file.attempt_count ?? 0} /> : null}
                </details>
              </div>
            </article>
          )
        })}
      </div>
      </div> : null}
    </div>
  )
}

function FileAttemptHistory({ fileId, count }: { fileId: string; count: number }) {
  const [open, setOpen] = useState(false)
  const [attempts, setAttempts] = useState<MaterialFileAttempt[] | null>(null)
  const [total, setTotal] = useState(count)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const mounted = useRef(true)
  const requestBusy = useRef(false)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  const load = async (skip: number) => {
    if (requestBusy.current) return
    requestBusy.current = true
    setLoading(true)
    setError(null)
    try {
      const page = await listMaterialFileAttempts(fileId, { skip, limit: 10 })
      if (!mounted.current) return
      setAttempts(previous => skip === 0 ? page.items : [...previous ?? [], ...page.items])
      setTotal(page.total)
    } catch (cause) {
      if (mounted.current) setError(errorMessage(cause))
    } finally {
      requestBusy.current = false
      if (mounted.current) setLoading(false)
    }
  }

  return (
    <div className="mg-history">
      <button type="button" className="mf-text-button" aria-expanded={open} onClick={() => { setOpen(value => !value); if (!open && attempts === null) void load(0) }}>识别历史（{total}）{open ? ' · 收起' : ''}</button>
      {open ? <div className="mg-history-content">
        {error ? <p className="mg-warning" role="alert">{error}</p> : null}
        {!loading && attempts?.length === 0 ? <p className="mf-hint">尚无识别尝试。</p> : null}
        {attempts?.map(attempt => <article className="mg-attempt" key={attempt.id}>
          <strong>{ATTEMPT_STATUS[attempt.status] ?? attempt.status}</strong>
          <span>{attempt.actual_call ? '已发起模型请求' : '未发起模型请求'}{attempt.force ? ' · 主动重新识别' : ''}</span>
          <time dateTime={attempt.created_at}>{new Date(attempt.created_at).toLocaleString()}</time>
          {attempt.finished_at ? <span>结束：{new Date(attempt.finished_at).toLocaleString()}</span> : null}
          {attempt.error ? <p className="mg-warning">{attempt.error}</p> : null}
          {attempt.status === 'unknown' ? <p className="mg-warning">本次结果未知，普通识别不会再次请求。</p> : null}
          {attempt.result ? <AttemptResult label="本次结果" result={attempt.result} /> : null}
          {attempt.previous_result ? <AttemptResult label="之前保留的结果" result={attempt.previous_result} /> : null}
        </article>)}
        {loading ? <p className="mf-hint">正在读取历史…</p> : null}
        {error ? <button type="button" className="mf-text-button" disabled={loading} onClick={() => void load(attempts?.length ?? 0)}>重试读取历史</button> : null}
        {attempts && attempts.length < total && !error ? <button type="button" className="mf-text-button" disabled={loading} onClick={() => void load(attempts.length)}>再显示 {Math.min(10, total - attempts.length)} 条历史（{attempts.length} / {total}）</button> : null}
      </div> : null}
    </div>
  )
}

function AttemptResult({ label, result }: { label: string; result: Record<string, unknown> }) {
  const name = typeof result.display_name === 'string' ? result.display_name : typeof result.name === 'string' ? result.name : [result.name_zh, result.name_en].filter(value => typeof value === 'string' && value).join('_')
  const description = typeof result.description === 'string' ? result.description : typeof result.reason === 'string' ? result.reason : ''
  const tags = Array.isArray(result.tags) ? result.tags.filter(value => typeof value === 'string').join('、') : ''
  return <div className="mg-attempt-result"><strong>{label}{name ? `：${name}` : ''}</strong>{description ? <p>{description}</p> : null}{tags ? <p>{tags}</p> : null}{!name && !description && !tags ? <span>已保存结果记录</span> : null}</div>
}
