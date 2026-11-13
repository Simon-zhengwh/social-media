import { useState } from 'react'
import { createPortal } from 'react-dom'
import type { WholeImageArtifact, WholeImageVersion } from '../types'
import { parseServerDate } from '../time'
import WholeArtifactPreview from './WholeArtifactPreview'

type Props = {
  versions: WholeImageVersion[]
  currentVersionId: string | null
  busy: boolean
  onSelect: (versionId: string) => void
}

function formatTime(value: string) {
  const date = parseServerDate(value)
  if (Number.isNaN(date.getTime())) {
    return ''
  }
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}

export default function WholeVersionPanel({
  versions,
  currentVersionId,
  busy,
  onSelect,
}: Props) {
  const [preview, setPreview] = useState<WholeImageVersion | null>(null)
  const [artifactPreview, setArtifactPreview] = useState<WholeImageArtifact | null>(null)
  const ordered = [...versions].sort((a, b) => b.version - a.version)
  const versionById = new Map(versions.map((item) => [item.id, item]))

  return (
    <div className="version-panel">
      <div className="layer-panel-head">
        <span>历史版本</span>
        <span className="tnum">{versions.length} 个</span>
      </div>
      {ordered.length === 0 ? (
        <p className="version-empty">生成后每次修改都会保存一个独立完整图片版本。</p>
      ) : (
        <ol className="version-list">
          {ordered.map((version) => {
            const current = version.id === currentVersionId
            const parent = version.parent_version_id
              ? versionById.get(version.parent_version_id)
              : null
            return (
              <li
                key={version.id}
                className={`version-item${current ? ' is-current' : ''}`}
              >
                <button
                  type="button"
                  className="version-thumb"
                  onClick={() => setPreview(version)}
                  title="查看该版本"
                >
                  {version.preview_url ? (
                    <img src={version.preview_url} alt={`v${version.version} 预览`} />
                  ) : (
                    <span>无预览</span>
                  )}
                  {version.outputs.length > 1 ? (
                    <span className="version-output-count">{version.outputs.length} 张</span>
                  ) : null}
                </button>
                <div className="version-copy">
                  <div className="version-title">
                    <strong>v{version.version}</strong>
                    {version.operation === 'original_replace' ? <span className="version-current">原照替换</span> : null}
                    {current ? <span className="version-current">当前</span> : null}
                    <span className="version-time tnum">{formatTime(version.created_at)}</span>
                  </div>
                  <p>{version.instruction || '初始设计'}</p>
                  <span className="version-parent">
                    {version.operation === 'generate'
                      ? '首轮生成'
                      : parent
                        ? `基于 v${parent.version}`
                        : '基于历史版本'}
                  </span>
                  {!current ? (
                    <button
                      type="button"
                      className="version-restore"
                      onClick={() => onSelect(version.id)}
                      disabled={busy}
                    >
                      选为编辑来源
                    </button>
                  ) : null}
                </div>
              </li>
            )
          })}
        </ol>
      )}

      {preview
        ? createPortal(
            <div
              className="version-preview-backdrop"
              role="dialog"
              aria-modal="true"
              aria-label={`v${preview.version} 预览`}
              onClick={() => setPreview(null)}
            >
              <div className="version-preview-card" onClick={(event) => event.stopPropagation()}>
                <div className="version-preview-head">
                  <strong>v{preview.version} · {preview.instruction || '版本预览'}</strong>
                  {preview.outputs
                    .flatMap((output) => output.artifacts ?? [])
                    .map((artifact) =>
                      artifact.preview_url ? (
                        <button
                          key={artifact.id}
                          type="button"
                          className="mp-btn-ghost mp-btn-small"
                          onClick={() => setArtifactPreview(artifact)}
                        >
                          打开 {artifact.title || artifact.file_name}
                        </button>
                      ) : artifact.download_url ? (
                        <a
                          key={artifact.id}
                          className="mp-btn-ghost mp-btn-small"
                          href={artifact.download_url}
                          download
                        >
                          下载 {artifact.title || artifact.file_name}
                        </a>
                      ) : null,
                    )}
                  <button type="button" onClick={() => setPreview(null)}>
                    关闭
                  </button>
                </div>
                {preview.outputs.length > 1 ? (
                  <div className="version-output-grid">
                    {preview.outputs.map((output) => (
                      <figure key={output.id} className={output.role === 'agent_transparent' ? 'is-transparent' : ''}>
                        <img src={output.preview_url ?? ''} alt={output.label} />
                        <figcaption>{output.label}</figcaption>
                      </figure>
                    ))}
                  </div>
                ) : preview.preview_url ? (
                  <img src={preview.preview_url} alt={`v${preview.version} 完整预览`} />
                ) : null}
              </div>
            </div>,
            document.body,
          )
        : null}
      <WholeArtifactPreview
        artifact={artifactPreview}
        onClose={() => setArtifactPreview(null)}
      />
    </div>
  )
}
