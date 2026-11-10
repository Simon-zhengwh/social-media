import { useState } from 'react'
import type { WholeImageArtifact, WholeImageBranch, WholeImageOutput, WholeImageTask } from '../types'
import WholeArtifactPreview from './WholeArtifactPreview'

type Props = {
  task: WholeImageTask | null
  busy: boolean
  onRetry: () => void
  candidates?: WholeImageTask[]
  onSelectCandidate?: (candidate: WholeImageTask) => void
  onSelectOutput?: (versionId: string, outputId: string) => void
  selectingOutput?: boolean
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

function BranchStatus({ branch, label }: { branch?: WholeImageBranch; label: string }) {
  if (!branch) return null
  const status = branch.status
  const text =
    status === 'completed'
      ? '已完成'
      : status === 'failed'
        ? '失败'
        : status === 'running'
          ? '处理中'
          : status === 'preparing'
            ? '准备图片'
            : '等待中'
  return (
    <div className={`branch-status is-${status}`}>
      <span className="branch-status-dot" aria-hidden="true" />
      <strong>{label}</strong>
      <span>{text}</span>
      {branch.run_id ? <code>{branch.run_id.slice(0, 8)}</code> : null}
      {branch.error ? <em>{branch.error}</em> : null}
    </div>
  )
}

function ArtifactActions({
  output,
  onPreview,
}: {
  output: WholeImageOutput
  onPreview: (artifact: WholeImageArtifact) => void
}) {
  const artifacts = output.artifacts ?? []
  const htmlArtifact = artifacts.find((artifact) => artifact.kind === 'html')
  const bundleArtifact = artifacts.find((artifact) => artifact.kind === 'bundle')
  if (!htmlArtifact && !bundleArtifact?.download_url) return null
  return (
    <>
      {htmlArtifact ? (
        <button
          type="button"
          className="mp-btn-secondary mp-btn-small"
          onClick={() => onPreview(htmlArtifact)}
        >
          打开 HTML编辑器
        </button>
      ) : null}
      {bundleArtifact?.download_url ? (
        <a className="mp-btn-ghost mp-btn-small" href={bundleArtifact.download_url} download>
          下载结果包
        </a>
      ) : null}
    </>
  )
}

function channelLabel(output: WholeImageOutput) {
  if (output.channel === 'gateway') return '接口直出'
  if (output.role === 'original_replace_result') return '原照替换'
  return '图片 Agent'
}

function OutputCard({
  output,
  task,
  onPreviewArtifact,
  onSelectOutput,
  selectingOutput,
}: {
  output: WholeImageOutput
  task: WholeImageTask
  onSelectOutput?: (versionId: string, outputId: string) => void
  onPreviewArtifact: (artifact: WholeImageArtifact) => void
  selectingOutput: boolean
}) {
  const selected = task.current_output_id === output.id
  const transparent = output.role === 'agent_transparent'
  return (
    <article className={`comparison-card${transparent ? ' is-transparent' : ''}${selected ? ' is-selected' : ''}`}>
      <div className="comparison-card-head">
        <div>
          <strong>{output.label}</strong>
          <span>
            {channelLabel(output)} · {output.width}×{output.height}
          </span>
        </div>
        {selected ? <span className="comparison-selected">当前编辑源</span> : null}
      </div>
      {output.preview_url ? (
        <a className="comparison-image-wrap" href={output.preview_url} target="_blank" rel="noreferrer">
          <img src={output.preview_url} alt={output.label} />
        </a>
      ) : (
        <div className="comparison-image-missing">图片不可用</div>
      )}
      <div className="comparison-actions">
        <ArtifactActions output={output} onPreview={onPreviewArtifact} />
        {output.download_url ? (
          <a className="mp-btn-ghost mp-btn-small" href={output.download_url} download>
            下载 PNG
          </a>
        ) : null}
        {output.selectable && task.current_version_id && !selected ? (
          <button
            type="button"
            className="mp-btn-secondary mp-btn-small"
            onClick={() => onSelectOutput?.(task.current_version_id!, output.id)}
            disabled={selectingOutput || task.edit.status === 'running'}
          >
            用这张继续改
          </button>
        ) : transparent ? (
          <span className="comparison-note">透明层仅用于查看和下载</span>
        ) : null}
      </div>
    </article>
  )
}

export default function WholeImageResultPanel({
  task: confirmedTask,
  busy,
  onRetry,
  candidates = [],
  onSelectCandidate,
  onSelectOutput,
  selectingOutput = false,
}: Props) {
  const [candidatePreviewId, setCandidatePreviewId] = useState<string | null>(null)
  const [artifactPreview, setArtifactPreview] = useState<WholeImageArtifact | null>(null)
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
  const currentOutput =
    task?.outputs.find((output) => output.id === task.current_output_id) ??
    task?.outputs[0] ??
    null
  const isReady = Boolean(
    task?.status === 'succeeded' && (currentOutput?.preview_url || task.preview_url),
  )
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
  const previewUrl = currentOutput?.preview_url ?? task?.preview_url
  const branches = task?.edit.branches
  const displayOutputs = isEditing
    ? branches?.gateway?.outputs ?? []
    : task?.outputs ?? []
  const comparisonVisible = Boolean(
    task && (displayOutputs.length > 1 || isEditing || task.requires_output_selection),
  )

  return (
    <section className="studio-preview">
      <div className={`preview-frame${isReady ? ' is-ready' : ''}`} style={previewStyle}>
        {candidates.length > 1 ? (
          <>
            <button type="button" className="candidate-arrow candidate-arrow-left" onClick={() => moveCandidate(-1)} aria-label="上一张候选">‹</button>
            <button type="button" className="candidate-arrow candidate-arrow-right" onClick={() => moveCandidate(1)} aria-label="下一张候选">›</button>
          </>
        ) : null}
        {isReady && previewUrl ? (
          <div className="poster-image-button poster-image-static">
            <img className="poster-image" src={previewUrl} alt="生成的完整设计" />
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
            <span>模型正在按本次要求改图，完成后自动生成新版本</span>
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
                {candidate.preview_url ? <img src={candidate.preview_url} alt="" /> : <span>{candidate.status === 'failed' ? '失败' : '生成中'}</span>}
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

      {isEditing || task?.edit.outcome ? (
        <div className="branch-status-row">
          <BranchStatus branch={branches?.gateway} label="Core AI 接口" />
        </div>
      ) : null}

      {comparisonVisible && task ? (
        <div className="comparison-board">
          <div className="comparison-board-head">
            <div>
              <span className="eyebrow">SIDE BY SIDE</span>
              <h3>{task.requires_output_selection ? '选择一张结果继续修改' : '本轮结果对比'}</h3>
            </div>
            {task.requires_output_selection ? <span className="comparison-required">需手动选择</span> : null}
          </div>
          {task.edit.outcome === 'partial' ? (
            <p className="comparison-partial">本轮有一条链路失败，已保留成功结果。</p>
          ) : null}
          {displayOutputs.length ? (
            <div className="comparison-grid">
              {displayOutputs.map((output) => (
                <OutputCard
                  key={output.id}
                  output={output}
                  task={task}
                  onSelectOutput={onSelectOutput}
                  onPreviewArtifact={setArtifactPreview}
                  selectingOutput={selectingOutput}
                />
              ))}
            </div>
          ) : (
            <div className="comparison-pending">
              <span className="generating-dots" aria-hidden="true"><span /><span /><span /></span>
              <strong>等待模型返回图片</strong>
              <span>改图接口返回后会自动保存为新版本。</span>
            </div>
          )}
        </div>
      ) : null}

      {isReady && task && !comparisonVisible ? (
        <div className="result-footer">
          <div className="preview-actions">
            <a className="mp-btn-dark" href={currentOutput?.download_url ?? task.download_url ?? '#'} download>
              <svg viewBox="0 0 20 20" aria-hidden="true" className="btn-icon">
                <path d="M10 3v9m0 0 3.5-3.5M10 12 6.5 8.5M4 15.5h12" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              下载 PNG
            </a>
            {currentOutput ? (
              <ArtifactActions output={currentOutput} onPreview={setArtifactPreview} />
            ) : null}
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
      {!task && !busy ? <p className="result-hint">填写左侧需求后，点击「生成设计」开始。</p> : null}
      <WholeArtifactPreview
        artifact={artifactPreview}
        onClose={() => setArtifactPreview(null)}
      />
    </section>
  )
}
