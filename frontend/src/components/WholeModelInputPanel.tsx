import type { WholeImageModelInput } from '../types'

type Props = {
  modelInput: WholeImageModelInput | null
}

const IMAGE_KIND_LABELS: Record<string, string> = {
  product: '商品素材',
  logo: 'Logo 素材',
  brand_reference: '风格参考',
  current_design: '当前设计',
}

export default function WholeModelInputPanel({ modelInput }: Props) {
  return (
    <section className="studio-panel model-input-panel">
      <div className="studio-panel-head">
        <div>
          <span className="eyebrow">CORE-AI REQUEST</span>
          <h3>最近一次模型输入</h3>
        </div>
      </div>
      {!modelInput ? (
        <p className="model-input-empty">这个历史版本没有保存模型输入快照。</p>
      ) : (
        <div className="model-input-scroll">
          <dl className="model-input-meta">
            <div>
              <dt>接口</dt>
              <dd>{modelInput.endpoint}</dd>
            </div>
            <div>
              <dt>模型</dt>
              <dd>{modelInput.model}</dd>
            </div>
            <div>
              <dt>尺寸</dt>
              <dd>{modelInput.parameters.size ?? '—'}</dd>
            </div>
            <div>
              <dt>质量</dt>
              <dd>{modelInput.parameters.quality ?? '—'}</dd>
            </div>
            <div>
              <dt>输出数</dt>
              <dd>{modelInput.parameters.n ?? '—'}</dd>
            </div>
            <div>
              <dt>格式</dt>
              <dd>{modelInput.parameters.output_format ?? '—'}</dd>
            </div>
          </dl>

          <section className="model-input-section">
            <h4>最终提示词</h4>
            <pre>{modelInput.prompt}</pre>
          </section>

          <section className="model-input-section">
            <h4>输入图片 <span>{modelInput.images.length}</span></h4>
            {modelInput.images.length ? (
              <div className="model-input-images">
                {modelInput.images.map((image, index) => (
                  <figure key={`${image.asset_id}-${index}`}>
                    {image.preview_url ? (
                      <img src={image.preview_url} alt={image.name} />
                    ) : (
                      <div className="model-input-image-missing">图片不可用</div>
                    )}
                    <figcaption>
                      <strong>{index + 1}. {IMAGE_KIND_LABELS[image.kind] ?? image.kind}</strong>
                      <span>{image.name}</span>
                    </figcaption>
                  </figure>
                ))}
              </div>
            ) : (
              <p className="model-input-no-images">本次调用没有输入图片。</p>
            )}
            <p className="model-input-hint">按上方顺序传入。此处显示图片预览，不展示底层 URL 或图片编码。</p>
          </section>
        </div>
      )}
    </section>
  )
}
