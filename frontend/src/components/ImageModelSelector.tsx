import type { ImageModel, WholeImageTask } from '../types'
import { IMAGE_MODEL_OPTIONS, normalizeImageModel } from '../imageModels'
import { modelGenerationState, imageModelLabel, type ModelCounts, type ModelSubmission } from '../modelGeneration'

type Props = {
  counts: ModelCounts
  tasks: WholeImageTask[]
  submissions: Partial<Record<ImageModel, ModelSubmission>>
  disabled: boolean
  onChange: (model: ImageModel, count: number) => void
  onGenerate: (model: ImageModel) => void
  selectedModel: ImageModel
  selectionDisabled: boolean
  onSelectModel: (model: ImageModel) => void
}

export default function ImageModelSelector({ counts, tasks, submissions, disabled, onChange, onGenerate,
  selectedModel, selectionDisabled, onSelectModel }: Props) {
  const historicalModels = [...new Set(tasks.flatMap(task => {
    const model = normalizeImageModel(task.input.image_model)
    return IMAGE_MODEL_OPTIONS.some(option => option.value === model) ? [] : [model]
  }))]
  const models = [...IMAGE_MODEL_OPTIONS, ...historicalModels.map(value => ({ value, label: imageModelLabel(value) }))]
  const available = IMAGE_MODEL_OPTIONS.some(option => option.value === selectedModel)
  const countOptions = (model: ImageModel) => (
    <div className="model-count-options" role="radiogroup" aria-label={`${imageModelLabel(model)} 生成张数`}>
      {[0, 1, 2, 3].map(count => (
        <button key={count} type="button" role="radio" aria-checked={(counts[model] ?? 0) === count}
          aria-label={`${imageModelLabel(model)} ${count} 张`}
          className={(counts[model] ?? 0) === count ? 'is-active' : ''}
          disabled={modelGenerationState(tasks, model).running || Boolean(submissions[model] && !submissions[model]?.error) || !available}
          onClick={() => onChange(model, count)}>{count}</button>
      ))}
    </div>
  )
  const generateButton = (model: ImageModel) => {
    const state = modelGenerationState(tasks, model)
    const submission = submissions[model]
    const running = Boolean(submission && !submission.error) || state.running
    const count = counts[model] ?? 0
    // The count sits in the pill row right after the button, so the button only carries the verb.
    const action = state.hasHistory ? '再生成' : '生成'
    const total = submission?.count ?? state.total
    const completed = submission && !submission.error
      ? tasks.filter(task => task.input.candidate_batch_id === submission.batchId && (task.status === 'succeeded' || task.status === 'failed')).length
      : state.completed
    return (
      <button type="button" className="model-generate-button" disabled={disabled || running || !count || !available}
        aria-label={`${imageModelLabel(model)} ${running ? '生成中' : action}`}
        onClick={() => onGenerate(model)}>
        {running ? `${submission && !submission.error ? '提交中' : '生成中'} ${completed}/${total}` : action}
      </button>
    )
  }
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
        {generateButton(selectedModel)}
        <div className="model-toolbar-count">{countOptions(selectedModel)}<span className="model-count-unit">张</span></div>
      </div>
      {submissions[selectedModel]?.error ? <p className="model-generation-error" role="alert">{submissions[selectedModel]?.error}</p> : null}
    </div>
  )
}
