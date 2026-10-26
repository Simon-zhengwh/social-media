type Props = {
  value: number
  busy: boolean
  onChange: (value: number) => void
}

export default function CandidateCountSelector({ value, busy, onChange }: Props) {
  return (
    <div className="candidate-count-field">
      <div className="brand-field-head">
        <span className="prompt-field-label">生成数量</span>
      </div>
      <div className="candidate-count-options" role="radiogroup" aria-label="生成数量">
        {[1, 2, 3, 4, 5, 6].map((count) => (
          <button
            key={count}
            type="button"
            role="radio"
            aria-checked={value === count}
            className={value === count ? 'is-active' : ''}
            onClick={() => onChange(count)}
            disabled={busy}
          >
            <strong>{count}</strong>
            <span>{count === 1 ? '张' : '张候选'}</span>
          </button>
        ))}
      </div>
    </div>
  )
}
