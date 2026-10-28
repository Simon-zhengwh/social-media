import { useEffect, useState } from 'react'
import { ApiError, listTaskCenterGroups } from '../api'
import type {
  WholeImageTaskGroup,
  WholeImageTaskGroupFilter,
} from '../types'

const PAGE_SIZE = 24

const FILTERS: Array<{ key: WholeImageTaskGroupFilter; label: string }> = [
  { key: 'all', label: '全部' },
  { key: 'active', label: '生成中' },
  { key: 'ready', label: '已完成' },
  { key: 'failed', label: '失败' },
]

const KINDS = [
  { key: 'all', label: '全部类型' },
  { key: 'generate', label: '海报设计' },
  { key: 'edit', label: '修改海报' },
] as const

type Props = {
  onOpenTask: (taskId: string) => void
  onOpenPosterBatch: (batchId: string) => void
  onCreate: (kind: 'generate' | 'edit') => void
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message
  }
  if (error instanceof Error) {
    return error.message
  }
  return '请求失败，请稍后重试'
}

function formatDate(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return ''
  }
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

function statusLabel(group: WholeImageTaskGroup): string {
  if (group.status === 'active') {
    return '生成中'
  }
  if (group.status === 'ready') {
    return '已完成'
  }
  return '失败'
}

function TaskCard({
  group,
  onOpenTask,
  onOpenPosterBatch,
}: {
  group: WholeImageTaskGroup
  onOpenTask: (taskId: string) => void
  onOpenPosterBatch: (batchId: string) => void
}) {
  const open = () => {
    if (group.kind === 'edit') {
      onOpenPosterBatch(group.id)
    } else {
      onOpenTask(group.representative_task_id)
    }
  }
  const ratio =
    group.canvas.width > 0 && group.canvas.height > 0
      ? `${group.canvas.width} : ${group.canvas.height}`
      : '—'
  const progress = Math.max(0, ...group.candidates.map((item) => item.progress))
  return (
    <article className={`task-card task-card-${group.status}`}>
      <div className="task-cover">
        {group.preview_url ? (
          <button
            type="button"
            className="task-cover-button"
            onClick={open}
            aria-label={group.kind === 'edit' ? '打开修改批次' : '在工作台打开任务'}
          >
            <img src={group.preview_url} alt={group.prompt || '生成的海报'} />
          </button>
        ) : (
          <button
            type="button"
            className="task-cover-placeholder"
            onClick={open}
          >
            <span className="task-cover-mark" aria-hidden="true" />
            <strong>{group.status === 'failed' ? '生成失败' : '正在生成'}</strong>
            <span>
              {group.status === 'failed'
                ? group.error_message || '可以在工作台重试这次生成。'
                : `${group.succeeded_count}/${group.candidate_count} 已完成 · ${progress}%`}
            </span>
          </button>
        )}
        <span className={`status-pill task-status status-${group.status}`}>
          {statusLabel(group)}
        </span>
        <span className="task-candidate-count tnum">
          {group.candidate_count} {group.kind === 'edit' ? '个候选' : '张'}
        </span>
        {group.status === 'active' ? (
          <span className="task-progress" aria-hidden="true">
            <span style={{ width: `${progress}%` }} />
          </span>
        ) : null}
      </div>

      <div className="task-card-body">
        <div className="task-card-meta">
          <span>{formatDate(group.created_at)}</span>
          <span>{ratio}</span>
        </div>
        <strong className="task-prompt" title={group.prompt}>
          {group.prompt || '未填写设计需求'}
        </strong>
        <div className="task-tags">
          <span>{group.kind === 'edit' ? '修改海报' : '海报设计'}</span>
          {group.brand_name ? <span>{group.brand_name}</span> : null}
          {group.platform_name ? (
            <span>
              {group.platform_name}
              {group.placement_name ? ` · ${group.placement_name}` : ''}
            </span>
          ) : null}
        </div>
        {group.failed_count > 0 && group.status !== 'failed' ? (
          <p className="task-card-warning">
            {group.failed_count} 张候选失败，可直接重试失败项。
          </p>
        ) : null}

      </div>
    </article>
  )
}

export default function TaskCenter({ onOpenTask, onOpenPosterBatch, onCreate }: Props) {
  const [filter, setFilter] = useState<WholeImageTaskGroupFilter>('all')
  const [kind, setKind] = useState<'all' | 'generate' | 'edit'>('all')
  const [groups, setGroups] = useState<WholeImageTaskGroup[]>([])
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleFilterChange = (next: WholeImageTaskGroupFilter) => {
    if (next === filter) {
      return
    }
    setFilter(next)
    setLoading(true)
    setError(null)
    setGroups([])
    setNextCursor(null)
  }

  const handleKindChange = (next: 'all' | 'generate' | 'edit') => {
    if (next === kind) return
    setKind(next)
    setLoading(true)
    setError(null)
    setGroups([])
    setNextCursor(null)
  }

  useEffect(() => {
    let cancelled = false
    listTaskCenterGroups({ status: filter, kind, limit: PAGE_SIZE })
      .then((page) => {
        if (cancelled) {
          return
        }
        setGroups(page.items)
        setNextCursor(page.next_cursor)
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          setError(errorMessage(reason))
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false)
        }
      })
    return () => {
      cancelled = true
    }
  }, [filter, kind])

  const hasActive = groups.some((group) => group.status === 'active')
  useEffect(() => {
    if (!hasActive || loading || loadingMore) {
      return
    }
    let cancelled = false
    const tick = async () => {
      try {
        const page = await listTaskCenterGroups({
          status: filter,
          kind,
          limit: Math.max(PAGE_SIZE, groups.length),
        })
        if (!cancelled) {
          setGroups(page.items)
          setNextCursor(page.next_cursor)
        }
      } catch {
        // Keep the current list while a polling refresh fails.
      }
    }
    const interval = window.setInterval(() => void tick(), 2000)
    return () => {
      cancelled = true
      window.clearInterval(interval)
    }
  }, [filter, kind, groups.length, hasActive, loading, loadingMore])

  const handleLoadMore = async () => {
    if (!nextCursor || loadingMore) {
      return
    }
    setLoadingMore(true)
    setError(null)
    try {
      const page = await listTaskCenterGroups({
        status: filter,
        kind,
        limit: PAGE_SIZE,
        cursor: nextCursor,
      })
      setGroups((previous) => [...previous, ...page.items])
      setNextCursor(page.next_cursor)
    } catch (reason: unknown) {
      setError(errorMessage(reason))
    } finally {
      setLoadingMore(false)
    }
  }

  const handleRefresh = async () => {
    setRefreshing(true)
    setError(null)
    try {
      const page = await listTaskCenterGroups({
        status: filter,
        kind,
        limit: Math.max(PAGE_SIZE, groups.length),
      })
      setGroups(page.items)
      setNextCursor(page.next_cursor)
    } catch (reason: unknown) {
      setError(errorMessage(reason))
    } finally {
      setRefreshing(false)
    }
  }

  const filterLabel = FILTERS.find((item) => item.key === filter)?.label ?? '全部'
  const createKind = kind === 'edit' ? 'edit' : 'generate'
  const createName = createKind === 'edit' ? '修改海报' : '海报设计'
  const createLabel = createKind === 'edit' ? '去修改海报' : '去海报设计'

  return (
    <section className="asset-page task-center-page">
      <div className="asset-head">
        <div>
          <span className="eyebrow">TASK CENTER</span>
          <h1>任务中心</h1>
          <p className="panel-lead">
            按候选批次查看生成记录，点击封面回到工作台继续改图。
          </p>
        </div>
        <div className="asset-head-actions task-stats">
          <button
            type="button"
            className="mp-btn-primary"
            onClick={() => onCreate('generate')}
          >
            ＋ 新建海报设计
          </button>
          <button
            type="button"
            className="mp-btn-secondary"
            onClick={() => onCreate('edit')}
          >
            ＋ 新建修改
          </button>
        </div>
      </div>

      <div className="asset-toolbar task-toolbar">
        <div className="task-toolbar-groups">
          <div className="task-filters" role="tablist" aria-label="任务状态筛选">
            {FILTERS.map((item) => (
              <button
                key={item.key}
                type="button"
                role="tab"
                aria-selected={filter === item.key}
                className={`task-filter${filter === item.key ? ' is-active' : ''}`}
                onClick={() => handleFilterChange(item.key)}
              >
                {item.label}
              </button>
            ))}
          </div>
          <div className="task-filters task-kind-filters" role="tablist" aria-label="任务类型筛选">
            {KINDS.map((item) => (
              <button
                key={item.key}
                type="button"
                role="tab"
                aria-selected={kind === item.key}
                className={`task-filter${kind === item.key ? ' is-active' : ''}`}
                onClick={() => handleKindChange(item.key)}
              >
                {item.label}
              </button>
            ))}
          </div>
        </div>
        <button
          type="button"
          className="mp-btn-ghost mp-btn-small"
          onClick={() => void handleRefresh()}
          disabled={refreshing || loading}
        >
          {refreshing ? '刷新中…' : '刷新'}
        </button>
      </div>

      {error ? <div className="asset-notice asset-notice-error">{error}</div> : null}

      {loading ? (
        <div className="task-grid">
          {[0, 1, 2, 3, 4, 5].map((item) => (
            <div className="task-card task-card-skeleton" key={item}>
              <span className="task-cover task-skeleton-cover" />
              <span className="asset-skeleton-line" />
              <span className="asset-skeleton-line asset-skeleton-short" />
            </div>
          ))}
        </div>
      ) : groups.length === 0 ? (
        <div className="asset-empty task-empty">
          <strong>{filter === 'all' ? '还没有生成任务' : `没有${filterLabel}的任务`}</strong>
          <span>
            {filter === 'all'
              ? `创建一次${createName}后，任务和候选会自动出现在这里。`
              : `切换到其他状态，或创建一次新的${createName}。`}
          </span>
          <button
            type="button"
            className="mp-btn-primary mp-btn-small"
            onClick={() => onCreate(createKind)}
          >
            {createLabel}
          </button>
        </div>
      ) : (
        <>
          <div className="task-grid">
            {groups.map((group) => (
              <TaskCard
                key={group.id}
                group={group}
                onOpenTask={onOpenTask}
                onOpenPosterBatch={onOpenPosterBatch}
              />
            ))}
          </div>
          {nextCursor ? (
            <div className="task-load-more">
              <button
                type="button"
                className="mp-btn-secondary mp-btn-small"
                onClick={() => void handleLoadMore()}
                disabled={loadingMore}
              >
                {loadingMore ? '载入中…' : '加载更多'}
              </button>
            </div>
          ) : null}
        </>
      )}
    </section>
  )
}
