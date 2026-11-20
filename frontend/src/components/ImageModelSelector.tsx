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
  onGenerate: (model: ImageModel) => void
  selectedModel: ImageModel
  selectionDisabled: boolean
  onSelectModel: (model: ImageModel) => void
  /** "张" for a single poster, "组" for a series round where the count is group count. */
  countUnit?: string
  countNoun?: string
  /**
   * Series mode submits one round for every model with a positive count, so the toolbar shows
   * a single group-submitting button instead of one per model.
   */
  seriesSubmit?: { total: number; busy: boolean; error?: string | null; onGenerate: () => void }
}

export default function ImageModelSelector({ counts, tasks, submissions, disabled, disabledReasons, onChange, onGenerate,
  selectedModel, selectionDisabled, onSelectModel, countUnit = '张', countNoun = '生成张数', seriesSubmit }: Props) {
  const historicalModels = [...new Set(tasks.flatMap(task => {
    const model = normalizeImageModel(task.input.image_model)
    return IMAGE_MODEL_OPTIONS.some(option => option.value === model) ? [] : [model]
  }))]
  const models = [...IMAGE_MODEL_OPTIONS, ...historicalModels.map(value => ({ value, label: imageModelLabel(value) }))]
  const available = IMAGE_MODEL_OPTIONS.some(option => option.value === selectedModel)
  const countOptions = (model: ImageModel) => (
    <div className="model-count-options" role="radiogroup" aria-label={`${imageModelLabel(model)} ${countNoun}`}>
      {[0, 1, 2, 3].map(count => (
        <button key={count} type="button" role="radio" aria-checked={(counts[model] ?? 0) === count}
          aria-label={`${imageModelLabel(model)} ${count} ${countUnit}`}
          className={(counts[model] ?? 0) === count ? 'is-active' : ''}
          disabled={modelGenerationState(tasks, model).running || Boolean(submissions[model] && !submissions[model]?.error) || !available}
          onClick={() => onChange(model, count)}>{count}</button>
      ))}
    </div>
  )
  const countMissingReason = `请选择${countNoun.replace('生成', '')}`
  const generateButton = (model: ImageModel) => {
    const state = modelGenerationState(tasks, model)
    const submission = submissions[model]
    const running = Boolean(submission && !submission.error) || state.running
    const count = counts[model] ?? 0
    // A zero count disables the button as well, so it has to say why on its own.
    const reasons = !count ? [...disabledReasons, countMissingReason] : disabledReasons
    const blocked = disabled || !count || !available
    // The count sits in the pill row right after the button, so the button only carries the verb.
    const action = state.hasHistory ? '再生成' : '生成'
    const total = submission?.count ?? state.total
    const completed = submission && !submission.error
      ? tasks.filter(task => task.input.candidate_batch_id === submission.batchId && (task.status === 'succeeded' || task.status === 'failed')).length
      : state.completed
    return (
      <button type="button" className="model-generate-button" disabled={disabled || running || !count || !available}
        aria-label={`${imageModelLabel(model)} ${running ? '生成中' : action}`}
        title={blocked && !running && reasons.length ? reasons.join('\n') : undefined}
        onClick={() => onGenerate(model)}>
        {running ? `${submission && !submission.error ? '提交中' : '生成中'} ${completed}/${total}` : action}
      </button>
    )
  }

  /*
    Series mode submits one round for every model with a positive count, so a single model at
    zero groups is not a blocker there: the count row already shows it, and the toolbar keeps
    the area under the controls free of status copy.
  */
  const blockedReasons = !available
    ? ['当前模型不可用，请切换其他模型']
    : !seriesSubmit && !(counts[selectedModel] ?? 0) ? [...disabledReasons, countMissingReason] : disabledReasons
  return (
    <div className="model-toolbar" aria-label="图片模型与生成">
      <strong className="model-toolbar-label">图片模型</strong>
      <div className="model-toolbar-tabs" role="group" aria-label="查看模型候选">
        {models.map(option => (
          <button type="button" key={option.value} className={`model-view-button${selectedModel === option.value ? ' is-active' : ''}`}
            aria-label={`切换到 ${option.label}`} aria-pressed={selectedModel === option.value} disabled={selectionDisabled}
            onClick={() => onSelectModel(option.value)}>
            <span>{option.label}</span>
            {modelGenerationState(tasks, option.value).running ? <span className="model-running-dot" aria-label="生成中" /> : null}
          </button>
        ))}
      </div>
      <div className="model-toolbar-actions">
        {seriesSubmit ? (
          <button type="button" className="model-generate-button"
            disabled={disabled || seriesSubmit.busy || seriesSubmit.total < 1}
            aria-label="生成"
            title={seriesSubmit.total < 1 ? `请选择至少一个模型的${countUnit}数` : blockedReasons[0]}
            onClick={seriesSubmit.onGenerate}>
            {seriesSubmit.busy ? '提交中…' : '生成'}
          </button>
        ) : generateButton(selectedModel)}
        <div className="model-toolbar-count">{countOptions(selectedModel)}<span className="model-count-unit">{countUnit}</span></div>
      </div>
      {seriesSubmit?.error ? <p className="model-generation-error" role="alert">{seriesSubmit.error}</p> : null}
      {submissions[selectedModel]?.error ? <p className="model-generation-error" role="alert">{submissions[selectedModel]?.error}</p> : null}
      {/* Series rounds report a submitted error above; the area below the controls stays clean. */}
      {!seriesSubmit && !submissions[selectedModel]?.error && blockedReasons.length
        ? <p className="model-generation-hint" role="status">{blockedReasons[0]}</p> : null}
    </div>
  )
}
