import { useState } from 'react'

import { optimizePrompt, type PromptOptimizePayload } from '../api'

type Props = {
  label: string
  availability: string
  placeholder: string
  hint: string
  prompt: string
  rows?: number
  busy: boolean
  canOptimize: boolean
  optimizationBlockedReason?: string
  optimizeContext: Omit<PromptOptimizePayload, 'prompt'>
  onPromptChange: (value: string) => void
}

function SparkleIcon() {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true">
      <path
        d="M8.2 3.2c.5 2.6 1.7 3.8 4.3 4.3-2.6.5-3.8 1.7-4.3 4.3-.5-2.6-1.7-3.8-4.3-4.3 2.6-.5 3.8-1.7 4.3-4.3Z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      <path
        d="M15.2 3.2v3.1M13.7 4.8h3"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
      <path
        d="M4.2 14.8v2.4M3 16h2.4"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
    </svg>
  )
}

export default function PromptField({
  label,
  availability,
  placeholder,
  hint,
  prompt,
  rows = 5,
  busy,
  canOptimize,
  optimizationBlockedReason,
  optimizeContext,
  onPromptChange,
}: Props) {
  const [optimizing, setOptimizing] = useState(false)
  const [optimizeError, setOptimizeError] = useState<string | null>(null)
  const [optimizeNotice, setOptimizeNotice] = useState<string | null>(null)
  const disabled = busy || optimizing
  const iconDisabled = disabled || !canOptimize

  const handleChange = (value: string) => {
    setOptimizeError(null)
    setOptimizeNotice(null)
    onPromptChange(value)
  }

  const handleOptimize = async () => {
    if (iconDisabled || optimizing) {
      return
    }
    setOptimizing(true)
    setOptimizeError(null)
    setOptimizeNotice(null)
    try {
      const result = await optimizePrompt({ ...optimizeContext, prompt })
      onPromptChange(result.prompt)
      setOptimizeNotice('AI 已更新提示词，可再次点击继续优化。')
    } catch (reason: unknown) {
      setOptimizeError(reason instanceof Error ? reason.message : '提示词优化失败，请稍后重试')
    } finally {
      setOptimizing(false)
    }
  }

  return (
    <div className="prompt-field">
      <span className="prompt-field-label">
        {label} <span>{availability}</span>
      </span>
      <span className={`prompt-textarea-wrap${optimizing ? ' is-optimizing' : ''}`}>
        <textarea
          value={prompt}
          maxLength={2000}
          rows={rows}
          placeholder={placeholder}
          onChange={(event) => handleChange(event.target.value)}
          disabled={disabled}
        />
        <button
          type="button"
          className="prompt-ai-button"
          onClick={() => void handleOptimize()}
          disabled={iconDisabled}
          aria-label={optimizing ? '正在优化提示词' : 'AI 优化提示词'}
          title={
            canOptimize
              ? '根据当前风格、参考图和素材图优化提示词'
              : (optimizationBlockedReason ?? '请先完善当前生成条件')
          }
        >
          {optimizing ? <span className="spinner spinner-dark" aria-hidden="true" /> : <SparkleIcon />}
        </button>
      </span>
      <span className="prompt-field-hint">{hint}</span>
      {optimizeNotice ? (
        <span className="prompt-optimize-notice" role="status">
          {optimizeNotice}
        </span>
      ) : null}
      {optimizeError ? (
        <span className="prompt-optimize-error" role="alert">
          {optimizeError}
        </span>
      ) : null}
    </div>
  )
}

