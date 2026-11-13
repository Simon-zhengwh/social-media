import type { ImageModel, WholeImageTask } from '../types'
import { normalizeImageModel } from '../imageModels'
import { imageModelLabel } from '../modelGeneration'

type Props = {
  model: ImageModel
  candidates: WholeImageTask[]
  selectedTaskId?: string
  selectionDisabled: boolean
  deletingCandidateId: string | null
  onSelect: (candidate: WholeImageTask, anchor: HTMLButtonElement) => void
  onDelete: (candidate: WholeImageTask) => void
  showHeading?: boolean
  expandedTaskId?: string | null
}

export default function ModelCandidateGroup({ model, candidates, selectedTaskId, selectionDisabled,
  deletingCandidateId, onSelect, onDelete, showHeading = false, expandedTaskId }: Props) {
  const group = candidates.filter(candidate => normalizeImageModel(candidate.input.image_model) === normalizeImageModel(model))
  if (!group.length) return null
  const rounds = [...new Set(group.map(candidate => candidate.input.candidate_batch_id ?? candidate.id))]
  return (
    <div className="candidate-model-group">
      {showHeading ? <strong>{imageModelLabel(model)}</strong> : null}
      <div className="candidate-thumbnails">
        {group.map((candidate, index) => {
          const round = rounds.indexOf(candidate.input.candidate_batch_id ?? candidate.id) + 1
          const label = `${imageModelLabel(model)} 第 ${round} 轮 · ${index + 1}`
          const running = candidate.status === 'queued' || candidate.status === 'running' || candidate.edit.status === 'running'
          return (
            <div className="candidate-item" key={candidate.id}>
              <button type="button" className={`candidate-thumbnail${candidate.id === selectedTaskId ? ' is-active' : ''}`}
                onClick={event => onSelect(candidate, event.currentTarget)} disabled={selectionDisabled}
                aria-haspopup="dialog" aria-expanded={expandedTaskId === candidate.id}
                aria-controls={expandedTaskId === candidate.id ? 'candidate-version-picker' : undefined}
                aria-pressed={candidate.id === selectedTaskId} aria-label={`查看 ${label}`} title={label}>
                {candidate.preview_url ? <img src={candidate.preview_url} alt="" /> : <span>{candidate.status === 'failed' ? '失败' : '生成中'}</span>}
              </button>
              <button type="button" className="candidate-delete" aria-label={`删除 ${label}`}
                disabled={running || deletingCandidateId !== null} title={running ? '生成或改图中不能删除' : '删除这张候选'}
                onClick={() => onDelete(candidate)}>{deletingCandidateId === candidate.id ? '…' : '×'}</button>
            </div>
          )
        })}
      </div>
    </div>
  )
}
