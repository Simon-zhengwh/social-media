import { useState } from 'react'
import type { VersionSummary } from '../types'

type Props = {
  versions: VersionSummary[]
  currentVersion: number
  busy: boolean
  onRestore: (version: number) => void
}

function formatTime(value: string) {
  const date = new Date(value)
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

export default function VersionPanel({
  versions,
  currentVersion,
  busy,
  onRestore,
}: Props) {
  const [preview, setPreview] = useState<VersionSummary | null>(null)
  const ordered = [...versions].sort((a, b) => b.version - a.version)

  return (
    <div className="version-panel">
      <div className="layer-panel-head">
        <span>历史版本</span>
        <span className="tnum">{versions.length} 个</span>
      </div>
      {ordered.length === 0 ? (
        <p className="version-empty">生成后每次修改都会保存一个可回滚版本。</p>
      ) : (
        <ol className="version-list">
          {ordered.map((version) => (
            <li
              key={version.version}
              className={`version-item${version.version === currentVersion ? ' is-current' : ''}`}
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
              </button>
              <div className="version-copy">
                <div className="version-title">
                  <strong>v{version.version}</strong>
                  {version.version === currentVersion ? (
                    <span className="version-current">当前</span>
                  ) : null}
                  <span className="version-time tnum">{formatTime(version.created_at)}</span>
                </div>
                <p>{version.summary || version.instruction || '初稿'}</p>
                {version.version !== currentVersion ? (
                  <button
                    type="button"
                    className="version-restore"
                    onClick={() => onRestore(version.version)}
                    disabled={busy}
                  >
                    恢复到此版本
                  </button>
                ) : null}
              </div>
            </li>
          ))}
        </ol>
      )}
      {preview ? (
        <div
          className="version-preview-backdrop"
          role="dialog"
          aria-modal="true"
          aria-label={`v${preview.version} 预览`}
          onClick={() => setPreview(null)}
        >
          <div className="version-preview-card" onClick={(event) => event.stopPropagation()}>
            <div className="version-preview-head">
              <strong>v{preview.version} · {preview.summary || '版本预览'}</strong>
              <button type="button" onClick={() => setPreview(null)}>
                关闭
              </button>
            </div>
            {preview.preview_url ? (
              <img src={preview.preview_url} alt={`v${preview.version} 完整预览`} />
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  )
}
