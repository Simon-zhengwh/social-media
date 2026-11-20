import { useEffect, useRef, useState } from 'react'
import type { ImageModel, WholeImageTask } from '../types'
import { IMAGE_MODEL_OPTIONS, normalizeImageModel } from '../imageModels'
import { modelGenerationState, imageModelLabel, type ModelCounts, type ModelSubmission } from '../modelGeneration'

type Props = {
  counts: ModelCounts
  tasks: WholeImageTask[]
  submissions: Partial<Record<ImageModel, ModelSubmission>>
  disabled: boolean
  /** Unmet generation conditions, in the order the operator has to solve them. */
  disabledReasons: string[]
  onChange: (model: ImageModel, count: number) => void
  onGenerate: (model: ImageModel, count?: number) => void
  selectedModel: ImageModel
  selectionDisabled: boolean
  onSelectModel: (model: ImageModel) => void
  /** "张" for a single poster, "组" for a series round where the count is group count. */
  countUnit?: string
  countNoun?: string
  /**
   * Series mode submits one round for every model with a positive count, so the toolbar shows
   * a multi-model popover and a single group-submitting button instead of one per model.
   */
  seriesSubmit?: { total: number; busy: boolean; error?: string | null; onGenerate: () => void }
}

export default function ImageModelSelector({ counts, tasks, submissions, disabled, disabledReasons, onChange, onGenerate,
  selectedModel, selectionDisabled, onSelectModel, countUnit = '张', countNoun = '生成张数', seriesSubmit }: Props) {
  const [popoverOpen, setPopoverOpen] = useState(false)
  const popoverRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!popoverOpen) return
    const onPointerDown = (event: PointerEvent) => {
      if (popoverRef.current && !popoverRef.current.contains(event.target as Node)) setPopoverOpen(false)
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setPopoverOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [popoverOpen])

  const historicalModels = [...new Set(tasks.flatMap(task => {
    const model = normalizeImageModel(task.input.image_model)
    return IMAGE_MODEL_OPTIONS.some(option => option.value === model) ? [] : [model]
  }))]
  const models = [...IMAGE_MODEL_OPTIONS, ...historicalModels.map(value => ({ value, label: imageModelLabel(value) }))]
  const available = IMAGE_MODEL_OPTIONS.some(option => option.value === selectedModel)

  if (seriesSubmit) {
    const activeSeriesModels = IMAGE_MODEL_OPTIONS.filter(option => (counts[option.value] ?? 0) > 0)
    const summaryText = activeSeriesModels.length === 0
      ? '选择模型与组数'
      : activeSeriesModels.length === 1
        ? `${activeSeriesModels[0].label} · ${counts[activeSeriesModels[0].value]} ${countUnit}`
        : `已选 ${activeSeriesModels.length} 个模型 · 共 ${seriesSubmit.total} ${countUnit}`
    const blockedReason = seriesSubmit.total < 1
      ? `请选择至少一个模型的${countUnit}数`
      : disabledReasons[0]

    return (
      <div className="model-sidebar-footer" aria-label="图片模型与生成" ref={popoverRef}>
        <div className="model-popover-wrap">
          <button
            type="button"
            className={`model-popover-trigger${activeSeriesModels.length === 0 ? ' is-empty' : ''}${popoverOpen ? ' is-open' : ''}`}
            aria-expanded={popoverOpen}
            aria-haspopup="dialog"
            disabled={selectionDisabled || seriesSubmit.busy}
            onClick={() => setPopoverOpen(open => !open)}
          >
            <span className="model-popover-summary">{summaryText}</span>
            <span className="model-popover-caret" aria-hidden="true">▾</span>
          </button>
          {popoverOpen ? (
            <div className="model-series-popover" role="dialog" aria-label="选择系列图模型与组数">
              <div className="model-series-popover-head">
                <strong>模型与组数</strong>
                <span>{seriesSubmit.total > 0 ? `共 ${seriesSubmit.total} ${countUnit}` : `每模型 0–3 ${countUnit}`}</span>
              </div>
              <div className="model-series-popover-list">
                {IMAGE_MODEL_OPTIONS.map(option => {
                  const modelCount = counts[option.value] ?? 0
                  const locked = Boolean(submissions[option.value] && !submissions[option.value]?.error)
                  return (
                    <div key={option.value} className={`model-series-popover-item${modelCount > 0 ? ' is-selected' : ''}`}>
                      <span className="model-series-popover-name">{option.label}</span>
                      <div className="model-count-options" role="radiogroup" aria-label={`${option.label} ${countNoun}`}>
                        {[0, 1, 2, 3].map(count => (
                          <button
                            key={count}
                            type="button"
                            role="radio"
                            aria-checked={modelCount === count}
                            aria-label={`${option.label} ${count} ${countUnit}`}
                            className={modelCount === count ? 'is-active' : ''}
                            disabled={locked || seriesSubmit.busy}
                            onClick={() => onChange(option.value, count)}
                          >
                            {count}
                          </button>
                        ))}
                      </div>
                    </div>
                  )
                })}
              </div>
            </div>
          ) : null}
        </div>
        <button
          type="button"
          className="model-generate-button model-generate-primary"
          disabled={disabled || seriesSubmit.busy || seriesSubmit.total < 1}
          aria-label="生成系列图"
          title={disabled || seriesSubmit.total < 1 ? blockedReason : undefined}
          onClick={() => {
            setPopoverOpen(false)
            seriesSubmit.onGenerate()
          }}
        >
          {seriesSubmit.busy
            ? '提交中…'
            : seriesSubmit.total > 0
              ? `生成系列图（${seriesSubmit.total} ${countUnit}）`
              : '生成系列图'}
        </button>
        {seriesSubmit.error ? <p className="model-generation-error" role="alert">{seriesSubmit.error}</p> : null}
      </div>
    )
  }

  const state = modelGenerationState(tasks, selectedModel)
  const submission = submissions[selectedModel]
  const running = Boolean(submission && !submission.error) || state.running
  const effectiveCount = counts[selectedModel] || 1
  const blockedReasons = !available ? ['当前模型不可用，请切换其他模型'] : disabledReasons
  const blocked = disabled || !available
  const action = state.hasHistory ? '再生成' : '生成海报'
  const total = submission?.count ?? state.total
  const completed = submission && !submission.error
    ? tasks.filter(task => task.input.candidate_batch_id === submission.batchId && (task.status === 'succeeded' || task.status === 'failed')).length
    : state.completed

  return (
    <div className="model-sidebar-footer" aria-label="图片模型与生成">
      <div className="model-compact-row">
        <select
          className="model-select"
          aria-label="选择图片模型"
          value={selectedModel}
          disabled={selectionDisabled}
          onChange={event => onSelectModel(event.target.value as ImageModel)}
        >
          {models.map(option => {
            const optionRunning = modelGenerationState(tasks, option.value).running
              || Boolean(submissions[option.value] && !submissions[option.value]?.error)
            return (
              <option key={option.value} value={option.value}>
                {optionRunning ? `${option.label}（生成中）` : option.label}
              </option>
            )
          })}
        </select>
        <div className="model-toolbar-count" role="radiogroup" aria-label={`${imageModelLabel(selectedModel)} ${countNoun}`}>
          <div className="model-count-options">
            {[1, 2, 3].map(count => (
              <button
                key={count}
                type="button"
                role="radio"
                aria-checked={effectiveCount === count}
                aria-label={`${imageModelLabel(selectedModel)} ${count} ${countUnit}`}
                className={effectiveCount === count ? 'is-active' : ''}
                disabled={running || !available}
                onClick={() => onChange(selectedModel, count)}
              >
                {count}
              </button>
            ))}
          </div>
          <span className="model-count-unit">{countUnit}</span>
        </div>
      </div>
      <button
        type="button"
        className="model-generate-button model-generate-primary"
        disabled={disabled || running || !available}
        aria-label={`${imageModelLabel(selectedModel)} ${running ? '生成中' : action}`}
        title={blocked && !running && blockedReasons.length ? blockedReasons.join('\n') : undefined}
        onClick={() => onGenerate(selectedModel, effectiveCount)}
      >
        {running ? `${submission && !submission.error ? '提交中' : '生成中'} ${completed}/${total}` : action}
      </button>
      {submission?.error ? <p className="model-generation-error" role="alert">{submission.error}</p> : null}
    </div>
  )
}
