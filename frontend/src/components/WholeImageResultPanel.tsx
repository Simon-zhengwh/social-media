import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import type { ImageModel, WholeImageArtifact, WholeImageOutput, WholeImageTask, WholeImageVersion } from '../types'
import WholeArtifactPreview from './WholeArtifactPreview'
import ModelCandidateGroup from './ModelCandidateGroup'
import CandidateVersionPicker from './CandidateVersionPicker'

type Props = {
  task: WholeImageTask | null
  busy: boolean
  onRetry: (candidate: WholeImageTask) => void
  selectedModel: ImageModel
  candidates?: WholeImageTask[]
  onSelectCandidate?: (candidate: WholeImageTask) => void
  onSelectOutput?: (versionId: string, outputId: string) => void
  selectingOutput?: boolean
  selectingCandidateDisabled?: boolean
  deletingCandidateId?: string | null
  onDeleteCandidate: (candidate: WholeImageTask) => void
  children?: ReactNode
  versions: WholeImageVersion[]
  versionsLoading: boolean
  versionSelectionDisabled: boolean
  onSelectVersion: (versionId: string) => Promise<boolean>
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
  selectedModel,
  candidates = [],
  onSelectCandidate,
  onSelectOutput,
  selectingOutput = false,
  selectingCandidateDisabled = false,
  deletingCandidateId = null,
  onDeleteCandidate,
  children,
  versions,
  versionsLoading,
  versionSelectionDisabled,
  onSelectVersion,
}: Props) {
  const [artifactPreview, setArtifactPreview] = useState<WholeImageArtifact | null>(null)
  const previewRef = useRef<HTMLElement>(null)
  const [versionAnchor, setVersionAnchor] = useState<{ taskId: string; element: HTMLButtonElement } | null>(null)
  const closeVersionPicker = useCallback(() => setVersionAnchor(null), [])
  const task = candidates.find(candidate => candidate.id === confirmedTask?.id) ?? confirmedTask
  const selectionDisabled = selectingCandidateDisabled || deletingCandidateId !== null
  const candidateIndex = Math.max(
    0,
    candidates.findIndex((candidate) => candidate.id === task?.id),
  )
  useEffect(() => {
    if (candidates.length <= 1 || selectionDisabled || !onSelectCandidate) return

    const onKeyDown = (event: KeyboardEvent) => {
      const offset = event.key === 'ArrowLeft' || event.key === 'ArrowUp' ? -1
        : event.key === 'ArrowRight' || event.key === 'ArrowDown' ? 1 : 0
      if (!offset || event.defaultPrevented || event.isComposing || event.altKey || event.ctrlKey || event.metaKey) return
      const target = event.target
      if (target instanceof HTMLElement && (
        target.isContentEditable ||
        target.closest('input, textarea, select, [role="textbox"], [role="tab"], [role="menu"], [role="listbox"], [role="combobox"], [role="dialog"]')
      )) return
      if (document.querySelector('dialog[open], [aria-modal="true"]')) return

      event.preventDefault()
      setVersionAnchor(null)
      const nextIndex = (candidateIndex + offset + candidates.length) % candidates.length
      onSelectCandidate(candidates[nextIndex])
      const thumbnail = previewRef.current?.querySelectorAll<HTMLButtonElement>('.candidate-thumbnail')[nextIndex]
      thumbnail?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
      if (target instanceof HTMLElement && target.closest('.candidate-thumbnail')) {
        thumbnail?.focus({ preventScroll: true })
      }
    }

    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [candidateIndex, candidates, onSelectCandidate, selectionDisabled])
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
  const previewUrl = currentOutput?.preview_url ?? task?.preview_url
  const branches = task?.edit.branches
  const displayOutputs = isEditing
    ? branches?.gateway?.outputs ?? []
    : task?.outputs ?? []
  const comparisonVisible = Boolean(
    task && (displayOutputs.length > 1 || task.requires_output_selection),
  )

  return (
    <section ref={previewRef} className="studio-preview" aria-label="海报预览" aria-keyshortcuts="ArrowLeft ArrowRight ArrowUp ArrowDown">
      <div className={`preview-workspace${candidates.length ? ' has-candidates' : ''}`}>
        {candidates.length ? (
          <aside className="candidate-carousel" aria-label="候选图片">
            <div className="candidate-model-groups">
              <ModelCandidateGroup model={selectedModel} candidates={candidates} selectedTaskId={task?.id}
                selectionDisabled={selectionDisabled} deletingCandidateId={deletingCandidateId}
                expandedTaskId={versionAnchor?.taskId === task?.id && !versionsLoading && versions.length > 1 ? versionAnchor?.taskId : null}
                onSelect={(candidate, element) => {
                  if (versionAnchor?.taskId === candidate.id) {
                    setVersionAnchor(null)
                    return
                  }
                  onSelectCandidate?.(candidate)
                  setVersionAnchor({ taskId: candidate.id, element })
                }}
                onDelete={candidate => { setVersionAnchor(null); onDeleteCandidate(candidate) }} />
            </div>
          </aside>
        ) : null}

        <div className="preview-main">
          <div className={`preview-frame${isReady ? ' is-ready' : ''}`}>
            {isReady && previewUrl ? (
              <div className="poster-image-button poster-image-static">
                <img className="poster-image" src={previewUrl} alt="生成的完整设计" />
              </div>
            ) : isActive && task ? (
              <LoadingPreview />
            ) : (
              <GhostPreview />
            )}
            {isReady && isEditing ? (
              <div className="preview-edit-overlay">
                <span className="scanline" aria-hidden="true" />
                <strong>修改中</strong>
                <span>模型正在按本次要求改图，完成后自动生成新版本</span>
              </div>
            ) : null}
          </div>

          <div className="preview-footer">
            {isReady && task && !comparisonVisible && currentOutput ? (
              <div className="preview-actions">
                <ArtifactActions output={currentOutput} onPreview={setArtifactPreview} />
              </div>
            ) : null}
            {children ? <div className="preview-chat">{children}</div> : null}
          </div>
        </div>
      </div>

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

      {failed ? (
        <div className="result-error" role="alert">
          <strong>{task?.edit.status === 'failed' ? '修改未完成' : '生成失败'}</strong>
          <span>{errorText}</span>
          <button type="button" className="mp-btn-secondary mp-btn-small" disabled={selectingCandidateDisabled || task?.status === 'running' || task?.status === 'queued'} onClick={() => task && onRetry(task)}>
            {task?.edit.status === 'failed' ? '重新尝试这次修改' : '重新生成'}
          </button>
        </div>
      ) : null}

      {busy && !task ? <p className="result-hint">正在创建任务…</p> : null}
      {versionAnchor ? (
        <CandidateVersionPicker key={versionAnchor.taskId} anchor={versionAnchor.element}
          versions={versionAnchor.taskId === task?.id ? versions : []}
          loading={versionsLoading || versionAnchor.taskId !== task?.id}
          currentVersionId={task?.current_version_id ?? null}
          busy={versionSelectionDisabled || selectionDisabled}
          onSelect={onSelectVersion}
          onClose={closeVersionPicker} />
      ) : null}
      <WholeArtifactPreview
        artifact={artifactPreview}
        onClose={() => setArtifactPreview(null)}
      />
    </section>
  )
}
