import { useState } from 'react'
import type { WholeImageTask } from '../types'

type Props = {
  task: WholeImageTask | null
  busy: boolean
  onRetry: () => void
  candidates?: WholeImageTask[]
  onSelectCandidate?: (candidate: WholeImageTask) => void
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
        <strong>选择素材后生成完整设计</strong>
        <span>模型直接输出整张图片，不拆分图层。</span>
      </div>
    </div>
  )
}

function LoadingPreview() {
  return (
    <div className="preview-loading" role="status" aria-live="polite">
      <span className="generating-dots" aria-hidden="true">
        <span />
        <span />
        <span />
      </span>
      <strong>生成中</strong>
    </div>
  )
}

export default function WholeImageResultPanel({
  task: confirmedTask,
  busy,
  onRetry,
  candidates = [],
  onSelectCandidate,
}: Props) {
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
  const isActive = Boolean(task && (task.status === 'queued' || task.status === 'running'))
  const isEditing = task?.edit.status === 'running'
  const isReady = Boolean(task?.status === 'succeeded' && task.preview_url)
  const failed = Boolean(task?.status === 'failed' || task?.edit.status === 'failed')
  const errorText =
    task?.edit.status === 'failed'
      ? task.edit.error || '这次修改没有完成。'
      : task?.error?.message || '生成失败，请稍后重试。'
  const canvas = task?.canvas ?? { width: 1, height: 1 }
  const previewStyle = {
    aspectRatio: `${canvas.width} / ${canvas.height}`,
    maxWidth: `min(100%, ${((72 * canvas.width) / canvas.height).toFixed(2)}vh)`,
  }

  return (
    <section className="studio-preview">

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
          <div className="poster-image-button poster-image-static">
            <img className="poster-image" src={task.preview_url} alt="生成的完整设计" />
          </div>
        ) : isActive && task ? (
          <LoadingPreview />
        ) : (
          <GhostPreview />
        )}
        {isPreviewConfirmed && isReady && isEditing ? (
          <div className="preview-edit-overlay">
            <span className="scanline" aria-hidden="true" />
            <strong>修改中</strong>
            <span>基于当前图片重新调用编辑模型</span>
          </div>
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
          <div className="preview-actions">
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

      {failed ? (
        <div className="result-error" role="alert">
          <strong>{task?.edit.status === 'failed' ? '修改未完成' : '生成失败'}</strong>
          <span>{errorText}</span>
          <button type="button" className="mp-btn-secondary mp-btn-small" onClick={onRetry}>
            {task?.edit.status === 'failed' ? '重新尝试这次修改' : '重新生成'}
          </button>
        </div>
      ) : null}

      {busy && !task ? <p className="result-hint">正在创建任务…</p> : null}
      {!task && !busy ? (
        <p className="result-hint">填写左侧需求后，点击「生成设计」开始。</p>
      ) : null}

    </section>
  )
}
