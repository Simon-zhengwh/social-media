import type { WholeImageBranch, WholeImageModelInput, WholeImageOutput } from '../types'
import { ZoomableImage } from './ImageLightbox'

type Props = {
  modelInput: WholeImageModelInput | null
  outputs?: WholeImageOutput[]
  branches?: Partial<Record<'gateway' | 'agent', WholeImageBranch>>
}

const IMAGE_ROLE_LABELS: Record<string, string> = {
  product: '内容素材',
  logo: 'Logo',
  style_reference: '风格参考',
  brand_reference: '品牌参考',
  current_design: '当前设计',
}

function channelLabel(channel?: string) {
  return channel === 'agent' ? '图片 Agent' : 'Core AI 接口'
}

function InputView({ input }: { input: WholeImageModelInput }) {
  return (
    <div className="model-output-input">
      <dl className="model-input-meta">
        <div>
          <dt>渠道</dt>
          <dd>{channelLabel(input.channel)}</dd>
        </div>
        <div>
          <dt>接口</dt>
          <dd>{input.endpoint || '—'}</dd>
        </div>
        <div>
          <dt>{input.channel === 'agent' ? '执行器' : '模型'}</dt>
          <dd>{input.model || '—'}</dd>
        </div>
        <div>
          <dt>{input.channel === 'agent' ? '超时' : '尺寸'}</dt>
          <dd>
            {input.channel === 'agent'
              ? `${input.parameters.timeout_seconds ?? '—'} 秒`
              : input.parameters.size ?? '—'}
          </dd>
        </div>
      </dl>

      <section className="model-input-section">
        <h4>{input.channel === 'agent' ? 'Agent input' : '最终提示词'}</h4>
        <pre>{input.prompt}</pre>
      </section>

      <section className="model-input-section">
        <h4>输入图片 <span>{input.images.length}</span></h4>
        {input.images.length ? (
          <div className="model-input-images">
            {input.images.map((image, index) => (
              <figure key={`${image.asset_id}-${index}`}>
                {image.preview_url ? (
                  <ZoomableImage
                    className="model-input-image-button"
                    src={image.preview_url}
                    alt={image.name}
                    title={image.name}
                  />
                ) : (
                  <div className="model-input-image-missing">图片不可用</div>
                )}
                <figcaption>
                  <strong>{index + 1}. {IMAGE_ROLE_LABELS[image.role ?? image.kind ?? "reference"] ?? (image.role ?? image.kind ?? "reference")}</strong>
                  <span>{image.name}</span>
                  {image.role ? <span>{image.role}</span> : null}
                  {image.url ? <a href={image.url} target="_blank" rel="noreferrer">打开 Agent 图片 URL</a> : null}
                </figcaption>
              </figure>
            ))}
          </div>
        ) : (
          <p className="model-input-no-images">本次调用没有输入图片。</p>
        )}
        <p className="model-input-hint">按此顺序传入。Agent 在本轮运行中通过公开图片 URL 读取输入图。</p>
      </section>
    </div>
  )
}

export default function WholeModelInputPanel({ modelInput, outputs = [], branches }: Props) {
  const entries: Array<{
    key: string
    label: string
    status?: string
    previewUrl?: string | null
    input: WholeImageModelInput | null
    error?: string | null
    runId?: string | null
  }> = outputs.map((output) => ({
    key: output.id,
    label: output.label,
    status: 'completed',
    previewUrl: output.preview_url,
    input: output.model_input,
  }))
  if (!entries.length && branches) {
    for (const key of ['gateway', 'agent'] as const) {
      const branch = branches[key]
      if (!branch) continue
      entries.push({
        key,
        label: key === 'gateway' ? '接口结果' : 'Agent 结果',
        status: branch.status,
        previewUrl: branch.outputs[0]?.preview_url,
        input: branch.model_input,
        error: branch.error,
        runId: branch.run_id,
      })
    }
  }
  if (!entries.length && modelInput) {
    entries.push({
      key: 'current',
      label: channelLabel(modelInput.channel),
      status: 'completed',
      input: modelInput,
    })
  }

  return (
    <section className="studio-panel model-input-panel">
      <div className="studio-panel-head">
        <div>
          <span className="eyebrow">MODEL × IMAGE</span>
          <h3>每张图片的模型输入</h3>
        </div>
      </div>
      {!entries.length ? (
        <p className="model-input-empty">这个版本还没有可查看的模型输入。</p>
      ) : (
        <div className="model-input-scroll">
          {entries.map((entry, index) => (
            <details className="model-output-card" key={entry.key} open={index === 0}>
              <summary>
                <span className={`model-output-channel ${entry.input?.channel === 'agent' ? 'is-agent' : ''}`}>
                  {channelLabel(entry.input?.channel)}
                </span>
                <span className="model-output-title">{entry.label}</span>
                {entry.previewUrl ? <img src={entry.previewUrl} alt="" /> : null}
                <span className={`model-output-status is-${entry.status ?? 'idle'}`}>{entry.status ?? 'idle'}</span>
              </summary>
              {entry.runId ? <p className="model-output-run">run_id: {entry.runId}</p> : null}
              {entry.error ? <p className="model-output-error">{entry.error}</p> : null}
              {entry.input ? <InputView input={entry.input} /> : <p className="model-output-error">尚未生成模型输入快照。</p>}
            </details>
          ))}
        </div>
      )}
    </section>
  )
}
