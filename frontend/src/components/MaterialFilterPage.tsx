import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ApiError,
  addMaterialFilterFolder,
  cloneMaterialFilterScheme,
  deleteMaterialFilterFolder,
  exportPickedMaterialFilter,
  createMaterialFilterScheme,
  getMaterialFilterDefaults,
  getMaterialFilterRun,
  getMaterialFilterScheme,
  listMaterialFilterFolders,
  listMaterialFilterResults,
  listMaterialFilterSchemes,
  materialFilterExportPath,
  recheckMaterialFilterEvaluation,
  renameMaterialFilterFolder,
  resumeMaterialFilterRun,
  retryMaterialFilterFailed,
  reviewMaterialFilterEvaluation,
  setMaterialFilterPick,
  startMaterialFilterRun,
  updateMaterialFilterMetadata,
} from '../api'
import type {
  MaterialFilterDefaults,
  MaterialFilterEvaluation,
  MaterialFilterFolder,
  MaterialFilterRule,
  MaterialFilterRun,
  MaterialFilterScheme,
} from '../types'
import type { MaterialFilterSort } from '../api'
import './MaterialFilterPage.css'

const RESULTS_PAGE_SIZE = 60
const MAX_VISIBLE_RESULTS = 500

const STATUS_TABS: Array<{ key: string; label: string }> = [
  { key: 'all', label: '全部' },
  { key: 'pass', label: '通过' },
  { key: 'fail', label: '不通过' },
  { key: 'uncertain', label: '待复核' },
  { key: 'error', label: '请求失败' },
  { key: 'picked', label: '已入选' },
]

const SORT_OPTIONS = [
  { key: 'judged', label: '按判断时间' },
  { key: 'name', label: '按原文件名' },
  { key: 'path', label: '按来源路径' },
] as const

const DECISION_LABEL: Record<string, string> = {
  pass: '通过',
  fail: '不通过',
  uncertain: '待复核',
  error: '请求失败',
}

const RUN_LABEL: Record<string, string> = {
  queued: '排队中',
  running: '扫描中',
  succeeded: '已完成',
  interrupted: '已中断',
  failed: '失败',
}

function message(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return '请求失败，请稍后重试'
}

function readParams(): { folder: string; scheme: string } {
  const params = new URLSearchParams(window.location.search)
  return { folder: params.get('folder') ?? '', scheme: params.get('scheme') ?? '' }
}

function writeParams(folder: string, scheme: string) {
  const url = new URL(window.location.href)
  if (url.searchParams.get('view') !== 'material-filters') return
  if (folder) url.searchParams.set('folder', folder)
  else url.searchParams.delete('folder')
  if (scheme) url.searchParams.set('scheme', scheme)
  else url.searchParams.delete('scheme')
  window.history.replaceState({}, '', url)
}

function requestId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID()
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`
}

export default function MaterialFilterPage() {
  const [initial] = useState(readParams)
  const [folders, setFolders] = useState<MaterialFilterFolder[]>([])
  const [foldersLoading, setFoldersLoading] = useState(true)
  const [foldersError, setFoldersError] = useState<string | null>(null)
  const [activeFolderId, setActiveFolderId] = useState(initial.folder)
  const [schemes, setSchemes] = useState<MaterialFilterScheme[]>([])
  const [schemesLoading, setSchemesLoading] = useState(false)
  const [activeSchemeId, setActiveSchemeId] = useState(initial.scheme)
  const [detail, setDetail] = useState<MaterialFilterScheme | null>(null)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [run, setRun] = useState<MaterialFilterRun | null>(null)
  const [runBusy, setRunBusy] = useState(false)
  const [runError, setRunError] = useState<string | null>(null)
  // Live counters and the newest stored result make a long run visibly alive,
  // even before the run document's own counters are finalized.
  const [live, setLive] = useState<{
    counts: Record<string, number>
    total: number
    last: MaterialFilterEvaluation | null
    at: number
  } | null>(null)
  const [menuOpen, setMenuOpen] = useState(false)
  const [, setTick] = useState(0)
  const [results, setResults] = useState<MaterialFilterEvaluation[]>([])
  const [counts, setCounts] = useState<Record<string, number>>({})
  const [resultsTotal, setResultsTotal] = useState(0)
  // The API pages by skip/limit; the page widens the window instead of replacing
  // the list, so "load more" keeps the scroll position and the cards already read.
  const [visibleLimit, setVisibleLimit] = useState(RESULTS_PAGE_SIZE)
  const [resultsLoading, setResultsLoading] = useState(false)
  const [resultsError, setResultsError] = useState<string | null>(null)
  const [status, setStatus] = useState('all')
  const [sort, setSort] = useState<MaterialFilterSort>('judged')
  const [searchDraft, setSearchDraft] = useState('')
  const [search, setSearch] = useState('')
  const [busyEvaluation, setBusyEvaluation] = useState<string | null>(null)
  const [editing, setEditing] = useState<{ id: string; name: string; tags: string } | null>(null)

  const [addingFolder, setAddingFolder] = useState(false)
  const [folderInput, setFolderInput] = useState('')
  const [folderBusy, setFolderBusy] = useState(false)
  const [folderError, setFolderError] = useState<string | null>(null)
  const [renaming, setRenaming] = useState(false)
  const [renameValue, setRenameValue] = useState('')
  const [deleting, setDeleting] = useState<{ purgeLocal: boolean } | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const [defaults, setDefaults] = useState<MaterialFilterDefaults | null>(null)
  const [schemeForm, setSchemeForm] = useState<{
    name: string
    rules: MaterialFilterRule[]
    recursive: boolean
    outputRoot: string
    model: string
  } | null>(null)
  const [schemeBusy, setSchemeBusy] = useState(false)
  const [schemeError, setSchemeError] = useState<string | null>(null)

  const schemeListRequest = useRef(0)
  const detailRequest = useRef(0)
  const resultsRequest = useRef(0)
  const statusRef = useRef(status)
  const searchRef = useRef(search)
  const sortRef = useRef<MaterialFilterSort>('judged')
  const seenTotal = useRef(-1)
  const visibleLimitRef = useRef(RESULTS_PAGE_SIZE)

  const activeFolder = folders.find((item) => item.id === activeFolderId) ?? null
  const runActive = run?.status === 'queued' || run?.status === 'running'
  const scannedTotal = run?.counters.scanned ?? 0
  // During a run the live total is freshest; afterwards the stored evaluation
  // count keeps the finished progress bar honest (instead of resetting to zero).
  const judgedTotal = live?.total ?? counts.all ?? 0
  const progressPercent = scannedTotal > 0 ? Math.min(100, Math.round((judgedTotal / scannedTotal) * 100)) : 0
  const lastJudged = live?.last ? `${live.last.display_name || live.last.original_name}（${relativeLabel(live.last.updated_at)}）` : ''

  const loadFolders = useCallback(async () => {
    setFoldersLoading(true)
    try {
      const items = await listMaterialFilterFolders()
      setFolders(items)
      setFoldersError(null)
      setActiveFolderId((current) => (items.some((item) => item.id === current) ? current : (items[0]?.id ?? '')))
    } catch (error: unknown) {
      setFoldersError(message(error))
    } finally {
      setFoldersLoading(false)
    }
  }, [])

  const loadSchemes = useCallback(async (folderId: string) => {
    const ticket = schemeListRequest.current + 1
    schemeListRequest.current = ticket
    setSchemesLoading(true)
    try {
      const items = await listMaterialFilterSchemes(folderId)
      if (ticket !== schemeListRequest.current) return
      setSchemes(items)
      setActiveSchemeId((current) =>
        items.some((item) => item.id === current) ? current : (items[0]?.id ?? ''),
      )
    } catch (error: unknown) {
      if (ticket !== schemeListRequest.current) return
      setSchemes([])
      setDetailError(message(error))
    } finally {
      if (ticket === schemeListRequest.current) setSchemesLoading(false)
    }
  }, [])

  const loadResults = useCallback(
    async (
      schemeId: string,
      nextStatus: string,
      nextSearch: string,
      limit?: number,
      sortOrder?: MaterialFilterSort,
    ) => {
      const ticket = resultsRequest.current + 1
      resultsRequest.current = ticket
      // One request never exceeds the API window, so a larger scheme pages by filter.
      const take = Math.min(limit ?? visibleLimitRef.current, MAX_VISIBLE_RESULTS)
      setResultsLoading(true)
      try {
        const page = await listMaterialFilterResults(schemeId, {
          status: nextStatus,
          q: nextSearch,
          limit: take,
          sort: sortOrder ?? sortRef.current,
        })
        if (ticket !== resultsRequest.current) return
        setResults(page.items)
        setCounts(page.counts ?? {})
        setResultsTotal(page.total)
        visibleLimitRef.current = take
        setVisibleLimit(take)
        setResultsError(null)
      } catch (error: unknown) {
        if (ticket !== resultsRequest.current) return
        setResultsError(message(error))
      } finally {
        if (ticket === resultsRequest.current) setResultsLoading(false)
      }
    },
    [],
  )

  const loadDetail = useCallback(
    async (schemeId: string) => {
      const ticket = detailRequest.current + 1
      detailRequest.current = ticket
      try {
        const scheme = await getMaterialFilterScheme(schemeId)
        if (ticket !== detailRequest.current) return
        setDetail(scheme)
        setRun(scheme.last_run ?? null)
        setLive(null)
        seenTotal.current = -1
        setDetailError(null)
        await loadResults(schemeId, status, search)
      } catch (error: unknown) {
        if (ticket !== detailRequest.current) return
        setDetailError(message(error))
      }
    },
    [loadResults, search, status],
  )

  useEffect(() => {
    void loadFolders()
  }, [loadFolders])

  useEffect(() => {
    if (!activeFolderId) {
      setSchemes([])
      setDetail(null)
      setRun(null)
      setResults([])
      return
    }
    void loadSchemes(activeFolderId)
  }, [activeFolderId, loadSchemes])

  useEffect(() => {
    if (!activeSchemeId) {
      setDetail(null)
      setRun(null)
      setLive(null)
      setResults([])
      setCounts({})
      return
    }
    setStatus('all')
    setSearch('')
    setSearchDraft('')
    visibleLimitRef.current = RESULTS_PAGE_SIZE
    setVisibleLimit(RESULTS_PAGE_SIZE)
    void loadDetail(activeSchemeId)
    // The scheme id is the only trigger: filters are applied by their own effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSchemeId])

  useEffect(() => {
    writeParams(activeFolderId, activeSchemeId)
  }, [activeFolderId, activeSchemeId])

  useEffect(() => {
    if (!activeSchemeId) return
    void loadResults(activeSchemeId, status, search, visibleLimit, sort)
  }, [activeSchemeId, status, search, sort, visibleLimit, loadResults])

  useEffect(() => {
    statusRef.current = status
    searchRef.current = search
    sortRef.current = sort
  }, [status, search, sort])

  useEffect(() => {
    if (runActive) {
      document.title = `扫描中 ${judgedTotal}/${scannedTotal} · 素材筛选`
    } else {
      document.title = '素材筛选'
    }
    return () => {
      document.title = '素材筛选'
    }
  }, [runActive, judgedTotal, scannedTotal])

  // The overflow menu closes on any outside click or Escape.
  useEffect(() => {
    if (!menuOpen) return
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as HTMLElement | null
      if (target && target.closest('.mf-menu')) return
      setMenuOpen(false)
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMenuOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [menuOpen])

  // Poll the run and the stored results together: the processed count comes from
  // persisted evaluations, so progress stays real even when counters lag behind.
  useEffect(() => {
    if (!run || (run.status !== 'queued' && run.status !== 'running')) return
    const schemeId = run.scheme_id
    const runId = run.id
    let cancelled = false
    const poll = async () => {
      try {
        const [next, page] = await Promise.all([
          getMaterialFilterRun(runId),
          listMaterialFilterResults(schemeId, { status: 'all', limit: 1 }),
        ])
        if (cancelled || next.scheme_id !== schemeId) return
        setRun(next)
        setLive({
          counts: page.counts ?? {},
          total: page.total,
          last: page.items[0] ?? null,
          at: Date.now(),
        })
        if (page.total !== seenTotal.current) {
          seenTotal.current = page.total
          void loadResults(schemeId, statusRef.current, searchRef.current)
        }
        if (next.status !== 'queued' && next.status !== 'running') {
          void loadDetail(schemeId)
        }
      } catch (error: unknown) {
        if (!cancelled) setRunError(message(error))
      }
    }
    void poll()
    const timer = window.setInterval(() => void poll(), 2500)
    const ticker = window.setInterval(() => setTick((value) => value + 1), 1000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
      window.clearInterval(ticker)
    }
  }, [run, loadDetail, loadResults])

  const selectFolder = (folderId: string) => {
    if (folderId === activeFolderId) return
    setActiveFolderId(folderId)
    setActiveSchemeId('')
    setSchemes([])
    setDetail(null)
    setRun(null)
    setLive(null)
    setMenuOpen(false)
    seenTotal.current = -1
    setResults([])
    setCounts({})
    setRenaming(false)
  }

  const submitFolder = async () => {
    const value = folderInput.trim()
    if (!value) return
    setFolderBusy(true)
    setFolderError(null)
    try {
      const folder = await addMaterialFilterFolder(value)
      setFolders((current) =>
        current.some((item) => item.id === folder.id) ? current : [...current, folder],
      )
      setFolderInput('')
      setAddingFolder(false)
      selectFolder(folder.id)
      if (folder.created === false) {
        setFolderError(`${folder.display_name} 已在列表中，已切换到该文件夹。`)
      }
    } catch (error: unknown) {
      setFolderError(message(error))
    } finally {
      setFolderBusy(false)
    }
  }

  const submitRename = async () => {
    if (!activeFolder || !renameValue.trim()) return
    setFolderBusy(true)
    setFolderError(null)
    try {
      const updated = await renameMaterialFilterFolder(activeFolder.id, renameValue.trim())
      setFolders((current) => current.map((item) => (item.id === updated.id ? updated : item)))
      setRenaming(false)
    } catch (error: unknown) {
      setFolderError(message(error))
    } finally {
      setFolderBusy(false)
    }
  }

  const removeFolder = async () => {
    if (!activeFolder || !deleting) return
    setFolderBusy(true)
    setFolderError(null)
    try {
      const result = await deleteMaterialFilterFolder(activeFolder.id, deleting.purgeLocal)
      const remaining = folders.filter((item) => item.id !== activeFolder.id)
      setFolders(remaining)
      setDeleting(null)
      setRenaming(false)
      setSchemes([])
      setDetail(null)
      setRun(null)
      setLive(null)
      setResults([])
      setCounts({})
      setActiveSchemeId('')
      setActiveFolderId(remaining[0]?.id ?? '')
      setNotice(
        deleting.purgeLocal
          ? `已删除「${activeFolder.display_name}」：${result.schemes} 个方案、${result.evaluations} 条结果，本机副本已清理。`
          : `已删除「${activeFolder.display_name}」：${result.schemes} 个方案、${result.evaluations} 条结果；本机副本保留。SMB 原图未改动。`,
      )
    } catch (error: unknown) {
      setFolderError(message(error))
    } finally {
      setFolderBusy(false)
    }
  }

  const exportPicked = async () => {
    if (!detail) return
    setSchemeBusy(true)
    setSchemeError(null)
    setNotice(null)
    try {
      const result = await exportPickedMaterialFilter(detail.id)
      setNotice(`已导出 ${result.count} 张到 ${result.directory}（含 manifest.csv）`)
    } catch (error: unknown) {
      setSchemeError(message(error))
    } finally {
      setSchemeBusy(false)
      setMenuOpen(false)
    }
  }

  const openSchemeForm = async () => {
    setSchemeError(null)
    setSchemeBusy(false)
    try {
      const value = defaults ?? (await getMaterialFilterDefaults())
      setDefaults(value)
      setSchemeForm({
        name: '',
        rules: value.rules.map((rule) => ({ ...rule })),
        recursive: true,
        outputRoot: value.output_root,
        model: value.model,
      })
    } catch (error: unknown) {
      setSchemeError(message(error))
    }
  }

  const submitScheme = async () => {
    if (!schemeForm || !activeFolderId) return
    const name = schemeForm.name.trim()
    if (!name) {
      setSchemeError('请填写方案名称')
      return
    }
    setSchemeBusy(true)
    setSchemeError(null)
    try {
      const scheme = await createMaterialFilterScheme(activeFolderId, {
        name,
        rules: schemeForm.rules.map((rule, index) => ({
          id: rule.id || `rule_${index + 1}`,
          text: rule.text.trim(),
          interpretation: rule.interpretation.trim(),
        })),
        recursive: schemeForm.recursive,
        output_root: schemeForm.outputRoot.trim(),
        model: schemeForm.model.trim(),
      })
      setSchemeForm(null)
      setSchemes((current) => [...current, scheme])
      setActiveSchemeId(scheme.id)
    } catch (error: unknown) {
      setSchemeError(message(error))
    } finally {
      setSchemeBusy(false)
    }
  }

  const cloneScheme = async () => {
    if (!detail) return
    setSchemeBusy(true)
    setSchemeError(null)
    try {
      const cloned = await cloneMaterialFilterScheme(detail.id, { name: `${detail.name} 副本` })
      setSchemes((current) => [...current, cloned])
      setActiveSchemeId(cloned.id)
    } catch (error: unknown) {
      setSchemeError(message(error))
    } finally {
      setSchemeBusy(false)
    }
  }

  const startRun = async (limit?: number) => {
    if (!detail || runActive) return
    setRunBusy(true)
    setRunError(null)
    try {
      const next = await startMaterialFilterRun(detail.id, requestId(), limit)
      setRun(next)
    } catch (error: unknown) {
      setRunError(message(error))
    } finally {
      setRunBusy(false)
    }
  }

  const resumeRun = async () => {
    if (!run) return
    setRunBusy(true)
    setRunError(null)
    try {
      setRun(await resumeMaterialFilterRun(run.id))
    } catch (error: unknown) {
      setRunError(message(error))
    } finally {
      setRunBusy(false)
    }
  }

  const retryFailed = async () => {
    if (!run) return
    setRunBusy(true)
    setRunError(null)
    try {
      setRun(await retryMaterialFilterFailed(run.id))
    } catch (error: unknown) {
      setRunError(message(error))
    } finally {
      setRunBusy(false)
    }
  }

  const decide = async (evaluation: MaterialFilterEvaluation, decision: 'pass' | 'fail') => {
    setBusyEvaluation(evaluation.id)
    setResultsError(null)
    try {
      const updated = await reviewMaterialFilterEvaluation(evaluation.id, {
        decision,
        source_sha256: evaluation.source_sha256,
        request_id: requestId(),
      })
      setResults((current) => current.map((item) => (item.id === updated.id ? updated : item)))
      if (activeSchemeId) void loadResults(activeSchemeId, status, search)
      if (updated.copy_status === 'error' && updated.copy_error) {
        setResultsError(`已记录人工结论，但复制失败：${updated.copy_error}`)
      }
    } catch (error: unknown) {
      setResultsError(message(error))
    } finally {
      setBusyEvaluation(null)
    }
  }

  const togglePick = async (evaluation: MaterialFilterEvaluation) => {
    setBusyEvaluation(evaluation.id)
    setResultsError(null)
    try {
      const updated = await setMaterialFilterPick(evaluation.id, !evaluation.manual_pick)
      if (status === 'picked' && !updated.manual_pick) {
        // Removing from the shortlist while this tab is open re-reads the page.
        if (activeSchemeId) void loadResults(activeSchemeId, status, search)
      } else {
        setResults((current) => current.map((item) => (item.id === updated.id ? updated : item)))
      }
      setCounts((current) => ({
        ...current,
        picked: Math.max(0, (current.picked ?? 0) + (updated.manual_pick ? 1 : -1)),
      }))
    } catch (error: unknown) {
      setResultsError(message(error))
    } finally {
      setBusyEvaluation(null)
    }
  }

  const recheck = async (evaluation: MaterialFilterEvaluation) => {
    setBusyEvaluation(evaluation.id)
    setResultsError(null)
    try {
      const updated = await recheckMaterialFilterEvaluation(evaluation.id)
      setResults((current) => current.map((item) => (item.id === updated.id ? updated : item)))
      if (activeSchemeId) void loadResults(activeSchemeId, status, search)
    } catch (error: unknown) {
      setResultsError(message(error))
    } finally {
      setBusyEvaluation(null)
    }
  }

  const saveMetadata = async () => {
    if (!editing) return
    const tags = editing.tags
      .split(/[,，、\n]/)
      .map((tag) => tag.trim())
      .filter(Boolean)
    setBusyEvaluation(editing.id)
    setResultsError(null)
    try {
      const updated = await updateMaterialFilterMetadata(editing.id, {
        display_name: editing.name.trim(),
        tags,
      })
      setResults((current) => current.map((item) => (item.id === updated.id ? updated : item)))
      setEditing(null)
    } catch (error: unknown) {
      setResultsError(message(error))
    } finally {
      setBusyEvaluation(null)
    }
  }

  const statusCounts = useMemo(
    () => STATUS_TABS.map((tab) => ({ ...tab, count: counts[tab.key] ?? 0 })),
    [counts],
  )

  return (
    <section className="mf-page">
      <div className="mf-tabs" role="tablist" aria-label="SMB 文件夹">
        {folders.map((folder) => (
          <button
            key={folder.id}
            type="button"
            role="tab"
            aria-selected={folder.id === activeFolderId}
            className={`mf-tab${folder.id === activeFolderId ? ' is-active' : ''}`}
            onClick={() => selectFolder(folder.id)}
            title={folder.source_root}
          >
            {folder.display_name}
          </button>
        ))}
        <button
          type="button"
          className="mf-tab mf-tab-add"
          onClick={() => setAddingFolder((value) => !value)}
        >
          ＋添加文件夹
        </button>
      </div>

      {runActive ? (
        <div className="mf-run-strip" role="status">
          <span className="mf-spinner" aria-hidden="true" />
          <strong>扫描中</strong>
          <div className="mf-run-strip-track">
            <span style={{ width: `${progressPercent}%` }} />
          </div>
          <span className="mf-run-strip-count">{judgedTotal} / {scannedTotal} · {progressPercent}%</span>
          <span className="mf-run-strip-hint">
            {lastJudged ? `最近判断：${lastJudged}` : '正在读取图片…'}
          </span>
        </div>
      ) : null}

      {addingFolder ? (
        <div className="mf-inline-form">
          <label>
            输入 SMB 文件夹地址
            <input
              value={folderInput}
              onChange={(event) => setFolderInput(event.target.value)}
              placeholder="\\192.168.11.5\movies\See Joy素材\照片"
              spellCheck={false}
              onKeyDown={(event) => {
                if (event.key === 'Enter') void submitFolder()
              }}
            />
          </label>
          <div className="mf-inline-actions">
            <button type="button" className="mp-btn-primary" onClick={() => void submitFolder()} disabled={folderBusy}>
              {folderBusy ? '正在检查…' : '添加并检查'}
            </button>
            <button type="button" className="mp-btn-secondary" onClick={() => setAddingFolder(false)}>
              取消
            </button>
          </div>
          <p className="mf-hint">只读访问当前已连接的共享；添加时会枚举一次目录，不会在 SMB 上创建任何文件。</p>
        </div>
      ) : null}

      {foldersError ? <p className="mf-error">{foldersError}</p> : null}
      {folderError ? <p className="mf-error">{folderError}</p> : null}

      {foldersLoading ? <p className="mf-muted">正在读取本机文件夹…</p> : null}

      {!foldersLoading && folders.length === 0 ? (
        <div className="mf-empty">
          <strong>还没有添加 SMB 文件夹</strong>
          <p>点击「＋添加文件夹」，输入共享目录地址。每个目录会成为一个 TAB，所有结果只保存在本机。</p>
        </div>
      ) : null}

      {activeFolder ? (
        <>
          <div className="mf-card mf-controls">
            <div className="mf-folder-head">
              <div className="mf-folder-id">
                <span className="mf-kicker">当前来源</span>
                <strong>{activeFolder.display_name}</strong>
                <code>{activeFolder.source_root}</code>
              </div>
              {renaming ? (
                <div className="mf-rename">
                  <input
                    value={renameValue}
                    onChange={(event) => setRenameValue(event.target.value)}
                    aria-label="文件夹显示名称"
                  />
                  <button type="button" className="mp-btn-primary" onClick={() => void submitRename()} disabled={folderBusy}>
                    保存
                  </button>
                  <button type="button" className="mp-btn-secondary" onClick={() => setRenaming(false)}>
                    取消
                  </button>
                </div>
              ) : (
                <div className="mf-folder-tools">
                  <button
                    type="button"
                    className="mf-icon-button"
                    title="修改文件夹显示名"
                    aria-label="修改文件夹显示名"
                    onClick={() => {
                      setRenameValue(activeFolder.display_name)
                      setRenaming(true)
                    }}
                  >
                    ✎
                  </button>
                  <button
                    type="button"
                    className="mf-icon-button mf-icon-danger"
                    title="删除这个文件夹 TAB"
                    aria-label="删除这个文件夹 TAB"
                    onClick={() => setDeleting({ purgeLocal: false })}
                  >
                    🗑
                  </button>
                </div>
              )}
            </div>

            {deleting ? (
              <div className="mf-delete-confirm">
                <p>
                  删除「{activeFolder.display_name}」这个文件夹 TAB？该文件夹下的方案、运行记录与筛选结果都会从数据库移除，
                  <strong>SMB 原图不会被改动</strong>。
                </p>
                <label className="mf-checkbox">
                  <input
                    type="checkbox"
                    checked={deleting.purgeLocal}
                    onChange={(event) => setDeleting({ purgeLocal: event.target.checked })}
                  />
                  同时删除本机的通过副本和缩略图
                </label>
                <div className="mf-inline-actions">
                  <button
                    type="button"
                    className="mf-danger-button"
                    onClick={() => void removeFolder()}
                    disabled={folderBusy}
                  >
                    {folderBusy ? '正在删除…' : '确认删除'}
                  </button>
                  <button type="button" className="mp-btn-secondary" onClick={() => setDeleting(null)}>
                    取消
                  </button>
                </div>
              </div>
            ) : null}

            <div className="mf-scheme-row">
              <label className="mf-scheme-picker" htmlFor="mf-scheme-select">
                <span>筛选方案</span>
                <select
                  id="mf-scheme-select"
                  value={activeSchemeId}
                  onChange={(event) => setActiveSchemeId(event.target.value)}
                >
                  {schemes.length === 0 ? <option value="">暂无方案</option> : null}
                  {schemes.map((scheme) => (
                    <option key={scheme.id} value={scheme.id}>
                      {scheme.name}
                    </option>
                  ))}
                </select>
              </label>
              <button type="button" className="mf-icon-button" title="新建方案" aria-label="新建方案" onClick={() => void openSchemeForm()}>
                ＋
              </button>
              {schemesLoading ? <span className="mf-muted">读取中…</span> : null}

              <div className="mf-scheme-actions">
                <button
                  type="button"
                  className="mp-btn-primary"
                  onClick={() => void startRun()}
                  disabled={!detail || runActive || runBusy}
                  aria-busy={runActive}
                >
                  {runActive ? '扫描中…' : '扫描并筛选'}
                </button>
                <button
                  type="button"
                  className="mp-btn-secondary"
                  onClick={() => void startRun(5)}
                  disabled={!detail || runActive || runBusy}
                >
                  试跑 5 张
                </button>
                <div className="mf-menu">
                  <button
                    type="button"
                    className="mf-icon-button"
                    aria-haspopup="menu"
                    aria-expanded={menuOpen}
                    title="更多操作"
                    aria-label="更多操作"
                    onClick={() => setMenuOpen((value) => !value)}
                  >
                    ⋯
                  </button>
                  {menuOpen ? (
                    <div className="mf-menu-panel" role="menu">
                      <button
                        type="button"
                        role="menuitem"
                        disabled={!detail || schemeBusy}
                        onClick={() => {
                          setMenuOpen(false)
                          void cloneScheme()
                        }}
                      >
                        复制为新方案
                      </button>
                      <button
                        type="button"
                        role="menuitem"
                        disabled={!detail || schemeBusy}
                        onClick={() => void exportPicked()}
                      >
                        导出已入选到本机
                      </button>
                      {detail ? (
                        <a role="menuitem" href={materialFilterExportPath(detail.id)} onClick={() => setMenuOpen(false)}>
                          导出结果清单
                        </a>
                      ) : null}
                      {run?.status === 'interrupted' ? (
                        <button
                          type="button"
                          role="menuitem"
                          onClick={() => {
                            setMenuOpen(false)
                            void resumeRun()
                          }}
                          disabled={runBusy}
                        >
                          继续中断任务
                        </button>
                      ) : null}
                      {run && (run.status === 'failed' || (run.counters.errors ?? 0) > 0) ? (
                        <button
                          type="button"
                          role="menuitem"
                          onClick={() => {
                            setMenuOpen(false)
                            void retryFailed()
                          }}
                          disabled={runActive || runBusy}
                        >
                          重试失败项
                        </button>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              </div>
            </div>
          </div>

          {schemeError ? <p className="mf-error">{schemeError}</p> : null}
          {notice ? <p className="mf-notice">{notice}</p> : null}

          {schemeForm ? (
            <div className="mf-card mf-scheme-form">
              <h3>新建筛选方案</h3>
              <label>
                方案名称
                <input
                  value={schemeForm.name}
                  onChange={(event) => setSchemeForm({ ...schemeForm, name: event.target.value })}
                  placeholder="例如：排除空座位和蟹腿"
                />
              </label>
              <div className="mf-rules-editor">
                <span className="mf-kicker">筛选条件（自然语言，逐条判断）</span>
                {schemeForm.rules.map((rule, index) => (
                  <div className="mf-rule-row" key={rule.id || index}>
                    <input
                      value={rule.text}
                      aria-label={`规则 ${index + 1}`}
                      onChange={(event) => {
                        const rules = schemeForm.rules.map((item, position) =>
                          position === index ? { ...item, text: event.target.value } : item,
                        )
                        setSchemeForm({ ...schemeForm, rules })
                      }}
                      placeholder="不能出现空座位场景"
                    />
                    <textarea
                      value={rule.interpretation}
                      aria-label={`规则 ${index + 1} 解释`}
                      rows={2}
                      onChange={(event) => {
                        const rules = schemeForm.rules.map((item, position) =>
                          position === index ? { ...item, interpretation: event.target.value } : item,
                        )
                        setSchemeForm({ ...schemeForm, rules })
                      }}
                      placeholder="可选：说明边界，避免模型扩大解释"
                    />
                    <button
                      type="button"
                      className="mf-text-button"
                      onClick={() =>
                        setSchemeForm({
                          ...schemeForm,
                          rules: schemeForm.rules.filter((_, position) => position !== index),
                        })
                      }
                    >
                      删除
                    </button>
                  </div>
                ))}
                <button
                  type="button"
                  className="mf-text-button"
                  onClick={() =>
                    setSchemeForm({
                      ...schemeForm,
                      rules: [
                        ...schemeForm.rules,
                        { id: `rule_${schemeForm.rules.length + 1}`, text: '', interpretation: '' },
                      ],
                    })
                  }
                >
                  ＋新增规则
                </button>
              </div>
              <div className="mf-form-row">
                <label className="mf-checkbox">
                  <input
                    type="checkbox"
                    checked={schemeForm.recursive}
                    onChange={(event) => setSchemeForm({ ...schemeForm, recursive: event.target.checked })}
                  />
                  包含子目录
                </label>
                <label>
                  本机输出目录
                  <input
                    value={schemeForm.outputRoot}
                    onChange={(event) => setSchemeForm({ ...schemeForm, outputRoot: event.target.value })}
                  />
                </label>
                <label>
                  模型
                  <input
                    value={schemeForm.model}
                    onChange={(event) => setSchemeForm({ ...schemeForm, model: event.target.value })}
                  />
                </label>
              </div>
              <div className="mf-inline-actions">
                <button type="button" className="mp-btn-primary" onClick={() => void submitScheme()} disabled={schemeBusy}>
                  {schemeBusy ? '创建中…' : '创建方案'}
                </button>
                <button type="button" className="mp-btn-secondary" onClick={() => setSchemeForm(null)}>
                  取消
                </button>
              </div>
              <p className="mf-hint">输出目录必须在本地固定磁盘；SMB、映射网络盘和目录链接会被拒绝。</p>
            </div>
          ) : null}

          {detailError ? <p className="mf-error">{detailError}</p> : null}

          {detail ? (
            <div className="mf-card mf-scheme-summary">
              <div className="mf-summary-head">
                <div>
                  <span className="mf-kicker">当前方案</span>
                  <h3>{detail.name}</h3>
                </div>
                <span className="mf-summary-status">{runStatusLabel(run)}</span>
              </div>
              <ul className="mf-rule-list">
                {detail.rules.map((rule, index) => (
                  <li key={rule.id}>
                    <strong>{index + 1}. {rule.text}</strong>
                    {rule.interpretation ? <span>{rule.interpretation}</span> : null}
                  </li>
                ))}
              </ul>
              <p className="mf-hint">
                {detail.recursive ? '包含子目录' : '仅当前目录'} · 模型 {detail.model} · 提示词 {detail.prompt_version} · 输出 {detail.output_root}
              </p>
              {runError ? <p className="mf-error">{runError}</p> : null}
              {run ? (
                <RunProgress run={run} live={live} judged={judgedTotal} totals={live?.counts ?? counts} />
              ) : null}
            </div>
          ) : null}

          {detail ? (
            <div className="mf-results">
              <div className="mf-results-head">
                <div className="mf-status-tabs">
                  {statusCounts.map((tab) => (
                    <button
                      key={tab.key}
                      type="button"
                      className={`mf-status-tab${status === tab.key ? ' is-active' : ''}`}
                      onClick={() => {
                        setStatus(tab.key)
                        visibleLimitRef.current = RESULTS_PAGE_SIZE
                        setVisibleLimit(RESULTS_PAGE_SIZE)
                      }}
                    >
                      {tab.label}
                      <span>{tab.count}</span>
                    </button>
                  ))}
                </div>
                <label className="mf-sort">
                  排序
                  <select
                    value={sort}
                    onChange={(event) => {
                      setSort(event.target.value as MaterialFilterSort)
                      visibleLimitRef.current = RESULTS_PAGE_SIZE
                      setVisibleLimit(RESULTS_PAGE_SIZE)
                    }}
                  >
                    {SORT_OPTIONS.map((option) => (
                      <option key={option.key} value={option.key}>{option.label}</option>
                    ))}
                  </select>
                </label>
                <form
                  className="mf-search"
                  onSubmit={(event) => {
                    event.preventDefault()
                    setSearch(searchDraft.trim())
                    visibleLimitRef.current = RESULTS_PAGE_SIZE
                    setVisibleLimit(RESULTS_PAGE_SIZE)
                  }}
                >
                  <input
                    value={searchDraft}
                    onChange={(event) => setSearchDraft(event.target.value)}
                    placeholder="搜索名称、原文件名、分类、标签"
                  />
                  <button type="submit" className="mp-btn-secondary">搜索</button>
                </form>
              </div>
              {resultsError ? <p className="mf-error">{resultsError}</p> : null}
              {resultsLoading ? <p className="mf-muted">正在读取结果…</p> : null}
              {!resultsLoading && resultsTotal === 0 ? (
                <div className="mf-empty">
                  {status === 'picked' ? (
                    <>
                      <strong>还没有入选的图片</strong>
                      <p>在「通过」里浏览，点卡片上的「☆ 入选」把最终要用的照片挑出来；模型只负责硬性条件。</p>
                    </>
                  ) : (
                    <>
                      <strong>还没有结果</strong>
                      <p>点击「扫描并筛选」按当前方案判断图片；通过的原图会复制到本机输出目录。</p>
                    </>
                  )}
                </div>
              ) : null}
              <div className="mf-grid">
                {results.map((evaluation) => (
                  <article className="mf-card mf-result" key={evaluation.id}>
                    <div className="mf-result-media">
                      {evaluation.preview_url ? (
                        <img src={evaluation.preview_url} alt={evaluation.display_name || evaluation.original_name} loading="lazy" />
                      ) : (
                        <span className="mf-no-preview">无缩略图</span>
                      )}
                    </div>
                    <div className="mf-result-body">
                      <div className="mf-result-head">
                        <strong>{evaluation.display_name || evaluation.original_name}</strong>
                        <span className={`mf-badge is-${evaluation.effective_decision}`}>
                          {DECISION_LABEL[evaluation.effective_decision] ?? evaluation.effective_decision}
                        </span>
                        {evaluation.manual_pick ? <span className="mf-badge is-picked">已入选</span> : null}
                        {evaluation.uat_asset_id ? (
                          <a
                            className="mf-badge is-synced"
                            href={evaluation.uat_asset_url || undefined}
                            target="_blank"
                            rel="noreferrer"
                            title={evaluation.uat_synced_at ? `已同步 ${evaluation.uat_synced_at}` : '已同步到 UAT'}
                          >
                            已同步
                          </a>
                        ) : null}
                      </div>
                      <p className="mf-result-meta">
                        {evaluation.category || '未分类'}
                        {evaluation.human_decision ? ' · 人工已复核' : ''}
                      </p>
                      <div className="mf-tags">
                        {evaluation.tags.map((tag) => (
                          <span key={tag}>{tag}</span>
                        ))}
                      </div>
                      {evaluation.rules.length ? (
                        <ul className="mf-verdicts">
                          {evaluation.rules.map((verdict) => (
                            <li key={verdict.id} className={`is-${verdict.verdict}`}>
                              <strong>{verdict.id}</strong>
                              <span>{verdict.reason}</span>
                            </li>
                          ))}
                        </ul>
                      ) : null}
                      {evaluation.error ? <p className="mf-error">技术失败：{evaluation.error}</p> : null}
                      {evaluation.copy_status === 'error' ? (
                        <p className="mf-error">复制失败：{evaluation.copy_error}</p>
                      ) : null}
                      <p className="mf-path" title={evaluation.relative_path}>
                        {evaluation.relative_path}
                      </p>
                      {editing?.id === evaluation.id ? (
                        <div className="mf-metadata-editor">
                          <label>
                            名称
                            <input
                              value={editing.name}
                              onChange={(event) => setEditing({ ...editing, name: event.target.value })}
                            />
                          </label>
                          <label>
                            标签（逗号分隔）
                            <input
                              value={editing.tags}
                              onChange={(event) => setEditing({ ...editing, tags: event.target.value })}
                            />
                          </label>
                          <div className="mf-inline-actions">
                            <button type="button" className="mp-btn-primary" onClick={() => void saveMetadata()} disabled={busyEvaluation === evaluation.id}>
                              保存
                            </button>
                            <button type="button" className="mp-btn-secondary" onClick={() => setEditing(null)}>
                              取消
                            </button>
                          </div>
                        </div>
                      ) : (
                        <div className="mf-result-actions">
                          <button
                            type="button"
                            className={`mf-pick${evaluation.manual_pick ? ' is-picked' : ''}`}
                            onClick={() => void togglePick(evaluation)}
                            disabled={busyEvaluation === evaluation.id}
                            aria-pressed={evaluation.manual_pick}
                          >
                            {evaluation.manual_pick ? '★ 已入选' : '☆ 入选'}
                          </button>
                          {evaluation.effective_decision === 'uncertain' ? (
                            <>
                              <button
                                type="button"
                                className="mp-btn-primary"
                                onClick={() => void decide(evaluation, 'pass')}
                                disabled={busyEvaluation === evaluation.id}
                              >
                                确认通过
                              </button>
                              <button
                                type="button"
                                className="mp-btn-secondary"
                                onClick={() => void decide(evaluation, 'fail')}
                                disabled={busyEvaluation === evaluation.id}
                              >
                                确认不通过
                              </button>
                            </>
                          ) : null}
                          {evaluation.download_url ? (
                            <a className="mp-btn-secondary" href={evaluation.download_url}>
                              下载原图
                            </a>
                          ) : null}
                          <button
                            type="button"
                            className="mf-text-button"
                            onClick={() => void recheck(evaluation)}
                            disabled={busyEvaluation === evaluation.id}
                          >
                            重新检查
                          </button>
                          <button
                            type="button"
                            className="mf-text-button"
                            onClick={() =>
                              setEditing({
                                id: evaluation.id,
                                name: evaluation.display_name,
                                tags: evaluation.tags.join('，'),
                              })
                            }
                          >
                            改名称/标签
                          </button>
                        </div>
                      )}
                    </div>
                  </article>
                ))}
              </div>
              {resultsTotal > 0 ? (
                <div className="mf-results-foot">
                  <span>
                    已显示 <strong>{results.length}</strong> / {resultsTotal} 张
                  </span>
                  {results.length < resultsTotal && results.length < MAX_VISIBLE_RESULTS ? (
                    <>
                      <button
                        type="button"
                        className="mp-btn-secondary"
                        onClick={() => setVisibleLimit((value) => value + RESULTS_PAGE_SIZE)}
                        disabled={resultsLoading}
                      >
                        {resultsLoading
                          ? '加载中…'
                          : `再显示 ${Math.min(RESULTS_PAGE_SIZE, resultsTotal - results.length)} 张`}
                      </button>
                      <button
                        type="button"
                        className="mf-text-button"
                        onClick={() => setVisibleLimit(Math.min(resultsTotal, MAX_VISIBLE_RESULTS))}
                        disabled={resultsLoading}
                      >
                        全部显示（{resultsTotal} 张）
                      </button>
                    </>
                  ) : results.length < resultsTotal ? (
                    <span className="mf-muted">
                      单次最多显示 {MAX_VISIBLE_RESULTS} 张，请用状态筛选或搜索缩小范围
                    </span>
                  ) : (
                    <span className="mf-muted">已全部显示</span>
                  )}
                </div>
              ) : null}
            </div>
          ) : null}
        </>
      ) : null}
    </section>
  )
}

type LiveState = {
  counts: Record<string, number>
  total: number
  last: MaterialFilterEvaluation | null
  at: number
} | null

function runStatusLabel(run: MaterialFilterRun | null): string {
  if (!run) return '尚未运行'
  if (run.status === 'running') return '扫描中'
  if (run.status === 'queued') return '等待开始'
  return `上次运行：${RUN_LABEL[run.status] ?? run.status}`
}

function elapsedLabel(startedAt: string | null): string {
  if (!startedAt) return ''
  const start = new Date(startedAt).getTime()
  if (Number.isNaN(start)) return ''
  const seconds = Math.max(0, Math.floor((Date.now() - start) / 1000))
  const minutes = Math.floor(seconds / 60)
  return minutes > 0 ? `${minutes} 分 ${String(seconds % 60).padStart(2, '0')} 秒` : `${seconds} 秒`
}

function relativeLabel(value: string | undefined): string {
  if (!value) return ''
  const at = new Date(value).getTime()
  if (Number.isNaN(at)) return ''
  const seconds = Math.max(0, Math.floor((Date.now() - at) / 1000))
  if (seconds < 5) return '刚刚'
  if (seconds < 60) return `${seconds} 秒前`
  const minutes = Math.floor(seconds / 60)
  return `${minutes} 分钟前`
}

function RunProgress({
  run,
  live,
  judged,
  totals,
}: {
  run: MaterialFilterRun
  live: LiveState
  judged: number
  totals: Record<string, number>
}) {
  const active = run.status === 'queued' || run.status === 'running'
  const scanned = run.counters.scanned ?? 0
  const percent = scanned > 0 ? Math.min(100, Math.round((judged / scanned) * 100)) : 0
  const finished = !active
  return (
    <div className={`mf-run mf-run-${run.status}`}>
      <div className="mf-run-head">
        <strong>
          {RUN_LABEL[run.status] ?? run.status}
          {active ? <span className="mf-spinner" aria-hidden="true" /> : null}
        </strong>
        {active ? <span>已运行 {elapsedLabel(run.started_at)}</span> : null}
        {run.limit ? <span>本次最多判断 {run.limit} 张</span> : null}
        {run.error ? <span className="mf-error-text">{run.error}</span> : null}
      </div>

      {scanned > 0 ? (
        <div className="mf-progress">
          <div className="mf-progress-track" role="progressbar" aria-valuemin={0} aria-valuemax={scanned} aria-valuenow={judged}>
            <span style={{ width: `${percent}%` }} />
          </div>
          <div className="mf-progress-meta">
            <strong>{judged} / {scanned}</strong>
            <span>{percent}%</span>
            {active ? (
              <span className="mf-progress-live">
                {live?.last
                  ? `最近判断：${live.last.display_name || live.last.original_name}（${relativeLabel(live.last.updated_at)}）`
                  : '正在读取图片…'}
              </span>
            ) : null}
          </div>
        </div>
      ) : null}

      <div className="mf-counters">
        <div>
          <span>通过</span>
          <strong>{totals.pass ?? 0}</strong>
        </div>
        <div>
          <span>不通过</span>
          <strong>{totals.fail ?? 0}</strong>
        </div>
        <div>
          <span>待复核</span>
          <strong>{totals.uncertain ?? 0}</strong>
        </div>
        <div>
          <span>失败</span>
          <strong>{totals.error ?? 0}</strong>
        </div>
        {finished ? (
          <>
            <div>
              <span>本次新增</span>
              <strong>{run.counters.added}</strong>
            </div>
            <div>
              <span>本次跳过</span>
              <strong>{run.counters.skipped}</strong>
            </div>
            <div>
              <span>本次已复制</span>
              <strong>{run.counters.copied}</strong>
            </div>
          </>
        ) : null}
      </div>

      {active ? (
        <p className="mf-hint">单张约 7–30 秒；关闭页面不会中断任务，重新打开可继续查看进度。</p>
      ) : null}

      {run.item_errors.length ? (
        <details className="mf-run-errors">
          <summary>最近 {run.item_errors.length} 条读取失败</summary>
          <ul>
            {run.item_errors.map((item, index) => (
              <li key={`${item.path}-${index}`}>
                {item.path}：{item.message}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  )
}
