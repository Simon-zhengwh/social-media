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
  /** Series-mode button and status overrides; shares the same model + count picker as single mode. */
  seriesState?: {
    busy: boolean
    running: boolean
    hasHistory: boolean
    runningModels: ImageModel[]
    error?: string | null
  }
}

export default function ImageModelSelector({ counts, tasks, submissions, disabled, disabledReasons, onChange, onGenerate,
  selectedModel, selectionDisabled, onSelectModel, countUnit = '张', countNoun = '生成张数', seriesState }: Props) {
  const historicalModels = [...new Set(tasks.flatMap(task => {
    const model = normalizeImageModel(task.input.image_model)
    return IMAGE_MODEL_OPTIONS.some(option => option.value === model) ? [] : [model]
  }))]
  const models = [...IMAGE_MODEL_OPTIONS, ...historicalModels.map(value => ({ value, label: imageModelLabel(value) }))]
  const available = IMAGE_MODEL_OPTIONS.some(option => option.value === selectedModel)
  const effectiveCount = counts[selectedModel] || 1
  const blockedReasons = !available ? ['当前模型不可用，请切换其他模型'] : disabledReasons
  const blocked = disabled || !available

  const singleState = modelGenerationState(tasks, selectedModel)
  const submission = submissions[selectedModel]
  const isSeries = Boolean(seriesState)
  const running = isSeries
    ? Boolean(seriesState?.busy || seriesState?.running)
    : Boolean(submission && !submission.error) || singleState.running
  const hasHistory = isSeries ? Boolean(seriesState?.hasHistory) : singleState.hasHistory
  const action = hasHistory ? '再生成' : isSeries ? '生成系列图' : '生成海报'

  const total = submission?.count ?? singleState.total
  const completed = submission && !submission.error
    ? tasks.filter(task => task.input.candidate_batch_id === submission.batchId && (task.status === 'succeeded' || task.status === 'failed')).length
    : singleState.completed

  const runningLabel = isSeries
    ? (seriesState?.busy ? '提交中…' : '生成中…')
    : `${submission && !submission.error ? '提交中' : '生成中'} ${completed}/${total}`

  const errorText = isSeries ? seriesState?.error : submission?.error

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
            const optionRunning = isSeries
              ? Boolean(seriesState?.runningModels.includes(option.value))
              : modelGenerationState(tasks, option.value).running
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
        {running ? runningLabel : action}
      </button>
      {errorText ? <p className="model-generation-error" role="alert">{errorText}</p> : null}
    </div>
  )
}
