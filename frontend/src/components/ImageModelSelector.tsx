import { IMAGE_MODEL_OPTIONS } from '../imageModels'
import type { ImageModel } from '../types'

type Props = {
  value: ImageModel
  busy: boolean
  onChange: (value: ImageModel) => void
}

export default function ImageModelSelector({ value, busy, onChange }: Props) {
  const selected = IMAGE_MODEL_OPTIONS.find((option) => option.value === value)

  return (
    <label className="platform-select-field image-model-field">
      <span>生图模型</span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value as ImageModel)}
        disabled={busy}
        aria-label="选择生图模型"
      >
        {IMAGE_MODEL_OPTIONS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      {selected ? <small>{selected.description}</small> : null}
    </label>
  )
}
