import type { ImageModel } from '../types'
import { IMAGE_MODEL_OPTIONS } from '../imageModels'

type Props = {
  value: ImageModel
  busy: boolean
  onChange: (value: ImageModel) => void
}

export default function ImageModelSelector({ value, busy, onChange }: Props) {
  return (
    <div className="candidate-count-field image-model-field">
      <div className="brand-field-head">
        <span className="prompt-field-label">图片模型</span>
      </div>
      <div className="candidate-count-options" role="radiogroup" aria-label="图片模型">
        {IMAGE_MODEL_OPTIONS.map((option) => (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={value === option.value}
            className={value === option.value ? 'is-active' : ''}
            onClick={() => onChange(option.value)}
            disabled={busy}
            title={option.hint}
          >
            <strong>{option.label}</strong>
            <span>{option.hint}</span>
          </button>
        ))}
      </div>
    </div>
  )
}
