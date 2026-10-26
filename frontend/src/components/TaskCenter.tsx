import { useEffect, useState } from 'react'
import { ApiError, listWholeImageTaskGroups } from '../api'
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

type Props = {
  onOpenTask: (taskId: string) => void
  onCreate: () => void
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
}: {
  group: WholeImageTaskGroup
  onOpenTask: (taskId: string) => void
}) {
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
            onClick={() => onOpenTask(group.representative_task_id)}
            aria-label="在工作台打开任务"
          >
            <img src={group.preview_url} alt={group.prompt || '生成的海报'} />
          </button>
        ) : (
          <button
            type="button"
            className="task-cover-placeholder"
            onClick={() => onOpenTask(group.representative_task_id)}
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
          {group.candidate_count} 张
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
          {group.brand_name ? <span>{group.brand_name}</span> : <span>无品牌模式</span>}
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

export default function TaskCenter({ onOpenTask, onCreate }: Props) {
  const [filter, setFilter] = useState<WholeImageTaskGroupFilter>('all')
  const [groups, setGroups] = useState<WholeImageTaskGroup[]>([])
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
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

  useEffect(() => {
    let cancelled = false
    listWholeImageTaskGroups({ status: filter, limit: PAGE_SIZE })
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
  }, [filter])

  const hasActive = groups.some((group) => group.status === 'active')
  useEffect(() => {
    if (!hasActive || loading || loadingMore) {
      return
    }
    let cancelled = false
    const tick = async () => {
      try {
        const page = await listWholeImageTaskGroups({
          status: filter,
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
  }, [filter, groups.length, hasActive, loading, loadingMore])

  const handleLoadMore = async () => {
    if (!nextCursor || loadingMore) {
      return
    }
    setLoadingMore(true)
    setError(null)
    try {
      const page = await listWholeImageTaskGroups({
        status: filter,
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

  const filterLabel = FILTERS.find((item) => item.key === filter)?.label ?? '全部'

  return (
    <section className="asset-page task-center-page">
      <div className="asset-toolbar task-toolbar">
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
        <div className="task-toolbar-actions">
          <button type="button" className="mp-btn-primary" onClick={onCreate}>
            ＋ 新建设计
          </button>
        </div>
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
              ? '创建一次海报设计后，任务和候选会自动出现在这里。'
              : '切换到其他状态，或创建一次新的海报设计。'}
          </span>
          <button type="button" className="mp-btn-primary mp-btn-small" onClick={onCreate}>
            去海报设计
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
