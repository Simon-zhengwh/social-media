import { useEffect, useState } from 'react'
import { getPosterCosts, type PosterCostCall, type PosterCostChild, type PosterCostReport } from '../api'
import './PosterCostPanel.css'

const KIND_LABEL = { image: '图片模型', llm: 'LLM Call', agent: 'Agent' }
const OPERATION_LABEL: Record<string, string> = {
  generate: '生成海报', edit: '对话改图', replace: '原照替换',
  plan: '系列图规划', series: '生成系列图', optimize: '提示词优化',
}
const STATUS_LABEL: Record<string, string> = { running: '运行中', failed: '失败', unknown: '结果待确认' }

const usd = new Intl.NumberFormat('en-US', {
  style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 8,
})
const points = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 6 })
const time = new Intl.DateTimeFormat('zh-CN', {
  hour: '2-digit', minute: '2-digit', second: '2-digit', month: 'numeric', day: 'numeric',
})

function amount(value: number | null, formatter: Intl.NumberFormat) {
  return value === null ? '—' : formatter.format(value)
}

function CostRow({ call }: { call: PosterCostCall | PosterCostChild }) {
  const started = call.started_at ? new Date(call.started_at) : null
  const operation = 'operation' in call ? OPERATION_LABEL[call.operation] : null
  const status = 'status' in call ? STATUS_LABEL[call.status] : null
  return (
    <span className="poster-cost-row">
      <span>
        <span className="poster-cost-operation">
          {operation ?? KIND_LABEL[call.kind]}
          {'image_number' in call && <span>{call.image_number ? ' · 图片 ' + call.image_number : ' · 整组'}</span>}
          {status && <span> · {status}</span>}
        </span>
        <span className="poster-cost-target">
          {operation && KIND_LABEL[call.kind] + ' · '}{call.target || '—'}
        </span>
        {started && !Number.isNaN(started.getTime()) && <time dateTime={started.toISOString()}>{time.format(started)}</time>}
      </span>
      <span className="poster-cost-values">
        <span>{amount(call.cost_usd, usd)}</span>
        <span>{call.credits === null ? '— 积分' : points.format(call.credits) + ' 积分'}</span>
      </span>
    </span>
  )
}

function CostItem({ call }: { call: PosterCostCall | PosterCostChild }) {
  return (
    <li>
      {call.children?.length ? (
        <details>
          <summary aria-label={call.target + ' 调用详情'}><CostRow call={call} /></summary>
          <ol className="poster-cost-children">
            {call.children.map(child => <CostItem key={child.id} call={child} />)}
          </ol>
        </details>
      ) : <CostRow call={call} />}
    </li>
  )
}

export default function PosterCostPanel({ workspaceId }: { workspaceId: string }) {
  const [report, setReport] = useState<PosterCostReport | null>(null)
  const [expanded, setExpanded] = useState(false)
  const [failed, setFailed] = useState(false)
  const [refresh, setRefresh] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    let loading = false
    const load = async () => {
      if (loading || document.hidden) return
      loading = true
      try {
        const next = await getPosterCosts(workspaceId, expanded, controller.signal)
        if (!controller.signal.aborted) {
          setReport(next)
          setFailed(false)
        }
      } catch {
        if (!controller.signal.aborted) setFailed(true)
      } finally {
        loading = false
      }
    }
    void load()
    const timer = window.setInterval(() => void load(), 5000)
    return () => {
      controller.abort()
      window.clearInterval(timer)
    }
  }, [workspaceId, expanded, refresh])

  return (
    <section className="poster-cost-panel" aria-label="任务费用">
      <dl className="poster-cost-summary" aria-live="polite">
        <dt>总费用</dt>
        <dd className="poster-cost-total">{report ? amount(report.total_usd, usd) : '—'}</dd>
        <dt className="poster-cost-credit-label">总积分</dt>
        <dd className="poster-cost-credit">{report ? points.format(report.total_credits) : '—'}</dd>
      </dl>
      {failed ? (
        <button className="mp-btn-secondary mp-btn-small" type="button" onClick={() => setRefresh(refresh + 1)}>
          费用加载失败，重试
        </button>
      ) : report?.pending_count ? (
        <div className="poster-cost-status" role="status">{report.pending_count} 笔费用待回传</div>
      ) : report?.unreported_count ? (
        <div className="poster-cost-status" role="status">部分费用未回传</div>
      ) : null}
      {!!report?.call_count && (
        <button type="button" className="mp-btn-secondary mp-btn-small"
          aria-expanded={expanded} aria-controls="poster-cost-log" onClick={() => setExpanded(!expanded)}>
          {expanded ? '收起调用明细' : '查看调用明细（' + report.call_count + ' 笔）'}
        </button>
      )}
      {expanded && (
        <ol id="poster-cost-log" className="poster-cost-log" aria-label="调用明细" tabIndex={0}>
          {report?.calls?.map(call => <CostItem key={call.id} call={call} />)}
        </ol>
      )}
    </section>
  )
}
