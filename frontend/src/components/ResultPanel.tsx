import { useState } from 'react'
import type { Layer, Task } from '../types'

type Props = {
  task: Task | null
  busy: boolean
  selectedLayerId: string | null
  onSelectLayer: (id: string | null) => void
  onResume: () => void
  onRetry: () => void
}

const STAGE_LABELS: Record<string, string> = {
  pending: '排队中',
  planning: '理解需求与文案',
  uploading: '上传参考图',
  submitting: '提交生图',
  generating: '生成场景底图',
  downloading: '下载底图',
  layout: '规划文字图层',
  integrating_logo: '融合品牌 Logo',
  rendering: '合成海报',
  editing: '识别修改意图',
  regenerating_background: '重新生成底图',
  done: '已完成',
  failed: '失败',
  interrupted: '已中断',
}

const STATUS_LABELS: Record<Task['status'], string> = {
  queued: '排队中',
  running: '生成中',
  succeeded: '已完成',
  failed: '失败',
  waiting: '等待中',
  interrupted: '已中断',
}

function GhostPreview() {
  return (
    <div className="preview-empty">
      <div className="ghost-card" aria-hidden="true">
        <span className="ghost-block ghost-block-wide" />
        <span className="ghost-block" />
        <span className="ghost-block ghost-block-short" />
      </div>
      <div className="preview-empty-copy">
        <strong>选择素材后生成海报</strong>
        <span>底图、文字与 Logo 会保存为独立图层。</span>
      </div>
    </div>
  )
}

function LoadingPreview({ task }: { task: Task }) {
  return (
    <div className="preview-loading">
      <span className="scanline" aria-hidden="true" />
      <span className="preview-stage">{STAGE_LABELS[task.stage] ?? task.stage}</span>
      <div className="progress-track" aria-hidden="true">
        <span className="progress-fill" style={{ width: `${Math.max(4, task.progress)}%` }} />
      </div>
      <span className="progress-value">{task.progress}%</span>
      <p>底图独立生成；完成后文字和 Logo 作为真实图层叠加。</p>
    </div>
  )
}

function ErrorNotice({ task, onRetry }: { task: Task; onRetry: () => void }) {
  return (
    <div className="notice notice-error">
      <strong>这次没有生成成功</strong>
      <p>{task.error?.message ?? '生成流程中断，请重试。'}</p>
      <button type="button" className="mp-btn-secondary mp-btn-small" onClick={onRetry}>
        重新生成
      </button>
    </div>
  )
}

function WaitingNotice({ task, onResume }: { task: Task; onResume: () => void }) {
  return (
    <div className="notice notice-warn">
      <strong>{task.status === 'interrupted' ? '任务已中断' : '仍在等待供应商'}</strong>
      <p>{task.error?.message ?? '任务还在供应商侧处理中，可继续查询已有任务。'}</p>
      <button type="button" className="mp-btn-secondary mp-btn-small" onClick={onResume}>
        继续查询
      </button>
    </div>
  )
}

function layerLabel(layer: Layer) {
  if (layer.type === 'text') return layer.name
  if (layer.type === 'logo') return 'Logo'
  return '底图'
}

function LayerBoxes({
  task,
  selectedLayerId,
  onSelect,
}: {
  task: Task
  selectedLayerId: string | null
  onSelect: (id: string | null) => void
}) {
  if (!task.design) return null
  const canvas = task.design.canvas
  const layers = [...(task.design?.layers ?? [])]
    .filter((layer) => layer.type !== 'background' && layer.visible)
    .sort((a, b) => b.z_index - a.z_index)
  return (
    <div className="layer-box-layer">
      {layers.map((layer) => {
        const selected = layer.id === selectedLayerId
        const box = 'position' in layer ? layer.position : { x: 0, y: 0 }
        const size =
          'size' in layer ? layer.size : { width: canvas.width, height: canvas.height }
        return (
          <button
            key={layer.id}
            type="button"
            className={`layer-box layer-box-${layer.type}${selected ? ' is-selected' : ''}`}
            style={{
              left: `${(box.x / canvas.width) * 100}%`,
              top: `${(box.y / canvas.height) * 100}%`,
              width: `${(size.width / canvas.width) * 100}%`,
              height: `${(size.height / canvas.height) * 100}%`,
            }}
            onClick={() => onSelect(selected ? null : layer.id)}
            aria-label={`选择图层：${layerLabel(layer)}`}
          >
            <span>{layerLabel(layer)}</span>
          </button>
        )
      })}
    </div>
  )
}

export default function ResultPanel({
  task,
  busy,
  selectedLayerId,
  onSelectLayer,
  onResume,
  onRetry,
}: Props) {
  const [showBoxes, setShowBoxes] = useState(true)
  const isActive = Boolean(task && ['queued', 'running'].includes(task.status))
  const isReady = Boolean(task?.status === 'succeeded' && task.preview_url)
  const isWaiting = Boolean(task && ['waiting', 'interrupted'].includes(task.status))
  const isEditing = task?.edit.status === 'running'
  const canvas = task?.canvas ?? { width: 1, height: 1 }
  const previewStyle = {
    aspectRatio: `${canvas.width} / ${canvas.height}`,
    maxWidth: `min(100%, ${((72 * canvas.width) / canvas.height).toFixed(2)}vh)`,
  }
  const designLayers = task?.design?.layers ?? []
  const textCount = designLayers.filter((layer) => layer.type === 'text').length
  const hasLogo = designLayers.some((layer) => layer.type === 'logo')
  const qualityWarnings = task?.quality?.warnings ?? []

  return (
    <section className="studio-preview">
      <div className="preview-head">
        <div>
          <span className="eyebrow">02 · 预览</span>
          <h2>成品海报</h2>
        </div>
        <div className="preview-head-meta">
          {task?.brand?.name ? (
            <span className="brand-badge">{task.brand.name}</span>
          ) : null}
          {task ? (
            <span className="tnum">
              {task.canvas.width} × {task.canvas.height}
            </span>
          ) : null}
          {task && task.current_version > 0 ? (
            <span className="version-badge tnum">v{task.current_version}</span>
          ) : null}
          {task ? (
            <span className={`status-pill status-${task.status}`}>
              <span className="status-dot" aria-hidden="true" />
              {STATUS_LABELS[task.status]}
            </span>
          ) : null}
        </div>
      </div>

      <div className={`preview-frame${isReady ? ' is-ready' : ''}`} style={previewStyle}>
        {isReady && task?.preview_url ? (
          <img className="poster-image" src={task.preview_url} alt="生成的品牌海报" />
        ) : isActive && task ? (
          <LoadingPreview task={task} />
        ) : (
          <GhostPreview />
        )}
        {isReady && task ? (
          <>
            {showBoxes ? (
              <LayerBoxes
                task={task}
                selectedLayerId={selectedLayerId}
                onSelect={onSelectLayer}
              />
            ) : null}
            {isEditing ? (
              <div className="preview-edit-overlay">
                <span className="scanline" aria-hidden="true" />
                <strong>{STAGE_LABELS[task.stage] ?? '正在修改'}</strong>
                <span>只更新目标图层</span>
              </div>
            ) : null}
          </>
        ) : null}
      </div>

      {isReady && task ? (
        <div className="result-footer">
          <div className="legend-row">
            <span className="legend-chip legend-ai">底图 v{task.current_version}</span>
            <span className="legend-chip legend-layer">{textCount} 个文字层</span>
            {hasLogo ? (
              <span className="legend-chip legend-brand">
                {task.design?.background.logo_integrated ? 'Logo 模型融合' : 'Logo 透明层'}
              </span>
            ) : null}
          </div>
          <div className="preview-actions">
            <button
              type="button"
              className="box-toggle"
              onClick={() => setShowBoxes((value) => !value)}
              aria-pressed={showBoxes}
            >
              {showBoxes ? '隐藏边界' : '显示边界'}
            </button>
            <a className="mp-btn-dark" href={task.download_url ?? '#'} download>
              <svg viewBox="0 0 20 20" aria-hidden="true" className="btn-icon">
                <path
                  d="M10 3v9m0 0 3.5-3.5M10 12 6.5 8.5M4 15.5h12"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.7"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
              下载 PNG
            </a>
          </div>
        </div>
      ) : null}

      {isReady && qualityWarnings.length > 0 ? (
        <div className="quality-notice">
          <strong>可读性检查</strong>
          <ul>
            {qualityWarnings.slice(0, 3).map((warning, index) => (
              <li key={`${warning.code}-${warning.layer_id}-${index}`}>{warning.message}</li>
            ))}
          </ul>
          <span>可以在对话里说“标题换一个更亮的颜色”来调整，底图不会重画。</span>
        </div>
      ) : null}

      {task?.status === 'failed' ? <ErrorNotice task={task} onRetry={onRetry} /> : null}
      {isWaiting && task ? <WaitingNotice task={task} onResume={onResume} /> : null}
      {busy && !task ? <p className="result-hint">正在创建任务…</p> : null}
      {!task && !busy ? (
        <p className="result-hint">素材就绪后，点击左侧「生成海报」开始。</p>
      ) : null}
    </section>
  )
}
