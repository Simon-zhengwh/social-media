import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import {
  ApiError,
  cancelMediaMigrationJob,
  getMediaMigrationDefaults,
  getMediaMigrationJob,
  listMediaMigrationJobs,
  previewMediaMigration,
  startMediaMigrationJob,
  type MediaMigrationItem,
  type MediaMigrationJob,
  type MediaMigrationJobSummary,
  type MediaMigrationPayload,
  type MediaMigrationPreview,
} from '../api'
import './MediaMigrationPage.css'

/** Non-sensitive form memory only: the sessions are never written to any browser storage. */
const FORM_STORAGE_KEY = 'media-migration-form'
const JOB_POLL_MS = 1500

type MigrationScope = 'custom' | 'all'

type StoredForm = {
  uat_base_url: string
  uat_merchant_id: string
  prod_base_url: string
  prod_merchant_id: string
  scope: MigrationScope
  custom_limit: string
}

type FilterKey = 'all' | 'planned' | 'uploaded' | 'skipped' | 'failed'

// Stable empty list: a fresh array per render would defeat the item filtering memo.
const NO_ITEMS: MediaMigrationItem[] = []

const FILTERS: Array<{ key: FilterKey; label: string }> = [
  { key: 'all', label: '全部' },
  { key: 'planned', label: '待上传' },
  { key: 'uploaded', label: '已上传' },
  { key: 'skipped', label: '已跳过' },
  { key: 'failed', label: '失败' },
]

const STATUS_LABEL: Record<string, string> = {
  planned: '待上传',
  skipped_ledger: '已同步',
  skipped_same_name: '同名跳过',
  uploaded: '已上传',
  failed: '失败',
}

const JOB_LABEL: Record<string, string> = {
  queued: '排队中',
  running: '迁移中',
  succeeded: '已完成',
  partial: '部分完成',
  failed: '失败',
  cancelled: '已停止',
}

function message(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return '请求失败，请稍后重试'
}

function readStoredForm(): Partial<StoredForm> {
  try {
    const raw = window.sessionStorage.getItem(FORM_STORAGE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as Partial<StoredForm>
    const pick = (value: unknown) => (typeof value === 'string' ? value : '')
    return {
      uat_base_url: pick(parsed.uat_base_url),
      uat_merchant_id: pick(parsed.uat_merchant_id),
      prod_base_url: pick(parsed.prod_base_url),
      prod_merchant_id: pick(parsed.prod_merchant_id),
      scope: parsed.scope === 'all' || parsed.scope === 'custom' ? parsed.scope : undefined,
      custom_limit: pick(parsed.custom_limit),
    }
  } catch {
    return {}
  }
}

function storeForm(form: StoredForm) {
  try {
    window.sessionStorage.setItem(FORM_STORAGE_KEY, JSON.stringify(form))
  } catch {
    // A private window without storage only loses the convenience prefill.
  }
}

function matches(item: MediaMigrationItem, filter: FilterKey): boolean {
  if (filter === 'all') return true
  if (filter === 'skipped') {
    return item.status === 'skipped_ledger' || item.status === 'skipped_same_name'
  }
  return item.status === filter
}

function itemDetail(item: MediaMigrationItem): string {
  if (item.status === 'uploaded') return item.prod_media_id || '已上传'
  if (item.status === 'failed') return item.error || '失败'
  if (item.status === 'skipped_ledger') return item.prod_media_id ? `Prod ${item.prod_media_id}` : '已同步'
  return item.reason || ''
}

function formatTime(value: string | null): string {
  if (!value) return ''
  return value.replace('T', ' ').replace('Z', '').slice(0, 19)
}

export default function MediaMigrationPage() {
  // Read the remembered non-sensitive fields once; the sessions always start blank.
  const [stored] = useState(readStoredForm)
  const [uatBaseUrl, setUatBaseUrl] = useState(stored.uat_base_url ?? '')
  const [uatMerchantId, setUatMerchantId] = useState(stored.uat_merchant_id ?? '')
  const [uatCookie, setUatCookie] = useState('')
  const [prodBaseUrl, setProdBaseUrl] = useState(stored.prod_base_url ?? '')
  const [prodMerchantId, setProdMerchantId] = useState(stored.prod_merchant_id ?? '')
  const [prodCookie, setProdCookie] = useState('')
  const [showCookies, setShowCookies] = useState(false)
  const [skipSameName, setSkipSameName] = useState(true)
  const [force, setForce] = useState(false)
  const [scope, setScope] = useState<MigrationScope>(stored.scope ?? 'custom')
  const [customLimit, setCustomLimit] = useState(stored.custom_limit || '5')
  const [preview, setPreview] = useState<MediaMigrationPreview | null>(null)
  const [job, setJob] = useState<MediaMigrationJob | null>(null)
  const [history, setHistory] = useState<MediaMigrationJobSummary[]>([])
  const [busy, setBusy] = useState<'preview' | 'start' | 'cancel' | 'history' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState<FilterKey>('all')
  const pollTimer = useRef<number | null>(null)

  useEffect(() => {
    storeForm({
      uat_base_url: uatBaseUrl,
      uat_merchant_id: uatMerchantId,
      prod_base_url: prodBaseUrl,
      prod_merchant_id: prodMerchantId,
      scope,
      custom_limit: customLimit,
    })
  }, [uatBaseUrl, uatMerchantId, prodBaseUrl, prodMerchantId, scope, customLimit])

  useEffect(() => {
    let cancelled = false
    getMediaMigrationDefaults()
      .then((defaults) => {
        if (cancelled) return
        // Never overwrite something the operator typed or restored.
        setUatBaseUrl((current) => current || defaults.uat_base_url)
        setProdBaseUrl((current) => current || defaults.prod_base_url)
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(message(reason))
      })
    return () => {
      cancelled = true
    }
  }, [])

  const loadHistory = useCallback(async (uatId: string, prodId: string) => {
    try {
      const rows = await listMediaMigrationJobs({ uatMerchantId: uatId, prodMerchantId: prodId })
      setHistory(rows)
    } catch {
      // History is a convenience: a failure must not hide the form.
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    listMediaMigrationJobs({})
      .then((rows) => {
        if (!cancelled) setHistory(rows)
      })
      .catch(() => {
        // History is a convenience: a failure must not hide the form.
      })
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => () => {
    if (pollTimer.current) window.clearInterval(pollTimer.current)
  }, [])

  const jobActive = job !== null && (job.status === 'queued' || job.status === 'running')
  const jobId = job?.job_id ?? ''
  const jobStatus = job?.status ?? ''

  // Poll the running job; the server owns the checkpoints, the page only mirrors them.
  useEffect(() => {
    if (jobStatus !== 'queued' && jobStatus !== 'running') return
    let cancelled = false
    const poll = async () => {
      try {
        const next = await getMediaMigrationJob(jobId)
        if (cancelled) return
        setJob(next)
        if (next.status !== 'queued' && next.status !== 'running') {
          void loadHistory(next.uat_merchant_id, next.prod_merchant_id)
        }
      } catch (reason: unknown) {
        if (!cancelled) setError(message(reason))
      }
    }
    pollTimer.current = window.setInterval(() => void poll(), JOB_POLL_MS)
    return () => {
      cancelled = true
      if (pollTimer.current) window.clearInterval(pollTimer.current)
      pollTimer.current = null
    }
  }, [jobId, jobStatus, loadHistory])

  const ready = Boolean(
    uatBaseUrl.trim() &&
      uatMerchantId.trim() &&
      uatCookie.trim() &&
      prodBaseUrl.trim() &&
      prodMerchantId.trim() &&
      prodCookie.trim(),
  )

  const payload = useCallback(
    (extra: Partial<MediaMigrationPayload> = {}): MediaMigrationPayload => ({
      uat_base_url: uatBaseUrl.trim(),
      uat_merchant_id: uatMerchantId.trim(),
      uat_cookie: uatCookie.trim(),
      prod_base_url: prodBaseUrl.trim(),
      prod_merchant_id: prodMerchantId.trim(),
      prod_cookie: prodCookie.trim(),
      skip_same_name: skipSameName,
      force,
      ...extra,
    }),
    [
      uatBaseUrl,
      uatMerchantId,
      uatCookie,
      prodBaseUrl,
      prodMerchantId,
      prodCookie,
      skipSameName,
      force,
    ],
  )

  const handlePreview = async () => {
    setBusy('preview')
    setError(null)
    try {
      const result = await previewMediaMigration(payload())
      setPreview(result)
      setJob(null)
      setFilter('all')
    } catch (reason: unknown) {
      setError(message(reason))
    } finally {
      setBusy(null)
    }
  }

  const parsedCustomLimit = useMemo(() => {
    const value = Number.parseInt(customLimit.trim(), 10)
    return Number.isFinite(value) && value >= 1 ? value : 0
  }, [customLimit])
  const effectiveLimit = scope === 'custom' ? parsedCustomLimit : 0
  const canStart = ready && (scope === 'all' || parsedCustomLimit >= 1)

  const handleStart = async () => {
    if (scope === 'custom' && parsedCustomLimit < 1) return
    setBusy('start')
    setError(null)
    try {
      const started = await startMediaMigrationJob(payload({ limit: effectiveLimit }))
      setJob(started)
      setPreview(null)
      setFilter('all')
      void loadHistory(started.uat_merchant_id, started.prod_merchant_id)
    } catch (reason: unknown) {
      setError(message(reason))
    } finally {
      setBusy(null)
    }
  }

  const handleCancel = async () => {
    if (!job) return
    setBusy('cancel')
    try {
      setJob(await cancelMediaMigrationJob(job.job_id))
    } catch (reason: unknown) {
      setError(message(reason))
    } finally {
      setBusy(null)
    }
  }

  const handleOpenHistory = async (summary: MediaMigrationJobSummary) => {
    setBusy('history')
    setError(null)
    try {
      setJob(await getMediaMigrationJob(summary.job_id))
      setPreview(null)
      setFilter('all')
    } catch (reason: unknown) {
      setError(message(reason))
    } finally {
      setBusy(null)
    }
  }

  const counters = job?.counters ?? {}
  const items = job?.items ?? preview?.items ?? NO_ITEMS
  const uatTotal = job ? counters.uat_total ?? 0 : preview?.uat_total ?? 0
  const prodTotal = job ? counters.prod_total ?? 0 : preview?.prod_total ?? 0
  const planned = job ? counters.planned ?? 0 : preview?.planned ?? 0
  const skippedLedger = job ? counters.skipped_ledger ?? 0 : preview?.skipped_ledger ?? 0
  const skippedSameName = job ? counters.skipped_same_name ?? 0 : preview?.skipped_same_name ?? 0
  const skipped = skippedLedger + skippedSameName
  const uploaded = counters.uploaded ?? items.filter((item) => item.status === 'uploaded').length
  const failed = counters.failed ?? items.filter((item) => item.status === 'failed').length
  const visible = useMemo(() => items.filter((item) => matches(item, filter)), [items, filter])
  const progress = planned > 0 ? Math.min(100, Math.round(((uploaded + failed) / planned) * 100)) : 0
  const banner = error ?? job?.error ?? null

  return (
    <div className="mm-page">
      <header className="mm-head">
        <h1>素材迁移</h1>
        <p>UAT 素材库单向复制到 Prod，已上传的素材记录在本机 MongoDB。</p>
      </header>

      {banner ? <div className="mm-banner">{banner}</div> : null}

      <div className="mm-sources">
        <section className="mm-card">
          <h2>UAT 来源</h2>
          <label className="mm-field">
            <span>UAT 地址</span>
            <input
              value={uatBaseUrl}
              onChange={(event) => setUatBaseUrl(event.target.value)}
              placeholder="https://am.connexup-uat.net"
              spellCheck={false}
            />
          </label>
          <label className="mm-field">
            <span>UAT Merchant ID</span>
            <input
              value={uatMerchantId}
              onChange={(event) => setUatMerchantId(event.target.value)}
              placeholder="UAT 商户 ID"
              spellCheck={false}
            />
          </label>
          <label className="mm-field">
            <span>UAT Cookie</span>
            <input
              type={showCookies ? 'text' : 'password'}
              value={uatCookie}
              onChange={(event) => setUatCookie(event.target.value)}
              placeholder="AMSessionId=... 或直接粘贴 Session 值"
              spellCheck={false}
              autoComplete="off"
            />
          </label>
        </section>

        <section className="mm-card">
          <h2>Prod 目标</h2>
          <label className="mm-field">
            <span>Prod 地址</span>
            <input
              value={prodBaseUrl}
              onChange={(event) => setProdBaseUrl(event.target.value)}
              placeholder="https://am.connexup.com"
              spellCheck={false}
            />
          </label>
          <label className="mm-field">
            <span>Prod Merchant ID</span>
            <input
              value={prodMerchantId}
              onChange={(event) => setProdMerchantId(event.target.value)}
              placeholder="Prod 商户 ID"
              spellCheck={false}
            />
          </label>
          <label className="mm-field">
            <span>Prod Cookie</span>
            <input
              type={showCookies ? 'text' : 'password'}
              value={prodCookie}
              onChange={(event) => setProdCookie(event.target.value)}
              placeholder="AMSessionId=... 或直接粘贴 Session 值"
              spellCheck={false}
              autoComplete="off"
            />
          </label>
        </section>
      </div>

      <div className="mm-actions">
        <label className="mm-check">
          <input
            type="checkbox"
            checked={skipSameName}
            onChange={(event) => setSkipSameName(event.target.checked)}
          />
          <span>跳过同名素材</span>
        </label>
        <label className="mm-check">
          <input
            type="checkbox"
            checked={force}
            onChange={(event) => setForce(event.target.checked)}
          />
          <span>强制重传已同步记录</span>
        </label>
        <label className="mm-check">
          <input
            type="checkbox"
            checked={showCookies}
            onChange={(event) => setShowCookies(event.target.checked)}
          />
          <span>显示 Cookie</span>
        </label>
        <div className="mm-buttons">
          <button
            type="button"
            className="mm-btn"
            onClick={() => void handlePreview()}
            disabled={!ready || busy !== null || jobActive}
          >
            {busy === 'preview' ? '对比中…' : '预览对比'}
          </button>
          <div className="mm-scope" role="group" aria-label="迁移范围">
            <button
              type="button"
              className={`mm-scope-option${scope === 'custom' ? ' is-active' : ''}`}
              onClick={() => setScope('custom')}
              disabled={busy !== null || jobActive}
            >
              指定张数
            </button>
            {scope === 'custom' ? (
              <label className="mm-scope-count">
                <input
                  type="number"
                  min={1}
                  step={1}
                  inputMode="numeric"
                  value={customLimit}
                  onChange={(event) => setCustomLimit(event.target.value)}
                  disabled={busy !== null || jobActive}
                  aria-label="迁移张数"
                />
                <span>张</span>
              </label>
            ) : null}
            <button
              type="button"
              className={`mm-scope-option${scope === 'all' ? ' is-active' : ''}`}
              onClick={() => setScope('all')}
              disabled={busy !== null || jobActive}
            >
              全量
            </button>
          </div>
          <button
            type="button"
            className="mm-btn is-primary"
            onClick={() => void handleStart()}
            disabled={!canStart || busy !== null || jobActive}
          >
            {busy === 'start'
              ? '启动中…'
              : scope === 'all'
                ? '开始全量迁移'
                : parsedCustomLimit >= 1
                  ? `开始迁移（${parsedCustomLimit} 张）`
                  : '开始迁移'}
          </button>
          {jobActive ? (
            <button
              type="button"
              className="mm-btn is-danger"
              onClick={() => void handleCancel()}
              disabled={busy !== null}
            >
              停止迁移
            </button>
          ) : null}
        </div>
      </div>

      <div className="mm-stats">
        <div className="mm-stat">
          <span>UAT 图片</span>
          <strong>{uatTotal}</strong>
        </div>
        <div className="mm-stat">
          <span>Prod 已有</span>
          <strong>{prodTotal}</strong>
        </div>
        <div className="mm-stat">
          <span>待上传</span>
          <strong>{planned}</strong>
        </div>
        <div className="mm-stat">
          <span>已跳过</span>
          <strong>{skipped}</strong>
        </div>
        <div className="mm-stat is-ok">
          <span>已上传</span>
          <strong>{uploaded}</strong>
        </div>
        <div className="mm-stat is-danger">
          <span>失败</span>
          <strong>{failed}</strong>
        </div>
      </div>

      {job ? (
        <div className="mm-progress">
          <div className="mm-progress-head">
            <span className={`mm-status is-${job.status}`}>{JOB_LABEL[job.status] ?? job.status}</span>
            <span className="mm-current">{job.current || job.job_id}</span>
            <span className="mm-progress-count">
              {uploaded + failed}/{planned}
            </span>
          </div>
          <div className="mm-progress-bar">
            <span style={{ width: `${progress}%` }} />
          </div>
        </div>
      ) : null}

      {history.length > 0 ? (
        <section className="mm-history">
          <h2>最近迁移记录</h2>
          <div className="mm-history-list">
            {history.map((entry) => (
              <button
                key={entry.job_id}
                type="button"
                className={`mm-history-item${job?.job_id === entry.job_id ? ' is-active' : ''}`}
                onClick={() => void handleOpenHistory(entry)}
                disabled={busy !== null}
              >
                <span className={`mm-status is-${entry.status}`}>
                  {JOB_LABEL[entry.status] ?? entry.status}
                </span>
                <span className="mm-history-time">{formatTime(entry.created_at)}</span>
                <span className="mm-history-count">
                  {entry.counters.uploaded ?? 0} 上传 · {entry.counters.failed ?? 0} 失败
                  {entry.limit ? ` · 限 ${entry.limit} 张` : ''}
                </span>
                <span className="mm-history-ids">
                  {entry.uat_merchant_id} → {entry.prod_merchant_id}
                </span>
              </button>
            ))}
          </div>
        </section>
      ) : null}

      <div className="mm-filters">
        {FILTERS.map((entry) => (
          <button
            key={entry.key}
            type="button"
            className={`mm-filter${filter === entry.key ? ' is-active' : ''}`}
            onClick={() => setFilter(entry.key)}
          >
            {entry.label}
          </button>
        ))}
      </div>

      <div className="mm-table-wrap">
        <table className="mm-table">
          <thead>
            <tr>
              <th>素材名称</th>
              <th>格式</th>
              <th>标签</th>
              <th>状态</th>
              <th>目标 ID / 原因</th>
            </tr>
          </thead>
          <tbody>
            {visible.length === 0 ? (
              <tr>
                <td colSpan={5} className="mm-empty">
                  {items.length === 0 ? '先预览对比，再开始迁移' : '当前筛选没有素材'}
                </td>
              </tr>
            ) : (
              visible.map((item) => (
                <tr key={item.source_id}>
                  <td className="mm-name" title={item.source_id}>
                    {item.name || item.source_id}
                  </td>
                  <td>{item.format || '—'}</td>
                  <td className="mm-tags">
                    {item.tags.length > 0
                      ? item.tags.map((tag) => (
                          <span key={tag} className="mm-tag">
                            {tag}
                          </span>
                        ))
                      : '—'}
                  </td>
                  <td>
                    <span className={`mm-status is-${item.status}`}>
                      {STATUS_LABEL[item.status] ?? item.status}
                    </span>
                  </td>
                  <td className={item.status === 'failed' ? 'mm-detail is-error' : 'mm-detail'}>
                    {itemDetail(item) || '—'}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
