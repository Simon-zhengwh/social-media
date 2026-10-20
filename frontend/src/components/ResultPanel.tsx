import { useState, type PointerEvent as ReactPointerEvent } from 'react'
import type { Layer, LayerPatch, Task } from '../types'

type Props = {
  task: Task | null
  busy: boolean
  selectedLayerId: string | null
  onSelectLayer: (id: string | null) => void
  onPatchLayer: (id: string, patch: LayerPatch) => void
  onResume: () => void
  onRetry: () => void
  candidates?: Task[]
  onSelectCandidate?: (candidate: Task) => void
}

const STAGE_LABELS: Record<string, string> = {
  pending: '排队中',
  planning: '理解需求与文案',
  uploading: '上传参考图',
  submitting: '提交生图',
  designing_reference: '生成全局设计参考',
  generating_components: '生成标题/价格/贴片素材',
  regenerating_component: '只重生成当前组件',
  generating: '生成场景底图',
  downloading: '下载底图',
  layout: '规划对象与文字',
  integrating_logo: '合成图片素材',
  reviewing: '视觉审查成图',
  correcting: '应用视觉修正',
  committing: '提交最佳版本',
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
        <span>底图、文字与图片素材会保存为独立图层。</span>
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
      <p>底图独立生成；完成后文字和图片素材作为真实图层叠加。</p>
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
      <strong>{task.status === 'interrupted' ? '任务已中断' : '任务需要恢复'}</strong>
      <p>{task.error?.message ?? '恢复后会重新调用 core-ai 网关，可能产生新的生成费用。'}</p>
      <button type="button" className="mp-btn-secondary mp-btn-small" onClick={onResume}>
        恢复任务
      </button>
    </div>
  )
}

function layerLabel(layer: Layer) {
  if (layer.type === 'text') return layer.name
  if (layer.type === 'image') return layer.name
  if (layer.type === 'shape') return layer.name
  if (layer.type === 'visual_component') return layer.name
  return '底图'
}

function LayerBoxes({
  task,
  selectedLayerId,
  onSelect,
  onPatch,
}: {
  task: Task
  selectedLayerId: string | null
  onSelect: (id: string | null) => void
  onPatch: (id: string, patch: LayerPatch) => void
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
        const beginTransform = (
          event: ReactPointerEvent<HTMLElement>,
          mode: 'move' | 'resize',
        ) => {
          if (event.button !== 0 || !('position' in layer) || !('size' in layer)) return
          event.preventDefault()
          event.stopPropagation()
          onSelect(layer.id)
          const frame = event.currentTarget.closest('.preview-frame')
          if (!(frame instanceof HTMLElement)) return
          const startX = event.clientX
          const startY = event.clientY
          const frameRef = frame
          const startBox = { ...box }
          const startSize = { ...size }
          let latest = { dx: 0, dy: 0, dw: 0 }
          const baseRotation = ('rotation' in layer ? layer.rotation : 0) ?? 0

          const boxElement = frameRef.querySelector<HTMLElement>(
            `[data-layer-box="${layer.id}"]`,
          )
          const move = (pointer: PointerEvent) => {
            const rect = frameRef.getBoundingClientRect()
            latest = {
              dx: ((pointer.clientX - startX) / Math.max(1, rect.width)) * canvas.width,
              dy: ((pointer.clientY - startY) / Math.max(1, rect.height)) * canvas.height,
              dw: ((pointer.clientX - startX) / Math.max(1, rect.width)) * canvas.width,
            }
            if (!boxElement) return
            if (mode === 'move') {
              boxElement.style.transform = `translate(${latest.dx}px, ${latest.dy}px) rotate(${baseRotation}deg)`
            } else {
              const width = Math.max(32, startSize.width + latest.dw)
              const height = Math.max(32, startSize.height * (width / startSize.width))
              boxElement.style.width = `${(width / canvas.width) * 100}%`
              boxElement.style.height = `${(height / canvas.height) * 100}%`
            }
          }
          const finish = () => {
            window.removeEventListener('pointermove', move)
            window.removeEventListener('pointerup', finish)
            window.removeEventListener('pointercancel', finish)
            if (boxElement) {
              boxElement.style.transform = `rotate(${baseRotation}deg)`
              boxElement.style.width = `${(size.width / canvas.width) * 100}%`
              boxElement.style.height = `${(size.height / canvas.height) * 100}%`
            }
            if (mode === 'move' && (Math.abs(latest.dx) > 2 || Math.abs(latest.dy) > 2)) {
              onPatch(layer.id, {
                position: {
                  x: Math.round(startBox.x + latest.dx),
                  y: Math.round(startBox.y + latest.dy),
                },
              })
            } else if (mode === 'resize' && Math.abs(latest.dw) > 2) {
              const width = Math.max(32, Math.round(startSize.width + latest.dw))
              const height = Math.max(
                32,
                Math.round(startSize.height * (width / startSize.width)),
              )
              onPatch(layer.id, { size: { width, height } })
            }
          }
          window.addEventListener('pointermove', move)
          window.addEventListener('pointerup', finish, { once: true })
          window.addEventListener('pointercancel', finish, { once: true })
        }

        return (
          <button
            key={layer.id}
            type="button"
            data-layer-box={layer.id}
            className={`layer-box layer-box-${layer.type}${selected ? ' is-selected' : ''}`}
            style={{
              left: `${(box.x / canvas.width) * 100}%`,
              top: `${(box.y / canvas.height) * 100}%`,
              width: `${(size.width / canvas.width) * 100}%`,
              height: `${(size.height / canvas.height) * 100}%`,
              transform: `rotate(${('rotation' in layer ? layer.rotation : 0) ?? 0}deg)`,
            }}
            onPointerDown={(event) => beginTransform(event, 'move')}
            onClick={(event) => {
              event.stopPropagation()
            }}
            aria-label={`选择并拖动图层：${layerLabel(layer)}`}
          >
            <span>{layerLabel(layer)}</span>
            {selected ? (
              <i
                className="layer-resize-handle"
                aria-hidden="true"
                onPointerDown={(event) => beginTransform(event, 'resize')}
              />
            ) : null}
          </button>
        )
      })}
    </div>
  )
}

export default function ResultPanel({
  task: confirmedTask,
  busy,
  selectedLayerId,
  onSelectLayer,
  onPatchLayer,
  onResume,
  onRetry,
  candidates = [],
  onSelectCandidate,
}: Props) {
  const [showBoxes, setShowBoxes] = useState(true)
  const [candidatePreviewId, setCandidatePreviewId] = useState<string | null>(null)
  const previewCandidate =
    candidates.find((candidate) => candidate.id === candidatePreviewId) ??
    candidates.find((candidate) => candidate.id === confirmedTask?.id) ??
    null
  const task = previewCandidate ?? confirmedTask
  const isPreviewConfirmed = Boolean(task && confirmedTask && task.id === confirmedTask.id)
  const candidateIndex = Math.max(
    0,
    candidates.findIndex((candidate) => candidate.id === task?.id),
  )
  const moveCandidate = (offset: number) => {
    if (candidates.length <= 1) return
    const nextIndex = (candidateIndex + offset + candidates.length) % candidates.length
    setCandidatePreviewId(candidates[nextIndex].id)
  }
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
  const imageCount = designLayers.filter((layer) => layer.type === 'image').length
  const shapeCount = designLayers.filter((layer) => layer.type === 'shape').length
  const componentCount = designLayers.filter((layer) => layer.type === 'visual_component').length
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
        {candidates.length > 1 ? (
          <>
            <button
              type="button"
              className="candidate-arrow candidate-arrow-left"
              onClick={() => moveCandidate(-1)}
              aria-label="上一张候选"
            >
              ‹
            </button>
            <button
              type="button"
              className="candidate-arrow candidate-arrow-right"
              onClick={() => moveCandidate(1)}
              aria-label="下一张候选"
            >
              ›
            </button>
          </>
        ) : null}
        {isReady && task?.preview_url ? (
          <img className="poster-image" src={task.preview_url} alt="生成的品牌海报" />
        ) : isActive && task ? (
          <LoadingPreview task={task} />
        ) : (
          <GhostPreview />
        )}
        {isReady && task ? (
          <>
            {showBoxes && isPreviewConfirmed ? (
              <LayerBoxes
                task={task}
                selectedLayerId={selectedLayerId}
                onSelect={onSelectLayer}
                onPatch={onPatchLayer}
              />
            ) : null}
            {isPreviewConfirmed && isEditing ? (
              <div className="preview-edit-overlay">
                <span className="scanline" aria-hidden="true" />
                <strong>{STAGE_LABELS[task.stage] ?? '正在修改'}</strong>
                <span>只更新目标图层</span>
              </div>
            ) : null}
          </>
        ) : null}
      </div>

      {candidates.length > 1 ? (
        <div className="candidate-carousel">
          <div className="candidate-thumbnails">
            {candidates.map((candidate, index) => (
              <button
                type="button"
                key={candidate.id}
                className={`candidate-thumbnail${candidate.id === task?.id ? ' is-active' : ''}`}
                onClick={() => setCandidatePreviewId(candidate.id)}
                disabled={candidate.status !== 'succeeded'}
                aria-label={`查看候选 ${index + 1}`}
                title={`候选 ${index + 1}`}
              >
                {candidate.preview_url ? (
                  <img src={candidate.preview_url} alt="" />
                ) : (
                  <span>{candidate.status === 'failed' ? '失败' : '生成中'}</span>
                )}
              </button>
            ))}
          </div>
          {task && !isPreviewConfirmed ? (
            <button
              type="button"
              className="candidate-confirm-btn"
              onClick={() => {
                onSelectCandidate?.(task)
                setCandidatePreviewId(task.id)
              }}
              disabled={task.status !== 'succeeded'}
            >
              就选这张
            </button>
          ) : (
            <span className="candidate-confirmed">已定为当前主图</span>
          )}
        </div>
      ) : null}

      {isReady && task ? (
        <div className="result-footer">
          <div className="legend-row">
            <span className="legend-chip legend-ai">底图 v{task.current_version}</span>
            <span className="legend-chip legend-layer">{textCount} 个文字层</span>
            {imageCount ? <span className="legend-chip">{imageCount} 个图片层</span> : null}
            {shapeCount ? <span className="legend-chip">{shapeCount} 个形状层</span> : null}
            {componentCount ? <span className="legend-chip">{componentCount} 个视觉组件</span> : null}
            {task?.reference_guided?.enabled ? (
              <span className="legend-chip">上下文融合</span>
            ) : null}
            {task.review && task.review.status !== 'not_run' ? (
              <span className="legend-chip">
                视觉审查：{task.review.status === 'corrected' ? '已修正' : task.review.status === 'fallback' ? '回退初稿' : '保留初稿'}
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
